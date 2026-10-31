"""Bounded accounting and admission for worker-owned archive content."""

import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from peewee import fn

from frigate.models import ArchiveTransfer, Recordings
from frigate.record.archive import archive_destination, fsync_directory
from frigate.record.archive_store import expire_recording
from frigate.record.policy import CapturePadding, recording_event_windows

if TYPE_CHECKING:
    from frigate.config import FrigateConfig

BACKFILL_BATCH = 256


def plan_archive_batch(backlog_bytes: int) -> tuple[int, int]:
    """Read only the indexed oldest-first prefix required by the drain estimate."""
    if type(backlog_bytes) is not int or backlog_bytes < 0:
        raise ValueError("Archive backlog must be a nonnegative integer")
    if backlog_bytes == 0:
        return 0, 0
    total = 0.0
    count = 0
    # Match transport ordering. Equal start times have equal archive priority.
    cursor = Recordings._meta.database.execute_sql(
        "SELECT segment_size FROM recordings "
        "WHERE storage_tier = ? AND archive_bytes IS NULL AND NOT delete_requested AND end_time < ? "
        "ORDER BY start_time",
        ("local", time.time() - 60),
    )
    try:
        while total < backlog_bytes / 2**20:
            row = cursor.fetchone()
            if row is None:
                break
            size = row[0]
            if size is None or size < 0:
                raise ValueError("Recording contains an invalid size estimate")
            total += size
            count += 1
    finally:
        cursor.close()
    return int(total * 2**20), count


class ArchiveBlocked(OSError):
    """An explicit archive admission failure safe to display as a status code."""


def archive_maintenance_active() -> bool:
    """Keep transfer admission separate from owner-scoped maintenance reservations."""
    return ArchiveTransfer.select().where(ArchiveTransfer.kind != "transfer").exists()


class ArchiveCapacity:
    """Advance bounded oldest-first passes, ordinary footage before event footage."""

    def __init__(self) -> None:
        self.cursor: tuple[float, str] | None = None
        self.reset()

    def reset(self) -> None:
        """Restart classification after a settings change or completed eviction."""
        self.cursor = None
        self.events = False

    def ensure_room(
        self,
        config: "FrigateConfig",
        root: Path,
        pinned: Path,
        incoming: int,
        cancelled: Callable[[], bool],
        report: Callable[..., None],
    ) -> None:
        """Reserve sufficient exact content capacity and actual filesystem space."""
        settings = config.storage
        budget = archive_budget_bytes(settings.max_archive_gib)
        if archive_maintenance_active():
            raise ArchiveBlocked("archive_maintenance_pending")
        if budget is None and shutil.disk_usage(pinned).free >= incoming + int(
            getattr(settings, "archive_reserve_gib", 0) * 2**30
        ):
            # Aggregate usage is sampled by the worker, not once per admitted file.
            report(archive_budget_bytes=None, archive_reserved_bytes=incoming)
            self.reset()
            return
        try:
            used = archive_owned_bytes()
        except ArchiveBlocked:
            if budget is not None:
                raise
            # Legacy unknown lengths must not stop the formerly unlimited tier.
            # None explicitly denotes incomplete accounting, never zero usage.
            used = None

        def fits() -> bool:
            return (
                budget is None or (used is not None and used + incoming <= budget)
            ) and shutil.disk_usage(pinned).free >= incoming + int(
                getattr(settings, "archive_reserve_gib", 0) * 2**30
            )

        report(
            archive_usage_bytes=used,
            archive_budget_bytes=budget,
            archive_reserved_bytes=incoming,
        )
        if archive_maintenance_active():
            raise ArchiveBlocked("archive_maintenance_pending")
        if fits():
            self.reset()
            return
        if budget is not None and incoming > budget:
            raise ArchiveBlocked("archive_incoming_exceeds_budget")
        if settings.archive_full_behavior != "expire_oldest":
            raise ArchiveBlocked("archive_capacity_paused")
        default = CapturePadding.from_record(config.record)
        padding = {
            name: CapturePadding.from_record(camera.record)
            for name, camera in config.cameras.items()
        }
        windows = recording_event_windows(padding, default)
        candidates = Recordings.select().where(
            (Recordings.storage_tier == "archive")
            | Recordings.archive_bytes.is_null(False),
            Recordings.delete_requested == False,
        )
        if self.cursor is not None:
            end, identifier = self.cursor
            candidates = candidates.where(
                (Recordings.end_time > end)
                | ((Recordings.end_time == end) & (Recordings.id > identifier))
            )
        batch = list(candidates.order_by(Recordings.end_time, Recordings.id).limit(256))
        for candidate in batch:
            self.cursor = (candidate.end_time, candidate.id)
            if archive_maintenance_active():
                raise ArchiveBlocked("archive_maintenance_pending")
            if cancelled():
                raise ArchiveBlocked("archive_settings_changed")
            if (
                windows.overlaps(
                    candidate.camera, candidate.start_time, candidate.end_time
                )
                != self.events
            ):
                continue
            if expire_recording(
                candidate,
                root,
                pinned,
                0,
                time.time(),
                padding.get(candidate.camera, default),
            ):
                if used is not None:
                    used = archive_owned_bytes()
                report(archive_usage_bytes=used)
                if fits():
                    self.reset()
                    return
        if len(batch) == 256:
            raise ArchiveBlocked("archive_cleanup_pending")
        self.cursor = None
        if not self.events:
            self.events = True
            raise ArchiveBlocked("archive_cleanup_pending")
        self.events = False
        raise ArchiveBlocked("archive_capacity_protected_or_leased")


def archive_budget_bytes(gib: float | None) -> int | None:
    """Convert binary GiB to an integer content ceiling without rounding up."""
    return None if gib is None else int(gib * (2**30))


def archive_owned_bytes() -> int:
    """Include durable reservations even when their recording row disappeared."""
    if (
        Recordings.select()
        .where(
            (Recordings.storage_tier == "archive")
            | Recordings.archive_bytes.is_null(False),
            Recordings.archive_bytes.is_null() | (Recordings.archive_bytes < 0),
        )
        .exists()
    ):
        raise ArchiveBlocked("archive_accounting_pending")
    archived = (
        Recordings.select(fn.SUM(Recordings.archive_bytes))
        .where(
            (Recordings.storage_tier == "archive")
            | Recordings.archive_bytes.is_null(False)
        )
        .scalar()
        or 0
    )
    pending = ArchiveTransfer.select(fn.SUM(ArchiveTransfer.size)).scalar() or 0
    return int(archived + pending)


def backfill_archive_bytes(
    pinned: Path, progress: Callable[[], None] | None = None
) -> bool:
    """Stat a bounded indexed batch only; unknown or missing files block admission."""
    rows = list(
        Recordings.select(Recordings.id, Recordings.path)
        .where(
            Recordings.storage_tier == "archive",
            Recordings.archive_bytes.is_null() | (Recordings.archive_bytes < 0),
        )
        .limit(BACKFILL_BATCH)
    )
    writer_yield = threading.Event()
    for offset in range(0, len(rows), 25):
        measured = []
        if progress is not None:
            progress()
        for row in rows[offset : offset + 25]:
            destination = archive_destination(Path(row.path), pinned)
            try:
                if destination.is_symlink() or not destination.is_file():
                    raise ArchiveBlocked("archive_size_unavailable")
                size = destination.stat().st_size
            except OSError as error:
                raise ArchiveBlocked("archive_size_unavailable") from error
            measured.append((row.id, size))
        with Recordings._meta.database.atomic("IMMEDIATE"):
            for identifier, size in measured:
                Recordings.update(archive_bytes=size).where(
                    Recordings.id == identifier,
                    Recordings.storage_tier == "archive",
                    Recordings.archive_bytes.is_null() | (Recordings.archive_bytes < 0),
                ).execute()
        if offset + 25 < len(rows):
            writer_yield.wait(0.1)
    return not (
        Recordings.select()
        .where(
            Recordings.storage_tier == "archive",
            Recordings.archive_bytes.is_null() | (Recordings.archive_bytes < 0),
        )
        .exists()
    )


def recover_archive_transfers(root: Path, pinned: Path) -> bool:
    """Clean known interrupted copies without scanning the NAS or losing reservations."""
    pending = ArchiveTransfer.select().where(ArchiveTransfer.kind == "transfer")
    transfers = list(pending.limit(BACKFILL_BATCH))
    for transfer in transfers:
        row = Recordings.get_or_none(Recordings.id == transfer.id)
        source = Path(transfer.path)
        destination = archive_destination(source, pinned)
        if row is not None and (
            row.storage_tier == "archive"
            or row.archive_bytes is not None
            or (
                source.is_symlink()
                and source.readlink() == archive_destination(source, root)
            )
        ):
            try:
                measured = destination.stat().st_size
            except FileNotFoundError:
                if row.storage_tier != "local" or source.is_symlink():
                    raise
                with Recordings._meta.database.atomic():
                    Recordings.update(archive_bytes=None).where(
                        Recordings.id == row.id
                    ).execute()
                    ArchiveTransfer.delete_by_id(transfer.id)
                continue
            if (
                row.storage_tier == "local"
                and row.archive_bytes is not None
                and measured != row.archive_bytes
                and not source.is_symlink()
            ):
                destination.unlink()
                fsync_directory(destination.parent)
                with Recordings._meta.database.atomic():
                    Recordings.update(archive_bytes=None).where(
                        Recordings.id == row.id
                    ).execute()
                    ArchiveTransfer.delete_by_id(transfer.id)
                continue
            destination.with_suffix(destination.suffix + ".partial").unlink(
                missing_ok=True
            )
            fsync_directory(destination.parent)
            with Recordings._meta.database.atomic():
                Recordings.update(
                    storage_tier=(
                        "archive" if source.is_symlink() else row.storage_tier
                    ),
                    archive_bytes=measured,
                ).where(Recordings.id == row.id).execute()
                ArchiveTransfer.delete_by_id(transfer.id)
            continue
        # A reservation is released only after both possible NAS artifacts are gone.
        destination.with_suffix(destination.suffix + ".partial").unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        if destination.parent.exists():
            fsync_directory(destination.parent)
        ArchiveTransfer.delete_by_id(transfer.id)
    return not pending.exists()

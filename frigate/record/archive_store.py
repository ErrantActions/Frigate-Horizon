"""Recording archive lifecycle operations independent of capture processes."""

import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from peewee import ModelSelect

from frigate.const import RECORD_DIR, STREAM_TYPE_SUB
from frigate.models import ArchiveTransfer, Recordings, ReviewSegment
from frigate.record.archive import (
    UnverifiedArchiveCopy,
    archive_destination,
    copy_unverified,
    copy_verified,
    fsync_directory,
    prepare_archive_directory,
    publish_archive_link,
    storage_lock,
    verify_archive_copy,
)
from frigate.record.archive_rate import ArchiveRateLimiter
from frigate.record.policy import (
    CapturePadding,
    mark_recording_unavailable,
    recording_is_protected,
)
from frigate.util.media import remove_empty_directories

if TYPE_CHECKING:
    from frigate.config import CameraConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreparedArchiveMove:
    """A verified durable copy whose original path is not yet published."""

    recording: Recordings
    source: Path
    destination: Path
    pinned_destination: Path
    original: os.stat_result
    recovering: bool = False


@dataclass(frozen=True)
class PendingArchiveMove:
    """A copied recording still requiring batch-phase readback."""

    move: PreparedArchiveMove
    copy: UnverifiedArchiveCopy | None


def copy_recording_unverified(
    recording: Recordings,
    root: Path,
    pinned: Path,
    rate_mib: float,
    cancelled: Callable[[], bool],
    reserved_bytes: int,
    progress: Callable[[str, int, int], None],
    limiter: ArchiveRateLimiter | None,
    prepare_directory: Callable[[Path], None] | None = None,
) -> PendingArchiveMove | None:
    """Copy admitted HDD bytes without reading archive contents back yet."""
    source = Path(recording.path)
    destination = archive_destination(source, root)
    pinned_destination = archive_destination(source, pinned)
    if source.is_symlink():
        if source.readlink() != destination:
            raise OSError("Recording link does not match configured archive")
        return PendingArchiveMove(
            PreparedArchiveMove(
                recording, source, destination, pinned_destination, source.stat(), True
            ),
            None,
        )
    original = source.stat()
    if original.st_size != reserved_bytes:
        raise OSError("Recording changed after archive reservation")
    if prepare_directory is None:
        prepare_archive_directory(pinned_destination.parent, pinned)
    else:
        prepare_directory(pinned_destination.parent)
    try:
        copied = copy_unverified(
            source,
            pinned_destination,
            rate_mib,
            cancelled,
            reserved_bytes,
            progress,
            limiter,
        )
    except InterruptedError:
        logger.info("Archive copy interrupted; keeping local recording")
        return None
    return PendingArchiveMove(
        PreparedArchiveMove(
            recording, source, destination, pinned_destination, original
        ),
        copied,
    )


def verify_recording_copy(
    pending: PendingArchiveMove,
    rate_mib: float,
    cancelled: Callable[[], bool],
    progress: Callable[[str, int, int], None],
    limiter: ArchiveRateLimiter | None,
) -> PreparedArchiveMove | None:
    """Promote copied bytes to publishable status only after successful readback."""
    try:
        if pending.copy is not None:
            verify_archive_copy(pending.copy, rate_mib, cancelled, progress, limiter)
    except InterruptedError:
        logger.info("Archive verification interrupted; keeping local recording")
        return None
    return pending.move


def archive_candidates(limit: int) -> ModelSelect:
    """Select oldest eligible rows without sorting the local recording set."""
    if type(limit) is not int or limit <= 0:
        raise ValueError("Archive candidate limit must be a positive integer")
    # Equality on delete_requested makes SQLite prefer the expiration index and sort.
    return (
        Recordings.select()
        .where(
            Recordings.storage_tier == "local",
            Recordings.archive_bytes.is_null(),
            ~Recordings.delete_requested,
            Recordings.end_time < time.time() - 60,
        )
        .order_by(Recordings.start_time)
        .limit(limit)
    )


def move_recording(
    recording: Recordings,
    root: Path,
    pinned: Path,
    rate_mib: float,
    cancelled: Callable[[], bool],
    reserved_bytes: int | None = None,
    progress: Callable[[str, int, int], None] | None = None,
    limiter: ArchiveRateLimiter | None = None,
    *,
    preserve_local: bool = False,
) -> bool:
    """Lease the source until verified publication or failure has completed."""
    from frigate.models import RecordingLease
    from frigate.record.leases import lease_recordings, release_recordings

    token = f"archive-{uuid.uuid4().hex}"
    renewed = 0.0

    def keep_source() -> bool:
        nonlocal renewed
        if cancelled():
            return True
        if time.monotonic() - renewed >= 60:
            if renewed == 0:
                lease_recordings(
                    token,
                    recording.camera,
                    recording.start_time,
                    recording.end_time,
                    lifetime=300,
                )
            else:
                RecordingLease.update(expires=time.time() + 300).where(
                    RecordingLease.id == token
                ).bind(Recordings._meta.database).execute()
            renewed = time.monotonic()
        return False

    try:
        if keep_source():
            return False
        return _move_recording(
            recording,
            root,
            pinned,
            rate_mib,
            keep_source,
            reserved_bytes,
            progress,
            limiter,
            preserve_local,
        )
    finally:
        release_recordings(token)


def _move_recording(
    recording: Recordings,
    root: Path,
    pinned: Path,
    rate_mib: float,
    cancelled: Callable[[], bool],
    reserved_bytes: int | None,
    progress: Callable[[str, int, int], None] | None,
    limiter: ArchiveRateLimiter | None = None,
    preserve_local: bool = False,
) -> bool:
    """Preserve the serial path using the same verified-copy publication protocol."""
    prepared = prepare_recording_copy(
        recording, root, pinned, rate_mib, cancelled, reserved_bytes, progress, limiter
    )
    return prepared is not None and bool(
        publish_recording_copies(
            [prepared], cancelled, track_bytes=True, preserve_local=preserve_local
        )
    )


def prepare_recording_copy(
    recording: Recordings,
    root: Path,
    pinned: Path,
    rate_mib: float,
    cancelled: Callable[[], bool],
    reserved_bytes: int | None = None,
    progress: Callable[[str, int, int], None] | None = None,
    limiter: ArchiveRateLimiter | None = None,
) -> PreparedArchiveMove | None:
    """Verify bytes while the caller retains its source lease or durable reservation."""
    source = Path(recording.path)
    destination = archive_destination(source, root)
    pinned_destination = archive_destination(source, pinned)
    if source.is_symlink():
        if source.readlink() != destination:
            raise OSError("Recording link does not match configured archive")
        return PreparedArchiveMove(
            recording, source, destination, pinned_destination, source.stat(), True
        )

    try:
        original = source.stat()
        if reserved_bytes is not None and original.st_size != reserved_bytes:
            raise OSError("Recording changed after archive reservation")
        prepare_archive_directory(pinned_destination.parent, pinned)
        if reserved_bytes is None and progress is None and limiter is None:
            copy_verified(source, pinned_destination, rate_mib, cancelled)
        else:
            copy_verified(
                source,
                pinned_destination,
                rate_mib,
                cancelled,
                maximum_bytes=reserved_bytes,
                progress=progress,
                limiter=limiter,
            )
    except FileNotFoundError:
        if Recordings.select().where(Recordings.id == recording.id).exists():
            raise
        return None
    except InterruptedError:
        logger.info("Archive copy interrupted; keeping local recording")
        return None

    if cancelled():
        pinned_destination.unlink(missing_ok=True)
        return None
    return PreparedArchiveMove(
        recording, source, destination, pinned_destination, original
    )


def publish_recording_copies(
    prepared: list[PreparedArchiveMove],
    cancelled: Callable[[], bool],
    *,
    track_bytes: bool = False,
    preserve_local: bool = False,
    on_commit: Callable[[Recordings], None] | None = None,
    on_progress: Callable[[], None] | None = None,
) -> list[Recordings]:
    """Flush links before bounded FULL transactions for corresponding metadata.

    A crash before the database commit leaves recoverable local-tier symlinks.
    Original bytes are released only after verified archive-file durability.
    Keep the storage lock across both phases, but never hold SQLite's writer
    lock during filesystem flushes. The metadata-only callback participates in
    the SQL transaction.
    """
    if not prepared:
        return []
    moved: list[Recordings] = []
    discard: list[Path] = []
    stopping = False
    last_progress = float("-inf")
    writer_yield = threading.Event()

    def progress() -> None:
        nonlocal last_progress
        now = time.monotonic()
        if on_progress is not None and now - last_progress >= 1:
            on_progress()
            last_progress = now

    database = Recordings._meta.database
    with storage_lock():
        published: list[PreparedArchiveMove] = []
        progress()
        for item in prepared:
            if stopping or cancelled():
                stopping = True
                if not item.recovering:
                    discard.append(item.pinned_destination)
                continue
            if item.recovering:
                if (
                    not item.source.is_symlink()
                    or item.source.readlink() != item.destination
                ):
                    raise OSError("Recording link changed during archive recovery")
            else:
                try:
                    current = item.source.stat()
                except FileNotFoundError:
                    current = None
                retained = (
                    Recordings.select()
                    .where(
                        Recordings.id == item.recording.id,
                        Recordings.storage_tier == "local",
                        Recordings.delete_requested == False,
                    )
                    .exists()
                )
                if current is None or not retained:
                    discard.append(item.pinned_destination)
                    continue
                if (current.st_ino, current.st_size, current.st_mtime_ns) != (
                    item.original.st_ino,
                    item.original.st_size,
                    item.original.st_mtime_ns,
                ):
                    raise OSError("Recording changed during archive transfer")
                if not preserve_local:
                    publish_archive_link(item.source, item.destination)
            published.append(item)
            progress()
        for offset in range(0, len(published), 25):
            with database.atomic("IMMEDIATE"):
                for item in published[offset : offset + 25]:
                    values: dict[str, str | int] = {
                        "storage_tier": (
                            "local"
                            if preserve_local and not item.recovering
                            else "archive"
                        )
                    }
                    if track_bytes or preserve_local:
                        values["archive_bytes"] = item.original.st_size
                    updated = (
                        Recordings.update(**values)
                        .where(Recordings.id == item.recording.id)
                        .execute()
                    )
                    if updated:
                        if on_commit is not None:
                            on_commit(item.recording)
                        moved.append(item.recording)
            progress()
            if offset + 25 < len(published):
                # SQLite busy handlers poll on a roughly 100ms cadence.
                # Give recording ingestion a real opportunity between commits.
                writer_yield.wait(0.1)
    for path in discard:
        path.unlink(missing_ok=True)
    return moved


def recover_archive_links(
    root: Path, progress: Callable[[], None] | None = None
) -> None:
    """Repair a crash between local link publication and the database update."""
    last_report = time.monotonic()
    if progress is not None:
        progress()
    for recording in (
        Recordings.select(Recordings.id, Recordings.path)
        .where(Recordings.storage_tier == "local")
        .iterator()
    ):
        if progress is not None and time.monotonic() - last_report >= 5:
            progress()
            last_report = time.monotonic()
        source = Path(recording.path)
        if not source.is_symlink():
            continue
        if source.readlink() != archive_destination(source, root):
            raise OSError(f"Unexpected archive link for recording {recording.id}")
        Recordings.update(storage_tier="archive").where(
            Recordings.id == recording.id
        ).execute()


def audit_mirror_copy(recording: Recordings, pinned: Path) -> None:
    """Requeue missing or wrong-length mirrors, reserving originals during NFS I/O."""
    with storage_lock():
        current = Recordings.get_or_none(Recordings.id == recording.id)
        if (
            current is None
            or current.storage_tier != "local"
            or current.archive_bytes is None
            or current.delete_requested
            or ArchiveTransfer.select().where(ArchiveTransfer.id == current.id).exists()
        ):
            return
        ArchiveTransfer.create(id=current.id, path=current.path, size=0)
    destination = archive_destination(Path(current.path), pinned)
    try:
        size = destination.stat().st_size
    except FileNotFoundError:
        size = None
    if size != current.archive_bytes:
        destination.unlink(missing_ok=True)
        if destination.parent.exists():
            fsync_directory(destination.parent)
        with storage_lock(), Recordings._meta.database.atomic("IMMEDIATE"):
            Recordings.update(archive_bytes=None).where(
                Recordings.id == current.id, Recordings.storage_tier == "local"
            ).execute()
            ArchiveTransfer.delete_by_id(current.id)
    else:
        ArchiveTransfer.delete_by_id(current.id)


def archive_retention_days(
    recording: Recordings,
    camera: "CameraConfig",
    minimum: float,
    maximum: float | None = None,
) -> float:
    """Honor matching retention unless an explicit archive age ceiling is set."""
    record = camera.record
    stream = record.sub if recording.stream_type == STREAM_TYPE_SUB else record
    days = max(minimum, stream.continuous.days)
    if recording.motion or recording.dBFS:
        days = max(days, stream.motion.days)
    reviews = ReviewSegment.select().where(
        ReviewSegment.camera == recording.camera,
        ReviewSegment.start_time <= recording.end_time + record.event_pre_capture,
        ReviewSegment.end_time.is_null()
        | (
            ReviewSegment.end_time
            >= recording.start_time
            - max(record.alerts.post_capture, record.detections.post_capture)
        ),
    )
    for review in reviews:
        if review.start_time - record.get_review_pre_capture(
            review.severity
        ) > recording.end_time or (
            review.end_time is not None
            and review.end_time + record.get_review_post_capture(review.severity)
            < recording.start_time
        ):
            continue
        if recording.stream_type == STREAM_TYPE_SUB:
            policy = (
                record.sub.alerts
                if review.severity == "alert"
                else record.sub.detections
            )
        else:
            policy = (
                record.alerts.retain
                if review.severity == "alert"
                else record.detections.retain
            )
        if (
            policy.mode == "all"
            or (
                policy.mode == "motion"
                and (recording.motion or recording.objects or recording.dBFS)
            )
            or (policy.mode == "active_objects" and recording.objects)
        ):
            days = max(days, policy.days)
    return min(days, maximum) if maximum is not None else days


def expire_recording(
    recording: Recordings,
    root: Path,
    pinned: Path,
    days: float,
    now: float,
    capture_padding: CapturePadding = CapturePadding(),
) -> bool:
    """Delete expired archive content and its stable anchor, preserving saved events."""
    source = Path(recording.path)
    destination = archive_destination(source, root)
    pinned_destination = archive_destination(source, pinned)
    with storage_lock():
        current = Recordings.get_or_none(Recordings.id == recording.id)
        if current is None:
            return False
        if ArchiveTransfer.select().where(ArchiveTransfer.id == current.id).exists():
            return False
        from frigate.record.leases import recording_has_lease

        if recording_has_lease(current):
            return False
        explicit = current.delete_requested and current.delete_reason == "user"
        if recording_is_protected(
            current.camera, current.start_time, current.end_time, capture_padding
        ):
            return False
        if not current.delete_requested and current.end_time >= now - days * 86400:
            return False
        if source.is_symlink():
            if source.readlink() != destination:
                raise OSError("Refusing to delete an unexpected archive target")
        elif source.exists() and current.archive_bytes is None:
            raise OSError(
                "Refusing to delete a recording with a non-archive local file"
            )
        Recordings.update(
            delete_requested=True,
            delete_reason="user" if explicit else "retention",
        ).where(Recordings.id == current.id).execute()
    # Persist deletion intent before touching NFS so a crash can resume even
    # if retention settings change before the next cleanup pass.
    pinned_destination.unlink(missing_ok=True)
    if pinned_destination.parent.exists():
        fsync_directory(pinned_destination.parent)
    with storage_lock():
        source.unlink(missing_ok=True)
        mark_recording_unavailable(current, capture_padding)
        Recordings.delete().where(Recordings.id == recording.id).execute()
    remove_empty_directories(pinned, {pinned_destination.parent})
    remove_empty_directories(Path(RECORD_DIR), {source.parent})
    return True

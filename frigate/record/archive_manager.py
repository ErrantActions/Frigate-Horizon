"""Isolate archive filesystem I/O from recording, API, and stats threads."""

import logging
import multiprocessing as mp
import pickle
import shutil
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import ExitStack
from multiprocessing.synchronize import Event as MpEvent
from pathlib import Path
from queue import Empty, Full
from typing import Any

from peewee import DatabaseError, SqliteDatabase, fn

from frigate.config import FrigateConfig
from frigate.config.camera.updater import (
    CameraConfigUpdateEnum,
    CameraConfigUpdateSubscriber,
)
from frigate.const import PROCESS_PRIORITY_LOW, RECORD_DIR
from frigate.models import (
    ArchiveTransfer,
    Event,
    RecordingLease,
    Recordings,
    ReviewSegment,
)
from frigate.record.archive import (
    ArchiveDirectoryBatch,
    ArchiveDrain,
    archive_destination,
    needs_transport,
    pin_archive,
    storage_lock,
    validate_mount,
)
from frigate.record.archive_concurrency import ArchiveConcurrency
from frigate.record.archive_drain_state import ArchiveDrainState
from frigate.record.archive_jobs import ARCHIVE_QUEUE_MULTIPLIER, run_archive_jobs
from frigate.record.archive_limits import (
    ArchiveBlocked,
    ArchiveCapacity,
    archive_budget_bytes,
    archive_maintenance_active,
    archive_owned_bytes,
    backfill_archive_bytes,
    plan_archive_batch,
    recover_archive_transfers,
)
from frigate.record.archive_rate import ArchiveRateLimiter
from frigate.record.archive_store import (
    PendingArchiveMove,
    PreparedArchiveMove,
    archive_candidates,
    archive_retention_days,
    audit_mirror_copy,
    copy_recording_unverified,
    expire_recording,
    move_recording,
    publish_recording_copies,
    recover_archive_links,
    verify_recording_copy,
)
from frigate.record.local_mirror import validate_local_mirror_mount
from frigate.record.policy import CapturePadding, RecordingDeletionInProgress
from frigate.util.process import FrigateProcess
from frigate.util.storage import invalidate_storage_capacity, storage_capacity

logger = logging.getLogger(__name__)


class ArchiveProcess(FrigateProcess):
    def __init__(self, config: FrigateConfig, stop_event: MpEvent) -> None:
        super().__init__(
            stop_event,
            PROCESS_PRIORITY_LOW,
            name="frigate.archive",
            daemon=True,
        )
        self.config = config
        self.updates = mp.Queue()
        # Each counter has one writer. Unsynchronized values cannot strand an
        # API request on a process lock if a worker is terminated during NFS I/O.
        self.config_revision = mp.Value("I", 1, lock=False)
        self.applied_revision = mp.Value("I", 0, lock=False)
        self.status = mp.Queue(maxsize=1)
        self.snapshot: dict[str, Any] = {}
        self.expiry_cursor: tuple[float, str] | None = None
        self.capacity = ArchiveCapacity()
        self.camera_subscriber: CameraConfigUpdateSubscriber | None = None
        self.batch = ArchiveDrain()
        self.current_transfer: dict[str, Any] | None = None
        self.last_progress = 0.0
        self.ingest_rate: float | None = None
        self.local_usage_mib = 0.0
        self.hourly_usage_mib = 0.0
        self.local_sampled_at = 0.0
        self.adaptive = ArchiveConcurrency()
        self.mirror_backlog_bytes = 0
        self.mirror_audit_cursor: tuple[float, str] | None = None

    def report(self, **values) -> None:
        """Publish a bounded health snapshot without blocking on the API."""
        self.snapshot.update(values)
        values = {**self.snapshot, "updated_at": time.time()}
        self.snapshot = values
        try:
            self.status.get_nowait()
        except Empty:
            pass
        try:
            self.status.put_nowait(values)
        except Full:
            pass

    def update_config(self, config: FrigateConfig) -> None:
        """Invalidate in-flight work before asynchronously delivering its settings."""
        payload = pickle.dumps(config)
        self.config_revision.value += 1
        self.updates.put((self.config_revision.value, payload))

    def apply_updates(self) -> bool:
        """Apply queued persisted configurations between individual transfers."""
        changed = bool(
            self.camera_subscriber.check_for_updates()
            if self.camera_subscriber is not None
            else False
        )
        while True:
            try:
                revision, payload = self.updates.get_nowait()
                self.config = pickle.loads(payload)
                if self.camera_subscriber is not None:
                    self.camera_subscriber.config = self.config
                    self.camera_subscriber.camera_configs = self.config.cameras
                changed = True
                self.applied_revision.value = revision
            except Empty:
                if changed:
                    self.expiry_cursor = None
                    self.mirror_audit_cursor = None
                    self.capacity.reset()
                    self.adaptive = ArchiveConcurrency()
                return changed

    def run(self) -> None:
        self.pre_run_setup(self.config.logger)
        database = SqliteDatabase(self.config.database.path, timeout=30)
        database.bind(
            [Recordings, Event, ReviewSegment, ArchiveTransfer, RecordingLease]
        )
        database.connect()
        drain_state = ArchiveDrainState(
            Path(str(self.config.database.path) + ".archive-drain.json")
        )
        self.camera_subscriber = CameraConfigUpdateSubscriber(
            self.config,
            self.config.cameras,
            [
                CameraConfigUpdateEnum.record,
                CameraConfigUpdateEnum.add,
                CameraConfigUpdateEnum.remove,
            ],
        )
        if self.config.storage.enabled or "storage" in self.config.model_fields_set:
            self.report(state="checking", error=None)
            recover_archive_links(
                Path(self.config.storage.archive_path),
                progress=lambda: self.report(state="checking", error=None),
            )
        self.applied_revision.value = 1
        draining = False
        last_expire = 0.0
        local_mib = 0.0
        hourly_mib = 0.0
        while not self.stop_event.is_set():
            self.apply_updates()
            if self.config_revision.value != self.applied_revision.value:
                self.stop_event.wait(0.1)
                continue
            settings = self.config.storage
            try:
                if settings.mode == "mirror":
                    validate_local_mirror_mount()
                local_mib, hourly_mib = self.sample_local(force=False)
            except (OSError, DatabaseError):
                capacities = self.snapshot.get("capacity", {})
                self.report(
                    state="blocked",
                    error="local_capacity_unavailable",
                    transfer=self.blocked_transfer(),
                    capacity={
                        key: invalidate_storage_capacity(value, "stale")
                        for key, value in capacities.items()
                    },
                )
                self.stop_event.wait(5)
                continue
            if (
                not settings.enabled
                and not Recordings.select()
                .where(
                    (Recordings.storage_tier == "archive")
                    | Recordings.archive_bytes.is_null(False)
                )
                .exists()
                and not ArchiveTransfer.select().exists()
            ):
                self.batch.active = False
                self.report(state="disabled", error=None, transfer=None)
                self.stop_event.wait(5)
                continue
            try:
                admitted = drain_state.load(settings.model_dump())
                if self.snapshot["capacity"]["local"]["usage_bytes"] is None:
                    raise ArchiveBlocked("local_accounting_pending")
                root = Path(settings.archive_path)
                if self.snapshot.get("state") not in ("ready", "moving"):
                    self.report(state="checking", error=None)
                validate_mount(root, settings.mount_id)
                with pin_archive(root, settings.mount_id) as pinned:
                    if not recover_archive_transfers(root, pinned):
                        raise ArchiveBlocked("archive_recovery_pending")
                    if time.monotonic() - last_expire >= 60:
                        self.audit_mirrors(pinned)
                        self._expire_archive(root, pinned)
                        last_expire = time.monotonic()
                        if self.config.storage != settings:
                            last_expire = 0
                            continue
                    budget = archive_budget_bytes(settings.max_archive_gib)
                    try:
                        if (
                            not backfill_archive_bytes(
                                pinned,
                                progress=lambda: self.report(
                                    state="checking", error=None
                                ),
                            )
                            and budget is not None
                        ):
                            raise ArchiveBlocked("archive_accounting_pending")
                    except ArchiveBlocked:
                        if budget is not None:
                            raise
                    try:
                        used = archive_owned_bytes()
                    except ArchiveBlocked:
                        used = None
                    self.sample_archive(pinned, used=used)
                    if budget is not None and used is not None and used > budget:
                        self.ensure_archive_room(root, pinned, 0)
                budget_mib = settings.local_budget_gib * 1024
                local_disk = shutil.disk_usage(RECORD_DIR)
                draining = needs_transport(
                    local_mib,
                    budget_mib,
                    settings.drain_percent,
                    admitted,
                    local_disk.free / (1024 * 1024),
                    hourly_mib,
                    start_percent=settings.start_percent,
                )
                if settings.mode == "mirror":
                    draining = bool(self.mirror_backlog_bytes)
                if admitted and not draining and settings.mode != "mirror":
                    local_mib, hourly_mib = self.sample_local()
                    draining = needs_transport(
                        local_mib,
                        budget_mib,
                        settings.drain_percent,
                        admitted,
                        local_disk.free / (1024 * 1024),
                        hourly_mib,
                        start_percent=settings.start_percent,
                    )
                drain_state.save(
                    settings.model_dump(),
                    draining if settings.mode == "move" else False,
                )
                if settings.enabled and draining and not self.batch.active:
                    planned, files = plan_archive_batch(
                        backlog_bytes=max(
                            0,
                            self.mirror_backlog_bytes
                            if settings.mode == "mirror"
                            else int(
                                (local_mib - budget_mib * settings.drain_percent / 100)
                                * 2**20
                            ),
                        )
                    )
                    self.batch.start(
                        int(local_mib * 2**20),
                        planned,
                        files,
                        "mirror"
                        if settings.mode == "mirror"
                        else "local_pressure"
                        if local_disk.free < hourly_mib * 2 * 2**20
                        else "high_water",
                    )
                self.batch.active = draining and settings.enabled
                backlog = (
                    self.mirror_backlog_bytes / 2**20
                    if settings.mode == "mirror"
                    else max(0, local_mib - budget_mib * settings.drain_percent / 100)
                    if draining
                    else 0
                )
                self.report(
                    state=("moving" if draining else "ready")
                    if settings.enabled
                    else "disabled",
                    error=None,
                    archive_free_mib=self.snapshot["capacity"]["archive"][
                        "filesystem_available_bytes"
                    ]
                    / 2**20,
                    archive_total_mib=self.snapshot["capacity"]["archive"][
                        "filesystem_total_bytes"
                    ]
                    / 2**20,
                    backlog_mib=backlog,
                    transfer=self.batch_status(local_mib) if settings.enabled else None,
                )
                if settings.enabled and draining:
                    recordings = list(
                        archive_candidates(
                            settings.parallel_transfers * ARCHIVE_QUEUE_MULTIPLIER
                        )
                    )
                    if recordings:
                        moved = self.move_batch(recordings, root)
                        if settings.mode != "mirror":
                            local_mib = max(
                                0, local_mib - sum(row.segment_size for row in moved)
                            )
                        if not moved:
                            self.stop_event.wait(1)
                        continue
                    self.report(
                        state="blocked",
                        error="archive_no_eligible_segments",
                        transfer=self.batch_status(local_mib, phase="waiting"),
                    )
            except ArchiveBlocked as error:
                if settings.adaptive_transfers:
                    self.adaptive.failed()
                code = str(error)
                if self.snapshot.get("error") != code:
                    logger.warning("Archive transfers paused: %s", code)
                self.report(
                    state="blocked", error=code, transfer=self.blocked_transfer()
                )
            except (OSError, DatabaseError, ValueError):
                if settings.adaptive_transfers:
                    self.adaptive.failed()
                self.expiry_cursor = None
                logger.exception(
                    "Archive maintenance failed; uncommitted transfers retain their local sources"
                )
                self.report(
                    state="blocked",
                    error="archive_unavailable",
                    transfer=self.blocked_transfer(),
                )
                capacities = self.snapshot.get("capacity", {})
                if "archive" in capacities:
                    self.report(
                        capacity={
                            **capacities,
                            "archive": invalidate_storage_capacity(
                                capacities["archive"], "offline"
                            ),
                        }
                    )
            self.stop_event.wait(5)
        database.close()
        self.camera_subscriber.stop()

    def sample_local(self, force: bool = True) -> tuple[float, float]:
        """Sample periodically, forcing fresh usage before declaring a drain complete."""
        if not force and time.monotonic() - self.local_sampled_at < 15:
            return self.local_usage_mib, self.hourly_usage_mib
        local = (
            Recordings.select(fn.SUM(Recordings.segment_size))
            .where(Recordings.storage_tier == "local")
            .scalar()
            or 0
        )
        self.mirror_backlog_bytes = 0
        if self.config.storage.mode == "mirror":
            pending_mib = (
                Recordings.select(fn.SUM(Recordings.segment_size))
                .where(
                    Recordings.storage_tier == "local",
                    Recordings.archive_bytes.is_null(),
                    ~Recordings.delete_requested,
                    Recordings.end_time < time.time() - 60,
                )
                .scalar()
                or 0
            )
            self.mirror_backlog_bytes = int(pending_mib * 2**20)
        hourly = (
            Recordings.select(
                fn.SUM(Recordings.segment_size),
                fn.COUNT(Recordings.id),
                fn.MIN(Recordings.start_time),
            )
            .where(Recordings.end_time > time.time() - 3600)
            .tuples()
            .get()
        )
        elapsed = min(3600, time.time() - hourly[2]) if hourly[2] is not None else 0
        self.ingest_rate = (
            float(hourly[0] or 0) * 2**20 / elapsed
            if hourly[1] >= 2 and elapsed >= 60
            else None
        )
        disk = shutil.disk_usage(RECORD_DIR)
        unknown = (
            Recordings.select()
            .where(Recordings.storage_tier == "local", Recordings.segment_size <= 0)
            .exists()
        )
        capacity = storage_capacity(
            None if unknown else int(local * 2**20),
            int(self.config.storage.local_budget_gib * 2**30),
            disk.total,
            disk.free,
            reserve_bytes=int((hourly[0] or 0) * 2 * 2**20),
            sampled_at=time.time(),
            filesystem_id=str(Path(RECORD_DIR).stat().st_dev),
        )
        self.report(capacity={**self.snapshot.get("capacity", {}), "local": capacity})
        self.local_usage_mib = float(local)
        self.hourly_usage_mib = float(hourly[0] or 0)
        self.local_sampled_at = time.monotonic()
        return self.local_usage_mib, self.hourly_usage_mib

    def sample_archive(
        self,
        pinned: Path,
        *,
        disk=None,
        used: int | None = None,
        reserved: int = 0,
        written: int = 0,
    ) -> None:
        """Probe mounted filesystem statistics only in the isolated archive worker."""
        disk = disk or shutil.disk_usage(pinned)
        reservations = int(
            ArchiveTransfer.select(fn.SUM(ArchiveTransfer.size)).scalar() or 0
        )
        maintenance = archive_maintenance_active()
        capacity = storage_capacity(
            None if used is None else max(0, used - reservations),
            archive_budget_bytes(self.config.storage.max_archive_gib),
            disk.total,
            disk.free,
            reserved_bytes=reservations,
            written_reservation_bytes=written,
            reserve_bytes=int(self.config.storage.archive_reserve_gib * 2**30),
            sampled_at=time.time(),
            filesystem_id=str(pinned.stat().st_dev),
        )
        capacity["reservation_accounting_known"] = not maintenance
        if maintenance:
            capacity["effective_headroom_bytes"] = None
        capacities = {**self.snapshot.get("capacity", {}), "archive": capacity}
        local = capacities.get("local")
        if local is not None and local["filesystem_id"] == capacity["filesystem_id"]:
            capacities["local"] = storage_capacity(
                local["usage_bytes"],
                local["budget_bytes"],
                disk.total,
                disk.free,
                reserve_bytes=local["reserve_bytes"],
                physical_reserved_bytes=reservations,
                written_reservation_bytes=written,
                sampled_at=capacity["sampled_at"],
                filesystem_id=capacity["filesystem_id"],
            )
            if maintenance:
                capacities["local"]["effective_headroom_bytes"] = None
                capacities["local"]["reservation_accounting_known"] = False
        self.report(
            capacity=capacities,
            archive_usage_bytes=used,
            archive_budget_bytes=capacity["budget_bytes"],
            archive_reserved_bytes=reservations,
        )

    def batch_status(self, local_mib: float, phase: str | None = None) -> dict:
        """Compose a bounded transfer snapshot without probing archive paths."""
        status = self.batch.status(
            int(local_mib * 2**20),
            int(
                self.config.storage.local_budget_gib
                * 2**30
                * self.config.storage.drain_percent
                / 100
            ),
            self.ingest_rate,
        )
        if self.config.storage.mode == "mirror":
            status.update(
                remaining_bytes=self.mirror_backlog_bytes,
                drain_target_bytes=None,
                eta_seconds=None,
                local_reclaimed_bytes=0,
            )
        return {
            **status,
            "sampled_at": time.time(),
            "mode": self.config.storage.mode,
            "phase": phase or ("waiting" if self.batch.active else "buffering"),
            "current_file": self.current_transfer,
            "adaptive_transfers": {
                "enabled": self.config.storage.adaptive_transfers,
                "maximum_workers": self.config.storage.parallel_transfers,
                "effective_workers": (
                    self.adaptive.workers(self.config.storage.parallel_transfers)
                    if self.config.storage.adaptive_transfers
                    else self.config.storage.parallel_transfers
                ),
                "state": (
                    self.adaptive.reason
                    if self.config.storage.adaptive_transfers
                    else "fixed"
                ),
            },
            "buffer_seconds": (
                max(
                    0,
                    self.config.storage.local_budget_gib
                    * 2**30
                    * self.config.storage.start_percent
                    / 100
                    - local_mib * 2**20,
                )
                / self.ingest_rate
                if not self.batch.active
                and self.ingest_rate is not None
                and self.ingest_rate > 0
                else None
            ),
        }

    def blocked_transfer(self) -> dict | None:
        """Do not keep publishing an ETA for paused or failed work."""
        transfer = self.snapshot.get("transfer")
        return (
            {
                **transfer,
                "phase": "waiting",
                "rate_bytes_per_second": None,
                "copy_rate_bytes_per_second": None,
                "verification_rate_bytes_per_second": None,
                "eta_seconds": None,
                "buffer_seconds": None,
            }
            if transfer
            else None
        )

    def _prepare_reservation(
        self, recording: Recordings, root: Path, pinned: Path
    ) -> int | None:
        """Check immutable source and destination paths outside SQLite writer locks."""
        current = Recordings.get_or_none(Recordings.id == recording.id)
        if (
            current is None
            or current.storage_tier != "local"
            or current.archive_bytes is not None
            or current.delete_requested
        ):
            return None
        size = Path(recording.path).stat().st_size
        destination = archive_destination(Path(recording.path), pinned)
        partial = destination.with_suffix(destination.suffix + ".partial")
        if (
            destination.exists()
            or destination.is_symlink()
            or partial.exists()
            or partial.is_symlink()
        ):
            raise ArchiveBlocked("archive_untracked_destination")
        self.ensure_archive_room(root, pinned, size)
        if self.transfer_cancelled():
            return None
        return size

    def _reserve_recording(
        self, recording: Recordings, root: Path, pinned: Path
    ) -> int | None:
        """Admit exact bytes before a serial file transfer."""
        size = self._prepare_reservation(recording, root, pinned)
        if size is None:
            return None
        free = shutil.disk_usage(pinned).free
        with storage_lock():
            current = Recordings.get_or_none(Recordings.id == recording.id)
            if (
                current is None
                or current.delete_requested
                or current.storage_tier != "local"
                or current.archive_bytes is not None
            ):
                return None
            if archive_maintenance_active():
                raise ArchiveBlocked("archive_maintenance_pending")
            budget = archive_budget_bytes(self.config.storage.max_archive_gib)
            if budget is not None and archive_owned_bytes() + size > budget:
                raise ArchiveBlocked("archive_capacity_changed")
            pending = ArchiveTransfer.select(fn.SUM(ArchiveTransfer.size)).scalar() or 0
            reserve = int(self.config.storage.archive_reserve_gib * 2**30)
            if free < pending + size + reserve:
                raise ArchiveBlocked("archive_physical_capacity_changed")
            ArchiveTransfer.create(id=recording.id, path=recording.path, size=size)
        return size

    def _reserve_batch(
        self, recordings: list[Recordings], root: Path, pinned: Path
    ) -> list[tuple[Recordings, int]]:
        """Commit bounded durable reservation groups before admitting copy work."""
        jobs: list[tuple[Recordings, int]] = []
        database = Recordings._meta.database
        for offset in range(0, len(recordings), 25):
            prepared: list[tuple[Recordings, int]] = []
            preparation_blocked = False
            for recording in recordings[offset : offset + 25]:
                try:
                    size = self._prepare_reservation(recording, root, pinned)
                except ArchiveBlocked as error:
                    if not jobs and not prepared:
                        raise
                    logger.warning("Archive batch admission paused: %s", error)
                    preparation_blocked = True
                    break
                if size is not None:
                    prepared.append((recording, size))
            if not prepared:
                break
            free = shutil.disk_usage(pinned).free
            with storage_lock():
                if self.transfer_cancelled():
                    break
                if archive_maintenance_active():
                    if not jobs:
                        raise ArchiveBlocked("archive_maintenance_pending")
                    break
                identifiers = [recording.id for recording, _ in prepared]
                retained = {
                    row.id
                    for row in Recordings.select(Recordings.id).where(
                        Recordings.id.in_(identifiers),
                        Recordings.storage_tier == "local",
                        Recordings.archive_bytes.is_null(),
                        Recordings.delete_requested == False,
                    )
                }
                budget = archive_budget_bytes(self.config.storage.max_archive_gib)
                owned = archive_owned_bytes() if budget is not None else 0
                pending = int(
                    ArchiveTransfer.select(fn.SUM(ArchiveTransfer.size)).scalar() or 0
                )
                reserve = int(self.config.storage.archive_reserve_gib * 2**30)
                admitted: list[tuple[Recordings, int]] = []
                incoming = 0
                blocked = None
                for recording, size in prepared:
                    if recording.id not in retained:
                        continue
                    if budget is not None and owned + incoming + size > budget:
                        blocked = "archive_capacity_changed"
                        break
                    if free < pending + incoming + size + reserve:
                        blocked = "archive_physical_capacity_changed"
                        break
                    admitted.append((recording, size))
                    incoming += size
                if admitted:
                    # No filesystem or NFS calls occur under the SQLite writer lock.
                    with database.atomic("IMMEDIATE"):
                        ArchiveTransfer.insert_many(
                            [
                                {
                                    "id": recording.id,
                                    "path": recording.path,
                                    "size": size,
                                }
                                for recording, size in admitted
                            ]
                        ).execute()
                    jobs.extend(admitted)
                if blocked is not None:
                    if not jobs:
                        raise ArchiveBlocked(blocked)
                    logger.warning("Archive batch admission paused: %s", blocked)
                    break
            self.report(transfer=self.batch_status(self.local_usage_mib, "waiting"))
            if preparation_blocked:
                break
        return jobs

    def move_recording(self, recording: Recordings, root: Path) -> bool:
        """Publish only a verified copy of a still-retained, immutable segment."""
        with pin_archive(root, self.config.storage.mount_id) as pinned:
            size = self._reserve_recording(recording, root, pinned)
            if size is None:
                return False
            destination = archive_destination(Path(recording.path), pinned)
            # The durable reservation survives every failure, including SIGKILL.
            # Recovery releases it only after publication or orphan cleanup.
            self.current_transfer = {
                "name": Path(recording.path).name,
                "camera": recording.camera,
                "start_time": recording.start_time,
                "end_time": recording.end_time,
                "size_bytes": size,
                "copied_bytes": 0,
                "readback_bytes": 0,
            }
            self.last_progress = 0

            def progress(phase: str, amount: int, total: int) -> None:
                self.batch.progress(phase, amount)
                if phase == "copying":
                    self.current_transfer["copied_bytes"] = amount
                elif phase == "verification":
                    self.current_transfer["readback_bytes"] = amount
                now = time.monotonic()
                if now - self.last_progress < 1 and amount != total:
                    return
                try:
                    used = archive_owned_bytes()
                except ArchiveBlocked:
                    used = None
                self.sample_archive(
                    pinned,
                    used=used,
                    reserved=size,
                    written=self.written_reservation(destination, size),
                )
                self.report(transfer=self.batch_status(self.local_usage_mib, phase))
                self.last_progress = time.monotonic()

            try:
                moved = move_recording(
                    recording,
                    root,
                    pinned,
                    self.config.storage.transfer_mib_per_second,
                    self.transfer_cancelled,
                    size,
                    progress,
                    preserve_local=self.config.storage.mode == "mirror",
                )
            except RecordingDeletionInProgress as error:
                raise ArchiveBlocked("archive_source_deletion_pending") from error
            finally:
                self.batch.progress("waiting", 0)
            if moved:
                ArchiveTransfer.delete_by_id(recording.id)
                self.batch.committed(
                    size,
                    0
                    if self.config.storage.mode == "mirror"
                    else int(recording.segment_size * 2**20),
                )
                if self.config.storage.mode != "mirror":
                    self.local_usage_mib = max(
                        0, self.local_usage_mib - recording.segment_size
                    )
                else:
                    self.mirror_backlog_bytes = max(0, self.mirror_backlog_bytes - size)
            self.current_transfer = None
            self.report(transfer=self.batch_status(self.local_usage_mib))
            return moved

    def move_batch(self, recordings: list[Recordings], root: Path) -> list[Recordings]:
        """Run one bounded batch with serial reservations and main-thread controls."""
        settings = self.config.storage
        workers = (
            self.adaptive.workers(settings.parallel_transfers)
            if settings.adaptive_transfers
            else settings.parallel_transfers
        )
        verified_before = self.batch.verified_bytes
        revision = self.config_revision.value
        cancelled = threading.Event()
        lock = threading.Lock()
        states: dict[str, dict[str, Any]] = {}
        finished: set[str] = set()
        moved: list[Recordings] = []
        prepared: list[PreparedArchiveMove] = []
        copied_files: list[PendingArchiveMove] = []
        failure: OSError | DatabaseError | ValueError | None = None
        limiter = (
            ArchiveRateLimiter(settings.transfer_mib_per_second)
            if settings.transfer_mib_per_second > 0
            else None
        )
        database = Recordings._meta.database
        with pin_archive(root, settings.mount_id) as pinned, ExitStack() as cleanup:
            directories = ArchiveDirectoryBatch(pinned)
            jobs = self._reserve_batch(
                recordings[: workers * ARCHIVE_QUEUE_MULTIPLIER],
                root,
                pinned,
            )
            for recording, size in jobs:
                states[recording.id] = {
                    "id": recording.id,
                    "name": Path(recording.path).name,
                    "camera": recording.camera,
                    "start_time": recording.start_time,
                    "end_time": recording.end_time,
                    "size_bytes": size,
                    "copied_bytes": 0,
                    "readback_bytes": 0,
                    "phase": "waiting",
                }
            if not jobs:
                return []
            started = time.monotonic()

            def stopping() -> bool:
                return (
                    cancelled.is_set()
                    or self.stop_event.is_set()
                    or revision != self.config_revision.value
                    or not self.batch.active
                )

            def progress_for(
                recording: Recordings,
            ) -> Callable[[str, int, int], None]:
                def progress(phase: str, amount: int, total: int) -> None:
                    with lock:
                        current = states[recording.id]
                        current["phase"] = phase
                        if phase == "copying":
                            current["copied_bytes"] = amount
                        elif phase == "verification":
                            current["readback_bytes"] = amount

                return progress

            def transfer(recording: Recordings, size: int) -> PendingArchiveMove | None:
                with database.connection_context():
                    # Durable admission protects the source through grouped publication.
                    return copy_recording_unverified(
                        recording,
                        root,
                        pinned,
                        settings.transfer_mib_per_second,
                        stopping,
                        size,
                        progress_for(recording),
                        limiter,
                        prepare_directory=directories.prepare,
                    )

            def publish() -> None:
                with lock:
                    files = [dict(value) for value in states.values()]
                active = [
                    item
                    for item in files
                    if item["id"] not in finished and item["phase"] != "waiting"
                ]
                phase = next(
                    (
                        value
                        for value in ("copying", "verification", "commit")
                        if any(item["phase"] == value for item in active)
                    ),
                    "waiting",
                )
                elapsed = time.monotonic() - started
                snapshot = self.batch_status(self.local_usage_mib, phase)
                snapshot.update(
                    aggregate_rates=True,
                    parallel_transfers=workers,
                    active_files=active,
                    current_file=active[0] if active else None,
                    copy_rate_bytes_per_second=(
                        sum(item["copied_bytes"] for item in files) / elapsed
                        if elapsed >= 1
                        else None
                    ),
                    verification_rate_bytes_per_second=(
                        sum(item["readback_bytes"] for item in files) / elapsed
                        if elapsed >= 1
                        else None
                    ),
                )
                self.report(transfer=snapshot)

            def completed(
                job: tuple[Recordings, int],
                future: Future[PendingArchiveMove | None],
            ) -> None:
                nonlocal failure
                recording, _size = job
                try:
                    copied = future.result()
                except (OSError, DatabaseError, ValueError) as error:
                    if failure is None:
                        failure = error
                    cancelled.set()
                    finished.add(recording.id)
                    return
                if copied is not None:
                    copied_files.append(copied)
                    if copied.copy is not None:
                        cleanup.callback(copied.copy.partial.unlink, missing_ok=True)
                    with lock:
                        states[recording.id]["phase"] = "waiting"
                else:
                    finished.add(recording.id)

            stopped = run_archive_jobs(
                jobs,
                workers,
                lambda job: transfer(*job),
                lambda: self.transfer_cancelled() or stopping(),
                completed,
                publish,
                cancelled,
            )
            if failure is not None:
                raise failure
            stopped = (
                stopped
                or self.stop_event.is_set()
                or revision != self.config_revision.value
                or not self.batch.active
            )
            if not stopped:
                # The scheduler joins every copy before any readback is admitted.
                cancelled.clear()

                def verify(item: PendingArchiveMove) -> PreparedArchiveMove | None:
                    with database.connection_context():
                        return verify_recording_copy(
                            item,
                            settings.transfer_mib_per_second,
                            stopping,
                            progress_for(item.move.recording),
                            limiter,
                        )

                def verified(
                    item: PendingArchiveMove,
                    future: Future[PreparedArchiveMove | None],
                ) -> None:
                    nonlocal failure
                    try:
                        result = future.result()
                    except (OSError, DatabaseError, ValueError) as error:
                        if failure is None:
                            failure = error
                        cancelled.set()
                        return
                    if result is not None:
                        prepared.append(result)

                stopped = run_archive_jobs(
                    copied_files,
                    workers,
                    verify,
                    lambda: self.transfer_cancelled() or stopping(),
                    verified,
                    publish,
                    cancelled,
                )
                if failure is not None:
                    raise failure
            if not stopped:

                def release_reservation(row: Recordings) -> None:
                    ArchiveTransfer.delete_by_id(row.id)

                def publication_progress() -> None:
                    self.report(
                        state="moving",
                        error=None,
                        transfer=self.batch_status(self.local_usage_mib, "commit"),
                    )

                moved = publish_recording_copies(
                    prepared,
                    lambda: (
                        self.transfer_cancelled()
                        or self.stop_event.is_set()
                        or revision != self.config_revision.value
                        or not self.batch.active
                    ),
                    track_bytes=True,
                    preserve_local=settings.mode == "mirror",
                    on_commit=release_reservation,
                    on_progress=publication_progress,
                )
                by_id = {item.recording.id: item for item in prepared}
                for recording in moved:
                    item = by_id[recording.id]
                    if not item.recovering:
                        self.batch.committed(
                            item.original.st_size,
                            0
                            if settings.mode == "mirror"
                            else int(recording.segment_size * 2**20),
                        )
                    if settings.mode != "mirror":
                        self.local_usage_mib = max(
                            0, self.local_usage_mib - recording.segment_size
                        )
                    else:
                        self.mirror_backlog_bytes = max(
                            0, self.mirror_backlog_bytes - item.original.st_size
                        )
            finished.update(states)
            publish()
            if settings.adaptive_transfers and not stopped:
                previous = workers
                self.adaptive.observe(
                    self.batch.verified_bytes - verified_before,
                    time.monotonic() - started,
                    len(moved),
                    settings.parallel_transfers,
                    settings.transfer_mib_per_second,
                )
                if self.adaptive.current != previous:
                    logger.info(
                        "Adaptive archive transfers changed from %s to %s workers (%s)",
                        previous,
                        self.adaptive.current,
                        self.adaptive.reason,
                    )
        return moved

    def written_reservation(self, destination: Path, size: int) -> int:
        """Credit only bytes allocated on the filesystem, not merely buffered writes."""
        partial = destination.with_suffix(destination.suffix + ".partial")
        try:
            stat = partial.stat() if partial.exists() else destination.stat()
        except FileNotFoundError:
            return 0
        # Filesystems without allocated-block reporting retain a conservative
        # full physical reservation until publication.
        return min(size, getattr(stat, "st_blocks", 0) * 512)

    def ensure_archive_room(self, root: Path, pinned: Path, incoming: int) -> None:
        """Admit a single reserved copy, evicting only explicitly eligible footage."""
        self.capacity.ensure_room(
            self.config,
            root,
            pinned,
            incoming,
            lambda: self.transfer_cancelled("checking"),
            self.report,
        )

    def transfer_cancelled(self, state: str = "moving") -> bool:
        """Maintain a progress heartbeat and honor live edits during long copies."""
        if time.time() - self.snapshot.get("updated_at", 0) > 5:
            self.report(state=state, error=None)
        changed = self.apply_updates()
        return (
            self.stop_event.is_set()
            or changed
            or self.config_revision.value != self.applied_revision.value
        )

    def expire_archive(self, root: Path) -> None:
        """Delete expired archive content only while the verified share is online."""
        with pin_archive(root, self.config.storage.mount_id) as pinned:
            self._expire_archive(root, pinned)

    def _expire_archive(self, root: Path, pinned: Path) -> None:
        now = time.time()
        RecordingLease.delete().where(RecordingLease.expires <= now).execute()
        maximum = self.config.storage.max_age_days
        minimum = self.config.storage.retain_days
        cutoff_days = min(minimum, maximum) if maximum is not None else minimum
        candidates = (
            Recordings.select()
            .where(
                (Recordings.storage_tier == "archive")
                | Recordings.archive_bytes.is_null(False),
                Recordings.end_time < now - cutoff_days * 86400,
                Recordings.delete_requested == False,
            )
            .order_by(Recordings.end_time, Recordings.id)
        )
        if self.expiry_cursor is not None:
            end_time, recording_id = self.expiry_cursor
            candidates = candidates.where(
                (Recordings.end_time > end_time)
                | ((Recordings.end_time == end_time) & (Recordings.id > recording_id))
            )
        batch = list(candidates.limit(1000))
        self.expiry_cursor = (batch[-1].end_time, batch[-1].id) if batch else None
        queued = (
            Recordings.select()
            .where(
                (Recordings.storage_tier == "archive")
                | Recordings.archive_bytes.is_null(False),
                Recordings.delete_requested == True,
            )
            .limit(1000)
        )
        for recording in [*queued, *batch]:
            if self.transfer_cancelled("checking"):
                self.expiry_cursor = None
                return
            if time.time() - self.snapshot.get("updated_at", 0) > 5:
                self.report(state="checking", error=None)
            camera = self.config.cameras.get(recording.camera)
            days = self.config.storage.retain_days
            if camera is not None and not recording.delete_requested:
                days = archive_retention_days(recording, camera, days, maximum)
            if maximum is not None:
                days = min(days, maximum)
            expired = expire_recording(
                recording,
                root,
                pinned,
                days,
                now,
                CapturePadding.from_record(
                    camera.record if camera is not None else self.config.record
                ),
            )
            if expired:
                # Let live metadata writers acquire SQLite between durable deletions.
                self.stop_event.wait(0.05)

    def audit_mirrors(self, pinned: Path) -> None:
        """Check a bounded oldest-first mirror pass only in the isolated worker."""
        rows = Recordings.select().where(
            Recordings.storage_tier == "local",
            Recordings.archive_bytes.is_null(False),
            ~Recordings.delete_requested,
        )
        if self.mirror_audit_cursor is not None:
            end, identifier = self.mirror_audit_cursor
            rows = rows.where(
                (Recordings.end_time > end)
                | ((Recordings.end_time == end) & (Recordings.id > identifier))
            )
        batch = list(rows.order_by(Recordings.end_time, Recordings.id).limit(256))
        for row in batch:
            if self.transfer_cancelled("checking"):
                return
            audit_mirror_copy(row, pinned)
            self.mirror_audit_cursor = (row.end_time, row.id)
        if len(batch) < 256:
            self.mirror_audit_cursor = None

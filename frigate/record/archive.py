"""Verified cross-filesystem copies with stable, atomically replaced links."""

import hashlib
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from filelock import FileLock

    from frigate.config import FrigateConfig
    from frigate.record.policy import CapturePadding

from frigate.const import CACHE_DIR, RECORD_DIR
from frigate.record.archive_rate import ArchiveRateLimiter

logger = logging.getLogger(__name__)
CHUNK_SIZE = 1024 * 1024
STORAGE_LOCK = f"{CACHE_DIR}/recording-storage.lock"


class ArchiveDrain:
    """Track a fixed initial plan separately from a live hysteretic drain."""

    def __init__(self) -> None:
        self.active = False
        self.started_at: float | None = None
        self.started_monotonic = 0.0
        self.initial_local_bytes = 0
        self.planned_bytes = 0
        self.planned_files = 0
        self.verified_bytes = 0
        self.verified_files = 0
        self.released_bytes = 0
        self.trigger = "high_water"
        self.phase_totals: dict[str, tuple[int, float]] = {}
        self.progress_phase: str | None = None
        self.progress_bytes = 0
        self.progress_sampled_at = 0.0

    def start(
        self, local: int, planned: int, files: int, trigger: str = "high_water"
    ) -> None:
        """Fix the plan denominator at the moment the high-water trigger fires."""
        self.active = True
        self.started_at = time.time()
        self.started_monotonic = time.monotonic()
        self.initial_local_bytes = local
        self.planned_bytes = planned
        self.planned_files = files
        self.verified_bytes = self.verified_files = self.released_bytes = 0
        self.trigger = trigger
        self.phase_totals = {"copying": (0, 0.0), "verification": (0, 0.0)}
        self.progress_phase = None
        self.progress_bytes = 0
        self.progress_sampled_at = self.started_monotonic

    def progress(self, phase: str, amount: int) -> None:
        """Measure active copy/readback time across files, excluding idle intervals."""
        now = time.monotonic()
        if self.progress_phase in self.phase_totals:
            size, elapsed = self.phase_totals[self.progress_phase]
            self.phase_totals[self.progress_phase] = (
                size,
                elapsed + now - self.progress_sampled_at,
            )
        if phase in self.phase_totals:
            size, elapsed = self.phase_totals[phase]
            self.phase_totals[phase] = (
                size
                + amount
                - (self.progress_bytes if phase == self.progress_phase else 0),
                elapsed,
            )
        self.progress_phase = phase
        self.progress_bytes = amount
        self.progress_sampled_at = now

    def committed(self, size: int, released: int) -> None:
        """Count a file only after readback, durable publication and DB commit."""
        self.verified_bytes += size
        self.verified_files += 1
        self.released_bytes += released

    def status(self, local: int, target: int, ingest_rate: float | None) -> dict:
        """Estimate a live drain only from sufficiently long completed samples."""
        now = time.monotonic()
        elapsed = now - self.started_monotonic
        phase_rates = {}
        for phase, (size, seconds) in self.phase_totals.items():
            if self.progress_phase == phase:
                seconds += now - self.progress_sampled_at
            phase_rates[phase] = (
                size / seconds if self.active and seconds >= 1 else None
            )
        rate = (
            self.verified_bytes / elapsed
            if self.active and self.verified_files >= 2 and elapsed >= 1
            else None
        )
        net = (
            rate - ingest_rate if rate is not None and ingest_rate is not None else None
        )
        backlog = max(0, local - target) if self.active else 0
        return {
            "active": self.active,
            "trigger": self.trigger,
            "started_at": self.started_at,
            "initial_plan_bytes": self.planned_bytes,
            "initial_plan_files": self.planned_files,
            "plan_is_estimate": True,
            "verified_bytes": self.verified_bytes,
            "verified_files": self.verified_files,
            "incoming_bytes": max(
                0, local - self.initial_local_bytes + self.released_bytes
            )
            if self.started_at is not None
            else 0,
            "local_usage_bytes": local,
            "drain_target_bytes": target,
            "remaining_bytes": backlog,
            "rate_bytes_per_second": rate,
            "copy_rate_bytes_per_second": phase_rates.get("copying"),
            "verification_rate_bytes_per_second": phase_rates.get("verification"),
            "ingest_bytes_per_second": ingest_rate,
            "eta_seconds": backlog / net if net is not None and net > 0 else None,
            "cannot_outpace_ingest": net is not None and net <= 0,
            "buffer_seconds": (
                max(0, self.initial_local_bytes - local) / ingest_rate
                if not self.active and ingest_rate is not None and ingest_rate > 0
                else None
            ),
        }


def apply_archive_retention(config: "FrigateConfig") -> None:
    """Keep metadata and local footage alive while archive storage is configured."""
    from frigate.models import Recordings

    active = (
        config.storage.enabled
        or Recordings.select().where(Recordings.storage_tier == "archive").exists()
    )
    days = config.storage.retain_days if active else 0
    config.record.archive_retention_days = days
    for camera in config.cameras.values():
        camera.record.archive_retention_days = days


def needs_transport(
    local_mib: float,
    budget_mib: float,
    drain_percent: int,
    draining: bool,
    free_mib: float,
    hourly_mib: float,
    start_percent: int = 100,
) -> bool:
    """Apply budget hysteresis and an independent two-hour free-space reserve."""
    pressure = free_mib < hourly_mib * 2
    if local_mib >= budget_mib * start_percent / 100 or pressure:
        return True
    if local_mib <= budget_mib * drain_percent / 100:
        return False
    return draining


def storage_lock() -> "FileLock":
    """Serialize local path publication and local deletion across processes."""
    from filelock import FileLock

    Path(STORAGE_LOCK).parent.mkdir(parents=True, exist_ok=True)
    return FileLock(STORAGE_LOCK, timeout=30)


def delete_local_recording(
    path: Path,
    missing_ok: bool = True,
    clear: bool = False,
    can_delete: Callable[[], bool] | None = None,
    capture_padding: "CapturePadding | None" = None,
) -> bool:
    """Delete only a local file; archive links belong to the archive worker."""
    with storage_lock():
        if path.is_symlink():
            return False
        from frigate.models import ArchiveTransfer, Recordings
        from frigate.record.leases import recording_has_lease
        from frigate.record.policy import (
            CapturePadding,
            RecordingProtected,
            recording_is_protected,
        )

        recording = Recordings.get_or_none(Recordings.path == str(path))
        if recording is not None and recording_is_protected(
            recording.camera,
            recording.start_time,
            recording.end_time,
            capture_padding or CapturePadding(),
        ):
            if capture_padding is not None:
                raise RecordingProtected(
                    "Saved recordings must be unprotected before deletion"
                )
            return False
        if recording is not None and (
            recording_has_lease(recording)
            or ArchiveTransfer.select()
            .where(ArchiveTransfer.id == recording.id)
            .exists()
        ):
            return False
        if can_delete is not None and not can_delete():
            return False
        if clear:
            from frigate.util.builtin import clear_and_unlink

            clear_and_unlink(path, missing_ok=missing_ok)
        else:
            path.unlink(missing_ok=missing_ok)
        return True


def request_recording_deletion(
    path: Path,
    storage_tier: str = "local",
    *,
    capture_padding: "CapturePadding | None" = None,
) -> bool:
    """Delete local content now or persist an offline-safe archive deletion."""
    from frigate.models import Recordings
    from frigate.record.policy import (
        CapturePadding,
        RecordingProtected,
        recording_is_protected,
    )

    padding = capture_padding or CapturePadding()

    def check_protection() -> None:
        recording = Recordings.get_or_none(Recordings.path == str(path))
        if recording is not None and recording_is_protected(
            recording.camera, recording.start_time, recording.end_time, padding
        ):
            raise RecordingProtected(
                "Saved recordings must be unprotected before deletion"
            )

    def claim() -> bool:
        check_protection()
        Recordings.update(delete_requested=True, delete_reason="user").where(
            Recordings.path == str(path)
        ).execute()
        return True

    if storage_tier != "archive" and delete_local_recording(
        path, can_delete=claim, capture_padding=padding
    ):
        return True
    with storage_lock():
        check_protection()
        if storage_tier != "archive" and not path.is_symlink():
            raise BlockingIOError(
                "Recording is leased or reserved for archive transfer"
            )
        Recordings.update(
            storage_tier="archive", delete_requested=True, delete_reason="user"
        ).where(Recordings.path == str(path)).execute()
    return False


def validate_mount(root: Path, mount_id: str) -> None:
    """Require an NFS mount and a pre-provisioned identity before any writes."""
    with open("/proc/self/mountinfo") as mounts:
        nfs_mounts = []
        for line in mounts:
            fields = line.split()
            separator = fields.index("-")
            mountpoint = Path(
                re.sub(
                    r"\\([0-7]{3})",
                    lambda match: chr(int(match[1], 8)),
                    fields[4],
                )
            )
            if root == mountpoint or mountpoint in root.parents:
                nfs_mounts.append((len(mountpoint.parts), fields[separator + 1]))
    if not nfs_mounts or max(nfs_mounts)[1] not in ("nfs", "nfs4"):
        raise OSError("Archive directory is not on an NFS filesystem")
    if root.is_symlink() or root.resolve() != root:
        raise OSError("Archive directory must not contain symbolic links")
    if (root / ".frigate-archive").read_text().strip() != mount_id:
        raise OSError("Archive identity marker does not match")


@contextmanager
def pin_archive(root: Path, mount_id: str) -> Generator[Path]:
    """Anchor writes to the verified mount even if its mountpoint is detached."""
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        validate_mount(root, mount_id)
        pinned = os.fstat(descriptor)
        current = root.stat()
        if (pinned.st_dev, pinned.st_ino) != (current.st_dev, current.st_ino):
            raise OSError("Archive mount changed during validation")
        yield Path(f"/proc/self/fd/{descriptor}")
    finally:
        os.close(descriptor)


def archive_destination(source: Path, root: Path) -> Path:
    """Preserve the recording's relative date/camera directory structure."""
    relative = source.relative_to(Path(RECORD_DIR))
    if ".." in relative.parts:
        raise ValueError("Recording path must not escape the recording directory")
    return root / relative


def prepare_archive_directory(
    directory: Path,
    root: Path,
    durable: dict[Path, tuple[int, int]] | None = None,
) -> None:
    """Persist each new directory before a local recording can be released."""
    relative = directory.relative_to(root)
    if ".." in relative.parts:
        raise ValueError("Archive destination must remain inside the archive")
    current = root
    for part in relative.parts:
        current = current / part
        try:
            current.mkdir()
        except FileExistsError:
            if current.is_symlink() or not current.is_dir():
                raise OSError(
                    "Archive directory contains a non-directory or symbolic link"
                ) from None
        stat = current.stat()
        identity = (stat.st_dev, stat.st_ino)
        if durable is not None and durable.get(current) == identity:
            continue
        fsync_directory(current)
        fsync_directory(current.parent)
        if durable is not None:
            durable[current] = identity


class ArchiveDirectoryBatch:
    """Reuse successful directory flushes only within one pinned mount lifetime."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._durable: dict[Path, tuple[int, int]] = {}
        self._lock = threading.Lock()

    def prepare(self, directory: Path) -> None:
        """Validate each ancestor and serialize its first durable preparation."""
        with self._lock:
            prepare_archive_directory(directory, self.root, self._durable)


@dataclass(frozen=True)
class UnverifiedArchiveCopy:
    """Durable temporary bytes that must not replace an original yet."""

    source: Path
    destination: Path
    partial: Path
    size: int
    digest: bytes


def copy_unverified(
    source: Path,
    destination: Path,
    rate_mib: float,
    cancelled: Callable[[], bool],
    maximum_bytes: int | None = None,
    progress: Callable[[str, int, int], None] | None = None,
    limiter: ArchiveRateLimiter | None = None,
) -> UnverifiedArchiveCopy:
    """Copy and flush bytes, deferring archive readback to a later batch phase."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".partial")
    started = time.monotonic()
    copied = 0
    digest = hashlib.sha256()
    total = source.stat().st_size
    complete = False

    def notify(phase: str, amount: int) -> None:
        if progress is not None:
            progress(phase, amount, total)

    def throttle(transferred: int) -> None:
        if not rate_mib:
            return
        deadline = started + transferred / (rate_mib * CHUNK_SIZE)
        while (delay := deadline - time.monotonic()) > 0:
            if cancelled():
                raise InterruptedError("Archive transfer cancelled")
            time.sleep(min(delay, 0.1))

    try:
        notify("copying", 0)
        with source.open("rb") as reader, partial.open("wb") as writer:
            while data := reader.read(CHUNK_SIZE):
                if cancelled():
                    raise InterruptedError("Archive transfer cancelled")
                if maximum_bytes is not None and copied + len(data) > maximum_bytes:
                    raise OSError("Recording exceeds its archive reservation")
                writer.write(data)
                digest.update(data)
                copied += len(data)
                notify("copying", copied)
                if limiter is not None:
                    limiter.consume(len(data), cancelled)
                else:
                    throttle(copied)
            writer.flush()
            os.fsync(writer.fileno())

        if copied != total or copied != source.stat().st_size:
            raise OSError("Recording changed during archive copy")
        complete = True
        return UnverifiedArchiveCopy(
            source, destination, partial, copied, digest.digest()
        )
    finally:
        if not complete:
            partial.unlink(missing_ok=True)


def verify_archive_copy(
    copied_file: UnverifiedArchiveCopy,
    rate_mib: float,
    cancelled: Callable[[], bool],
    progress: Callable[[str, int, int], None] | None = None,
    limiter: ArchiveRateLimiter | None = None,
) -> None:
    """Read back a completed copy before publishing its archive filename."""
    source = copied_file.source
    destination = copied_file.destination
    partial = copied_file.partial
    started = time.monotonic()
    verified = hashlib.sha256()
    read_back = 0

    def notify(phase: str, amount: int) -> None:
        if progress is not None:
            progress(phase, amount, copied_file.size)

    try:
        notify("verification", 0)
        with partial.open("rb") as reader:
            while data := reader.read(CHUNK_SIZE):
                if cancelled():
                    raise InterruptedError("Archive verification cancelled")
                verified.update(data)
                read_back += len(data)
                notify("verification", read_back)
                if limiter is not None:
                    limiter.consume(len(data), cancelled)
                else:
                    deadline = (
                        started + read_back / (rate_mib * CHUNK_SIZE)
                        if rate_mib
                        else started
                    )
                    while (delay := deadline - time.monotonic()) > 0:
                        if cancelled():
                            raise InterruptedError("Archive verification cancelled")
                        time.sleep(min(delay, 0.1))
        if (
            read_back != copied_file.size
            or copied_file.size != source.stat().st_size
            or verified.digest() != copied_file.digest
        ):
            raise OSError("Archive verification failed")
        if cancelled():
            raise InterruptedError("Archive transfer cancelled")
        os.replace(partial, destination)
        fsync_directory(destination.parent)
        notify("commit", copied_file.size)
    finally:
        partial.unlink(missing_ok=True)


def copy_verified(
    source: Path,
    destination: Path,
    rate_mib: float,
    cancelled: Callable[[], bool],
    maximum_bytes: int | None = None,
    progress: Callable[[str, int, int], None] | None = None,
    limiter: ArchiveRateLimiter | None = None,
) -> None:
    """Preserve the serial copy API using the same two-phase primitives."""
    copied = copy_unverified(
        source, destination, rate_mib, cancelled, maximum_bytes, progress, limiter
    )
    verify_archive_copy(copied, rate_mib, cancelled, progress, limiter)


def fsync_directory(path: Path) -> None:
    """Persist a directory entry on filesystems that support directory fsync."""
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_archive_link(source: Path, destination: Path) -> None:
    """Keep existing manifests and downloads valid across a tier transition."""
    link = source.with_suffix(source.suffix + ".archive-link")
    try:
        link.symlink_to(destination)
        os.replace(link, source)
        fsync_directory(source.parent)
    finally:
        link.unlink(missing_ok=True)

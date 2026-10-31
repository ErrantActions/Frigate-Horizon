"""Local-only reclamation of durably verified redundant recording copies."""

import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from frigate.models import ArchiveTransfer, Recordings
from frigate.record.archive import (
    archive_destination,
    publish_archive_link,
    storage_lock,
)
from frigate.record.leases import recording_has_lease
from frigate.record.policy import CapturePadding, recording_is_protected

if TYPE_CHECKING:
    from frigate.config import FrigateConfig

CLEANUP_INTERVAL = 24 * 3600
CLEANUP_BATCH = 256


def validate_local_mirror_mount() -> None:
    """Reject remote or ephemeral recording mounts using local mount metadata only."""
    from frigate.const import RECORD_DIR

    root = Path(RECORD_DIR)
    mounts = []
    with open("/proc/self/mountinfo", encoding="utf-8") as reader:
        for line in reader:
            fields = line.split()
            separator = fields.index("-")
            mountpoint = Path(
                re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4])
            )
            if root == mountpoint or mountpoint in root.parents:
                mounts.append((len(mountpoint.parts), fields[separator + 1]))
    if not mounts or max(mounts)[1] not in ("ext4", "xfs", "zfs", "btrfs"):
        raise OSError("Mirror recordings require a persistent local filesystem mount")


def release_local_copy(
    recording_id: str,
    root: Path,
    padding: CapturePadding = CapturePadding(),
) -> float | None:
    """Release only a verified redundant original without ever probing the NAS."""
    with storage_lock():
        row = Recordings.get_or_none(Recordings.id == recording_id)
        if (
            row is None
            or row.storage_tier != "local"
            or row.archive_bytes is None
            or row.archive_bytes < 0
            or row.delete_requested
            or ArchiveTransfer.select().where(ArchiveTransfer.id == row.id).exists()
            or recording_has_lease(row)
            or recording_is_protected(row.camera, row.start_time, row.end_time, padding)
        ):
            return None
        source = Path(row.path)
        destination = archive_destination(source, root)
        if source.is_symlink():
            if source.readlink() != destination:
                raise OSError("Recording link does not match configured archive")
            reclaimed = 0.0
        else:
            original = source.lstat()
            if original.st_size != row.archive_bytes:
                raise OSError("Verified local original changed before reclamation")
            reclaimed = original.st_size / 2**20
            publish_archive_link(source, destination)
        # A crash before this commit is repaired by recover_archive_links.
        with Recordings._meta.database.atomic("IMMEDIATE"):
            Recordings.update(storage_tier="archive").where(
                Recordings.id == row.id
            ).execute()
        return reclaimed


def cleanup_local_mirrors(config: "FrigateConfig", now: float | None = None) -> None:
    """Advance one restart-safe daily local-only cleanup batch."""
    if not config.storage.enabled or config.storage.mode != "mirror":
        return
    now = time.time() if now is None else now
    database = Recordings._meta.database
    database.execute_sql(
        "INSERT OR IGNORE INTO recordingmirrorstate (id,last_cleanup) VALUES (1,?)",
        (now,),
    )
    last, cutoff, end, identifier = database.execute_sql(
        "SELECT last_cleanup,cutoff,cursor_end,cursor_id "
        "FROM recordingmirrorstate WHERE id=1"
    ).fetchone()
    if cutoff is None:
        if now - last < CLEANUP_INTERVAL:
            return
        cutoff = now - config.storage.local_copy_hours * 3600
        database.execute_sql(
            "UPDATE recordingmirrorstate SET cutoff=? WHERE id=1", (cutoff,)
        )
    # A changed minimum can make an in-flight cleanup more conservative.
    cutoff = min(cutoff, now - config.storage.local_copy_hours * 3600)
    candidates = Recordings.select().where(
        Recordings.storage_tier == "local",
        Recordings.archive_bytes.is_null(False),
        Recordings.end_time < cutoff,
    )
    if end is not None:
        candidates = candidates.where(
            (Recordings.end_time > end)
            | ((Recordings.end_time == end) & (Recordings.id > identifier))
        )
    batch = list(
        candidates.order_by(Recordings.end_time, Recordings.id).limit(CLEANUP_BATCH)
    )
    for row in batch:
        camera = config.cameras.get(row.camera)
        release_local_copy(
            row.id,
            Path(config.storage.archive_path),
            CapturePadding.from_record(camera.record if camera else config.record),
        )
    if len(batch) < CLEANUP_BATCH:
        database.execute_sql(
            "UPDATE recordingmirrorstate SET last_cleanup=?,cutoff=NULL,"
            "cursor_end=NULL,cursor_id=NULL WHERE id=1",
            (now,),
        )
    else:
        database.execute_sql(
            "UPDATE recordingmirrorstate SET cursor_end=?,cursor_id=? WHERE id=1",
            (batch[-1].end_time, batch[-1].id),
        )

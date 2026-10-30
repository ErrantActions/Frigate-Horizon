"""Local database leases for original recordings, never archive filesystem I/O."""

import math
import time

from frigate.models import RecordingLease, Recordings
from frigate.record.archive import storage_lock
from frigate.record.policy import RecordingDeletionInProgress


def lease_recordings(
    token: str, camera: str, start: float, end: float, lifetime: float = 300,
    *, owner: str | None = None,
) -> None:
    """Acquire or renew a window before planning/opening original recording media."""
    if not all(math.isfinite(value) for value in (start, end, lifetime)) or (
        end < start or lifetime <= 0
    ):
        raise ValueError("Invalid recording lease window")
    database = Recordings._meta.database
    with storage_lock():
        existing = (
            RecordingLease.select().where(RecordingLease.id == token)
            .bind(database).first()
        )
        if existing is not None and (
            existing.camera != camera
            or existing.start_time != start
            or existing.end_time != end
            or existing.owner != owner
        ):
            raise ValueError("Recording lease identity cannot change")
        if (
            Recordings.select()
            .where(
                Recordings.camera == camera,
                Recordings.end_time >= start,
                Recordings.start_time <= end,
                Recordings.delete_requested == True,
            )
            .exists()
        ):
            raise RecordingDeletionInProgress("Recording deletion is in progress")
        RecordingLease.insert(
            id=token,
            camera=camera,
            start_time=start,
            end_time=end,
            expires=time.time() + lifetime,
            owner=owner,
        ).on_conflict_replace().bind(database).execute()


def release_recordings(token: str) -> None:
    """Release a completed reader's original-media reservation."""
    RecordingLease.delete().where(RecordingLease.id == token).bind(
        Recordings._meta.database
    ).execute()


def recording_has_lease(recording: Recordings) -> bool:
    """Check current reader windows while the caller holds the storage path lock."""
    return (
        RecordingLease.select()
        .where(
            RecordingLease.camera == recording.camera,
            RecordingLease.start_time <= recording.end_time,
            RecordingLease.end_time >= recording.start_time,
            RecordingLease.expires > time.time(),
        )
        .bind(Recordings._meta.database)
        .exists()
    )

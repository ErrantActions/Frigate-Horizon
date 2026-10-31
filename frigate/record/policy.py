"""Shared capture windows and protected-recording deletion rules."""

import logging
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from math import inf, isfinite
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from frigate.models import Event, Recordings, ReviewSegment
from frigate.record import archive

if TYPE_CHECKING:
    from frigate.config.camera.record import RecordConfig

logger = logging.getLogger(__name__)
AutomaticDeletionReason = Literal["retention", "emergency"]


class RecordingDeletionInProgress(RuntimeError):
    """A retain request conflicts with an already claimed recording deletion."""


class RecordingProtected(RuntimeError):
    """Explicit deletion requires confirmed removal of saved-event protection."""

    message = (
        "Protected media cannot be deleted. Remove permanent protection with "
        "confirmation first"
    )


@dataclass(frozen=True)
class CapturePadding:
    alert_pre: float = 0
    alert_post: float = 0
    detection_pre: float = 0
    detection_post: float = 0

    @classmethod
    def from_record(cls, record: "RecordConfig") -> "CapturePadding":
        """Capture both review severities, independently of recording retention."""
        return cls(
            record.alerts.pre_capture,
            record.alerts.post_capture,
            record.detections.pre_capture,
            record.detections.post_capture,
        )

    @property
    def pre(self) -> float:
        """Return the conservative pre-capture window for a legacy event."""
        return max(self.alert_pre, self.detection_pre)

    @property
    def post(self) -> float:
        """Return the conservative post-capture window for a legacy event."""
        return max(self.alert_post, self.detection_post)


class RecordingWindows:
    """Merged camera-specific intervals with logarithmic overlap lookup."""

    def __init__(self, intervals: Iterable[tuple[str, float, float | None]]):
        grouped: dict[str, list[tuple[float, float]]] = defaultdict(list)
        for camera, start, end in intervals:
            finish = inf if end is None else end
            if (
                not isfinite(start)
                or (end is not None and not isfinite(end))
                or finish < start
            ):
                raise ValueError("Invalid recording capture window")
            grouped[camera].append((start, finish))
        self._windows: dict[str, tuple[list[float], list[float]]] = {}
        for camera, windows in grouped.items():
            merged: list[tuple[float, float]] = []
            for start, end in sorted(windows):
                if merged and start <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], end))
                else:
                    merged.append((start, end))
            self._windows[camera] = (
                [start for start, _ in merged],
                [end for _, end in merged],
            )

    def overlaps(self, camera: str, start: float, end: float) -> bool:
        """Include segments touching either boundary of a capture window."""
        if not isfinite(start) or not isfinite(end) or end < start:
            raise ValueError("Invalid recording segment interval")
        windows = self._windows.get(camera)
        if windows is None:
            return False
        starts, ends = windows
        position = bisect_right(starts, end) - 1
        return position >= 0 and ends[position] >= start


def recording_event_windows(
    padding: Mapping[str, CapturePadding], default: CapturePadding
) -> RecordingWindows:
    """Classify both legacy events and review captures without loading thumbnails."""
    intervals: list[tuple[str, float, float | None]] = []
    for event in Event.select(
        Event.camera, Event.start_time, Event.end_time
    ).iterator():
        capture = padding.get(event.camera, default)
        intervals.append(
            (
                event.camera,
                event.start_time - capture.pre,
                None if event.end_time is None else event.end_time + capture.post,
            )
        )
    for review in ReviewSegment.select(
        ReviewSegment.camera,
        ReviewSegment.start_time,
        ReviewSegment.end_time,
        ReviewSegment.severity,
    ).iterator():
        capture = padding.get(review.camera, default)
        pre, post = (
            (capture.alert_pre, capture.alert_post)
            if review.severity == "alert"
            else (capture.detection_pre, capture.detection_post)
        )
        intervals.append(
            (
                review.camera,
                review.start_time - pre,
                None if review.end_time is None else review.end_time + post,
            )
        )
    return RecordingWindows(intervals)


def recording_is_protected(
    camera: str,
    start: float,
    end: float,
    padding: CapturePadding = CapturePadding(),
) -> bool:
    """Saved evidence remains protected even when its legacy clip flag was cleared."""
    return (
        Event.select()
        .where(
            Event.camera == camera,
            Event.retain_indefinitely == True,
            Event.start_time <= end + padding.pre,
            Event.end_time.is_null() | (Event.end_time >= start - padding.post),
        )
        .exists()
    )


def retain_event(event_id: str, padding: CapturePadding = CapturePadding()) -> None:
    """Serialize protection changes with transfer publication and deletion claims."""
    with archive.storage_lock():
        event = Event.get_by_id(event_id)
        pending = Recordings.select().where(
            Recordings.camera == event.camera,
            Recordings.delete_requested == True,
            Recordings.end_time >= event.start_time - padding.pre,
        )
        if event.end_time is not None:
            pending = pending.where(
                Recordings.start_time <= event.end_time + padding.post
            )
        if pending.exists():
            raise RecordingDeletionInProgress(
                "Overlapping recording deletion is in progress"
            )
        Event.update(retain_indefinitely=True).where(Event.id == event_id).execute()


def mark_recording_unavailable(
    recording: Recordings,
    padding: CapturePadding = CapturePadding(),
    include_protected: bool = False,
) -> None:
    """Clear only overlapping clip flags after deletion, while holding the path lock."""
    events = Event.update(has_clip=False).where(
        Event.camera == recording.camera,
        Event.start_time <= recording.end_time + padding.pre,
        Event.end_time.is_null()
        | (Event.end_time >= recording.start_time - padding.post),
    )
    if not include_protected:
        events = events.where(Event.retain_indefinitely == False)
    events.execute()


def delete_automatic_recording(
    recording_id: str,
    reason: AutomaticDeletionReason,
    padding: CapturePadding = CapturePadding(),
    clear: bool = False,
    preserve_unverified: bool = False,
) -> float | None:
    """Delete eligible local footage with a fresh protection check under the path lock.

    Returns removed file-content MiB, zero for already missing content, or None
    when protected, moved, or blocked. A persisted intent prevents a concurrent
    retain request from claiming success after file deletion has begun.
    """
    recording = Recordings.get_or_none(Recordings.id == recording_id)
    if recording is None:
        return None
    if recording.archive_bytes is not None or (
        reason == "retention" and preserve_unverified
    ):
        return None
    path = Path(recording.path)
    claimed = False
    reclaimed = 0.0

    def claim() -> bool:
        nonlocal claimed, reclaimed
        current = Recordings.get_or_none(Recordings.id == recording_id)
        if (
            current is None
            or current.path != str(path)
            or current.storage_tier != "local"
            or (current.delete_requested and current.delete_reason == "user")
            or recording_is_protected(
                current.camera, current.start_time, current.end_time, padding
            )
        ):
            return False
        Recordings.update(delete_requested=True, delete_reason=reason).where(
            Recordings.id == recording_id
        ).execute()
        claimed = True
        reclaimed = path.stat().st_size / (2**20)
        return True

    try:
        deleted = archive.delete_local_recording(
            path, missing_ok=False, clear=clear, can_delete=claim
        )
    except FileNotFoundError:
        if not claimed:
            logger.exception(
                "Unable to prepare deletion protection for recording %s", recording_id
            )
            return None
        logger.warning(
            "Recording %s is missing; no disk space was reclaimed", recording_id
        )
        deleted = claimed
        reclaimed = 0.0
    except OSError:
        logger.exception("Unable to delete local recording %s", recording_id)
        return None
    if not deleted:
        return None

    with archive.storage_lock():
        mark_recording_unavailable(recording, padding)
        Recordings.delete().where(
            Recordings.id == recording_id,
            Recordings.storage_tier == "local",
            Recordings.delete_requested == True,
            Recordings.delete_reason == reason,
        ).execute()
    return reclaimed

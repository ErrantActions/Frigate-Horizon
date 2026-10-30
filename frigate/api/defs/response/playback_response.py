"""Stable compatibility playback status and measured timeline."""

from typing import Literal

from pydantic import BaseModel


class PlaybackTimelineSpan(BaseModel):
    """Logical coverage bounds and a measured generated-playlist duration.

    lead_in_ms locates the logical start on the normalized media clock.
    It excludes CFR tail padding; duration alone cannot identify that offset.
    """

    start_time: float
    end_time: float
    duration: float
    measured: Literal[True] = True
    lead_in_ms: float | None = None


class PlaybackStatus(BaseModel):
    """Only measured spans are exposed; pending durations are not predictions."""

    id: str
    camera: str
    client_id: str
    generation: int
    state: Literal["preparing", "ready", "complete", "failed"]
    manifest_path: str
    timeline: list[PlaybackTimelineSpan]
    encoder: str = "libx264 (software)"
    error: str | None = None
    after: float
    before: float
    start_timestamp: float
    ready_through: float | None = None

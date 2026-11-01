"""Public HTTPS recording contracts, without source paths or bearer URLs."""

from typing import Literal

from pydantic import BaseModel


class RecordedHTTPSpan(BaseModel):
    source_start: float
    source_end: float
    media_start: float
    media_end: float


class RecordedHTTPDiscovery(BaseModel):
    enabled: bool
    available: bool
    version: Literal[1] = 1
    transport: Literal["https_mp4"] = "https_mp4"
    seek: Literal["http_byte_range"] = "http_byte_range"
    max_window_seconds: Literal[30] = 30
    session_url: str | None = None
    resolve_url: str | None = None
    qualities: list[str] = []
    reason: str | None = None
    original_recordings_unchanged: Literal[True] = True
    hls_fallback: Literal[False] = False


class RecordedHTTPStatus(BaseModel):
    version: Literal[1] = 1
    session_id: str
    camera: str
    state: Literal["preparing", "ready", "empty", "failed", "cleanup_pending"]
    quality: str
    audio: bool
    status_url: str
    close_url: str
    media_url: str
    hard_expires_at: float
    timeline: list[RecordedHTTPSpan]
    error: str | None
    width: int | None
    height: int | None
    after: float
    before: float
    start_timestamp: float
    effective_start_timestamp: float | None
    continuation_timestamp: float
    next_timestamp: float


class RecordedHTTPTarget(BaseModel):
    """A source-frame-backed target within an explicit caller-owned search range."""

    version: Literal[1] = 1
    after: float
    before: float
    start_timestamp: float
    effective_start_timestamp: float | None
    exhausted: bool
    searched_before: float
    reason: Literal["frame_found", "no_frame_in_range", "search_limit"]

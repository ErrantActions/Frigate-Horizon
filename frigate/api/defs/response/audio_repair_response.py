"""Safe public audio-repair maintenance DTOs."""

from typing import Literal

from pydantic import BaseModel

AudioRepairStatus = Literal[
    "scanning",
    "ready",
    "running",
    "pause_requested",
    "paused",
    "stop_requested",
    "stopped",
    "completed",
    "interrupted",
    "failed",
]
AudioRepairItemState = Literal[
    "candidate",
    "skipped",
    "selected",
    "repairing",
    "repaired",
    "failed",
    "stopped",
]


class AudioRepairItem(BaseModel):
    """An indexed recording's safe maintenance state, never its local path."""

    id: str
    camera: str
    start_time: float
    end_time: float
    video_codec: str | None = None
    audio_codec: str | None = None
    audio_rate: int | None = None
    has_audio: bool | None = None
    state: AudioRepairItemState
    reason: str | None = None


class AudioRepairJob(BaseModel):
    """Persistent progress separated into scan and repair phases."""

    id: str
    status: AudioRepairStatus
    phase: Literal["scan", "repair"]
    created_at: float
    updated_at: float
    total: int
    scanned: int
    eligible: int
    selected: int
    processed: int
    repaired: int
    skipped: int
    failed: int
    current_item_id: str | None = None
    current_item: AudioRepairItem | None = None
    error: str | None = None


class AudioRepairItemsPage(BaseModel):
    """A bounded page of scan/repair items."""

    items: list[AudioRepairItem]
    total: int
    limit: int
    offset: int


class AudioRepairJobsPage(BaseModel):
    """A bounded history including the latest maintenance job."""

    jobs: list[AudioRepairJob]
    total: int
    limit: int
    offset: int

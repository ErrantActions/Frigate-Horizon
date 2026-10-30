"""Safe compression job DTOs, excluding paths, credentials and tool stderr."""

from typing import Literal

from pydantic import BaseModel

CompressionStatus = Literal[
    "running",
    "pause_requested",
    "paused",
    "stop_requested",
    "stopped",
    "completed",
    "interrupted",
    "failed",
]


class CompressionItem(BaseModel):
    """A recording's durable processing outcome."""

    id: str
    camera: str
    start_time: float
    end_time: float
    state: str
    reason: str | None = None
    saved_bytes: int = 0


class CompressionJob(BaseModel):
    """Batch progress survives navigation and process restarts."""

    id: str
    status: CompressionStatus
    created_at: float
    updated_at: float
    scheduled: bool
    processed: int
    compressed: int
    skipped: int
    failed: int
    saved_bytes: int
    max_files: int
    target_mbps: float
    min_age_days: float
    encoder: str
    video_codec: str
    cpu_threads: int
    svt_preset: int
    frame_rate: int | None
    current_item_id: str | None
    reason: str | None


class CompressionJobs(BaseModel):
    """Recent jobs and the fail-closed activation/recovery gates."""

    jobs: list[CompressionJob]
    enabled: bool
    readers_leased: bool
    recovery_required: bool
    archive_supported: bool = True
    reservations_ready: bool


class CompressionItems(BaseModel):
    """A bounded page of audit outcomes."""

    items: list[CompressionItem]
    total: int

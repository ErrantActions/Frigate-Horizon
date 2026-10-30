"""Bounded read-only stationary tracker diagnostics."""

from typing import Literal

from pydantic import BaseModel, Field


class StationaryObjectResponse(BaseModel):
    id: str
    label: str
    box: tuple[int, int, int, int]
    score: float = Field(ge=0, le=1, allow_inf_nan=False)
    stationary_frames: int = Field(ge=0)
    position_changes: int = Field(ge=0)
    tracked_seconds: float = Field(ge=0, allow_inf_nan=False)
    last_tracking_update: float = Field(allow_inf_nan=False)
    updated_in_current_frame: bool


class StationaryObjectsResponse(BaseModel):
    camera: str
    status: Literal["ready", "stale", "warming_up", "disabled", "detection_disabled"]
    sampled_at: float
    frame_time: float | None = None
    frame_age_seconds: float | None = None
    frame_width: int
    frame_height: int
    stationary_threshold: int
    total: int = Field(ge=0)
    limit: int = 100
    objects: list[StationaryObjectResponse] = Field(
        default_factory=list, max_length=100
    )

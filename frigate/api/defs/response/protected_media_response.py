"""Protected evidence library metadata, without filesystem probes."""

from typing import Literal

from pydantic import BaseModel


class ProtectedStorageUsage(BaseModel):
    recordings: int = 0
    known_bytes: int = 0
    unknown_size_recordings: int = 0
    total_bytes: int | None = None


class ProtectedMediaItem(BaseModel):
    id: str
    camera: str
    start_time: float
    end_time: float | None
    label: str
    reason: Literal["saved_event"] = "saved_event"
    protection: Literal["permanent"] = "permanent"
    thumbnail_url: str
    snapshot_url: str | None
    original_url: str | None
    availability: Literal[
        "indexed_local", "archive_online", "archive_unavailable", "missing", "deleting"
    ]
    tiers: list[str]
    recordings: int
    has_snapshot: bool


class ProtectedMediaResponse(BaseModel):
    items: list[ProtectedMediaItem]
    total: int
    limit: int
    offset: int
    cameras: list[str]
    storage: dict[str, ProtectedStorageUsage]
    archive_state: str
    archive_updated_at: float | None

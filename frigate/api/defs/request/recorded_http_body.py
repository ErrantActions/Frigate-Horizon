"""Bounded, viewer-owned HTTPS recording windows."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RecordedHTTPCreate(BaseModel):
    """Request media by catalog time, never by a filesystem path."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    version: Literal[1] = 1
    after: float = Field(ge=0)
    before: float = Field(ge=0)
    start_timestamp: float = Field(ge=0)
    quality: Literal["auto", "original", "360", "720", "1080", "2160"] = "auto"
    start_policy: Literal["strict", "next"] = "strict"
    delivery: Literal["prepared", "auto", "direct"] = "prepared"
    video_codecs: list[Literal["h264", "hevc"]] = Field(
        default_factory=lambda: ["h264"], min_length=1, max_length=2
    )
    audio_codecs: list[Literal["aac", "mp3", "opus"]] = Field(
        default_factory=lambda: ["aac", "mp3"], max_length=3
    )

    @model_validator(mode="after")
    def bounded_window(self):
        """Keep immutable MP4 creation bounded independently of the Review range."""
        if not 0.1 <= self.before - self.after <= 30:
            raise ValueError("MP4 windows must be between 0.1 and 30 seconds")
        if not self.after <= self.start_timestamp < self.before:
            raise ValueError("Playback start must be inside the requested window")
        return self


class RecordedHTTPResolve(BaseModel):
    """Explicit, bounded catalog search, independent of the finite MP4 window."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    version: Literal[1] = 1
    after: float = Field(ge=0)
    before: float = Field(ge=0)
    start_timestamp: float = Field(ge=0)

    @model_validator(mode="after")
    def bounded_search(self):
        """Never search beyond the caller's requested range or more than an hour."""
        if not 0 < self.before - self.after <= 3600:
            raise ValueError(
                "Target searches must be greater than zero and at most 3600 seconds"
            )
        if not self.after <= self.start_timestamp < self.before:
            raise ValueError("Target must be inside the search range")
        return self

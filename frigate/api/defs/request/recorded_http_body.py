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

    @model_validator(mode="after")
    def bounded_window(self):
        """Keep immutable MP4 creation bounded independently of the Review range."""
        if not 0.1 <= self.before - self.after <= 30:
            raise ValueError("MP4 windows must be between 0.1 and 30 seconds")
        if not self.after <= self.start_timestamp < self.before:
            raise ValueError("Playback start must be inside the requested window")
        return self

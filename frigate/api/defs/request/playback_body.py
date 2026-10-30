"""Bounded stored-recording compatibility playback requests."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PlaybackCreateBody(BaseModel):
    """Create one bounded compatibility window, never a caller-supplied source."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    after: float = Field(ge=0)
    before: float = Field(ge=0)
    stream: Literal["auto", "main", "sub"] = "auto"
    client_id: str = Field(min_length=1, max_length=64, pattern=r"^[\w-]+$")
    generation: int = Field(default=0, ge=0, le=9007199254740991, strict=True)
    start_timestamp: float = Field(ge=0)

    @model_validator(mode="after")
    def bounded_window(self):
        """Require a finite window and a start inside that window."""
        if not 0.1 <= self.before - self.after <= 30:
            raise ValueError("Playback windows must be between 0.1 and 30 seconds")
        if not self.after <= self.start_timestamp < self.before:
            raise ValueError("Playback start must be inside the requested window")
        return self

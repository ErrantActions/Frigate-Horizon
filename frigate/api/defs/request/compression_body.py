"""Explicit administrator authorization for a bounded compression batch."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator


class CompressionStartBody(BaseModel):
    """No media paths or encoder commands are accepted from clients."""

    model_config = ConfigDict(extra="forbid")
    confirm: Literal[True]

    @field_validator("confirm", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        """Require actual boolean consent rather than coerced truthy input."""
        if value is not True:
            raise ValueError("Explicit compression confirmation is required")
        return value


class CompressionControlBody(BaseModel):
    """Only safe boundary controls are accepted."""

    model_config = ConfigDict(extra="forbid")
    action: Literal["pause", "resume", "stop"]

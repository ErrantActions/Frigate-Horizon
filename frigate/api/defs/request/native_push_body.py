"""Strict native push registration and installation-owned test requests."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from frigate.comms.native_preferences import NativeDeviceFeature


class NativePushRegistrationBody(BaseModel):
    """The authenticated account and provider topic are never caller-selected."""

    model_config = ConfigDict(extra="forbid")
    installation_id: UUID
    token: str = Field(min_length=2, max_length=512, pattern=r"^[0-9a-fA-F]+$")
    environment: Literal["production", "sandbox"]
    camera_ids: list[Annotated[str, Field(min_length=1, max_length=256)]] | None = None
    features: list[NativeDeviceFeature] = Field(default_factory=list, max_length=2)

    @field_validator("token")
    @classmethod
    def normalize_token(cls, value: str) -> str:
        """Normalize hex without assuming Apple's tokens have a fixed byte length."""
        if len(value) % 2:
            raise ValueError("Device token must contain complete hexadecimal bytes")
        return value.lower()

    @model_validator(mode="after")
    def unique_camera_ids(self) -> Self:
        """Keep None as all allowed cameras and an empty list as an explicit opt-out."""
        if self.camera_ids is not None:
            self.camera_ids = sorted(set(self.camera_ids))
        if len(set(self.features)) != len(self.features):
            raise ValueError("Device features must be unique")
        return self


class NativePushTestBody(BaseModel):
    """A test reaches only the requesting account's selected installation."""

    model_config = ConfigDict(extra="forbid")
    installation_id: UUID

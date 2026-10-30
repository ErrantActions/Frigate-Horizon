"""Requests for explicit ONVIF camera-device configuration."""

import ipaddress
import re
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictStr,
    field_validator,
    model_validator,
)

FiniteNumber = Annotated[float, Field(strict=True, allow_inf_nan=False)]
DeviceValue = FiniteNumber | StrictStr


class OnvifConnectionBody(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=80, ge=1, le=65535)
    username: str = Field(default="", max_length=256)
    password: SecretStr = Field(default_factory=lambda: SecretStr(""), repr=False)
    auth_type: Literal["basic", "digest"] = "basic"
    tls_insecure: bool = False
    ignore_time_mismatch: bool = False
    profile_token: str | None = Field(default=None, max_length=256)

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        """Accept camera addresses, not credentials, paths, or command fragments."""
        value = value.strip()
        address = value
        if "://" in value:
            parsed = urlsplit(value)
            if (
                parsed.scheme not in ("http", "https")
                or parsed.username is not None
                or parsed.password is not None
                or parsed.port is not None
                or parsed.path not in ("", "/")
                or parsed.query
                or parsed.fragment
                or not parsed.hostname
            ):
                raise ValueError("Use a camera host address and a separate port")
            address = parsed.hostname
        try:
            ipaddress.ip_address(address)
        except ValueError:
            if not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?", address):
                raise ValueError("Use a valid camera IP address or hostname") from None
        return value.rstrip("/")


class VideoResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width: int = Field(strict=True, gt=0, le=32768)
    height: int = Field(strict=True, gt=0, le=32768)


class OnvifEncoderChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolution: VideoResolution | None = None
    frame_rate: Annotated[FiniteNumber, Field(gt=0)] | None = None
    bitrate_kbps: int | None = Field(default=None, strict=True, gt=0)
    quality: FiniteNumber | None = None
    gop_length: int | None = Field(default=None, strict=True, gt=0)
    encoding_interval: int | None = Field(default=None, strict=True, gt=0)
    encoding: str | None = Field(default=None, strict=True, min_length=1, max_length=64)
    codec_profile: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def require_changes(self) -> Self:
        """Reject empty updates rather than reporting an unchanged camera as saved."""
        if not self.model_dump(exclude_none=True):
            raise ValueError("Choose at least one encoder setting to change")
        return self


class OnvifAudioChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")

    encoding: str | None = Field(default=None, min_length=1, max_length=64)
    bitrate_kbps: int | None = Field(default=None, strict=True, gt=0)
    sample_rate_khz: int | None = Field(default=None, strict=True, gt=0)

    @model_validator(mode="after")
    def require_changes(self) -> Self:
        """Reject an empty physical audio encoder update."""
        if not self.model_dump(exclude_none=True):
            raise ValueError("Choose at least one audio encoder setting")
        return self


class OnvifDeviceUpdateBody(OnvifConnectionBody):
    confirm_device_change: Literal[True]

    @field_validator("confirm_device_change", mode="before")
    @classmethod
    def require_confirmation(cls, value: object) -> bool:
        """Require a real affirmative boolean, not a coerced integer or string."""
        if value is not True:
            raise ValueError(
                "Explicit confirmation is required for camera-device changes"
            )
        return True


class OnvifEncoderUpdateBody(OnvifDeviceUpdateBody):
    profile_token: str = Field(min_length=1, max_length=256)
    expected_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    changes: OnvifEncoderChanges


class OnvifImagingUpdateBody(OnvifDeviceUpdateBody):
    profile_token: str = Field(min_length=1, max_length=256)
    changes: dict[str, DeviceValue] = Field(min_length=1, max_length=40)
    expected_values: dict[str, DeviceValue | None] = Field(max_length=40)


class OnvifProbeBody(OnvifConnectionBody):
    """Credential-body onboarding probe, without changing its response contract."""

    test: bool = Field(default=False, strict=True)

"""Reported ONVIF device controls and their camera-provided limits."""

from typing import Literal

from pydantic import BaseModel, Field

from frigate.api.defs.request.onvif_body import DeviceValue, VideoResolution


class OnvifNumberRange(BaseModel):
    minimum: float
    maximum: float


class OnvifEncoderOptions(BaseModel):
    resolutions: list[VideoResolution] = Field(default_factory=list)
    frame_rate: OnvifNumberRange | None = None
    bitrate_kbps: OnvifNumberRange | None = None
    quality: OnvifNumberRange | None = None
    gop_length: OnvifNumberRange | None = None
    encoding_interval: OnvifNumberRange | None = None


class OnvifEncoderState(BaseModel):
    token: str
    encoding: str
    resolution: VideoResolution | None = None
    frame_rate: float | None = None
    bitrate_kbps: int | None = None
    quality: float | None = None
    gop_length: int | None = None
    encoding_interval: int | None = None
    shared_profiles: list[str] = Field(default_factory=list)
    options: OnvifEncoderOptions
    revision: str
    editable: bool
    service: Literal["media", "media2"] = "media"
    frame_rates: list[float] = Field(default_factory=list)
    persistence_required: bool = False
    codec_profile: str | None = None
    codec_options: dict[str, OnvifEncoderOptions] = Field(default_factory=dict)
    codec_frame_rates: dict[str, list[float]] = Field(default_factory=dict)
    codec_profiles: dict[str, list[str]] = Field(default_factory=dict)
    reason: Literal["options_not_reported", "unsupported_encoding"] | None = None


class OnvifImagingControl(BaseModel):
    key: str
    value: DeviceValue | None = None
    limits: OnvifNumberRange | None = None
    choices: list[str] = Field(default_factory=list)
    available: bool
    limits_source: Literal["camera", "onvif_standard"] = "camera"


class OnvifImagingState(BaseModel):
    video_source_token: str
    controls: list[OnvifImagingControl]


class OnvifMediaProfile(BaseModel):
    token: str
    name: str
    encoder_token: str | None = None
    video_source_token: str | None = None


class OnvifImagingResponse(BaseModel):
    profiles: list[OnvifMediaProfile]
    profile_token: str | None = None
    state: Literal["ready", "choose_profile", "read_only"]
    imaging: OnvifImagingState | None = None


class OnvifSettingsResponse(BaseModel):
    profiles: list[OnvifMediaProfile]
    profile_token: str | None = None
    encoder: OnvifEncoderState | None = None
    imaging: OnvifImagingState | None = None
    unavailable: list[
        Literal["no_media_profiles", "encoder_options", "imaging_service"]
    ] = Field(default_factory=list)

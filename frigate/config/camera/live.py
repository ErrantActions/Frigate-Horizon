import logging
from typing import Any

from pydantic import Field, field_validator, model_validator

from frigate.util.live_streams import DEFAULT_TRANSCODE_QUALITIES

from ..base import FrigateBaseModel

__all__ = ["CameraLiveConfig", "LiveTranscodeConfig", "LiveTranscodeQualityConfig"]
logger = logging.getLogger(__name__)


class LiveTranscodeQualityConfig(FrigateBaseModel):
    height: int = Field(
        ge=144,
        le=2160,
        title="Height",
        description="Output height in pixels; width follows the source aspect ratio.",
    )
    bitrate: int = Field(
        ge=64,
        title="Bitrate",
        description="Target and maximum video bitrate in kbps.",
    )


class LiveTranscodeConfig(FrigateBaseModel):
    enabled: bool = Field(
        default=False,
        title="Enable transcoded streams",
        description="Add lower-quality live streams that go2rtc transcodes in real time while someone is watching.",
    )
    source: str | None = Field(
        default=None,
        title="Source stream",
        description="go2rtc stream to transcode. Defaults to the first live stream.",
    )
    qualities: list[LiveTranscodeQualityConfig] = Field(
        default_factory=lambda: [
            LiveTranscodeQualityConfig(**quality)
            for quality in DEFAULT_TRANSCODE_QUALITIES
        ],
        title="Qualities",
        description="One transcoded stream is added per quality.",
    )

    @field_validator("qualities")
    @classmethod
    def validate_unique_heights(
        cls, qualities: list[LiveTranscodeQualityConfig]
    ) -> list[LiveTranscodeQualityConfig]:
        heights = [quality.height for quality in qualities]

        if len(heights) != len(set(heights)):
            raise ValueError("Transcoded stream heights must be unique.")

        return qualities


class CameraLiveConfig(FrigateBaseModel):
    @model_validator(mode="before")
    @classmethod
    def remove_retired_encoder_settings(cls, value: Any) -> Any:
        """Migrate obsolete MPEG encoder options without selecting another transport."""
        if isinstance(value, dict) and {"height", "quality"}.intersection(value):
            logger.warning(
                "Ignoring retired live height/quality settings; live viewing uses HLS renditions"
            )
            return {
                key: item
                for key, item in value.items()
                if key not in {"height", "quality"}
            }
        return value

    streams: dict[str, str] = Field(
        default_factory=list,
        title="Live stream names",
        description="Mapping of configured stream names to restream/go2rtc names used for live playback.",
    )
    transcode: LiveTranscodeConfig = Field(
        default_factory=LiveTranscodeConfig,
        title="Transcoded streams",
        description="Lower-quality live streams transcoded on demand by go2rtc.",
    )

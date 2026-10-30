from enum import Enum

from pydantic import Field

from .base import FrigateBaseModel

__all__ = ["TimeFormatEnum", "UnitSystemEnum", "UIConfig"]


class TimeFormatEnum(str, Enum):
    browser = "browser"
    hours12 = "12hour"
    hours24 = "24hour"


class UnitSystemEnum(str, Enum):
    imperial = "imperial"
    metric = "metric"


class UIConfig(FrigateBaseModel):
    live_hls: bool = Field(
        default=False,
        strict=True,
        title="Bandwidth-adaptive live HLS",
        description="Use a private on-demand HLS packager for bandwidth-adaptive live viewing over authenticated HTTPS. Requires the live-hls companion service. Recordings, detection, and camera source settings are unchanged.",
    )
    live_compatibility: bool = Field(
        default=True,
        strict=True,
        title="Allow Live compatibility streams",
        description="Global administrator control for Frigate-generated H264 Live streams. Disable to use original configured streams only. Camera codecs and recordings are unchanged.",
    )
    recording_compatibility: bool = Field(
        default=True,
        strict=True,
        title="Allow Review compatibility playback",
        description="Global administrator control for temporary H264 recording playback copies. Disable to use original recordings only. Existing recording files and recording-time audio settings are unchanged.",
    )
    timezone: str | None = Field(
        default=None,
        title="Timezone",
        description="Optional timezone to display across the UI (defaults to browser local time if unset).",
    )
    time_format: TimeFormatEnum = Field(
        default=TimeFormatEnum.browser,
        title="Time format",
        description="Time format to use in the UI (browser, 12hour, or 24hour).",
    )
    unit_system: UnitSystemEnum = Field(
        default=UnitSystemEnum.metric,
        title="Unit system",
        description="Unit system for display (metric or imperial) used in the UI and MQTT.",
    )

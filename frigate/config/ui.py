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
        default=True,
        strict=True,
        title="Bandwidth-adaptive live HLS",
        description="Enable live viewing through the private on-demand HLS packager over authenticated HTTPS. Requires the live-hls companion service and configured HLS renditions. Disabling this stops live video; it does not select another transport. Stored recordings and detection are unchanged.",
    )
    live_compatibility: bool = Field(
        default=True,
        strict=True,
        title="Allow Live compatibility streams",
        description="Permit H264 live renditions. This legacy administrator gate is preserved: disabling it stops camera live HLS, not selects another viewing transport. Camera codecs and recordings are unchanged.",
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

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
    recorded_http: bool = Field(
        default=False,
        strict=True,
        title="HTTPS MP4 recorded review",
        description="Use private, viewer-owned MP4 files with HTTP byte-range seeking for recorded review instead of recorded WebRTC. Scaled H264/AAC copies are generated only on demand, with shared live media admission limits. Requires the live WebRTC admission runtime. Original files, retention, mirroring and live viewing are unchanged. No HLS fallback is selected.",
    )
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
        description="Administrator control for legacy compatibility playback copies. This does not gate private HTTPS MP4 review selected by recorded_http. Existing recording files and recording-time audio settings are unchanged.",
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

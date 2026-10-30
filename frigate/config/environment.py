"""Opt-in modeled weather and per-camera environmental profile configuration."""

from enum import Enum
from typing import Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator

from frigate.api.defs.request.onvif_body import DeviceValue

from .base import FrigateBaseModel


class WeatherConfig(FrigateBaseModel):
    enabled: bool = Field(
        default=False,
        title="Enable modeled weather",
        description="Fetch Open-Meteo modeled weather for environmental camera profiles. This is not a local weather-station observation.",
    )
    zip_code: str | None = Field(
        default=None,
        pattern=r"^[0-9]{5}(?:-[0-9]{4})?$",
        title="ZIP code",
        description="US ZIP code (or ZIP+4) for modeled weather. Open-Meteo resolves the first five digits to an approximate locality. Takes precedence over advanced coordinates. No IP geolocation is performed.",
    )
    latitude: float | None = Field(
        default=None,
        ge=-90,
        le=90,
        strict=True,
        allow_inf_nan=False,
        title="Latitude",
        description="Weather-model location latitude. Enter the location explicitly; no IP geolocation is performed.",
    )
    longitude: float | None = Field(
        default=None,
        ge=-180,
        le=180,
        strict=True,
        allow_inf_nan=False,
        title="Longitude",
        description="Weather-model location longitude. Coordinates are only returned to administrators.",
    )
    refresh_seconds: int = Field(
        default=600,
        ge=60,
        le=86400,
        title="Weather refresh interval",
        description="Seconds between modeled-weather requests. More frequent requests do not make the model a live observation.",
    )
    stale_after_seconds: int = Field(
        default=1800,
        ge=300,
        le=172800,
        title="Weather maximum age",
        description="Maximum model-data age in seconds before weather-triggered profile changes are suspended.",
    )

    @model_validator(mode="after")
    def validate_weather(self) -> Self:
        """Require a ZIP code or explicit coordinates and a usable freshness interval."""
        if (
            self.enabled
            and self.zip_code is None
            and (self.latitude is None or self.longitude is None)
        ):
            raise ValueError(
                "Enabled weather requires a US ZIP code or latitude and longitude"
            )
        if self.stale_after_seconds < self.refresh_seconds:
            raise ValueError(
                "Weather maximum age must not be shorter than its refresh interval"
            )
        return self


class WeatherCondition(str, Enum):
    rain = "rain"
    snow = "snow"
    fog = "fog"
    cloud = "cloud"
    wind = "wind"


class WeatherProfileRule(FrigateBaseModel):
    enabled: bool = Field(
        default=True,
        title="Enable weather rule",
        description="Apply this rule when environmental automation is enabled for the camera.",
    )
    id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-zA-Z0-9_-]+$",
        title="Rule identifier",
        description="Unique stable identifier for this camera's weather rule.",
    )
    profile: str = Field(
        min_length=1,
        title="Profile",
        description="Named profile defined for this camera.",
    )
    condition: WeatherCondition = Field(
        title="Weather condition",
        description="Modeled condition that activates this profile.",
    )
    threshold: float | None = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
        title="Condition threshold",
        description="Optional threshold: rain in mm per model sample, snow in cm per model sample, fog visibility in meters, cloud cover in percent, or gusts in m/s.",
    )
    priority: int = Field(
        default=100,
        ge=0,
        le=1000,
        title="Rule priority",
        description="Higher-priority matching weather rules win. Ties follow the listed rule order.",
    )

    @model_validator(mode="after")
    def validate_threshold(self) -> Self:
        """Cloud cover thresholds are percentages, not unbounded amounts."""
        if (
            self.condition == WeatherCondition.cloud
            and self.threshold is not None
            and self.threshold > 100
        ):
            raise ValueError("Cloud-cover threshold must be between 0 and 100")
        return self


class FixedProfileSchedule(FrigateBaseModel):
    enabled: bool = Field(
        default=True,
        title="Enable schedule",
        description="Apply this interval when environmental automation is enabled for the camera.",
    )
    id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-zA-Z0-9_-]+$",
        title="Schedule identifier",
        description="Unique stable identifier for this camera's schedule.",
    )
    profile: str = Field(
        min_length=1,
        title="Profile",
        description="Named camera profile to activate during this interval.",
    )
    start: str = Field(
        pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$",
        title="Start time",
        description="Local wall-clock start time, in HH:MM format.",
    )
    end: str = Field(
        pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$",
        title="End time",
        description="Local wall-clock end time. An earlier end time means the interval crosses midnight.",
    )
    weekdays: list[int] = Field(
        default_factory=lambda: list(range(7)),
        min_length=1,
        max_length=7,
        title="Weekdays",
        description="Start-day weekdays, Monday 0 through Sunday 6. Overnight intervals belong to the day they start.",
    )
    priority: int = Field(
        default=0,
        ge=0,
        le=1000,
        title="Schedule priority",
        description="Higher-priority fixed schedules win. Ties follow the listed schedule order.",
    )

    @field_validator("weekdays", mode="before")
    @classmethod
    def validate_weekdays(cls, value: object) -> object:
        """Reject duplicate weekdays and boolean/string coercion."""
        if (
            not isinstance(value, list)
            or any(type(day) is not int or not 0 <= day <= 6 for day in value)
            or len(set(value)) != len(value)
        ):
            raise ValueError("Weekdays must be unique integers from 0 through 6")
        return value

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        """Do not silently interpret equal times as either zero or 24 hours."""
        if self.start == self.end:
            raise ValueError(
                "Schedule start and end must differ; use a manual profile for an all-day override"
            )
        return self


class SolarProfileConfig(FrigateBaseModel):
    enabled: bool = Field(
        default=False,
        title="Enable day/night scheduling",
        description="Use modeled sunrise and sunset times to choose day and night profiles.",
    )
    day: str | None = Field(
        default=None,
        min_length=1,
        title="Day profile",
        description="Named camera profile active between adjusted sunrise and sunset.",
    )
    night: str | None = Field(
        default=None,
        min_length=1,
        title="Night profile",
        description="Named camera profile active outside the adjusted daytime interval.",
    )
    sunrise_profile: str | None = Field(
        default=None,
        min_length=1,
        title="Sunrise profile",
        description="Optional camera profile for low morning sun, useful for east-facing cameras. Overrides the solar day/night choice only within the sunrise window. Fixed schedules and manual overrides keep their existing precedence.",
    )
    sunrise_start_minutes: int = Field(
        default=-15,
        ge=-360,
        le=360,
        title="Sunrise window start",
        description="Minutes relative to actual modeled sunrise to start the sunrise profile. Negative values begin before sunrise. Independent of the day-profile sunrise offset.",
    )
    sunrise_end_minutes: int = Field(
        default=120,
        ge=-360,
        le=360,
        title="Sunrise window end",
        description="Minutes relative to actual modeled sunrise to stop the sunrise profile and recompute the current day/night choice. End must be later than start.",
    )
    sunrise_offset_minutes: int = Field(
        default=0,
        ge=-720,
        le=720,
        title="Sunrise offset",
        description="Minutes before or after modeled sunrise to start the day profile.",
    )
    sunset_offset_minutes: int = Field(
        default=0,
        ge=-720,
        le=720,
        title="Sunset offset",
        description="Minutes before or after modeled sunset to start the night profile.",
    )

    @model_validator(mode="after")
    def validate_profiles(self) -> Self:
        """Solar scheduling needs both explicit profile choices."""
        if self.sunrise_start_minutes >= self.sunrise_end_minutes:
            raise ValueError("Sunrise window end must be later than its start")
        if self.enabled and (self.day is None or self.night is None):
            raise ValueError(
                "Day/night scheduling requires both day and night profiles"
            )
        return self


class GlareProfileConfig(FrigateBaseModel):
    enabled: bool = Field(
        default=False,
        title="Enable transient glare response",
        description="Detect sudden localized brightness in selected zones, not ordinary steady nighttime illumination.",
    )
    profile: str | None = Field(
        default=None,
        min_length=1,
        title="Glare profile",
        description="Temporary anti-glare profile. It may override a manual profile until its lease expires.",
    )
    zones: list[str] = Field(
        default_factory=list,
        title="Glare zones",
        description="Camera zone names used to detect localized glare.",
    )
    brightness_threshold: int = Field(
        default=220,
        ge=1,
        le=255,
        title="Bright-pixel threshold",
        description="Luma level used to measure unusually bright regions.",
    )
    minimum_bright_fraction: float = Field(
        default=0.03,
        gt=0,
        le=1,
        allow_inf_nan=False,
        title="Minimum bright area",
        description="Minimum fraction of a glare zone that must be bright.",
    )
    minimum_fraction_increase: float = Field(
        default=0.02,
        gt=0,
        le=1,
        allow_inf_nan=False,
        title="Sudden bright-area increase",
        description="Minimum increase in bright-pixel fraction relative to the adaptive zone baseline.",
    )
    minimum_luminance_increase: int = Field(
        default=20,
        ge=1,
        le=255,
        title="Sudden pixel-luma increase",
        description="Minimum luma increase on newly bright pixels. Gradual threshold crossings do not count as transient glare.",
    )
    maximum_frame_gap_seconds: float = Field(
        default=5,
        gt=0,
        le=120,
        allow_inf_nan=False,
        title="Maximum analysis frame gap",
        description="Recalibrate rather than trigger glare after a longer gap between frames.",
    )
    baseline_seconds: float = Field(
        default=30,
        gt=0,
        le=3600,
        allow_inf_nan=False,
        title="Lighting baseline time",
        description="Time scale in seconds for adapting to ordinary lighting changes.",
    )
    restore_after_seconds: float = Field(
        default=30,
        gt=0,
        le=3600,
        allow_inf_nan=False,
        title="Glare override duration",
        description="Seconds before recomputing the latest manual/schedule/weather profile.",
    )
    cooldown_seconds: float = Field(
        default=30,
        ge=0,
        le=3600,
        allow_inf_nan=False,
        title="Glare cooldown",
        description="Minimum delay in seconds between new transient-glare activations.",
    )
    global_change_fraction: float = Field(
        default=0.5,
        gt=0,
        le=1,
        allow_inf_nan=False,
        title="Global illumination exclusion",
        description="Ignore changes affecting this fraction of the full frame, such as lightning or a global lighting transition.",
    )
    transition_exclusion_minutes: float = Field(
        default=10,
        ge=0,
        le=120,
        allow_inf_nan=False,
        title="Dawn/dusk exclusion",
        description="Ignore transient-glare activations this many minutes around known sunrise and sunset events.",
    )

    @model_validator(mode="after")
    def validate_glare(self) -> Self:
        """Require a specific profile and nonempty, nonduplicate zones."""
        if len(set(self.zones)) != len(self.zones):
            raise ValueError("Glare zones must not contain duplicates")
        if self.enabled and (self.profile is None or not self.zones):
            raise ValueError("Glare response requires a profile and at least one zone")
        return self


class PhysicalImageProfile(FrigateBaseModel):
    """Temporary device settings associated with one existing NVR profile name."""

    profile_token: str = Field(
        min_length=1,
        max_length=256,
        title="Image source media profile",
        description="Explicit Media1 profile token identifying the physical lens. Separate from the PTZ profile selector.",
    )
    settings: dict[str, DeviceValue] = Field(
        min_length=1,
        max_length=40,
        title="Physical image settings",
        description="Camera-advertised imaging values. Unsupported controls and out-of-range values are rejected at application time. Noise reduction is not exposed by the bundled ONVIF imaging schema.",
    )


class CameraEnvironmentConfig(FrigateBaseModel):
    enabled: bool = Field(
        default=False,
        title="Enable environmental profiles",
        description="Opt in this camera to scheduled, weather-aware, and transient-glare profile selection.",
    )
    timezone: str = Field(
        default="UTC",
        title="Schedule timezone",
        description="IANA timezone for fixed wall-clock schedules. Defaults to UTC and does not guess the browser's timezone.",
    )
    manual_profile: str | None = Field(
        default=None,
        min_length=1,
        title="Manual profile override",
        description="Persist a named per-camera override until automatic mode is selected again. A temporary glare lease may override it.",
    )
    solar: SolarProfileConfig = Field(
        default_factory=SolarProfileConfig,
        title="Day and night",
        description="Sunrise/sunset profile choices and offsets.",
    )
    schedules: list[FixedProfileSchedule] = Field(
        default_factory=list,
        title="Fixed schedules",
        description="Per-camera recurring wall-clock profile intervals.",
    )
    weather_rules: list[WeatherProfileRule] = Field(
        default_factory=list,
        title="Weather profiles",
        description="Prioritized per-camera modeled-weather profile rules.",
    )
    weather_dwell_seconds: float = Field(
        default=30,
        ge=0,
        le=3600,
        allow_inf_nan=False,
        title="Weather condition dwell",
        description="Seconds a new modeled-weather selection must remain stable before applying it.",
    )
    glare: GlareProfileConfig = Field(
        default_factory=GlareProfileConfig,
        title="Glare response",
        description="Zone-based transient lighting response and temporary override duration.",
    )
    allow_device_changes: bool = Field(
        default=False,
        title="Allow physical camera automation",
        description="Opt in to supported physical camera image-setting changes. These affect every camera client and require explicit setup confirmation.",
    )
    image_profiles: dict[str, PhysicalImageProfile] = Field(
        default_factory=dict,
        title="Physical image profiles",
        description="Opt-in temporary ONVIF imaging values keyed by existing environmental NVR profile names. These affect all camera clients. Disabling automation restores only values still owned by Horizon; external edits cause a conflict instead.",
    )

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        """Fixed schedules must have a real, unambiguous IANA timezone."""
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("Unknown schedule timezone") from error
        return value

    @model_validator(mode="after")
    def validate_rule_ids(self) -> Self:
        """Stable rule identifiers must not collide within their respective lists."""
        for rules in (self.schedules, self.weather_rules):
            identifiers = [rule.id for rule in rules]
            if len(set(identifiers)) != len(identifiers):
                raise ValueError("Environmental rule identifiers must be unique")
        return self

    def referenced_profiles(self) -> set[str]:
        """Return all configured profile references for root-config validation."""
        names = {rule.profile for rule in self.weather_rules}
        names.update(self.image_profiles)
        names.update(schedule.profile for schedule in self.schedules)
        names.update(
            name
            for name in (
                self.solar.day,
                self.solar.night,
                self.solar.sunrise_profile,
                self.glare.profile,
                self.manual_profile,
            )
            if name is not None
        )
        return names

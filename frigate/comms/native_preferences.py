"""Installation-local alert preferences, independent of server notification rules."""

import math
import time
from datetime import datetime
from typing import Annotated, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

NativeDeviceFeature = Literal["actions-v1", "health-v1"]
Minute = Annotated[int, Field(strict=True, ge=0, le=1439)]
Weekday = Annotated[int, Field(strict=True, ge=1, le=7)]


class QuietHours(BaseModel):
    """Cross-midnight intervals belong to the day on which they start."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    enabled: bool = Field(strict=True)
    start_minute: Minute
    end_minute: Minute
    timezone: str = Field(min_length=1, max_length=128)
    days: list[Weekday] = Field(min_length=1, max_length=7)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        """Accept only an installed IANA timezone, never a path or fixed offset."""
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("A valid IANA timezone is required") from None
        return value

    @model_validator(mode="after")
    def valid_interval(self) -> Self:
        """Reject ambiguous all-day intervals and duplicate weekdays."""
        if self.start_minute == self.end_minute:
            raise ValueError("Quiet hours start and end must differ")
        if len(set(self.days)) != len(self.days):
            raise ValueError("Quiet hours weekdays must be unique")
        return self

    def active(self, timestamp: float) -> bool:
        """Evaluate local wall time, including repeated or skipped DST hours."""
        if not math.isfinite(timestamp):
            raise ValueError("Invalid preference evaluation time")
        if not self.enabled:
            return False
        local = datetime.fromtimestamp(timestamp, ZoneInfo(self.timezone))
        minute = local.hour * 60 + local.minute
        day = local.isoweekday()
        if self.start_minute < self.end_minute:
            return day in self.days and self.start_minute <= minute < self.end_minute
        previous_day = 7 if day == 1 else day - 1
        return (day in self.days and minute >= self.start_minute) or (
            previous_day in self.days and minute < self.end_minute
        )


class DevicePreferences(BaseModel):
    """An account-owned installation's preference revision and explicit opt-ins."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    revision: int = Field(default=0, strict=True, ge=0)
    camera_snooze_until: dict[str, float] = Field(default_factory=dict)
    quiet_hours: QuietHours | None = None
    health_alerts: bool = Field(default=False, strict=True)

    @field_validator("camera_snooze_until", mode="before")
    @classmethod
    def finite_snoozes(cls, value: object) -> object:
        """Never convert invalid timestamps or boolean values into a valid snooze."""
        if not isinstance(value, dict) or len(value) > 1024:
            raise ValueError("Invalid camera snooze map")
        for camera, timestamp in value.items():
            if (
                not isinstance(camera, str)
                or not 1 <= len(camera) <= 256
                or not isinstance(timestamp, (int, float))
                or isinstance(timestamp, bool)
                or not math.isfinite(timestamp)
                or timestamp < 0
            ):
                raise ValueError("Invalid camera snooze timestamp")
        return value

    def allows(self, camera: str | None, now: float | None = None) -> bool:
        """Filter this installation only; this method never grants server eligibility."""
        current = time.time() if now is None else now
        if not math.isfinite(current):
            raise ValueError("Invalid notification evaluation time")
        return not (
            (self.quiet_hours is not None and self.quiet_hours.active(current))
            or (
                camera is not None and self.camera_snooze_until.get(camera, 0) > current
            )
        )


class DevicePreferenceConflict(ValueError):
    """A concurrent installation preference update must be reloaded, not overwritten."""


class DevicePreferenceUpdate(DevicePreferences):
    """A complete current-revision wrapper is required for compare-and-swap updates."""

    revision: int = Field(strict=True, ge=0)
    camera_snooze_until: dict[str, float] = Field()
    quiet_hours: QuietHours | None = Field()
    health_alerts: bool = Field(strict=True)


class NativeSnoozeBody(BaseModel):
    """The native action can pause only one currently allowed camera."""

    model_config = ConfigDict(extra="forbid")
    camera_id: str = Field(min_length=1, max_length=256)
    seconds: Literal[900, 3600, 0]

    @field_validator("seconds", mode="before")
    @classmethod
    def supported_seconds(cls, value: object) -> object:
        """A boolean is not a request to cancel a camera snooze."""
        if type(value) is not int or value not in (0, 900, 3600):
            raise ValueError("Snooze duration must be 0, 900 or 3600 seconds")
        return value

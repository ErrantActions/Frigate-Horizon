"""Deterministic per-camera profile selection without device or network side effects."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from frigate.environment.weather import WeatherSnapshot

if TYPE_CHECKING:
    from frigate.config.environment import CameraEnvironmentConfig

RAIN_CODES = frozenset({51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99})
SNOW_CODES = frozenset({71, 73, 75, 77, 85, 86})
FOG_CODES = frozenset({45, 48})


def weather_condition_matches(
    condition: str, threshold: float | None, weather: WeatherSnapshot
) -> bool | None:
    """Return unknown rather than clear weather when a required metric is missing."""
    if threshold is not None and (
        isinstance(threshold, bool) or not isfinite(threshold) or threshold < 0
    ):
        raise ValueError("Invalid weather condition threshold")
    if condition == "rain":
        amount = weather.rain_and_showers_mm
        if threshold is None and weather.weather_code in RAIN_CODES:
            return True
        if amount is None:
            return None
        return amount > 0 and (threshold is None or amount >= threshold)
    if condition == "snow":
        if threshold is None and weather.weather_code in SNOW_CODES:
            return True
        amount = weather.snowfall_cm
        if amount is None:
            return None
        return amount > 0 and (threshold is None or amount >= threshold)
    if condition == "fog":
        if threshold is None and weather.weather_code in FOG_CODES:
            return True
        return (
            None
            if weather.visibility_m is None
            else weather.visibility_m <= (1000 if threshold is None else threshold)
        )
    if condition == "cloud":
        if threshold is not None and threshold > 100:
            raise ValueError("Cloud cover is a percentage")
        return (
            None
            if weather.cloud_cover_percent is None
            else weather.cloud_cover_percent >= (80 if threshold is None else threshold)
        )
    if condition == "wind":
        return (
            None
            if weather.wind_gusts_mps is None
            else (
                weather.wind_gusts_mps > 0
                and weather.wind_gusts_mps >= (15 if threshold is None else threshold)
            )
        )
    raise ValueError("Unknown weather condition")


def schedule_is_active(
    start: str, end: str, weekdays: list[int], timezone_name: str, now: datetime
) -> bool:
    """Evaluate local wall-clock intervals, assigning overnight time to its start day.

    Both occurrences of a repeated fall-back wall time match. Nonexistent
    spring-forward wall times are not replayed after the gap.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Profile clock must be timezone-aware")
    local = now.astimezone(UTC if timezone_name == "UTC" else ZoneInfo(timezone_name))
    start_hour, start_minute = map(int, start.split(":"))
    end_hour, end_minute = map(int, end.split(":"))
    first = start_hour * 60 + start_minute
    last = end_hour * 60 + end_minute
    if (
        not 0 <= start_hour <= 23
        or not 0 <= end_hour <= 23
        or not 0 <= start_minute <= 59
        or not 0 <= end_minute <= 59
        or first == last
        or not weekdays
        or any(type(day) is not int or not 0 <= day <= 6 for day in weekdays)
        or len(set(weekdays)) != len(weekdays)
    ):
        raise ValueError("Invalid fixed profile schedule")
    minute = local.hour * 60 + local.minute
    day = local.weekday()
    if first < last:
        return day in weekdays and first <= minute < last
    return (day in weekdays and minute >= first) or (
        (day - 1) % 7 in weekdays and minute < last
    )


class WeatherDwell:
    """Debounce weather changes using monotonic time, independently of wall-clock jumps."""

    def __init__(self) -> None:
        self.current: str | None = None
        self.pending: str | None = None
        self.pending_since: float | None = None
        self.last_tick: float | None = None

    def update(self, candidate: str | None, now: float, seconds: float) -> str | None:
        """Apply a candidate only after it remains stable for the configured dwell."""
        if (
            not isfinite(now)
            or not isfinite(seconds)
            or seconds < 0
            or (self.last_tick is not None and now < self.last_tick)
        ):
            raise ValueError("Invalid monotonic profile clock or dwell interval")
        self.last_tick = now
        if candidate == self.current:
            self.pending = None
            self.pending_since = None
            return self.current
        if self.pending_since is None or candidate != self.pending:
            self.pending = candidate
            self.pending_since = now
        if now - self.pending_since >= seconds:
            self.current = candidate
            self.pending = None
            self.pending_since = None
        return self.current


@dataclass(frozen=True)
class ProfilePlan:
    """Desired layers and provenance, not a claim that camera writes have succeeded."""

    profiles: tuple[str, ...]
    reason: str
    base_reason: str = "base"
    schedule_id: str | None = None
    weather_rule_id: str | None = None
    pending_weather_profile: str | None = None
    weather_change_pending: bool = False
    warnings: tuple[str, ...] = ()


class ProfileSelector:
    """Compose automatic base/weather layers, explicit manual choices, and glare leases."""

    def __init__(self, config: "CameraEnvironmentConfig") -> None:
        self.config = config
        self.weather_dwell = WeatherDwell()
        self._active_weather_rule: str | None = None

    def decide(
        self,
        now: datetime,
        monotonic_now: float,
        weather: WeatherSnapshot | None,
        weather_max_age_seconds: float,
        manual_profile: str | None = None,
        glare_until: float | None = None,
    ) -> ProfilePlan:
        """Recompute from current inputs, including while a temporary glare lease is active."""
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Profile clock must be timezone-aware")
        if not isfinite(monotonic_now) or (
            glare_until is not None and not isfinite(glare_until)
        ):
            raise ValueError("Invalid glare lease clock")
        now = now.astimezone(UTC)
        if not self.config.enabled:
            self.weather_dwell = WeatherDwell()
            self._active_weather_rule = None
            return ProfilePlan((), "disabled")

        warnings: list[str] = []
        base, base_reason, schedule_id = self._base_profile(now, weather, warnings)
        candidate = None
        rule_id = None
        weather_unavailable = weather is None
        if weather is not None and weather.model_time > now + timedelta(minutes=5):
            weather_unavailable = True
            warnings.append("weather_clock_invalid")
        elif weather is not None and weather.is_stale(now, weather_max_age_seconds):
            weather_unavailable = True
            warnings.append("weather_stale")
        elif weather is None and self.config.weather_rules:
            warnings.append("weather_unavailable")
        if not weather_unavailable and weather is not None:
            for _, rule in sorted(
                enumerate(self.config.weather_rules),
                key=lambda item: (-item[1].priority, item[0]),
            ):
                if not rule.enabled:
                    continue
                matches = weather_condition_matches(
                    rule.condition, rule.threshold, weather
                )
                if matches is None:
                    warnings.append("weather_metric_unavailable")
                elif matches:
                    candidate, rule_id = rule.profile, rule.id
                    break
        selected = self.weather_dwell.update(
            candidate,
            monotonic_now,
            0 if weather_unavailable else self.config.weather_dwell_seconds,
        )
        if selected == candidate:
            self._active_weather_rule = rule_id

        layers = [name for name in (base, selected) if name is not None]
        reason = "weather_rule" if selected is not None else base_reason
        if manual_profile is not None:
            layers = [manual_profile]
            reason = "manual_override"
        if (
            self.config.glare.enabled
            and self.config.glare.profile is not None
            and glare_until is not None
            and monotonic_now < glare_until
        ):
            layers.append(self.config.glare.profile)
            reason = "temporary_glare"
        return ProfilePlan(
            profiles=tuple(dict.fromkeys(layers)),
            reason=reason,
            base_reason=base_reason,
            schedule_id=schedule_id,
            weather_rule_id=self._active_weather_rule,
            pending_weather_profile=candidate if candidate != selected else None,
            weather_change_pending=self.weather_dwell.pending_since is not None,
            warnings=tuple(dict.fromkeys(warnings)),
        )

    def _base_profile(
        self, now: datetime, weather: WeatherSnapshot | None, warnings: list[str]
    ) -> tuple[str | None, str, str | None]:
        for _, schedule in sorted(
            enumerate(self.config.schedules),
            key=lambda item: (-item[1].priority, item[0]),
        ):
            if schedule.enabled and schedule_is_active(
                schedule.start,
                schedule.end,
                schedule.weekdays,
                self.config.timezone,
                now,
            ):
                return schedule.profile, "fixed_schedule", schedule.id

        solar = self.config.solar
        if not solar.enabled:
            return None, "base", None
        if weather is None:
            warnings.append("solar_unavailable")
            return None, "base", None
        current = next(
            (day for day in weather.solar_days if day.day_start.date() == now.date()),
            None,
        )
        if current is None or current.sunrise is None or current.sunset is None:
            warnings.append("solar_unavailable")
            return None, "base", None
        sunrise = current.sunrise + timedelta(minutes=solar.sunrise_offset_minutes)
        sunset = current.sunset + timedelta(minutes=solar.sunset_offset_minutes)
        if sunrise >= sunset:
            warnings.append("solar_interval_invalid")
            return None, "base", None
        if solar.sunrise_profile is not None:
            for day in weather.solar_days:
                if day.sunrise is None or day.sunset is None:
                    continue
                first = day.sunrise + timedelta(minutes=solar.sunrise_start_minutes)
                last = min(
                    day.sunrise + timedelta(minutes=solar.sunrise_end_minutes),
                    day.sunset,
                )
                if first < last and first <= now < last:
                    return solar.sunrise_profile, "solar_sunrise", None
        for day in weather.solar_days:
            if day.sunrise is None or day.sunset is None:
                continue
            first = day.sunrise + timedelta(minutes=solar.sunrise_offset_minutes)
            last = day.sunset + timedelta(minutes=solar.sunset_offset_minutes)
            if first < last and first <= now < last:
                return solar.day, "solar_day", None
        return solar.night, "solar_night", None

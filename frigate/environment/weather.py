"""Validated Open-Meteo modeled weather with explicit missing and stale data."""

import json
import re
from bisect import bisect_right
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import Literal

import httpx

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
MAX_RESPONSE_BYTES = 1024 * 1024
CURRENT_VARIABLES = (
    "temperature_2m,rain,showers,snowfall,weather_code,cloud_cover,"
    "wind_speed_10m,wind_gusts_10m,wind_direction_10m,is_day"
)


class WeatherProviderError(RuntimeError):
    """Weather could not be fetched from the configured provider."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class WeatherDataError(WeatherProviderError):
    """The provider returned incompatible or invalid data."""


class WeatherLocationError(WeatherProviderError):
    """The configured ZIP code could not be resolved to an exact US postal match."""


@dataclass(frozen=True)
class SolarDay:
    """UTC solar times, with null values representing unavailable solar events."""

    day_start: datetime
    sunrise: datetime | None
    sunset: datetime | None


@dataclass(frozen=True)
class WeatherSnapshot:
    """Modeled weather values; missing measurements remain explicitly unknown."""

    model_time: datetime
    fetched_at: datetime
    interval_seconds: int
    temperature_c: float | None
    rain_mm: float | None
    showers_mm: float | None
    snowfall_cm: float | None
    weather_code: int | None
    cloud_cover_percent: float | None
    wind_speed_mps: float | None
    wind_gusts_mps: float | None
    is_day: bool | None
    visibility_m: float | None
    visibility_model_time: datetime | None
    solar_days: tuple[SolarDay, ...]
    wind_direction_degrees: float | None = None
    provider: Literal["open-meteo"] = "open-meteo"
    modeled: Literal[True] = True

    @property
    def rain_and_showers_mm(self) -> float | None:
        """Return combined modeled rain only when both components are known."""
        if self.rain_mm is None or self.showers_mm is None:
            return None
        return self.rain_mm + self.showers_mm

    def is_stale(self, now: datetime, max_age_seconds: float) -> bool:
        """Judge freshness by the model timestamp, not the last successful fetch."""
        if not isfinite(max_age_seconds) or max_age_seconds <= 0:
            raise ValueError("Weather maximum age must be finite and positive")
        return (_utc(now) - self.model_time).total_seconds() > max_age_seconds


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Weather clock must be timezone-aware")
    return value.astimezone(UTC)


def _object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise WeatherDataError(f"Missing or invalid {name} object")
    return value


def _array(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise WeatherDataError(f"Missing or invalid {name} array")
    return value


def _number(
    value: object,
    name: str,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WeatherDataError(f"Invalid {name} value")
    result = float(value)
    if (
        not isfinite(result)
        or (minimum is not None and result < minimum)
        or (maximum is not None and result > maximum)
    ):
        raise WeatherDataError(f"Out-of-range {name} value")
    return result


def _integer(value: object, name: str, minimum: int = 0) -> int | None:
    number = _number(value, name, minimum)
    if number is None:
        return None
    if not number.is_integer():
        raise WeatherDataError(f"Invalid {name} integer")
    return int(number)


def _timestamp(value: object, name: str) -> datetime:
    number = _number(value, name)
    if number is None:
        raise WeatherDataError(f"Missing {name} timestamp")
    try:
        return datetime.fromtimestamp(number, UTC)
    except (ValueError, OverflowError, OSError) as error:
        raise WeatherDataError(f"Invalid {name} timestamp") from error


def _optional_timestamp(value: object, name: str) -> datetime | None:
    return None if value is None else _timestamp(value, name)


def _units(value: object, expected: dict[str, str], name: str) -> None:
    units = _object(value, name)
    for field, unit in expected.items():
        if units.get(field) != unit:
            raise WeatherDataError(f"Unexpected units for {field}")


def parse_open_meteo(payload: object, fetched_at: datetime) -> WeatherSnapshot:
    """Parse canonical SI weather, preserving null values rather than inventing zeroes.

    Requests use UTC and Unix timestamps to avoid provider offset arithmetic and
    ambiguous daylight-saving wall times. Visibility uses the last model hour
    at or before the current sample, never a future forecast hour.
    """
    now = _utc(fetched_at)
    body = _object(payload, "weather")
    offset = body.get("utc_offset_seconds")
    if isinstance(offset, bool) or offset != 0:
        raise WeatherDataError("Weather provider did not return UTC timestamps")
    _units(
        body.get("current_units"),
        {
            "time": "unixtime",
            "interval": "seconds",
            "temperature_2m": "\u00b0C",
            "rain": "mm",
            "showers": "mm",
            "snowfall": "cm",
            "weather_code": "wmo code",
            "cloud_cover": "%",
            "wind_speed_10m": "m/s",
            "wind_gusts_10m": "m/s",
            "is_day": "",
        },
        "current units",
    )
    _units(
        body.get("hourly_units"),
        {
            "time": "unixtime",
            "visibility": "m",
        },
        "hourly units",
    )
    _units(
        body.get("daily_units"),
        {
            "time": "unixtime",
            "sunrise": "unixtime",
            "sunset": "unixtime",
        },
        "daily units",
    )

    current = _object(body.get("current"), "current weather")
    required = (set(CURRENT_VARIABLES.split(",")) - {"wind_direction_10m"}) | {
        "time",
        "interval",
    }
    if not required.issubset(current):
        raise WeatherDataError("Current weather fields are missing")
    model_time = _timestamp(current["time"], "model time")
    if model_time > now + timedelta(minutes=5):
        raise WeatherDataError("Weather model timestamp is unexpectedly in the future")
    interval = _integer(current["interval"], "weather interval", 1)
    if interval is None:
        raise WeatherDataError("Missing weather interval")
    day_flag = _integer(current["is_day"], "day flag")
    if day_flag not in (None, 0, 1):
        raise WeatherDataError("Invalid weather day flag")
    if current.get("wind_direction_10m") is not None:
        _units(
            body.get("current_units"),
            {"wind_direction_10m": "\u00b0"},
            "wind direction units",
        )

    hourly = _object(body.get("hourly"), "hourly weather")
    hours = [
        _timestamp(value, "visibility time")
        for value in _array(hourly.get("time"), "hourly time")
    ]
    visibility = _array(hourly.get("visibility"), "visibility")
    if len(hours) != len(visibility) or any(
        left >= right for left, right in zip(hours, hours[1:])
    ):
        raise WeatherDataError("Visibility timestamps and values do not align")
    position = bisect_right(hours, model_time) - 1
    visibility_time = hours[position] if position >= 0 else None
    visibility_m = (
        _number(visibility[position], "visibility", 0)
        if visibility_time is not None
        and model_time - visibility_time < timedelta(hours=1)
        else None
    )

    daily = _object(body.get("daily"), "daily weather")
    dates = _array(daily.get("time"), "daily time")
    rises = _array(daily.get("sunrise"), "sunrise")
    sets = _array(daily.get("sunset"), "sunset")
    if len(dates) != len(rises) or len(dates) != len(sets):
        raise WeatherDataError("Solar timestamps do not align")
    solar_days = tuple(
        SolarDay(
            _timestamp(day, "solar day"),
            _optional_timestamp(rise, "sunrise"),
            _optional_timestamp(setting, "sunset"),
        )
        for day, rise, setting in zip(dates, rises, sets)
    )
    if any(
        left.day_start >= right.day_start
        for left, right in zip(solar_days, solar_days[1:])
    ):
        raise WeatherDataError("Solar days are not in chronological order")
    for day in solar_days:
        if (
            day.sunrise is not None
            and day.sunset is not None
            and day.sunrise >= day.sunset
        ):
            raise WeatherDataError("Invalid sunrise and sunset interval")

    return WeatherSnapshot(
        model_time=model_time,
        fetched_at=now,
        interval_seconds=interval,
        temperature_c=_number(current["temperature_2m"], "temperature"),
        rain_mm=_number(current["rain"], "rain", 0),
        showers_mm=_number(current["showers"], "showers", 0),
        snowfall_cm=_number(current["snowfall"], "snowfall", 0),
        weather_code=_integer(current["weather_code"], "weather code"),
        cloud_cover_percent=_number(current["cloud_cover"], "cloud cover", 0, 100),
        wind_speed_mps=_number(current["wind_speed_10m"], "wind speed", 0),
        wind_gusts_mps=_number(current["wind_gusts_10m"], "wind gusts", 0),
        is_day=None if day_flag is None else day_flag == 1,
        visibility_m=visibility_m,
        visibility_model_time=visibility_time,
        solar_days=solar_days,
        wind_direction_degrees=_number(
            current.get("wind_direction_10m"), "wind direction", 0, 360
        ),
    )


class OpenMeteoClient:
    """Read-only asynchronous modeled weather from the fixed Open-Meteo endpoint."""

    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self._zip_location: tuple[str, float, float] | None = None

    async def fetch_zip_code(
        self, zip_code: str, *, now: datetime | None = None
    ) -> WeatherSnapshot:
        """Resolve a US ZIP code to a modeled locality, then fetch its weather."""
        if (
            not isinstance(zip_code, str)
            or re.fullmatch(r"[0-9]{5}(?:-[0-9]{4})?", zip_code) is None
        ):
            raise ValueError("Invalid US ZIP code")
        if now is not None:
            _utc(now)
        postal = zip_code[:5]
        location = self._zip_location
        if location is None or location[0] != postal:
            payload = _object(
                await self._get_json(
                    GEOCODING_URL,
                    {
                        "name": postal,
                        "count": 100,
                        "language": "en",
                        "format": "json",
                        "countryCode": "US",
                    },
                ),
                "geocoding",
            )
            for candidate in _array(payload.get("results", []), "geocoding results"):
                result = _object(candidate, "geocoding result")
                if result.get("country_code") != "US":
                    continue
                postcodes = result.get("postcodes")
                if not isinstance(postcodes, list) or postal not in postcodes:
                    continue
                latitude = _number(result.get("latitude"), "latitude", -90, 90)
                longitude = _number(result.get("longitude"), "longitude", -180, 180)
                if latitude is None or longitude is None:
                    raise WeatherDataError("Postal location coordinates are missing")
                location = (postal, latitude, longitude)
                self._zip_location = location
                break
            else:
                raise WeatherLocationError("No exact US ZIP code match was found")
        return await self.fetch(location[1], location[2], now=now)

    async def _get_json(self, url: str, params: dict[str, str | int | float]) -> object:
        try:
            response = await self.client.get(
                url,
                params=params,
                timeout=httpx.Timeout(15, connect=5),
                follow_redirects=False,
            )
        except httpx.RequestError as error:
            raise WeatherProviderError("Weather provider connection failed") from error
        if response.status_code != 200:
            raise WeatherProviderError(
                f"Weather provider returned HTTP {response.status_code}",
                status_code=response.status_code,
            )
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise WeatherDataError("Weather response exceeds the size limit")
        try:
            return response.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise WeatherDataError(
                "Weather provider did not return valid JSON"
            ) from error

    async def fetch(
        self, latitude: float, longitude: float, *, now: datetime | None = None
    ) -> WeatherSnapshot:
        """Fetch weather without following redirects or logging private coordinates."""
        if now is not None:
            _utc(now)
        if (
            isinstance(latitude, bool)
            or isinstance(longitude, bool)
            or not isinstance(latitude, (int, float))
            or not isinstance(longitude, (int, float))
            or not isfinite(latitude)
            or not isfinite(longitude)
            or not -90 <= latitude <= 90
            or not -180 <= longitude <= 180
        ):
            raise ValueError("Invalid weather coordinates")
        params: dict[str, str | int | float] = {
            "latitude": latitude,
            "longitude": longitude,
            "current": CURRENT_VARIABLES,
            "hourly": "visibility",
            "daily": "sunrise,sunset",
            "timezone": "UTC",
            "timeformat": "unixtime",
            "temperature_unit": "celsius",
            "wind_speed_unit": "ms",
            "precipitation_unit": "mm",
            "forecast_days": 3,
            "past_days": 1,
        }
        payload = await self._get_json(FORECAST_URL, params)
        return parse_open_meteo(payload, datetime.now(UTC) if now is None else now)

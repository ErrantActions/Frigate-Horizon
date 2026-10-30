"""Open-Meteo contract tests use real HTTP parsing and no external requests."""

import unittest
from datetime import UTC, datetime, timedelta, timezone

import httpx

from frigate.environment.weather import (
    MAX_RESPONSE_BYTES,
    OpenMeteoClient,
    WeatherDataError,
    WeatherLocationError,
    WeatherProviderError,
    parse_open_meteo,
)

MODEL_TIME = datetime(2026, 10, 17, 12, tzinfo=UTC)
NOW = MODEL_TIME + timedelta(minutes=15)


def weather_payload():
    """Return the provider's verified UTC/Unix field and unit layout."""
    stamp = int(MODEL_TIME.timestamp())
    return {
        "utc_offset_seconds": 0,
        "timezone": "GMT",
        "current_units": {
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
        "current": {
            "time": stamp + 900,
            "interval": 900,
            "temperature_2m": 12.3,
            "rain": 0.2,
            "showers": 0.1,
            "snowfall": 0.0,
            "weather_code": 3,
            "cloud_cover": 100,
            "wind_speed_10m": 2.33,
            "wind_gusts_10m": 5.4,
            "is_day": 1,
        },
        "hourly_units": {"time": "unixtime", "visibility": "m"},
        "hourly": {
            "time": [stamp - 3600, stamp, stamp + 3600],
            "visibility": [10000.0, 2000.0, 99.0],
        },
        "daily_units": {
            "time": "unixtime",
            "sunrise": "unixtime",
            "sunset": "unixtime",
        },
        "daily": {
            "time": [stamp - 12 * 3600],
            "sunrise": [stamp - 6 * 3600],
            "sunset": [stamp + 6 * 3600],
        },
    }


class TestWeatherParsing(unittest.TestCase):
    def test_returns_modeled_si_values_and_utc_solar_times(self):
        snapshot = parse_open_meteo(weather_payload(), NOW)
        self.assertTrue(snapshot.modeled)
        self.assertEqual(snapshot.provider, "open-meteo")
        self.assertEqual(snapshot.temperature_c, 12.3)
        self.assertAlmostEqual(snapshot.rain_and_showers_mm, 0.3)
        self.assertEqual(snapshot.wind_gusts_mps, 5.4)
        self.assertEqual(
            snapshot.solar_days[0].sunrise, MODEL_TIME - timedelta(hours=6)
        )
        self.assertEqual(snapshot.solar_days[0].sunset, MODEL_TIME + timedelta(hours=6))

    def test_selects_past_visibility_not_a_future_forecast(self):
        snapshot = parse_open_meteo(weather_payload(), NOW)
        self.assertEqual(snapshot.visibility_m, 2000)
        self.assertEqual(snapshot.visibility_model_time, MODEL_TIME)

    def test_old_hourly_coverage_is_not_used_as_current_visibility(self):
        payload = weather_payload()
        payload["hourly"]["time"] = [int((MODEL_TIME - timedelta(hours=2)).timestamp())]
        payload["hourly"]["visibility"] = [100]
        snapshot = parse_open_meteo(payload, NOW)
        self.assertIsNone(snapshot.visibility_m)
        self.assertEqual(
            snapshot.visibility_model_time, MODEL_TIME - timedelta(hours=2)
        )

    def test_missing_values_do_not_turn_into_zero_or_clear_weather(self):
        payload = weather_payload()
        payload["current"]["temperature_2m"] = None
        payload["current"]["rain"] = None
        payload["current"]["is_day"] = None
        payload["hourly"]["visibility"][1] = None
        snapshot = parse_open_meteo(payload, NOW)
        self.assertIsNone(snapshot.temperature_c)
        self.assertIsNone(snapshot.rain_and_showers_mm)
        self.assertIsNone(snapshot.is_day)
        self.assertIsNone(snapshot.visibility_m)

    def test_missing_solar_events_remain_unknown(self):
        payload = weather_payload()
        payload["daily"]["sunrise"] = [None]
        payload["daily"]["sunset"] = [None]
        snapshot = parse_open_meteo(payload, NOW)
        self.assertIsNone(snapshot.solar_days[0].sunrise)
        self.assertIsNone(snapshot.solar_days[0].sunset)

    def test_fresh_fetch_does_not_hide_stale_model_data(self):
        snapshot = parse_open_meteo(weather_payload(), NOW + timedelta(hours=3))
        self.assertTrue(snapshot.is_stale(NOW + timedelta(hours=3), 1800))
        self.assertFalse(snapshot.is_stale(NOW, 1800))

    def test_timezone_aware_clocks_are_normalized_without_double_offsets(self):
        local = NOW.astimezone(timezone(timedelta(hours=-5)))
        self.assertEqual(parse_open_meteo(weather_payload(), local).fetched_at, NOW)
        with self.assertRaises(ValueError):
            parse_open_meteo(weather_payload(), NOW.replace(tzinfo=None))

    def test_rejects_unexpected_units(self):
        for field, wrong in (
            ("temperature_2m", "\u00b0F"),
            ("wind_speed_10m", "km/h"),
            ("snowfall", "mm"),
        ):
            payload = weather_payload()
            payload["current_units"][field] = wrong
            with self.subTest(field=field), self.assertRaises(WeatherDataError):
                parse_open_meteo(payload, NOW)

    def test_rejects_invalid_numeric_fields_and_boolean_numbers(self):
        for field, value in (
            ("cloud_cover", 101),
            ("wind_speed_10m", -1),
            ("temperature_2m", float("nan")),
            ("rain", True),
            ("weather_code", 2.5),
            ("is_day", 2),
            ("interval", 0),
        ):
            payload = weather_payload()
            payload["current"][field] = value
            with (
                self.subTest(field=field, value=value),
                self.assertRaises(WeatherDataError),
            ):
                parse_open_meteo(payload, NOW)
        payload = weather_payload()
        payload["utc_offset_seconds"] = False
        with self.assertRaises(WeatherDataError):
            parse_open_meteo(payload, NOW)

    def test_rejects_missing_current_fields(self):
        payload = weather_payload()
        del payload["current"]["rain"]
        with self.assertRaises(WeatherDataError):
            parse_open_meteo(payload, NOW)

    def test_rejects_future_model_timestamps(self):
        payload = weather_payload()
        payload["current"]["time"] = int((NOW + timedelta(minutes=6)).timestamp())
        with self.assertRaises(WeatherDataError):
            parse_open_meteo(payload, NOW)

    def test_rejects_misaligned_or_unsorted_time_series(self):
        payload = weather_payload()
        payload["hourly"]["visibility"].pop()
        with self.assertRaises(WeatherDataError):
            parse_open_meteo(payload, NOW)
        payload = weather_payload()
        payload["hourly"]["time"].reverse()
        with self.assertRaises(WeatherDataError):
            parse_open_meteo(payload, NOW)
        payload = weather_payload()
        payload["daily"]["sunset"] = []
        with self.assertRaises(WeatherDataError):
            parse_open_meteo(payload, NOW)


class TestOpenMeteoClient(unittest.IsolatedAsyncioTestCase):
    async def test_requests_verified_units_and_parses_the_response(self):
        async def handler(request):
            self.assertEqual(request.url.host, "api.open-meteo.com")
            self.assertEqual(request.url.params["timezone"], "UTC")
            self.assertEqual(request.url.params["timeformat"], "unixtime")
            self.assertEqual(request.url.params["wind_speed_unit"], "ms")
            self.assertEqual(request.url.params["hourly"], "visibility")
            self.assertEqual(request.url.params["daily"], "sunrise,sunset")
            return httpx.Response(200, json=weather_payload())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            snapshot = await OpenMeteoClient(client).fetch(52.52, 13.41, now=NOW)
        self.assertEqual(snapshot.visibility_m, 2000)

    async def test_invalid_coordinates_and_naive_clocks_do_not_make_requests(self):
        def handler(request):
            self.fail("Invalid inputs must not make a weather request")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            for latitude, longitude in (
                (91, 0),
                (0, 181),
                (True, 0),
                (float("inf"), 0),
            ):
                with self.subTest(latitude=latitude), self.assertRaises(ValueError):
                    await OpenMeteoClient(client).fetch(latitude, longitude, now=NOW)
            with self.assertRaises(ValueError):
                await OpenMeteoClient(client).fetch(0, 0, now=NOW.replace(tzinfo=None))

    async def test_rejects_http_errors_instead_of_returning_fair_weather(self):
        for status in (301, 401, 429, 500):
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: httpx.Response(status))
            ) as client:
                with (
                    self.subTest(status=status),
                    self.assertRaises(WeatherProviderError) as caught,
                ):
                    await OpenMeteoClient(client).fetch(52.52, 13.41, now=NOW)
                self.assertEqual(caught.exception.status_code, status)
                self.assertNotIn("52.52", str(caught.exception))

    async def test_connection_errors_have_a_sanitized_public_message(self):
        def handler(request):
            raise httpx.ConnectError("private request details", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(WeatherProviderError) as caught:
                await OpenMeteoClient(client).fetch(52.52, 13.41, now=NOW)
        self.assertNotIn("private request details", str(caught.exception))
        self.assertNotIn("52.52", str(caught.exception))

    async def test_rejects_invalid_or_oversized_response_bodies(self):
        for body in (b"not-json", b"x" * (MAX_RESPONSE_BYTES + 1)):
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, content=body)
                )
            ) as client:
                with self.assertRaises(WeatherDataError):
                    await OpenMeteoClient(client).fetch(52.52, 13.41, now=NOW)


class TestZipCodeWeather(unittest.IsolatedAsyncioTestCase):
    async def test_zip_resolves_exact_us_match_and_caches_leading_zero_postal_code(
        self,
    ):
        requests = []

        def handler(request):
            requests.append(request)
            if request.url.host == "geocoding-api.open-meteo.com":
                self.assertEqual(request.url.params["name"], "00501")
                self.assertEqual(request.url.params["countryCode"], "US")
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {"country_code": "DE", "postcodes": ["00501"]},
                            {"country_code": "US", "postcodes": ["00502"]},
                            {
                                "country_code": "US",
                                "postcodes": ["00501"],
                                "latitude": 40.8,
                                "longitude": -73.0,
                            },
                        ]
                    },
                )
            self.assertEqual(request.url.params["latitude"], "40.8")
            self.assertEqual(request.url.params["longitude"], "-73.0")
            return httpx.Response(200, json=weather_payload())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = OpenMeteoClient(http)
            first = await client.fetch_zip_code("00501-1234", now=NOW)
            second = await client.fetch_zip_code("00501", now=NOW)
        self.assertEqual(first, second)
        self.assertEqual(len(requests), 3)

    async def test_unknown_or_fuzzy_zip_never_falls_back_to_another_location(self):
        for payload in (
            {},
            {
                "results": [
                    {
                        "country_code": "US",
                        "postcodes": ["10002"],
                        "latitude": 40,
                        "longitude": -74,
                    },
                ]
            },
        ):
            requests = []

            def handler(request):
                requests.append(request)
                return httpx.Response(200, json=payload)

            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            ) as http:
                with self.assertRaises(WeatherLocationError) as caught:
                    await OpenMeteoClient(http).fetch_zip_code("10001", now=NOW)
            self.assertEqual(len(requests), 1)
            self.assertNotIn("10001", str(caught.exception))

    async def test_invalid_postal_input_and_clock_do_not_make_requests(self):
        def handler(request):
            self.fail("Invalid ZIP inputs must not make requests")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = OpenMeteoClient(http)
            for value in (10001, "1000", "100001", "ABCDE", "10001/../../", ""):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    await client.fetch_zip_code(value, now=NOW)
            with self.assertRaises(ValueError):
                await client.fetch_zip_code("10001", now=NOW.replace(tzinfo=None))

    async def test_geocoder_invalid_data_and_http_errors_are_explicit(self):
        for payload in (
            {"results": "bad"},
            {
                "results": [
                    {
                        "country_code": "US",
                        "postcodes": ["10001"],
                        "latitude": True,
                        "longitude": -74,
                    }
                ]
            },
            {"results": [{"country_code": "US", "postcodes": ["10001"]}]},
        ):
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, json=payload)
                )
            ) as http:
                with self.assertRaises(WeatherDataError):
                    await OpenMeteoClient(http).fetch_zip_code("10001", now=NOW)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(429))
        ) as http:
            with self.assertRaises(WeatherProviderError) as caught:
                await OpenMeteoClient(http).fetch_zip_code("10001", now=NOW)
        self.assertEqual(caught.exception.status_code, 429)


if __name__ == "__main__":
    unittest.main()

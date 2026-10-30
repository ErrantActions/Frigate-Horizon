"""Pure condition, wall-clock, and monotonic weather-rule checks."""

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from frigate.environment.profiles import (
    WeatherDwell,
    schedule_is_active,
    weather_condition_matches,
)
from frigate.environment.weather import parse_open_meteo
from frigate.test.test_environment_weather import NOW, weather_payload


class TestWeatherConditions(unittest.TestCase):
    def setUp(self):
        self.weather = parse_open_meteo(weather_payload(), NOW)

    def test_rain_and_snow_require_positive_amount_even_with_zero_threshold(self):
        clear = replace(
            self.weather, rain_mm=0, showers_mm=0, snowfall_cm=0, weather_code=0
        )
        self.assertFalse(weather_condition_matches("rain", 0, clear))
        self.assertFalse(weather_condition_matches("snow", 0, clear))
        self.assertTrue(weather_condition_matches("rain", 0.3, self.weather))
        self.assertFalse(weather_condition_matches("rain", 0.31, self.weather))

    def test_categorical_rain_snow_and_fog_are_usable_without_amounts(self):
        rain = replace(self.weather, rain_mm=None, weather_code=61)
        self.assertTrue(weather_condition_matches("rain", None, rain))
        self.assertIsNone(weather_condition_matches("rain", 0.1, rain))
        snow = replace(self.weather, snowfall_cm=None, weather_code=73)
        self.assertTrue(weather_condition_matches("snow", None, snow))
        fog = replace(self.weather, visibility_m=None, weather_code=45)
        self.assertTrue(weather_condition_matches("fog", None, fog))
        self.assertIsNone(weather_condition_matches("fog", 1000, fog))

    def test_visibility_cloud_and_gust_threshold_boundaries(self):
        self.assertTrue(weather_condition_matches("fog", 2000, self.weather))
        self.assertFalse(weather_condition_matches("fog", 1999, self.weather))
        self.assertTrue(weather_condition_matches("cloud", 100, self.weather))
        self.assertTrue(weather_condition_matches("wind", 5.4, self.weather))
        self.assertFalse(weather_condition_matches("wind", 5.41, self.weather))

    def test_missing_metrics_remain_unknown(self):
        snapshot = replace(
            self.weather,
            visibility_m=None,
            cloud_cover_percent=None,
            wind_gusts_mps=None,
        )
        for condition in ("fog", "cloud", "wind"):
            with self.subTest(condition=condition):
                self.assertIsNone(weather_condition_matches(condition, 10, snapshot))

    def test_rejects_invalid_conditions_and_thresholds(self):
        for condition, threshold in (
            ("unknown", None),
            ("cloud", 101),
            ("rain", -1),
            ("snow", float("nan")),
        ):
            with self.subTest(condition=condition), self.assertRaises(ValueError):
                weather_condition_matches(condition, threshold, self.weather)


class TestFixedSchedules(unittest.TestCase):
    def test_overnight_window_belongs_to_the_start_weekday(self):
        monday = datetime(2026, 10, 12, 23, tzinfo=UTC)
        self.assertTrue(schedule_is_active("22:00", "06:00", [0], "UTC", monday))
        self.assertTrue(
            schedule_is_active(
                "22:00", "06:00", [0], "UTC", monday + timedelta(hours=4)
            )
        )
        self.assertFalse(
            schedule_is_active(
                "22:00", "06:00", [0], "UTC", monday + timedelta(hours=7)
            )
        )
        self.assertFalse(
            schedule_is_active(
                "22:00", "06:00", [0], "UTC", monday + timedelta(days=1, hours=4)
            )
        )

    def test_start_is_inclusive_end_is_exclusive(self):
        start = datetime(2026, 10, 12, 10, tzinfo=UTC)
        self.assertTrue(schedule_is_active("10:00", "12:00", [0], "UTC", start))
        self.assertFalse(
            schedule_is_active("10:00", "12:00", [0], "UTC", start + timedelta(hours=2))
        )

    def test_rejects_invalid_intervals_and_naive_clocks(self):
        now = datetime(2026, 10, 12, 10, tzinfo=UTC)
        for first, last, days in (
            ("10:00", "10:00", [0]),
            ("25:00", "12:00", [0]),
            ("10:00", "12:00", [True]),
        ):
            with self.subTest(first=first), self.assertRaises(ValueError):
                schedule_is_active(first, last, days, "UTC", now)
        with self.assertRaises(ValueError):
            schedule_is_active("10:00", "12:00", [0], "UTC", now.replace(tzinfo=None))


class TestWeatherDwell(unittest.TestCase):
    def test_activation_uses_exact_monotonic_dwell_threshold(self):
        dwell = WeatherDwell()
        self.assertIsNone(dwell.update("rain", 100, 30))
        self.assertIsNone(dwell.update("rain", 129.99, 30))
        self.assertEqual(dwell.update("rain", 130, 30), "rain")

    def test_flapping_candidate_resets_the_dwell_timer(self):
        dwell = WeatherDwell()
        dwell.update("rain", 100, 30)
        dwell.update("snow", 110, 30)
        self.assertIsNone(dwell.update("rain", 120, 30))
        self.assertIsNone(dwell.update("rain", 149, 30))
        self.assertEqual(dwell.update("rain", 150, 30), "rain")

    def test_clearing_is_also_debounced_but_stale_data_can_clear_immediately(self):
        dwell = WeatherDwell()
        self.assertEqual(dwell.update("rain", 100, 0), "rain")
        self.assertEqual(dwell.update(None, 110, 30), "rain")
        self.assertIsNotNone(dwell.pending_since)
        self.assertIsNone(dwell.update(None, 111, 0))

    def test_rejects_backwards_or_nonfinite_clocks(self):
        dwell = WeatherDwell()
        dwell.update("rain", 100, 30)
        for now, delay in ((99, 30), (float("nan"), 30), (101, -1)):
            with self.subTest(now=now), self.assertRaises(ValueError):
                dwell.update("rain", now, delay)


if __name__ == "__main__":
    unittest.main()

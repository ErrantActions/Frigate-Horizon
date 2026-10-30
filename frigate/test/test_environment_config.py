"""Environmental automation is opt-in and rejects ambiguous profile settings."""

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from frigate.api.app import config as config_response
from frigate.config import FrigateConfig
from frigate.config.environment import (
    CameraEnvironmentConfig,
    FixedProfileSchedule,
    GlareProfileConfig,
    SolarProfileConfig,
    WeatherConfig,
    WeatherProfileRule,
)
from frigate.environment.profiles import ProfileSelector, schedule_is_active
from frigate.environment.weather import parse_open_meteo
from frigate.test.test_environment_weather import NOW, weather_payload


class TestWeatherConfiguration(unittest.TestCase):
    def test_disabled_default_has_no_implicit_location(self):
        config = WeatherConfig()
        self.assertFalse(config.enabled)
        self.assertIsNone(config.latitude)
        self.assertIsNone(config.longitude)

    def test_enabled_weather_requires_both_explicit_coordinates(self):
        for values in ({}, {"latitude": 40.0}, {"longitude": -90.0}):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                WeatherConfig(enabled=True, **values)

    def test_rejects_boolean_nonfinite_and_out_of_range_coordinates(self):
        for latitude in (True, 91.0, float("nan"), float("inf")):
            with self.subTest(latitude=latitude), self.assertRaises(ValidationError):
                WeatherConfig(enabled=True, latitude=latitude, longitude=0.0)

    def test_rejects_freshness_shorter_than_polling_interval(self):
        with self.assertRaises(ValidationError):
            WeatherConfig(refresh_seconds=3600, stale_after_seconds=1800)

    def test_zip_code_enables_weather_without_coordinates_and_preserves_zeroes(self):
        for value in ("00501", "00501-1234"):
            config = WeatherConfig(enabled=True, zip_code=value)
            self.assertEqual(config.zip_code, value)
            self.assertIsNone(config.latitude)
            self.assertIsNone(config.longitude)

    def test_zip_code_rejects_numbers_and_invalid_postal_formats(self):
        for value in (501, "501", "", "10001 ", "10001-123", "ABCDE"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                WeatherConfig(enabled=True, zip_code=value)


class TestCameraEnvironmentConfiguration(unittest.TestCase):
    def schedule(self, **overrides):
        values = {"id": "night", "profile": "night", "start": "22:00", "end": "06:00"}
        values.update(overrides)
        return FixedProfileSchedule(**values)

    def test_all_automation_and_physical_writes_are_off_by_default(self):
        config = CameraEnvironmentConfig()
        self.assertFalse(config.enabled)
        self.assertFalse(config.solar.enabled)
        self.assertFalse(config.glare.enabled)
        self.assertFalse(config.allow_device_changes)
        self.assertEqual(config.referenced_profiles(), set())

    def test_accepts_overnight_intervals_and_explicit_weekdays(self):
        self.assertEqual(self.schedule(weekdays=[0, 4]).weekdays, [0, 4])

    def test_rejects_ambiguous_or_invalid_clock_intervals(self):
        for start, end in (("24:00", "06:00"), ("22:60", "06:00"), ("06:00", "06:00")):
            with self.subTest(start=start), self.assertRaises(ValidationError):
                self.schedule(start=start, end=end)

    def test_rejects_duplicate_coerced_or_invalid_weekdays(self):
        for days in ([0, 0], [True], ["1"], [7], [-1], []):
            with self.subTest(days=days), self.assertRaises(ValidationError):
                self.schedule(weekdays=days)

    def test_rejects_invalid_timezones(self):
        with self.assertRaises(ValidationError):
            CameraEnvironmentConfig(timezone="Not/A_Timezone")

    def test_solar_requires_both_profile_choices(self):
        with self.assertRaises(ValidationError):
            SolarProfileConfig(enabled=True, day="day")

    def test_sunrise_window_requires_ordered_bounded_offsets(self):
        for start, end in ((0, 0), (120, -15), (-361, 120), (-15, 361)):
            with self.subTest(start=start, end=end), self.assertRaises(ValidationError):
                SolarProfileConfig(sunrise_start_minutes=start, sunrise_end_minutes=end)
        self.assertIn(
            "sunrise",
            CameraEnvironmentConfig(
                solar={"sunrise_profile": "sunrise"}
            ).referenced_profiles(),
        )

    def test_glare_requires_explicit_zones_and_profile(self):
        for fields in ({}, {"profile": "glare"}, {"zones": ["driveway"]}):
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                GlareProfileConfig(enabled=True, **fields)

    def test_rejects_duplicate_identifiers_and_out_of_range_cloud_percent(self):
        rule = {"id": "rain", "profile": "rain", "condition": "rain"}
        with self.assertRaises(ValidationError):
            CameraEnvironmentConfig(weather_rules=[rule, rule])
        with self.assertRaises(ValidationError):
            WeatherProfileRule(
                id="cloud", profile="rain", condition="cloud", threshold=101
            )

    def test_collects_references_across_all_automation_sources(self):
        config = CameraEnvironmentConfig(
            solar={"enabled": True, "day": "day", "night": "night"},
            schedules=[self.schedule()],
            weather_rules=[{"id": "rain", "profile": "rain", "condition": "rain"}],
            glare={"enabled": True, "profile": "glare", "zones": ["driveway"]},
        )
        self.assertEqual(
            config.referenced_profiles(), {"day", "night", "rain", "glare"}
        )


class TestEnvironmentProfileReferences(unittest.TestCase):
    def configuration(self, environment, profiles=None, definitions=None):
        return FrigateConfig(
            mqtt={"enabled": False},
            profiles=definitions or {},
            cameras={
                "front": {
                    "ffmpeg": {
                        "inputs": [
                            {
                                "path": "rtsp://camera.invalid/test",
                                "roles": ["detect", "record"],
                            }
                        ]
                    },
                    "detect": {"height": 1080, "width": 1920, "fps": 5},
                    "profiles": profiles or {},
                    "environment": environment,
                }
            },
        )

    def test_rejects_environment_reference_without_camera_override(self):
        with self.assertRaises(ValidationError):
            self.configuration(
                {
                    "weather_rules": [
                        {"id": "rain", "profile": "rain", "condition": "rain"}
                    ]
                },
                definitions={"rain": {"friendly_name": "Rain"}},
            )

    def test_rejects_unknown_glare_zone(self):
        with self.assertRaises(ValidationError):
            self.configuration(
                {"glare": {"enabled": True, "profile": "glare", "zones": ["missing"]}},
                profiles={"glare": {}},
                definitions={"glare": {"friendly_name": "Glare"}},
            )

    def test_accepts_defined_per_camera_weather_override(self):
        config = self.configuration(
            {"weather_rules": [{"id": "rain", "profile": "rain", "condition": "rain"}]},
            profiles={"rain": {"motion": {"threshold": 40}}},
            definitions={"rain": {"friendly_name": "Rain"}},
        )
        self.assertFalse(config.cameras["front"].environment.enabled)
        self.assertFalse(config.weather.enabled)


class TestEnvironmentProfileSelection(unittest.TestCase):
    def setUp(self):
        self.weather = parse_open_meteo(weather_payload(), NOW)
        self.config = CameraEnvironmentConfig(
            enabled=True,
            solar={"enabled": True, "day": "day", "night": "night"},
            weather_dwell_seconds=0,
        )
        self.selector = ProfileSelector(self.config)

    def test_day_and_night_follow_absolute_solar_times(self):
        self.assertEqual(
            self.selector.decide(NOW, 0, self.weather, 1800).profiles, ("day",)
        )
        night = NOW.replace(hour=21)
        snapshot = replace(self.weather, model_time=night, fetched_at=night)
        self.assertEqual(
            self.selector.decide(night, 1, snapshot, 1800).profiles, ("night",)
        )

    def test_sunrise_profile_uses_actual_sunrise_and_exact_window_boundaries(self):
        self.config.solar.sunrise_profile = "sunrise"
        self.config.solar.sunrise_offset_minutes = 60
        sunrise = self.weather.solar_days[0].sunrise
        for offset, expected in (
            (-16, "night"),
            (-15, "sunrise"),
            (0, "sunrise"),
            (119, "sunrise"),
            (120, "day"),
        ):
            now = sunrise + timedelta(minutes=offset)
            weather = replace(self.weather, model_time=now, fetched_at=now)
            plan = self.selector.decide(now, offset + 20, weather, 1800)
            self.assertEqual(plan.profiles, (expected,))
            if expected == "sunrise":
                self.assertEqual(plan.base_reason, "solar_sunrise")

    def test_sunrise_window_tracks_seasonal_sunrise_changes(self):
        self.config.solar.sunrise_profile = "sunrise"
        day = self.weather.solar_days[0]
        later = replace(day, sunrise=day.sunrise + timedelta(hours=2))
        now = day.sunrise + timedelta(minutes=30)
        weather = replace(self.weather, solar_days=(later,), model_time=now)
        self.assertEqual(
            self.selector.decide(now, 0, weather, 1800).profiles, ("night",)
        )
        now = later.sunrise + timedelta(minutes=30)
        self.assertEqual(
            self.selector.decide(
                now, 1, replace(weather, model_time=now), 1800
            ).profiles,
            ("sunrise",),
        )

    def test_sunrise_does_not_replace_fixed_or_manual_choices(self):
        self.config.solar.sunrise_profile = "sunrise"
        now = self.weather.solar_days[0].sunrise + timedelta(minutes=30)
        weather = replace(self.weather, model_time=now)
        self.config.schedules = [
            FixedProfileSchedule(
                id="scheduled",
                profile="fixed",
                start="00:00",
                end="23:59",
            )
        ]
        self.assertEqual(
            self.selector.decide(now, 0, weather, 1800).profiles, ("fixed",)
        )
        self.assertEqual(
            self.selector.decide(
                now, 1, weather, 1800, manual_profile="manual"
            ).profiles,
            ("manual",),
        )

    def test_sunrise_is_optional_and_missing_solar_never_guesses_a_profile(self):
        now = self.weather.solar_days[0].sunrise + timedelta(minutes=30)
        self.assertEqual(
            self.selector.decide(
                now, 0, replace(self.weather, model_time=now), 1800
            ).profiles,
            ("day",),
        )
        self.config.solar.sunrise_profile = "sunrise"
        self.assertEqual(self.selector.decide(now, 1, None, 1800).profiles, ())

    def test_sunrise_window_cannot_continue_past_sunset(self):
        self.config.solar.sunrise_profile = "sunrise"
        self.config.solar.sunrise_end_minutes = 360
        day = self.weather.solar_days[0]
        sunset = day.sunrise + timedelta(hours=1)
        weather = replace(
            self.weather, solar_days=(replace(day, sunset=sunset),), model_time=sunset
        )
        self.assertEqual(
            self.selector.decide(sunset, 0, weather, 1800).profiles, ("night",)
        )

    def test_weather_overlays_the_day_or_night_base(self):
        self.config.weather_rules = [
            WeatherProfileRule(id="rain-rule", profile="rain", condition="rain")
        ]
        self.assertEqual(
            self.selector.decide(NOW, 0, self.weather, 1800).profiles, ("day", "rain")
        )
        night = NOW.replace(hour=21)
        snapshot = replace(self.weather, model_time=night, fetched_at=night)
        plan = self.selector.decide(night, 1, snapshot, 1800)
        self.assertEqual(plan.profiles, ("night", "rain"))
        self.assertEqual(plan.weather_rule_id, "rain-rule")

    def test_manual_choice_replaces_automatic_layers(self):
        self.config.weather_rules = [
            WeatherProfileRule(id="rain-rule", profile="rain", condition="rain")
        ]
        plan = self.selector.decide(NOW, 0, self.weather, 1800, manual_profile="manual")
        self.assertEqual(plan.profiles, ("manual",))
        self.assertEqual(plan.reason, "manual_override")

    def test_glare_expiry_recomputes_the_latest_manual_choice(self):
        self.config.glare = GlareProfileConfig(
            enabled=True, profile="glare", zones=["driveway"]
        )
        first = self.selector.decide(
            NOW, 10, self.weather, 1800, manual_profile="manual-day", glare_until=30
        )
        self.assertEqual(first.profiles, ("manual-day", "glare"))
        changed = self.selector.decide(
            NOW, 20, self.weather, 1800, manual_profile="manual-night", glare_until=30
        )
        self.assertEqual(changed.profiles, ("manual-night", "glare"))
        expired = self.selector.decide(
            NOW, 30, self.weather, 1800, manual_profile="manual-night", glare_until=30
        )
        self.assertEqual(expired.profiles, ("manual-night",))
        self.assertEqual(expired.reason, "manual_override")

    def test_stale_weather_is_removed_without_waiting_for_clear_dwell(self):
        self.config.weather_rules = [
            WeatherProfileRule(id="rain-rule", profile="rain", condition="rain")
        ]
        self.selector.decide(NOW, 0, self.weather, 1800)
        self.config.weather_dwell_seconds = 30
        stale = replace(self.weather, model_time=NOW - timedelta(hours=3))
        plan = self.selector.decide(NOW, 1, stale, 1800)
        self.assertEqual(plan.profiles, ("day",))
        self.assertIn("weather_stale", plan.warnings)
        self.assertFalse(plan.weather_change_pending)

    def test_clear_dwell_is_exposed_even_when_the_pending_profile_is_none(self):
        self.config.weather_rules = [
            WeatherProfileRule(id="rain-rule", profile="rain", condition="rain")
        ]
        self.selector.decide(NOW, 0, self.weather, 1800)
        self.config.weather_dwell_seconds = 30
        dry = replace(self.weather, rain_mm=0, showers_mm=0, weather_code=0)
        plan = self.selector.decide(NOW, 1, dry, 1800)
        self.assertEqual(plan.profiles, ("day", "rain"))
        self.assertTrue(plan.weather_change_pending)
        self.assertIsNone(plan.pending_weather_profile)

    def test_fixed_schedules_override_solar_and_use_priority(self):
        self.config.schedules = [
            FixedProfileSchedule(
                id="lower", profile="lower", start="10:00", end="14:00", priority=1
            ),
            FixedProfileSchedule(
                id="higher", profile="higher", start="10:00", end="14:00", priority=2
            ),
        ]
        plan = self.selector.decide(NOW, 0, self.weather, 1800)
        self.assertEqual(plan.profiles, ("higher",))
        self.assertEqual(plan.schedule_id, "higher")

    def test_solar_offsets_have_exact_inclusive_and_exclusive_boundaries(self):
        self.config.solar.sunrise_offset_minutes = 60
        self.config.solar.sunset_offset_minutes = -60
        for tick, hour, minute, expected in (
            (0, 6, 59, "night"),
            (1, 7, 0, "day"),
            (2, 17, 0, "night"),
        ):
            now = NOW.replace(hour=hour, minute=minute)
            snapshot = replace(self.weather, model_time=now, fetched_at=now)
            self.assertEqual(
                self.selector.decide(now, tick, snapshot, 1800).profiles, (expected,)
            )

    def test_missing_or_inverted_solar_events_do_not_guess_night(self):
        missing = replace(self.weather, solar_days=())
        plan = self.selector.decide(NOW, 0, missing, 1800)
        self.assertEqual(plan.profiles, ())
        self.assertIn("solar_unavailable", plan.warnings)
        self.config.solar.sunrise_offset_minutes = 720
        self.config.solar.sunset_offset_minutes = -720
        plan = self.selector.decide(NOW, 1, self.weather, 1800)
        self.assertEqual(plan.profiles, ())
        self.assertIn("solar_interval_invalid", plan.warnings)

    def test_disabled_camera_automation_ignores_manual_and_glare_inputs(self):
        self.config.enabled = False
        plan = self.selector.decide(
            NOW, 0, self.weather, 1800, manual_profile="manual", glare_until=30
        )
        self.assertEqual(plan.profiles, ())
        self.assertEqual(plan.reason, "disabled")


class TestDaylightSavingSchedules(unittest.TestCase):
    def test_both_fall_back_occurrences_match_the_same_wall_clock_interval(self):
        for hour in (5, 6):
            now = datetime(2026, 11, 1, hour, 30, tzinfo=UTC)
            self.assertTrue(
                schedule_is_active("01:15", "01:45", [6], "America/New_York", now)
            )

    def test_spring_gap_is_not_replayed_but_existing_later_window_is_active(self):
        now = datetime(2026, 3, 8, 7, 30, tzinfo=UTC)
        self.assertFalse(
            schedule_is_active("02:15", "02:45", [6], "America/New_York", now)
        )
        self.assertTrue(
            schedule_is_active("02:15", "03:45", [6], "America/New_York", now)
        )


class TestWeatherCoordinatePrivacy(unittest.TestCase):
    def test_only_administrators_receive_location_coordinates(self):
        app = FastAPI()
        app.frigate_config = FrigateConfig(
            mqtt={"enabled": False},
            cameras={},
            weather={
                "enabled": True,
                "zip_code": "10001",
                "latitude": 20.0,
                "longitude": 30.0,
            },
        )
        app.profile_manager = None
        app.get("/config")(config_response)
        with TestClient(app) as client:
            viewer = client.get("/config", headers={"remote-role": "viewer"})
            self.assertEqual(viewer.status_code, 200)
            self.assertTrue(viewer.json()["weather"]["enabled"])
            self.assertNotIn("latitude", viewer.json()["weather"])
            self.assertNotIn("longitude", viewer.json()["weather"])
            self.assertNotIn("zip_code", viewer.json()["weather"])
            admin = client.get("/config", headers={"remote-role": "admin"})
            self.assertEqual(admin.status_code, 200)
            self.assertEqual(admin.json()["weather"]["latitude"], 20.0)
            self.assertEqual(admin.json()["weather"]["longitude"], 30.0)
            self.assertEqual(admin.json()["weather"]["zip_code"], "10001")


if __name__ == "__main__":
    unittest.main()

"""Opt-in environmental service behavior without real weather or device writes."""

import asyncio
import copy
import json
import multiprocessing as mp
import tempfile
import unittest
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx

from frigate.config import FrigateConfig
from frigate.config.profile_manager import ProfileManager
from frigate.const import REPLAY_CAMERA_PREFIX
from frigate.environment.messages import GlareSignal, glare_configuration_hash
from frigate.environment.runtime import EnvironmentManager
from frigate.environment.weather import (
    OpenMeteoClient,
    WeatherDataError,
    WeatherLocationError,
    WeatherProviderError,
    parse_open_meteo,
)
from frigate.test.test_environment_weather import NOW, weather_payload
from frigate.util.builtin import flatten_config_data, update_yaml_file_bulk


class TestEnvironmentRuntime(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.data = {
            "mqtt": {"enabled": False},
            "profiles": {
                name: {"friendly_name": name.title()}
                for name in ("day", "night", "rain", "glare")
            },
            "cameras": {
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
                    "motion": {"threshold": 10, "contour_area": 10},
                    "zones": {"driveway": {"coordinates": "0,0,1,0,1,1,0,1"}},
                    "environment": {"enabled": False},
                    "profiles": {
                        "day": {"motion": {"threshold": 20}},
                        "night": {"motion": {"threshold": 25}},
                        "rain": {"motion": {"contour_area": 40}},
                        "glare": {"motion": {"threshold": 50}},
                    },
                }
            },
        }

    def build(self, weather=False, environment=None):
        data = copy.deepcopy(self.data)
        if weather:
            data["weather"] = {
                "enabled": True,
                "latitude": 52.52,
                "longitude": 13.41,
            }
        if environment is not None:
            data["cameras"]["front"]["environment"] = environment
        config = FrigateConfig(**data)
        publisher = MagicMock()
        profiles = ProfileManager(config, publisher)
        stop = mp.Event()
        manager = EnvironmentManager(config, profiles, stop)
        return data, config, publisher, profiles, stop, manager

    async def test_disabled_service_does_not_fetch_or_republish_camera_config(self):
        _, _, publisher, _, _, manager = self.build()
        client = AsyncMock(spec=OpenMeteoClient)
        await manager.refresh_weather(client, NOW, 0)
        await manager.tick(NOW, 0)
        client.fetch.assert_not_awaited()
        publisher.publish_update.assert_not_called()
        self.assertEqual(manager.camera_status("front").phase, "disabled")

    async def test_weather_refresh_is_bounded_and_applies_camera_layers(self):
        _, config, publisher, _, _, manager = self.build(
            weather=True,
            environment={
                "enabled": True,
                "solar": {"enabled": True, "day": "day", "night": "night"},
                "weather_rules": [
                    {"id": "rain", "profile": "rain", "condition": "rain"}
                ],
                "weather_dwell_seconds": 0,
            },
        )
        client = AsyncMock(spec=OpenMeteoClient)
        client.fetch.return_value = parse_open_meteo(weather_payload(), NOW)
        await manager.refresh_weather(client, NOW, 0)
        await manager.refresh_weather(client, NOW, 1)
        client.fetch.assert_awaited_once()
        await manager.tick(NOW, 1)
        state = manager.camera_status("front")
        self.assertEqual(state.phase, "applied")
        self.assertEqual(state.applied_profiles, ("day", "rain"))
        self.assertEqual(config.cameras["front"].motion.threshold, 20)
        self.assertEqual(config.cameras["front"].motion.contour_area, 40)
        self.assertTrue(publisher.publish_update.called)
        public = asdict(manager.weather_status(NOW, 1))
        self.assertNotIn("latitude", public)
        self.assertNotIn("longitude", public)
        self.assertEqual(public["state"], "fresh")

    async def test_provider_failure_keeps_cached_data_then_marks_it_stale(self):
        _, _, _, _, _, manager = self.build(weather=True)
        client = AsyncMock(spec=OpenMeteoClient)
        client.fetch.return_value = parse_open_meteo(weather_payload(), NOW)
        await manager.refresh_weather(client, NOW, 0)
        client.fetch.side_effect = WeatherProviderError("private location")
        with self.assertLogs("frigate.environment.runtime", level="WARNING") as logs:
            await manager.refresh_weather(client, NOW + timedelta(minutes=20), 601)
        self.assertNotIn("private location", " ".join(logs.output))
        self.assertEqual(
            manager.weather_status(NOW + timedelta(minutes=20), 601).state, "cached"
        )
        self.assertEqual(
            manager.weather_status(NOW + timedelta(minutes=31), 602).state, "stale"
        )
        self.assertEqual(
            manager.weather_status(NOW, 602).error_code, "weather_provider_unavailable"
        )

    async def test_bad_data_and_rate_limits_are_explicit(self):
        for error, code in (
            (WeatherDataError("invalid units"), "weather_data_invalid"),
            (WeatherProviderError("limited", status_code=429), "weather_rate_limited"),
        ):
            _, _, _, _, _, manager = self.build(weather=True)
            client = AsyncMock(spec=OpenMeteoClient)
            client.fetch.side_effect = error
            with self.assertLogs("frigate.environment.runtime", level="WARNING"):
                await manager.refresh_weather(client, NOW, 0)
            status = manager.weather_status(NOW, 0)
            self.assertEqual(status.state, "unavailable")
            self.assertEqual(status.error_code, code)
            if error.status_code == 429:
                self.assertGreaterEqual(status.next_refresh_seconds, 3600)

    async def test_location_change_discards_an_in_flight_response(self):
        data, _, _, profiles, _, manager = self.build(weather=True)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def fetch(*_args, **_kwargs):
            entered.set()
            await release.wait()
            return parse_open_meteo(weather_payload(), NOW)

        client = AsyncMock(spec=OpenMeteoClient)
        client.fetch.side_effect = fetch
        pending = asyncio.create_task(manager.refresh_weather(client, NOW, 0))
        await entered.wait()
        data["weather"]["latitude"] = 40.0
        changed = FrigateConfig(**data)
        profiles.update_config(changed)
        manager.update_config(changed)
        release.set()
        await pending
        self.assertIsNone(manager.weather_status(NOW, 1).data)

    async def test_zip_code_takes_precedence_and_missing_zip_has_no_coordinate_fallback(
        self,
    ):
        data, _, _, profiles, _, manager = self.build(weather=True)
        data["weather"]["zip_code"] = "00501"
        config = FrigateConfig(**data)
        profiles.update_config(config)
        manager.update_config(config)
        client = AsyncMock(spec=OpenMeteoClient)
        client.fetch_zip_code.return_value = parse_open_meteo(weather_payload(), NOW)
        await manager.refresh_weather(client, NOW, 0)
        client.fetch_zip_code.assert_awaited_once_with("00501", now=NOW)
        client.fetch.assert_not_awaited()
        client.fetch_zip_code.side_effect = WeatherLocationError("No postal match")
        with self.assertLogs("frigate.environment.runtime", level="WARNING"):
            await manager.refresh_weather(client, NOW, 601)
        self.assertEqual(
            manager.weather_status(NOW, 602).error_code, "weather_location_not_found"
        )
        client.fetch.assert_not_awaited()

    async def test_zip_change_discards_old_cached_and_in_flight_weather(self):
        data, _, _, profiles, _, manager = self.build(weather=True)
        entered, release = asyncio.Event(), asyncio.Event()

        async def fetch(*_args, **_kwargs):
            entered.set()
            await release.wait()
            return parse_open_meteo(weather_payload(), NOW)

        client = AsyncMock(spec=OpenMeteoClient)
        client.fetch.side_effect = fetch
        pending = asyncio.create_task(manager.refresh_weather(client, NOW, 0))
        await entered.wait()
        data["weather"]["zip_code"] = "00501"
        config = FrigateConfig(**data)
        profiles.update_config(config)
        manager.update_config(config)
        release.set()
        await pending
        self.assertIsNone(manager.weather_status(NOW, 1).data)

    async def test_slow_weather_does_not_block_a_manual_profile_tick(self):
        _, config, _, _, _, manager = self.build(
            weather=True, environment={"enabled": True, "manual_profile": "day"}
        )
        entered = asyncio.Event()
        release = asyncio.Event()

        async def fetch(*_args, **_kwargs):
            entered.set()
            await release.wait()
            return parse_open_meteo(weather_payload(), NOW)

        client = AsyncMock(spec=OpenMeteoClient)
        client.fetch.side_effect = fetch
        pending = asyncio.create_task(manager.refresh_weather(client, NOW, 0))
        await entered.wait()
        await asyncio.wait_for(manager.tick(NOW, 0), 2)
        self.assertEqual(config.cameras["front"].motion.threshold, 20)
        release.set()
        await pending

    async def test_glare_preserves_a_manual_change_and_restores_latest_choice(self):
        data, _, _, profiles, _, manager = self.build(
            environment={
                "enabled": True,
                "manual_profile": "day",
                "glare": {"enabled": True, "profile": "glare", "zones": ["driveway"]},
            }
        )
        self.assertTrue(manager.trigger_glare("front", 10))
        await manager.tick(NOW, 10)
        self.assertEqual(
            manager.camera_status("front").applied_profiles, ("day", "glare")
        )
        data["cameras"]["front"]["environment"]["manual_profile"] = "night"
        changed = FrigateConfig(**data)
        profiles.update_config(changed)
        manager.update_config(changed)
        await manager.tick(NOW, 20)
        self.assertEqual(
            manager.camera_status("front").applied_profiles, ("night", "glare")
        )
        await manager.tick(NOW, 40)
        self.assertEqual(manager.camera_status("front").applied_profiles, ("night",))

    async def test_disabling_automation_clears_worker_layers(self):
        data, _, publisher, profiles, _, manager = self.build(
            environment={"enabled": True, "manual_profile": "day"}
        )
        await manager.tick(NOW, 0)
        data["cameras"]["front"]["environment"]["enabled"] = False
        changed = FrigateConfig(**data)
        profiles.update_config(changed)
        manager.update_config(changed)
        publisher.reset_mock()
        await manager.tick(NOW, 1)
        self.assertEqual(changed.cameras["front"].motion.threshold, 10)
        self.assertTrue(publisher.publish_update.called)
        self.assertEqual(manager.camera_status("front").phase, "disabled")

    async def test_superseded_config_cannot_publish_an_old_plan(self):
        data, _, publisher, profiles, _, manager = self.build(
            environment={"enabled": True, "manual_profile": "day"}
        )
        changed = FrigateConfig(**data)
        profiles.update_config(changed)
        publisher.reset_mock()
        await manager.tick(NOW, 0)
        publisher.publish_update.assert_not_called()

    async def test_invalid_glare_clock_is_logged_and_ignored(self):
        _, _, _, _, _, manager = self.build(
            environment={
                "enabled": True,
                "glare": {"enabled": True, "profile": "glare", "zones": ["driveway"]},
            }
        )
        with self.assertLogs("frigate.environment.runtime", level="WARNING"):
            self.assertFalse(manager.trigger_glare("front", float("nan")))

    async def test_fresh_bound_glare_messages_update_sensor_state_and_expire(self):
        _, config, _, _, _, manager = self.build(
            environment={
                "enabled": True,
                "manual_profile": "day",
                "glare": {"enabled": True, "profile": "glare", "zones": ["driveway"]},
            }
        )
        signal = GlareSignal(
            "front",
            glare_configuration_hash(config.cameras["front"]),
            "glare",
            NOW.timestamp(),
            ("driveway",),
        )
        self.assertTrue(manager.receive_glare_signal(signal, NOW, 0))
        await manager.tick(NOW, 0)
        self.assertEqual(
            manager.camera_status("front", NOW).applied_profiles, ("day", "glare")
        )
        sensor = manager.camera_status("front", NOW).glare
        self.assertIsNotNone(sensor)
        self.assertEqual(sensor.state, "ready")
        self.assertEqual(sensor.zones, ("driveway",))
        self.assertEqual(
            manager.camera_status("front", NOW + timedelta(seconds=16)).glare.state,
            "stale",
        )

    async def test_stale_geometry_and_stale_frames_cannot_trigger_current_profile(self):
        _, config, _, _, _, manager = self.build(
            environment={
                "enabled": True,
                "glare": {"enabled": True, "profile": "glare", "zones": ["driveway"]},
            }
        )
        stale_geometry = GlareSignal(
            "front", "0" * 64, "glare", NOW.timestamp(), ("driveway",)
        )
        self.assertFalse(manager.receive_glare_signal(stale_geometry, NOW, 0))
        stale_frame = GlareSignal(
            "front",
            glare_configuration_hash(config.cameras["front"]),
            "glare",
            NOW.timestamp() - 16,
            ("driveway",),
        )
        with self.assertLogs("frigate.environment.runtime", level="WARNING"):
            self.assertFalse(manager.receive_glare_signal(stale_frame, NOW, 0))

    async def test_known_dawn_transition_does_not_activate_a_glare_lease(self):
        _, config, _, _, _, manager = self.build(
            weather=True,
            environment={
                "enabled": True,
                "manual_profile": "day",
                "glare": {"enabled": True, "profile": "glare", "zones": ["driveway"]},
            },
        )
        dawn = NOW.replace(hour=6, minute=5)
        payload = weather_payload()
        payload["current"]["time"] = dawn.timestamp()
        client = AsyncMock(spec=OpenMeteoClient)
        client.fetch.return_value = parse_open_meteo(payload, dawn)
        await manager.refresh_weather(client, dawn, 0)
        signal = GlareSignal(
            "front",
            glare_configuration_hash(config.cameras["front"]),
            "glare",
            dawn.timestamp(),
            ("driveway",),
        )
        self.assertFalse(manager.receive_glare_signal(signal, dawn, 0))
        await manager.tick(dawn, 0)
        self.assertEqual(
            manager.camera_status("front", dawn).applied_profiles, ("day",)
        )
        self.assertTrue(manager.camera_status("front", dawn).glare.lighting_transition)

    async def test_replay_camera_insertion_does_not_apply_profiles_or_break_tick(self):
        _, config, publisher, _, _, manager = self.build()
        name = f"{REPLAY_CAMERA_PREFIX}front"
        config.cameras[name] = config.cameras["front"].model_copy(deep=True)
        config.cameras[name].name = name
        await manager.tick(NOW, 0)
        self.assertNotIn(name, manager.camera_names())
        publisher.publish_update.assert_not_called()

    async def test_service_can_stop_immediately_without_weather_or_camera_writes(self):
        _, config, publisher, profiles, stop, _ = self.build()

        def handler(_request):
            raise AssertionError("Disabled weather must not perform external requests")

        manager = EnvironmentManager(
            config,
            profiles,
            stop,
            http_client_factory=lambda: httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            ),
        )
        manager.start()
        manager.stop()
        await asyncio.to_thread(manager.join, 5)
        self.assertFalse(manager.is_alive())
        publisher.publish_update.assert_not_called()

    async def test_stopped_service_does_not_report_profile_automation_as_healthy(self):
        _, _, _, _, _, manager = self.build(
            environment={
                "enabled": True,
                "manual_profile": "day",
            }
        )
        await manager.tick(NOW, 0)
        self.assertEqual(manager.camera_status("front", NOW).phase, "applied")
        manager._service_started = True
        state = manager.camera_status("front", NOW)
        self.assertEqual(state.phase, "error")
        self.assertEqual(state.error_code, "environment_service_stopped")
        self.assertEqual(state.applied_profiles, ("day",))
        self.assertFalse(manager.weather_status(NOW, 0).service_running)

    async def test_gui_delete_sentinel_persists_automatic_mode_across_reload(self):
        data, _, _, _, _, _ = self.build(
            environment={
                "enabled": True,
                "manual_profile": "day",
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yml"
            path.write_text(json.dumps(data))
            update_yaml_file_bulk(
                str(path),
                flatten_config_data(
                    {
                        "cameras": {"front": {"environment": {"manual_profile": ""}}},
                    }
                ),
            )
            restored = FrigateConfig.parse_yaml(path.read_text())
        self.assertIsNone(restored.cameras["front"].environment.manual_profile)
        manager = EnvironmentManager(
            restored, ProfileManager(restored, MagicMock()), mp.Event()
        )
        await manager.tick(NOW, 0)
        status = manager.camera_status("front", NOW)
        self.assertEqual(status.desired.reason, "base")
        self.assertEqual(status.applied_profiles, ())


if __name__ == "__main__":
    unittest.main()

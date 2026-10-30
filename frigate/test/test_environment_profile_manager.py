"""Camera-specific layers compose with global profiles without touching other cameras."""

import copy
import unittest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from frigate.api.app import config as config_response
from frigate.config import FrigateConfig
from frigate.config.profile_manager import ProfileManager


class TestEnvironmentalProfileApplication(unittest.TestCase):
    def setUp(self):
        self.data = {
            "mqtt": {"enabled": False},
            "profiles": {
                name: {"friendly_name": name.title()}
                for name in ("away", "day", "rain", "wake", "privacy", "unsafe")
            },
            "cameras": {
                name: {
                    "ffmpeg": {
                        "inputs": [
                            {
                                "path": "rtsp://camera.invalid/test",
                                "roles": ["detect", "record"],
                            }
                        ]
                    },
                    "detect": {"height": 1080, "width": 1920, "fps": 5},
                    "record": {"enabled": True},
                    "motion": {"threshold": 20, "contour_area": 10},
                    "environment": {"enabled": True},
                    "profiles": {
                        "away": {
                            "motion": {"contour_area": 15 if name == "front" else 70}
                        },
                        "day": {"motion": {"threshold": 25}},
                        "rain": {"motion": {"contour_area": 40}},
                        "wake": {
                            "enabled": True,
                            "detect": {"enabled": True},
                            "record": {"enabled": True},
                        },
                        "privacy": {
                            "enabled": False,
                            "detect": {"enabled": False},
                            "record": {"enabled": False},
                        },
                        "unsafe": {"detect": {"fps": 6}},
                    },
                }
                for name in ("front", "back")
            },
        }
        self.config = FrigateConfig(**self.data)
        self.updater = MagicMock()
        self.manager = ProfileManager(self.config, self.updater)
        self.persist = self.enterContext(
            patch.object(self.manager, "_persist_active_profile")
        )

    def test_layers_merge_on_top_of_global_values_without_resetting_other_cameras(self):
        self.assertIsNone(self.manager.activate_profile("away"))
        self.updater.reset_mock()
        self.assertIsNone(self.manager.apply_camera_profiles("front", ("day", "rain")))
        self.assertEqual(self.config.cameras["front"].motion.threshold, 25)
        self.assertEqual(self.config.cameras["front"].motion.contour_area, 40)
        self.assertEqual(self.config.cameras["back"].motion.contour_area, 70)
        self.assertEqual(self.config.active_profile, "away")
        self.assertTrue(self.updater.publish_update.called)
        for call in self.updater.publish_update.call_args_list:
            self.assertTrue(call.args[0].topic.startswith("config/cameras/front/"))

    def test_repeated_unchanged_plan_does_not_republish_every_tick(self):
        self.manager.apply_camera_profiles("front", ("day",))
        self.updater.reset_mock()
        self.assertIsNone(self.manager.apply_camera_profiles("front", ("day",)))
        self.updater.publish_update.assert_not_called()

    def test_clearing_camera_layers_restores_its_global_profile_not_bare_defaults(self):
        self.manager.activate_profile("away")
        self.manager.apply_camera_profiles("front", ("day", "rain"))
        self.assertIsNone(self.manager.apply_camera_profiles("front", ()))
        self.assertEqual(self.config.cameras["front"].motion.threshold, 20)
        self.assertEqual(self.config.cameras["front"].motion.contour_area, 15)
        self.assertEqual(self.config.cameras["back"].motion.contour_area, 70)
        self.assertFalse(self.manager.has_camera_profiles("front"))

    def test_environmental_profiles_do_not_reenable_globally_disabled_services(self):
        self.manager.activate_profile("privacy")
        self.assertIsNone(self.manager.apply_camera_profiles("front", ("wake",)))
        self.assertFalse(self.config.cameras["front"].enabled)
        self.assertFalse(self.config.cameras["front"].detect.enabled)
        self.assertFalse(self.config.cameras["front"].record.enabled)
        blocked = self.manager.get_camera_profile_info("front")["blocked_fields"]
        self.assertTrue(
            {"enabled", "detect.enabled", "record.enabled"}.issubset(blocked)
        )

    def test_runtime_human_toggles_are_reapplied_without_clearing_them(self):
        dispatcher = MagicMock()
        dispatcher.reapply_runtime_state_to_config.side_effect = lambda: setattr(
            self.config.cameras["front"].detect, "enabled", False
        )
        self.manager.dispatcher = dispatcher
        self.manager.apply_camera_profiles("front", ("wake",))
        self.assertFalse(self.config.cameras["front"].detect.enabled)
        dispatcher.clear_runtime_state.assert_not_called()
        dispatcher.reapply_runtime_state_to_config.assert_called_once()

    def test_unknown_or_restart_required_profiles_do_not_change_configuration(self):
        for name in ("missing", "unsafe"):
            self.updater.reset_mock()
            self.assertIsNotNone(self.manager.apply_camera_profiles("front", (name,)))
            self.assertEqual(self.config.cameras["front"].motion.threshold, 20)
            self.assertEqual(self.config.cameras["front"].detect.fps, 5)
            self.updater.publish_update.assert_not_called()

    def test_automation_must_be_explicitly_enabled(self):
        self.config.cameras["front"].environment.enabled = False
        self.assertIsNotNone(self.manager.apply_camera_profiles("front", ("day",)))
        self.updater.publish_update.assert_not_called()

    def test_reload_rebases_layers_and_forces_a_new_worker_publication(self):
        self.manager.apply_camera_profiles("front", ("day", "rain"))
        changed = copy.deepcopy(self.data)
        changed["cameras"]["front"]["profiles"]["day"]["motion"]["threshold"] = 33
        replacement = FrigateConfig(**changed)
        self.updater.reset_mock()
        self.manager.update_config(replacement)
        self.assertEqual(replacement.cameras["front"].motion.threshold, 33)
        self.assertEqual(replacement.cameras["front"].motion.contour_area, 40)
        self.updater.publish_update.assert_not_called()
        self.manager.apply_camera_profiles("front", ("day", "rain"))
        self.assertTrue(self.updater.publish_update.called)

    def test_config_api_preserves_base_settings_when_only_camera_layers_are_active(
        self,
    ):
        self.manager.apply_camera_profiles("front", ("day",))
        app = FastAPI()
        app.frigate_config = self.config
        app.profile_manager = self.manager
        app.get("/config")(config_response)
        with TestClient(app) as client:
            response = client.get("/config", headers={"remote-role": "admin"})
        self.assertEqual(response.status_code, 200)
        camera = response.json()["cameras"]["front"]
        self.assertEqual(camera["motion"]["threshold"], 25)
        self.assertEqual(camera["base_config"]["motion"]["threshold"], 20)


if __name__ == "__main__":
    unittest.main()

"""Plugin admin gates and camera provisioning against the real Linux API."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


@unittest.skipIf(
    sys.platform == "win32", "Full Frigate API requires Linux dependencies"
)
class TestPluginsHttp(unittest.TestCase):
    def setUp(self):
        from fastapi import Depends, FastAPI
        from fastapi.testclient import TestClient

        from frigate.api.auth import require_admin_by_default
        from frigate.api.plugins import router
        from frigate.plugins.manager import PluginManager

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.app = FastAPI(dependencies=[Depends(require_admin_by_default())])
        self.app.frigate_config = SimpleNamespace(
            cameras={},
            auth=SimpleNamespace(roles={"admin": [], "viewer": []}),
            proxy=SimpleNamespace(separator=","),
        )
        self.owner = PluginManager(
            Path(self.temporary.name), lambda: self.app.frigate_config, MagicMock()
        )
        self.app.state.plugins = self.owner
        self.app.include_router(router)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.admin = {"remote-user": "admin", "remote-role": "admin"}

    def test_anonymous_and_viewers_cannot_install_or_read_plugin_state(self):
        for headers in ({}, {"remote-user": "viewer", "remote-role": "viewer"}):
            for method, url, body in (
                ("GET", "/plugins", None),
                ("POST", "/plugins/install", {"bundled_ring": True}),
                ("PUT", "/plugins/developer-mode", {"enabled": True}),
                ("POST", "/plugins/ring/actions", {"method": "authenticate"}),
            ):
                with self.subTest(url=url, headers=headers):
                    response = self.client.request(
                        method, url, headers=headers, json=body
                    )
                    self.assertIn(response.status_code, (401, 403))

    def test_catalog_install_never_implicitly_enables_a_worker(self):
        result = self.client.post(
            "/plugins/install", json={"bundled_ring": True}, headers=self.admin
        )
        self.assertEqual(result.status_code, 200)
        status = self.client.get("/plugins", headers=self.admin).json()
        self.assertFalse(status["installed"][0]["enabled"])
        self.assertEqual(self.owner.workers, {})

    def test_developer_mode_requires_code_execution_acknowledgement(self):
        response = self.client.put(
            "/plugins/developer-mode", json={"enabled": True}, headers=self.admin
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.owner.settings.developer_mode)

    def test_missing_service_is_an_explicit_failure(self):
        self.app.state.plugins_error = "invalid_plugin_state"
        response = self.client.get("/plugins", headers=self.admin)
        self.assertEqual(response.status_code, 503)

    def test_cloud_acknowledgement_is_required(self):
        response = self.client.post(
            "/plugins/ring/cameras",
            json={
                "device_id": "1",
                "camera": "ring_front",
                "acknowledge_cloud_gaps": False,
            },
            headers=self.admin,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["error"], "cloud_recording_acknowledgement_required"
        )

    def test_uninstall_does_not_touch_recordings(self):
        response = self.client.delete("/plugins/missing", headers=self.admin)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "plugin_not_installed")

    def test_camera_creation_uses_new_camera_precondition_and_preserves_global_settings(
        self,
    ):
        from fastapi.responses import JSONResponse

        from frigate.plugins.models import PluginDevice, PluginEntry, PluginManifest

        manifest = PluginManifest.model_validate_json(
            (
                Path(__file__).parents[2] / "plugins" / "ring" / "plugin.json"
            ).read_bytes()
        )
        self.owner.settings.plugins["ring"] = PluginEntry(
            manifest=manifest,
            enabled=True,
            digest="0" * 64,
        )
        self.owner.devices["ring"] = {
            "1": PluginDevice(id="1", name="Door", source="ring:?private"),
        }
        with (
            patch.object(self.owner, "_register", AsyncMock()),
            patch(
                "frigate.api.app.config_set",
                return_value=JSONResponse({"success": True}),
            ) as save,
        ):
            response = self.client.post(
                "/plugins/ring/cameras",
                json={
                    "device_id": "1",
                    "camera": "ring_front",
                    "acknowledge_cloud_gaps": True,
                },
                headers=self.admin,
            )
        self.assertEqual(response.status_code, 200)
        body = save.call_args.args[1]
        self.assertEqual(body.expected_absent_cameras, ["ring_front"])
        self.assertEqual(set(body.config_data), {"cameras"})
        camera = body.config_data["cameras"]["ring_front"]
        self.assertFalse(camera["detect"]["enabled"])
        self.assertFalse(camera["motion"]["enabled"])
        self.assertTrue(camera["record"]["enabled"])
        self.assertEqual(
            camera["ffmpeg"]["output_args"]["record"],
            "preset-record-generic-audio-copy",
        )
        self.assertNotIn("private", json.dumps(body.config_data))

    def _use_real_config(self):
        from ruamel.yaml import YAML

        from frigate.api import app as main_app
        from frigate.config import FrigateConfig

        raw = {
            "mqtt": {"enabled": False},
            "cameras": {
                "existing": {
                    "ffmpeg": {
                        "inputs": [
                            {
                                "path": "rtsp://camera.invalid/live",
                                "roles": ["detect"],
                            }
                        ]
                    },
                    "detect": {"width": 1280, "height": 720, "fps": 5},
                },
            },
        }
        path = self.owner.root / "config.yml"
        with path.open("w") as output:
            YAML().dump(raw, output)
        self.app.frigate_config = FrigateConfig.parse(path.read_text())
        self.app.config_holder = None
        self.app.genai_manager = MagicMock()
        self.app.profile_manager = None
        self.app.stats_emitter = None
        self.app.dispatcher = None
        self.app.storage_maintainer = None
        self.app.config_publisher = MagicMock()
        self.app.include_router(main_app.router)
        return path, raw

    def test_real_config_creation_keeps_existing_camera_and_scopes_decoder_off(self):
        from ruamel.yaml import YAML

        from frigate.motion import record_only
        from frigate.plugins.models import PluginDevice, PluginEntry, PluginManifest

        path, original = self._use_real_config()
        manifest = PluginManifest.model_validate_json(
            (
                Path(__file__).parents[2] / "plugins" / "ring" / "plugin.json"
            ).read_bytes()
        )
        self.owner.settings.plugins["ring"] = PluginEntry(
            manifest=manifest,
            enabled=True,
            digest="0" * 64,
        )
        self.owner.devices["ring"] = {
            "1": PluginDevice(id="1", name="Door", source="ring:?private"),
        }
        with (
            patch.object(self.owner, "_register", AsyncMock()),
            patch("frigate.api.app.find_config_file", return_value=str(path)),
            patch.object(
                record_only,
                "PLUGIN_MODES_PATH",
                self.owner.root / "recording-only.json",
            ),
            patch.object(record_only, "configuration", return_value=None),
        ):
            response = self.client.post(
                "/plugins/ring/cameras",
                json={
                    "device_id": "1",
                    "camera": "ring_front",
                    "acknowledge_cloud_gaps": True,
                },
                headers=self.admin,
            )
            self.assertEqual(response.status_code, 200, response.text)
            saved = YAML(typ="safe").load(path.read_text())
            self.assertEqual(
                saved["cameras"]["existing"], original["cameras"]["existing"]
            )
            self.assertEqual(saved["mqtt"], original["mqtt"])
            self.assertEqual(set(saved["cameras"]), {"existing", "ring_front"})
            self.assertNotIn("private", path.read_text())
            self.assertTrue(record_only.recording_only("ring_front"))
            self.assertFalse(record_only.recording_only("existing"))
            commands = record_only.recording_only_commands(
                self.app.frigate_config.cameras["ring_front"]
            )
            self.assertTrue(commands)
            for command in commands:
                self.assertNotIn("detect", command["roles"])
                self.assertNotIn("pipe:", command["cmd"])

    def test_existing_camera_precondition_rejects_without_writing_or_publishing(self):
        path, original = self._use_real_config()
        before = path.read_bytes()
        with patch("frigate.api.app.find_config_file", return_value=str(path)):
            response = self.client.put(
                "/config/set",
                json={
                    "requires_restart": 0,
                    "expected_absent_cameras": ["existing"],
                    "config_data": {
                        "cameras": {"existing": original["cameras"]["existing"]}
                    },
                },
                headers=self.admin,
            )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(path.read_bytes(), before)
        self.app.config_publisher.publish_update.assert_not_called()

    def test_enabling_motion_ai_preserves_doorbell_and_adds_object_review_labels(self):
        for global_labels, camera_labels in (
            (None, None),
            (None, ["motion"]),
            (["car"], None),
            (["car"], ["motion"]),
        ):
            with self.subTest(global_labels=global_labels, camera_labels=camera_labels):
                self._assert_motion_ai_review_labels(global_labels, camera_labels)

    def _assert_motion_ai_review_labels(self, global_labels, camera_labels):
        from ruamel.yaml import YAML

        from frigate.config import FrigateConfig
        from frigate.plugins import inference
        from frigate.plugins.models import PluginEntry, PluginManifest

        path, original = self._use_real_config()
        original["cameras"]["existing"]["review"] = {"alerts": {"labels": ["doorbell"]}}
        original["review"] = {"detections": {"labels": global_labels}}
        if camera_labels is not None or global_labels is not None:
            original["cameras"]["existing"]["review"]["detections"] = {
                "labels": camera_labels
            }
        with path.open("w") as output:
            YAML().dump(original, output)
        self.app.frigate_config = FrigateConfig.parse(path.read_text())
        manifest = PluginManifest.model_validate_json(
            (
                Path(__file__).parents[2] / "plugins" / "ring" / "plugin.json"
            ).read_bytes()
        )
        self.owner.settings.plugins["ring"] = PluginEntry(
            manifest=manifest,
            enabled=True,
            digest="0" * 64,
            bindings={"1": "existing"},
        )
        with (
            patch(
                "frigate.motion.record_only.plugin_recording_only", return_value=True
            ),
            patch("frigate.api.app.find_config_file", return_value=str(path)),
            patch.object(inference, "LEASE_PATH", self.owner.root / "leases.json"),
        ):
            response = self.client.put(
                "/plugins/ring/cameras/existing/event-inference",
                json={"enabled": True, "window_seconds": 30, "triggers": ["motion"]},
                headers=self.admin,
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["enabled"])
        self.assertFalse(response.json()["active"])
        self.assertEqual(response.json()["window_seconds"], 30)
        saved = YAML(typ="safe").load(path.read_text())
        self.assertIn(
            "doorbell", saved["cameras"]["existing"]["review"]["alerts"]["labels"]
        )
        self.assertIn(
            "person", saved["cameras"]["existing"]["review"]["alerts"]["labels"]
        )
        actual_labels = self.app.frigate_config.cameras[
            "existing"
        ].review.detections.labels
        if camera_labels is None:
            self.assertIsNone(actual_labels)
        else:
            expected_labels = set(camera_labels) | set(
                self.app.frigate_config.cameras["existing"].objects.track
                if global_labels is None
                else global_labels
            )
            self.assertEqual(set(actual_labels), expected_labels)
        self.assertEqual(
            saved["cameras"]["existing"]["ffmpeg"],
            original["cameras"]["existing"]["ffmpeg"],
        )
        self.assertEqual(saved["mqtt"], original["mqtt"])

    def test_motion_ai_cannot_target_an_unbound_camera(self):
        response = self.client.put(
            "/plugins/ring/cameras/existing/event-inference",
            json={"enabled": True, "window_seconds": 30},
            headers=self.admin,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "camera_not_bound")

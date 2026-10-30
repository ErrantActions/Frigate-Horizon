"""Atomic camera-setting updates without production data or migrations."""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from ruamel.yaml import YAML

from frigate.api.app import router
from frigate.api.auth import get_current_user
from frigate.config import FrigateConfig


class TestCameraCopyUpdates(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "config.yml"
        camera = {
            "ffmpeg": {
                "inputs": [{"path": "rtsp://127.0.0.1/fixture", "roles": ["detect"]}]
            },
            "detect": {"width": 640, "height": 360, "fps": 5},
        }
        data = {
            "mqtt": {"enabled": False},
            "cameras": {
                "front_door": copy.deepcopy(camera),
                "back_yard": copy.deepcopy(camera),
            },
        }
        with self.path.open("w") as output:
            YAML().dump(data, output)
        self.original = self.path.read_bytes()
        self.config = FrigateConfig(**data)
        self.app = FastAPI()
        self.app.frigate_config = self.config
        self.app.config_holder = None
        self.app.storage_maintainer = None
        self.app.dispatcher = None
        self.app.profile_manager = None
        self.app.stats_emitter = None
        self.app.genai_manager = MagicMock()
        self.app.config_publisher = MagicMock()

        async def user(request: Request):
            return {
                "username": "fixture",
                "role": request.headers.get("remote-role", "viewer"),
            }

        self.app.dependency_overrides[get_current_user] = user
        self.app.include_router(router)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        for target, value in (
            ("frigate.api.app.find_config_file", str(self.path)),
            ("frigate.api.app.sync_transcode_streams", True),
        ):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.body = {
            "requires_restart": 0,
            "update_topics": [
                "config/cameras/front_door/motion",
                "config/cameras/back_yard/motion",
            ],
            "config_data": {
                "cameras": {
                    "front_door": {"motion": {"threshold": 22}},
                    "back_yard": {"motion": {"threshold": 22}},
                }
            },
        }

    def put(self, body=None, role="admin"):
        """Submit a fixture update through the real authenticated route."""
        return self.client.put(
            "/config/set",
            headers={"remote-role": role},
            json=self.body if body is None else body,
        )

    def test_updates_all_cameras_and_publishes_each_topic_once(self):
        self.body["update_topics"].append(self.body["update_topics"][0])
        response = self.put()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        with self.path.open() as source:
            saved = YAML(typ="safe").load(source)
        for name in ("front_door", "back_yard"):
            self.assertEqual(saved["cameras"][name]["motion"]["threshold"], 22)
            self.assertEqual(self.app.frigate_config.cameras[name].motion.threshold, 22)
        topics = [
            call.args[0].topic
            for call in self.app.config_publisher.publish_update.call_args_list
        ]
        self.assertEqual(topics, self.body["update_topics"][:2])
        self.assertEqual(list(self.path.parent.glob(".frigate-config-*")), [])

    def test_one_invalid_target_leaves_every_camera_unchanged(self):
        target = self.body["config_data"]["cameras"]["back_yard"]
        target["motion"] = {"threshold": "not-a-number"}
        with self.assertLogs("frigate.api.app", level="WARNING"):
            response = self.put()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertIs(self.app.frigate_config, self.config)
        self.app.config_publisher.publish_update.assert_not_called()

    def test_invalid_and_lifecycle_topics_do_not_write_config(self):
        for topic in (
            "config/cameras/absent/motion",
            "config/cameras/front_door/not_a_section",
            "config/cameras/front_door/remove",
            "config/storage",
            "",
        ):
            with self.subTest(topic=topic):
                self.body["update_topics"] = [topic]
                with self.assertLogs("frigate.api.app", level="WARNING"):
                    response = self.put()
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.path.read_bytes(), self.original)
        self.app.config_publisher.publish_update.assert_not_called()

    def test_batch_cannot_be_combined_with_legacy_or_in_memory_updates(self):
        for field, value in (
            ("update_topic", "config/cameras/front_door/motion"),
            ("skip_save", True),
        ):
            with self.subTest(field=field):
                body = {**self.body, field: value}
                self.assertEqual(self.put(body).status_code, 422)
                self.assertEqual(self.path.read_bytes(), self.original)

    def test_viewer_cannot_copy_camera_settings(self):
        self.assertEqual(self.put(role="viewer").status_code, 403)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.app.config_publisher.publish_update.assert_not_called()

    def test_commit_failure_keeps_original_and_reports_failure(self):
        with (
            patch("frigate.util.config.os.replace", side_effect=OSError("fixture")),
            self.assertLogs("frigate.api.app", level="ERROR"),
        ):
            response = self.put()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(list(self.path.parent.glob(".frigate-config-*")), [])
        self.app.config_publisher.publish_update.assert_not_called()

    def test_legacy_single_topic_update_still_works(self):
        body = copy.deepcopy(self.body)
        body.pop("update_topics")
        body["update_topic"] = "config/cameras/front_door/motion"
        body["config_data"]["cameras"].pop("back_yard")
        self.assertEqual(self.put(body).status_code, 200)
        self.app.config_publisher.publish_update.assert_called_once()

    def test_raw_config_save_has_the_request_context_for_archive_guards(self):
        response = self.client.post(
            "/config/save?save_option=saveonly",
            headers={"remote-role": "admin", "content-type": "text/plain"},
            content=self.path.read_bytes(),
        )
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()

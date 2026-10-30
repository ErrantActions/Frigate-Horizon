"""Camera authorization and explicit readiness for stationary-object diagnostics."""

import copy
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from frigate.api.camera import router
from frigate.api.media import router as media_router
from frigate.config import FrigateConfig


class TestStationaryObjectsAPI(unittest.TestCase):
    def setUp(self):
        camera = {
            "ffmpeg": {
                "inputs": [{"path": "rtsp://example.test/video", "roles": ["detect"]}]
            },
            "detect": {"enabled": True, "width": 640, "height": 360, "fps": 5},
        }
        self.config = FrigateConfig(
            **{
                "mqtt": {"enabled": False},
                "auth": {"roles": {"restricted": ["front_door"]}},
                "cameras": {
                    "front_door": copy.deepcopy(camera),
                    "back_yard": copy.deepcopy(camera),
                },
            }
        )
        self.snapshot = {
            "frame_time": 99.0,
            "total": 1,
            "objects": [
                {
                    "id": "80-car",
                    "label": "car",
                    "box": (10, 20, 100, 150),
                    "score": 0.85,
                    "stationary_frames": 75,
                    "position_changes": 0,
                    "tracked_seconds": 19,
                    "last_tracking_update": 98,
                    "updated_in_current_frame": False,
                }
            ],
        }
        self.state = SimpleNamespace(
            get_stationary_objects=Mock(return_value=self.snapshot)
        )
        self.processor = Mock()
        self.processor.get_camera_state.return_value = self.state
        self.app = FastAPI()
        self.app.frigate_config = self.config
        self.app.detected_frames_processor = self.processor
        self.app.include_router(router)
        self.app.include_router(media_router)
        self.client = self.enterContext(TestClient(self.app))

        async def user(request):
            role = request.headers.get("remote-role")
            if not role:
                return JSONResponse(
                    {"message": "Authentication required"}, status_code=401
                )
            return {"username": "fixture", "role": role}

        self.enterContext(patch("frigate.api.auth.get_current_user", user))
        self.enterContext(patch("frigate.api.camera.time.time", return_value=100))

    def get(self, camera="front_door", role="admin"):
        """Read a fixture camera with the real camera-access dependency."""
        return self.client.get(
            f"/{camera}/stationary-objects",
            headers={"remote-role": role} if role else {},
        )

    def test_authorized_snapshot_is_bounded_read_only_and_noncacheable(self):
        before = copy.deepcopy(self.snapshot)
        response = self.get()
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "ready")
        self.assertEqual(data["frame_age_seconds"], 1)
        self.assertEqual(data["frame_width"], 640)
        self.assertEqual(data["limit"], 100)
        self.assertEqual(data["objects"][0]["id"], "80-car")
        self.assertFalse(data["objects"][0]["updated_in_current_frame"])
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertEqual(before, self.snapshot)

    def test_camera_acl_denies_access_before_reading_any_tracker_state(self):
        response = self.get("back_yard", "restricted")
        self.assertEqual(response.status_code, 403)
        self.processor.get_camera_state.assert_not_called()
        self.assertEqual(self.get(role="restricted").status_code, 200)

    def test_anonymous_requests_do_not_read_tracker_state(self):
        self.assertEqual(self.get(role=None).status_code, 401)
        self.processor.get_camera_state.assert_not_called()

    def test_unknown_camera_is_not_an_empty_success_response(self):
        self.assertEqual(self.get("missing").status_code, 404)

    def test_stale_camera_retains_last_known_tracks_with_an_explicit_status(self):
        self.state.get_stationary_objects.return_value = {
            **self.snapshot,
            "frame_time": 50,
        }
        data = self.get().json()
        self.assertEqual(data["status"], "stale")
        self.assertEqual(data["frame_age_seconds"], 50)
        self.assertEqual(data["total"], 1)

    def test_missing_state_and_missing_frame_are_explicitly_warming_up(self):
        self.processor.get_camera_state.return_value = None
        self.assertEqual(self.get().json()["status"], "warming_up")
        self.processor.get_camera_state.return_value = self.state
        self.state.get_stationary_objects.return_value = {
            "frame_time": None,
            "total": 0,
            "objects": [],
        }
        self.assertEqual(self.get().json()["status"], "warming_up")

    def test_disabled_camera_and_detection_do_not_read_tracker_state(self):
        camera = self.config.cameras["front_door"]
        camera.enabled = False
        self.assertEqual(self.get().json()["status"], "disabled")
        camera.enabled = True
        camera.detect.enabled = False
        self.assertEqual(self.get().json()["status"], "detection_disabled")
        self.processor.get_camera_state.assert_not_called()

    def test_stationary_image_does_not_substitute_an_unannotated_preview(self):
        self.processor.get_current_frame.return_value = None
        with patch("frigate.api.media.get_most_recent_preview_frame") as preview:
            response = self.client.get(
                "/front_door/latest.jpg?stationary=true",
                headers={"remote-role": "admin"},
            )
        self.assertEqual(response.status_code, 503)
        self.assertIn("no-store", response.headers["cache-control"])
        preview.assert_not_called()

    def test_stationary_image_is_noncacheable_even_when_store_is_requested(self):
        self.processor.get_current_frame.return_value = np.zeros(
            (360, 640, 3), np.uint8
        )
        self.processor.get_current_frame_time.return_value = 99
        clock = SimpleNamespace(now=lambda: SimpleNamespace(timestamp=lambda: 100))
        with patch("frigate.api.media.datetime", clock):
            response = self.client.get(
                "/front_door/latest.jpg?stationary=true&store=1&height=180",
                headers={"remote-role": "admin"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/jpeg")
        self.assertIn("no-store", response.headers["cache-control"])
        options = self.processor.get_current_frame.call_args.args[1]
        self.assertTrue(options["bounding_boxes"])
        self.assertTrue(options["stationary_only"])

    def test_live_stationary_options_are_authorized_bounded_and_noncacheable(self):
        with patch(
            "frigate.api.media.imagestream", return_value=iter([b"frame"])
        ) as feed:
            response = self.client.get(
                "/front_door?stationary=true&fps=5&height=640",
                headers={"remote-role": "admin"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn("no-store", response.headers["cache-control"])
            options = feed.call_args.args[4]
            self.assertTrue(options["stationary_only"])
            self.assertTrue(options["bounding_boxes"])
            self.assertGreaterEqual(options["max_frame_age"], 5)
            self.assertEqual(feed.call_args.args[2:4], (5, 640))
            feed.reset_mock()
            response = self.client.get(
                "/back_yard?stationary=true", headers={"remote-role": "restricted"}
            )
            self.assertEqual(response.status_code, 403)
            feed.assert_not_called()
            self.assertEqual(
                self.client.get("/front_door?stationary=true").status_code, 401
            )
            for query in ("fps=0", "fps=6", "height=0", "height=1081"):
                with self.subTest(query=query):
                    response = self.client.get(
                        f"/front_door?stationary=true&{query}",
                        headers={"remote-role": "admin"},
                    )
                    self.assertEqual(response.status_code, 422)
            feed.assert_not_called()

    def test_normal_live_detection_boxes_remain_available_without_stationary_filter(
        self,
    ):
        with patch(
            "frigate.api.media.imagestream", return_value=iter([b"frame"])
        ) as feed:
            response = self.client.get(
                "/front_door?bbox=1&fps=5&height=640",
                headers={"remote-role": "admin"},
            )
        self.assertEqual(response.status_code, 200)
        options = feed.call_args.args[4]
        self.assertTrue(options["bounding_boxes"])
        self.assertFalse(options["stationary_only"])
        self.assertNotIn("max_frame_age", options)

    def test_stationary_stream_stops_instead_of_repeating_stale_boxes_or_black_frames(
        self,
    ):
        from frigate.api.media import imagestream

        options = {
            "stationary_only": True,
            "bounding_boxes": True,
            "max_frame_age": 10,
        }
        self.processor.get_current_frame.return_value = np.zeros(
            (360, 640, 3), np.uint8
        )
        self.processor.get_current_frame_time.return_value = 99
        with (
            patch("frigate.api.media.time.sleep"),
            patch("frigate.api.media.time.time", return_value=100),
        ):
            feed = imagestream(self.processor, "front_door", 5, 180, options)
            self.assertIn(b"Content-Type: image/jpeg", next(feed))
            self.processor.get_current_frame_time.return_value = 50
            with self.assertRaises(StopIteration):
                next(feed)
            self.processor.get_current_frame.return_value = None
            with self.assertRaises(StopIteration):
                next(imagestream(self.processor, "front_door", 5, 180, options))

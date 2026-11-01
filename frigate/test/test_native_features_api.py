"""Native namespace authorization uses real sessions and fake-only camera services."""

import os
import unittest
from unittest.mock import MagicMock
from uuid import uuid4


@unittest.skipIf(os.name == "nt", "Full API imports require Linux")
class TestNativeFeaturesAPI(unittest.TestCase):
    def setUp(self):
        from frigate.api import native
        from frigate.models import User
        from frigate.test import test_native_push_images as image_tests

        self.native = native
        self.User = User
        self.fixture = image_tests.TestNativeImageAPI()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.client = self.fixture.client
        self.client.app.include_router(native.router)
        self.headers = {**self.fixture.headers, "remote-role": "front_only"}
        self.controller = MagicMock()
        self.controller.cams = {}
        self.client.app.onvif = self.controller
        native.initialize_native_controls(self.client.app)
        self.controls = self.client.app.state.native_controls
        self.client.app.state.native_export_protection = False
        self.enterContext(self.client)

        async def close():
            await self.controls.close()

        self.addCleanup(self.client.portal.call, close)

    def test_features_are_available_to_real_non_admin_sessions_but_not_headers_alone(
        self,
    ):
        response = self.client.get("/native/features", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json(),
            {
                "controls": True,
                "export_protection": False,
                "live_webrtc": {
                    "version": 1,
                    "enabled": False,
                    "available": False,
                    "status_url_template": "{camera_name}/live/webrtc/status",
                    "signaling": "frigate-rest-offer-answer-v1",
                    "on_demand": True,
                    "recordings": "original_source",
                },
                "recorded_webrtc": {
                    "version": 1,
                    "enabled": False,
                    "available": False,
                    "status_url_template": "{camera_name}/recordings/webrtc/status",
                    "signaling": "frigate-recorded-offer-answer-v1",
                    "on_demand": True,
                    "originals": "unchanged",
                    "max_window_seconds": 30,
                    "pause": "release_encoder",
                    "seek": "new_peer_generation",
                    "resume": "new_peer_generation",
                    "position_kind": "input_read_cursor",
                    "gap_policy": "skip",
                    "activation": "decoded_ready_control",
                    "warmup_max_seconds": 20,
                },
            },
        )
        response = self.client.get(
            "/native/features",
            headers={"remote-user": "alice", "remote-role": "front_only"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.controller.mock_calls, [])

    def test_namespace_exemption_never_allows_missing_or_forwarded_only_sessions(self):
        lease = str(uuid4())
        requests = (
            ("GET", "/native/features", None),
            ("GET", "/native/cameras/front/controls", None),
            (
                "POST",
                "/native/cameras/front/ptz",
                {"command": "MOVE_UP", "lease_id": lease, "duration_ms": 500},
            ),
        )
        for headers in ({}, {"remote-user": "alice", "remote-role": "admin"}):
            for method, path, body in requests:
                with self.subTest(method=method, path=path, forwarded=bool(headers)):
                    response = self.client.request(
                        method, path, headers=headers, json=body
                    )
                    self.assertIn(response.status_code, (401, 403), response.text)
        self.assertEqual(self.controller.mock_calls, [])

    def test_non_admin_controls_are_scoped_unsupported_and_never_discover_hardware(
        self,
    ):
        response = self.client.get(
            "/native/cameras/front/controls", headers=self.headers
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["ptz"]["supported"])
        self.assertFalse(response.json()["talkback"]["supported"])
        response = self.client.get(
            "/native/cameras/garden/controls", headers=self.headers
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.controller.mock_calls, [])

    def test_writes_still_require_current_database_admin_before_fake_transport(self):
        lease = str(uuid4())
        requests = (
            (
                "/native/cameras/front/ptz",
                {"command": "MOVE_UP", "lease_id": lease, "duration_ms": 500},
            ),
        )
        for role in ("front_only", "viewer"):
            self.User.update(role=role).where(self.User.username == "alice").execute()
            for headers in (self.headers, self.fixture.headers):
                for path, body in requests:
                    with self.subTest(
                        role=role, forwarded_role=headers["remote-role"], path=path
                    ):
                        response = self.client.post(path, headers=headers, json=body)
                        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.controller.mock_calls, [])

    def test_talkback_is_not_registered_or_initialized(self):
        self.assertFalse(hasattr(self.client.app.state, "native_talkback"))
        self.assertFalse(
            any(
                "talkback" in getattr(route, "path", "")
                for route in self.client.app.routes
            )
        )

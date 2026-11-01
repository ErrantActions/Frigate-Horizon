"""Real account, JWT, camera ownership, and sanitized WebRTC endpoint contracts."""

import time
import unittest
from pathlib import Path
from uuid import uuid4

from joserfc import jwt

from frigate.api.live_webrtc import router
from frigate.comms.live_webrtc import LiveWebRTCManager
from frigate.config.live_webrtc import LiveWebRTCConfig
from frigate.models import User
from frigate.test.test_live_webrtc import FakeTransport
from frigate.test.test_native_push_api import NativePushAPITestCase
from frigate.util.ovenmediaengine_bootstrap import generate_runtime


class TestLiveWebRTCAPI(unittest.TestCase):
    def setUp(self):
        self.fixture = NativePushAPITestCase()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.client = self.fixture.client
        self.headers = self.fixture.headers
        self.config = self.fixture.config
        self.config.live_webrtc = LiveWebRTCConfig(
            enabled=True, advertised_hosts=["10.1.1.4"]
        )
        self.config.go2rtc.streams = {
            "front": ["rtsp://synthetic.invalid/front"],
            "garden": ["rtsp://synthetic.invalid/garden"],
        }
        runtime = self.fixture.directory.name
        generate_runtime(self.config.live_webrtc, Path(runtime))
        self.transport = FakeTransport()
        self.manager = LiveWebRTCManager(
            lambda: self.config, runtime=Path(runtime), transport=self.transport
        )
        self.client.app.state.live_webrtc_manager = self.manager
        self.client.app.include_router(router)
        self.enterContext(self.client)
        self.client.portal.call(self.manager.start)
        self.addCleanup(self.client.portal.call, self.manager.close)
        self.base = "/front/live/webrtc"

    def create(self):
        response = self.client.post(
            self.base + "/sessions", headers=self.headers, json={"view": "detail"}
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_discovery_does_not_start_a_source_or_disclose_private_credentials(self):
        response = self.client.get(self.base + "/status", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["available"])
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertEqual(self.transport.created, [])
        self.assertNotIn("api_token", response.text)
        self.assertNotIn("turn_secret", response.text)

    def test_anonymous_forwarded_only_and_camera_denied_never_start_media(self):
        for headers in ({}, {"remote-user": "alice", "remote-role": "admin"}):
            for method, suffix, body in (
                ("GET", "/status", None),
                ("POST", "/sessions", {"view": "grid"}),
            ):
                response = self.client.request(
                    method, self.base + suffix, headers=headers, json=body
                )
                self.assertIn(response.status_code, (401, 403), response.text)
        response = self.client.post(
            "/garden/live/webrtc/sessions", headers=self.headers, json={}
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.transport.created, [])

    def test_owned_offer_answer_quality_heartbeat_and_last_viewer_cleanup(self):
        session = self.create()
        identifier = session["session_id"]
        base = f"{self.base}/sessions/{identifier}"
        self.assertEqual(session["answer_url"], base.removeprefix("/") + "/answer")
        self.assertEqual(
            self.client.post(
                base + "/answer",
                headers=self.headers,
                json={"type": "answer", "sdp": "v=0\r\nsynthetic-answer\r\n"},
            ).status_code,
            204,
        )
        self.assertEqual(
            self.client.patch(
                base + "/quality", headers=self.headers, json={"height": 720}
            ).status_code,
            204,
        )
        response = self.client.patch(base, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertLessEqual(response.json()["expires_at"], time.time() + 31)
        self.assertEqual(
            self.client.delete(base, headers=self.headers).status_code, 204
        )
        self.assertEqual(len(self.transport.deleted), 1)
        self.assertEqual(self.manager.sessions, {})

    def test_session_id_cannot_cross_accounts_or_cameras(self):
        session = self.create()
        identifier = session["session_id"]
        encoded = jwt.encode(
            {"alg": "HS256"},
            {
                "sub": "bob",
                "role": "admin",
                "iat": time.time() - 10,
                "exp": time.time() + 3600,
            },
            self.client.app.jwt_token,
        )
        bob = {
            "remote-user": "bob",
            "remote-role": "admin",
            "Authorization": f"Bearer {encoded}",
        }
        for method, suffix, body in (
            ("POST", "/answer", {"sdp": "v=0\r\nsynthetic-answer\r\n"}),
            ("POST", "/candidates", {"candidates": []}),
            ("PATCH", "/quality", {"height": 720}),
            ("PATCH", "", None),
            ("DELETE", "", None),
        ):
            for camera in ("front", "garden"):
                response = self.client.request(
                    method,
                    f"/{camera}/live/webrtc/sessions/{identifier}{suffix}",
                    headers=bob,
                    json=body,
                )
                self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(len(self.manager.sessions), 1)

    def test_invalid_sources_uuids_and_sdp_are_rejected_without_echoing_inputs(self):
        session = self.create()
        base = f"{self.base}/sessions/{session['session_id']}"
        marker = "sensitive-invalid-input-" + "x" * 128001
        response = self.client.post(
            base + "/answer", headers=self.headers, json={"sdp": marker}
        )
        self.assertEqual(response.status_code, 422, response.text[:1000])
        self.assertNotIn("sensitive-invalid-input", response.text)
        response = self.client.post(
            self.base + "/sessions",
            headers=self.headers,
            json={"source": "rtsp://sensitive-invalid-input"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("sensitive-invalid-input", response.text)
        self.assertEqual(
            self.client.delete(
                self.base + "/sessions/not-a-uuid", headers=self.headers
            ).status_code,
            422,
        )
        self.assertEqual(
            self.client.delete(
                self.base + f"/sessions/{uuid4()}", headers=self.headers
            ).status_code,
            404,
        )

    def test_revoked_account_cannot_renew_its_stream(self):
        session = self.create()
        User.delete().where(User.username == "alice").execute()
        response = self.client.patch(
            f"{self.base}/sessions/{session['session_id']}", headers=self.headers
        )
        self.assertEqual(response.status_code, 401, response.text)

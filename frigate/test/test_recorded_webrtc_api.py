"""Recorded WebRTC routes with real JWTs, current accounts and camera policies."""

import time
import unittest
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from joserfc import jwt

from frigate.api.native import router as native_router
from frigate.api.recorded_webrtc import router
from frigate.comms.recorded_webrtc import RecordedWebRTCManager
from frigate.models import User
from frigate.record.playback import SourceClip
from frigate.test import test_live_webrtc_api
from frigate.test.test_recorded_webrtc import FakeScheduledTransport, ReaderLease


class TestRecordedWebRTCAPI(unittest.TestCase):
    def setUp(self):
        self.fixture = test_live_webrtc_api.TestLiveWebRTCAPI()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.client = self.fixture.client
        self.headers = self.fixture.headers
        self.config = self.fixture.config
        self.original = Path(self.fixture.fixture.directory.name) / "original.mp4"
        self.original.write_bytes(b"synthetic immutable recording")
        self.native = FakeScheduledTransport()
        self.manager = RecordedWebRTCManager(
            self.fixture.manager,
            transport=self.native,
            planner=lambda *_: [
                SourceClip(str(self.original), 100, 20, 0, False, 100, 120)
            ],
            lease_factory=ReaderLease,
        )
        self.client.app.state.recorded_webrtc_manager = self.manager
        self.client.app.include_router(router)
        self.client.app.include_router(native_router)
        self.client.portal.call(self.manager.start)
        self.addCleanup(self.client.portal.call, self.manager.close)
        self.base = "/front/recordings/webrtc"
        self.body = {"after": 100, "before": 120, "start_timestamp": 105}

    def create(self):
        response = self.client.post(
            self.base + "/sessions",
            headers=self.headers,
            json=self.body,
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["state"], "warming")
        self.assertIsNotNone(response.json()["warmup_expires_at"])
        return response.json()

    def test_native_discovery_and_status_do_not_start_sources(self):
        response = self.client.get("/native/features", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        discovery = response.json()["recorded_webrtc"]
        self.assertTrue(discovery["available"])
        self.assertEqual(discovery["seek"], "new_peer_generation")
        response = self.client.get(self.base + "/status", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["available"])
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertFalse(self.native.created)
        self.assertFalse(self.fixture.transport.created)

    def test_disabled_camera_without_live_source_keeps_authorized_recordings_available(
        self,
    ):
        self.config.cameras["front"].enabled = False
        self.config.cameras["garden"].enabled = False
        self.config.go2rtc.streams.clear()
        response = self.client.get(self.base + "/status", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["available"])
        session = self.create()
        base = f"{self.base}/sessions/{session['session_id']}"
        response = self.client.post(
            base + "/answer",
            headers=self.headers,
            json={"generation": 1, "sdp": "v=0\r\nsynthetic-only\r\n"},
        )
        self.assertEqual(response.status_code, 204, response.text)
        response = self.client.post(
            base + "/control",
            headers=self.headers,
            json={"generation": 1, "action": "activate", "decoded_ready": True},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["state"], "playing")
        self.client.portal.call(self.manager._maintain_once)
        response = self.client.patch(base, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["state"], "playing")
        denied = self.client.post(
            "/garden/recordings/webrtc/sessions",
            headers=self.headers,
            json=self.body,
        )
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(len(self.native.created), 1)
        live = self.client.get("/front/live/webrtc/status", headers=self.headers)
        self.assertEqual(live.status_code, 404, live.text)
        self.assertEqual(
            self.client.delete(base, headers=self.headers).status_code, 204
        )
        self.assertFalse(self.manager.sessions)
        self.assertFalse(self.fixture.manager.external_encoders)

    def test_full_answer_quality_candidates_pause_resume_seek_heartbeat_and_close(self):
        session = self.create()
        base = f"{self.base}/sessions/{session['session_id']}"
        self.assertEqual(session["control_url"], base.removeprefix("/") + "/control")
        for suffix, payload in (
            ("/answer", {"generation": 1, "sdp": "v=0\r\nsynthetic-only\r\n"}),
            ("/candidates", {"generation": 1, "candidates": []}),
        ):
            response = self.client.post(
                base + suffix, headers=self.headers, json=payload
            )
            self.assertEqual(response.status_code, 204, response.text)
        response = self.client.patch(
            base + "/quality",
            headers=self.headers,
            json={"generation": 1, "height": 720},
        )
        self.assertEqual(response.status_code, 204, response.text)
        activated = self.client.post(
            base + "/control",
            headers=self.headers,
            json={"generation": 1, "action": "activate", "decoded_ready": True},
        )
        self.assertEqual(activated.status_code, 200, activated.text)
        self.assertEqual(activated.json()["state"], "playing")
        self.assertFalse(activated.json()["renegotiate"])
        self.assertEqual(activated.json()["generation"], 1)
        paused = self.client.post(
            base + "/control",
            headers=self.headers,
            json={"generation": 1, "action": "pause"},
        )
        self.assertEqual(paused.status_code, 200, paused.text)
        self.assertEqual(paused.json()["state"], "paused")
        self.assertEqual(paused.json()["position_timestamp"], 106)
        self.assertFalse(self.fixture.manager.external_encoders)
        resumed = self.client.post(
            base + "/control",
            headers=self.headers,
            json={"generation": 2, "action": "resume"},
        )
        self.assertEqual(resumed.status_code, 200, resumed.text)
        self.assertTrue(resumed.json()["renegotiate"])
        self.assertEqual(resumed.json()["generation"], 3)
        sought = self.client.post(
            base + "/control",
            headers=self.headers,
            json={
                "generation": 3,
                "action": "seek",
                "window": {**self.body, "start_timestamp": 110},
            },
        )
        self.assertEqual(sought.status_code, 200, sought.text)
        self.assertEqual(sought.json()["generation"], 4)
        response = self.client.patch(base, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["offer"])
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertEqual(
            self.client.delete(base, headers=self.headers).status_code, 204
        )
        self.assertFalse(self.manager.sessions)
        self.assertFalse(self.manager.generations)
        self.assertTrue(self.original.exists())

    def test_real_auth_and_current_camera_role_are_required_before_copy_or_native_work(
        self,
    ):
        for headers in ({}, {"remote-user": "alice", "remote-role": "admin"}):
            for method, suffix, body in (
                ("GET", "/status", None),
                ("POST", "/sessions", self.body),
            ):
                response = self.client.request(
                    method,
                    self.base + suffix,
                    headers=headers,
                    json=body,
                )
                self.assertIn(response.status_code, (401, 403), response.text)
                self.assertIn("no-store", response.headers["cache-control"])
        response = self.client.post(
            "/garden/recordings/webrtc/sessions",
            headers=self.headers,
            json=self.body,
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertFalse(self.native.created)

    def test_account_and_camera_ownership_apply_to_every_session_operation(self):
        session = self.create()
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
        headers = {
            "remote-user": "bob",
            "remote-role": "admin",
            "Authorization": "Bearer " + encoded,
        }
        for method, suffix, body in (
            ("POST", "/answer", {"generation": 1, "sdp": "v=0\r\nsynthetic-only\r\n"}),
            ("POST", "/candidates", {"generation": 1, "candidates": []}),
            ("PATCH", "/quality", {"generation": 1, "height": 720}),
            ("POST", "/control", {"generation": 1, "action": "pause"}),
            (
                "POST",
                "/control",
                {
                    "generation": 1,
                    "action": "activate",
                    "decoded_ready": True,
                },
            ),
            ("PATCH", "", None),
            ("DELETE", "", None),
        ):
            for camera in ("front", "garden"):
                response = self.client.request(
                    method,
                    f"/{camera}/recordings/webrtc/sessions/{session['session_id']}{suffix}",
                    headers=headers,
                    json=body,
                )
                self.assertEqual(response.status_code, 404, response.text)
                self.assertIn("no-store", response.headers["cache-control"])
        self.assertEqual(len(self.manager.sessions), 1)

    def test_stale_generation_cannot_write_into_replacement_peer(self):
        session = self.create()
        base = f"{self.base}/sessions/{session['session_id']}"
        response = self.client.post(
            base + "/control",
            headers=self.headers,
            json={
                "generation": 1,
                "action": "seek",
                "window": {**self.body, "start_timestamp": 110},
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        for suffix, payload in (
            ("/answer", {"generation": 1, "sdp": "v=0\r\nsynthetic-only\r\n"}),
            ("/candidates", {"generation": 1, "candidates": []}),
        ):
            response = self.client.post(
                base + suffix, headers=self.headers, json=payload
            )
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(
                response.json()["detail"], "recorded_webrtc_stale_generation"
            )

    def test_validation_never_echoes_sdp_paths_or_injected_sources(self):
        session = self.create()
        base = f"{self.base}/sessions/{session['session_id']}"
        for method, url, body in (
            (
                "POST",
                base + "/answer",
                {"generation": 1, "sdp": "private-marker-" + "x" * 128001},
            ),
            (
                "POST",
                self.base + "/sessions",
                {**self.body, "url": "file://private-marker-secret"},
            ),
            ("POST", base + "/control", {"generation": 1, "action": "seek"}),
            ("DELETE", self.base + "/sessions/private-marker-not-uuid", None),
        ):
            response = self.client.request(method, url, headers=self.headers, json=body)
            self.assertEqual(response.status_code, 422, response.text[:1000])
            self.assertNotIn("private-marker", response.text)
            self.assertIn("no-store", response.headers["cache-control"])
        self.assertEqual(
            self.client.delete(
                self.base + f"/sessions/{uuid4()}",
                headers=self.headers,
            ).status_code,
            404,
        )

    def test_deleted_or_password_revoked_account_cannot_renew_and_is_reaped(self):
        session = self.create()
        User.update(password_changed_at=datetime.now()).where(
            User.username == "alice"
        ).execute()
        response = self.client.patch(
            f"{self.base}/sessions/{session['session_id']}",
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 401, response.text)
        self.client.portal.call(self.manager._maintain_once)
        self.assertFalse(self.manager.sessions)
        self.assertFalse(self.fixture.manager.external_encoders)

    def test_live_and_recorded_viewers_share_one_capacity(self):
        self.config.live_webrtc.max_detail_cameras = 1
        self.fixture.manager.settings = self.config.live_webrtc.model_copy(deep=True)
        self.create()
        response = self.client.post(
            "/front/live/webrtc/sessions",
            headers=self.headers,
            json={"view": "detail"},
        )
        self.assertEqual(response.status_code, 429, response.text)

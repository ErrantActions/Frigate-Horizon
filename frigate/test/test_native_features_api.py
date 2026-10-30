"""Native namespace authorization uses real sessions and fake-only camera services."""

import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4


@unittest.skipIf(os.name == "nt", "Full API imports require Linux")
class TestNativeFeaturesAPI(unittest.TestCase):
    def setUp(self):
        from frigate.api import native
        from frigate.models import User
        from frigate.test import test_native_push_images as image_tests
        from frigate.test.test_native_talkback_leases import FakeOwnedTransport

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
        self.transport = FakeOwnedTransport()
        self.transport.available = AsyncMock(return_value=False)
        with patch(
            "frigate.api.native.OwnedTalkbackTransport", return_value=self.transport
        ):
            native.initialize_native_controls(self.client.app)
        self.controls = self.client.app.state.native_controls
        self.talkback = self.client.app.state.native_talkback
        self.client.app.state.native_export_protection = False
        self.enterContext(self.client)

        async def close():
            await self.talkback.close()
            await self.controls.close()

        self.addCleanup(self.client.portal.call, close)

    def open_owned(self, camera="front", *, demote=True):
        from starlette.requests import Request

        self.User.update(role="admin").where(self.User.username == "alice").execute()
        scope = {
            "type": "http",
            "app": self.client.app,
            "headers": [(b"cookie", self.headers["Cookie"].encode())],
        }
        identity = self.native._talkback_identity(
            Request(scope), {"username": "alice", "role": "admin"}
        )
        value = self.client.portal.call(
            self.talkback.create, camera, identity, "synthetic", "synthetic-offer"
        )
        if demote:
            self.User.update(role="front_only").where(
                self.User.username == "alice"
            ).execute()
        return value, self.transport.created[-1].identifier

    def test_features_are_available_to_real_non_admin_sessions_but_not_headers_alone(
        self,
    ):
        response = self.client.get("/native/features", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json(), {"controls": True, "export_protection": False}
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
            (
                "POST",
                "/native/cameras/front/talkback",
                {
                    "type": "offer",
                    "sdp": "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 0\r\na=fingerprint:sha-256 TEST\r\n",
                },
            ),
            ("POST", f"/native/cameras/front/talkback/{lease}/renew", None),
            ("DELETE", f"/native/cameras/front/talkback/{lease}", None),
        )
        for headers in ({}, {"remote-user": "alice", "remote-role": "admin"}):
            for method, path, body in requests:
                with self.subTest(method=method, path=path, forwarded=bool(headers)):
                    response = self.client.request(
                        method, path, headers=headers, json=body
                    )
                    self.assertIn(response.status_code, (401, 403), response.text)
        self.assertEqual(self.transport.created, [])
        self.assertEqual(self.transport.renewed, [])
        self.assertEqual(self.transport.closed, [])
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
        self.transport.available.assert_not_awaited()

    def test_writes_still_require_current_database_admin_before_fake_transport(self):
        lease = str(uuid4())
        requests = (
            (
                "/native/cameras/front/ptz",
                {"command": "MOVE_UP", "lease_id": lease, "duration_ms": 500},
            ),
            (
                "/native/cameras/front/talkback",
                {
                    "type": "offer",
                    "sdp": "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 0\r\na=fingerprint:sha-256 TEST\r\n",
                },
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
        self.assertEqual(self.transport.created, [])
        self.assertEqual(self.transport.renewed, [])
        self.assertEqual(self.controller.mock_calls, [])

    def test_renew_uses_real_broker_current_database_authorization_before_transport(
        self,
    ):
        value, peer = self.open_owned(demote=False)
        allowed = self.client.post(
            f"/native/cameras/front/talkback/{value['lease_id']}/renew",
            headers=self.headers,
        )
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertEqual(allowed.json()["lease_id"], value["lease_id"])
        self.User.update(role="viewer").where(self.User.username == "alice").execute()
        before = list(self.transport.renewed)
        response = self.client.post(
            f"/native/cameras/front/talkback/{value['lease_id']}/renew",
            headers=self.fixture.headers,
        )
        self.assertIn(response.status_code, (403, 409), response.text)
        self.assertEqual(self.transport.renewed, before)
        self.assertIn(peer, self.transport.closed)
        self.assertEqual(self.controller.mock_calls, [])

    def test_release_admission_survives_same_user_demotion_and_camera_scope_loss(self):
        value, peer = self.open_owned("garden")
        lease = value["lease_id"]
        response = self.client.delete(
            f"/native/cameras/garden/talkback/{lease}", headers=self.headers
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"success": True})
        self.assertIn(peer, self.transport.closed)
        self.assertNotIn(lease, self.talkback.leases)
        self.assertEqual(self.controller.mock_calls, [])

    def test_real_broker_rejects_another_account_or_camera_without_closing_owner_peer(
        self,
    ):
        value, peer = self.open_owned(demote=False)
        lease = value["lease_id"]
        other = {
            "remote-user": "bob",
            "remote-role": "admin",
            "Cookie": f"{self.fixture.config.auth.cookie_name}={self.fixture.token(username='bob')}",
        }
        response = self.client.delete(
            f"/native/cameras/front/talkback/{lease}", headers=other
        )
        self.assertEqual(response.status_code, 404, response.text)
        response = self.client.delete(
            f"/native/cameras/garden/talkback/{lease}", headers=self.headers
        )
        self.assertEqual(response.status_code, 404, response.text)
        self.assertNotIn(peer, self.transport.closed)
        self.assertIn(lease, self.talkback.leases)
        self.assertEqual(self.controller.mock_calls, [])

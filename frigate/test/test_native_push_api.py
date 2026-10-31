"""Offline native endpoint contracts with real auth dependencies and temporary users."""

import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import OctKey
from peewee import SqliteDatabase
from pydantic import ValidationError

from frigate.api.auth import require_admin_by_default
from frigate.api.notification import router
from frigate.comms.apns import APNsDelivery
from frigate.config import FrigateConfig
from frigate.models import User


class NativePushAPITestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        previous = User._meta.database
        self.database = SqliteDatabase(str(Path(self.directory.name) / "users.db"))
        self.database.bind([User])
        self.addCleanup(lambda: User.bind(previous))
        self.addCleanup(self.database.close)
        self.database.create_tables([User])
        for username, role in (("alice", "front_only"), ("bob", "admin")):
            User.create(
                username=username,
                role=role,
                password_hash="unused",
                notification_tokens=[],
            )
        self.config = FrigateConfig(
            mqtt={"enabled": False},
            cameras={
                name: {
                    "ffmpeg": {
                        "inputs": [
                            {
                                "path": "rtsp://example.invalid/stream",
                                "roles": ["record"],
                            }
                        ]
                    }
                }
                for name in ("front", "garden")
            },
            auth={
                "enabled": True,
                "roles": {"front_only": ["front"]},
            },
            apns={
                "enabled": True,
                "server_id": "home-nvr",
                "topic": "com.errantactions.frigatehorizon",
                "image_notifications": False,
                "team_id": "TEAMID1234",
                "key_id": "PUSHKEY123",
                "private_key_file": "/unused/synthetic.p8",
            },
        )
        app = FastAPI(dependencies=[Depends(require_admin_by_default())])
        app.frigate_config = self.config
        app.jwt_token = OctKey.import_key("synthetic-offline-session-key-32-bytes-long")
        app.include_router(router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        encoded = jwt.encode(
            {"alg": "HS256"},
            {
                "sub": "alice",
                "role": "admin",
                "iat": time.time() - 10,
                "exp": time.time() + 3600,
            },
            app.jwt_token,
        )
        self.headers = {
            "remote-user": "alice",
            "remote-role": "admin",
            "Cookie": f"{self.config.auth.cookie_name}={encoded}",
        }
        self.body = {
            "installation_id": str(uuid4()),
            "token": "a" * 96,
            "environment": "production",
            "camera_ids": ["front"],
        }


class TestNativePushAPI(NativePushAPITestCase):
    def test_contract_uses_database_role_not_header_role_and_never_exposes_tokens(self):
        status = self.client.get("/notifications/apns", headers=self.headers)
        self.assertEqual(
            status.json(),
            {
                "enabled": True,
                "server_id": "home-nvr",
                "topic": "com.errantactions.frigatehorizon",
                "image_notifications": self.config.apns.image_notifications,
                "device_preferences": True,
                "action_notifications": True,
                "supports_device_features": True,
                "health_notifications": False,
            },
        )
        response = self.client.post(
            "/notifications/apns/register", headers=self.headers, json=self.body
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "success": True,
                "message": "Native push registration saved",
                "server_id": "home-nvr",
            },
        )
        response = self.client.get("/notifications/apns/devices", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(self.body["token"], response.text)
        self.assertNotIn("token", response.json()[0])
        forbidden = {**self.body, "camera_ids": ["garden"]}
        self.assertEqual(
            self.client.post(
                "/notifications/apns/register", headers=self.headers, json=forbidden
            ).status_code,
            403,
        )

    def test_auth_disabled_anonymous_and_deleted_accounts_cannot_register(self):
        for headers in (
            {},
            {"remote-user": "anonymous", "remote-role": "admin"},
            {"remote-user": "missing", "remote-role": "admin"},
        ):
            self.assertIn(
                self.client.post(
                    "/notifications/apns/register", headers=headers, json=self.body
                ).status_code,
                (401, 403, 404),
            )
        self.config.auth.enabled = False
        self.assertEqual(
            self.client.post(
                "/notifications/apns/register", headers=self.headers, json=self.body
            ).status_code,
            403,
        )

    def test_password_reset_revokes_registration_even_before_cookie_refresh(self):
        User.update(password_changed_at=datetime.now()).where(
            User.username == "alice"
        ).execute()
        response = self.client.post(
            "/notifications/apns/register", headers=self.headers, json=self.body
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(User.get_by_id("alice").native_push_devices, [])

    def test_forwarded_headers_without_real_session_cannot_register(self):
        response = self.client.post(
            "/notifications/apns/register",
            headers={"remote-user": "alice", "remote-role": "admin"},
            json=self.body,
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(User.get_by_id("alice").native_push_devices, [])

    def test_disabled_provider_keeps_own_unregistration_available(self):
        self.client.post(
            "/notifications/apns/register", headers=self.headers, json=self.body
        )
        self.config.apns.enabled = False
        path = "/notifications/apns/register/" + self.body["installation_id"]
        other = {"remote-user": "bob", "remote-role": "admin"}
        self.assertEqual(self.client.delete(path, headers=other).status_code, 200)
        self.assertEqual(len(User.get_by_id("alice").native_push_devices), 1)
        self.assertEqual(
            self.client.delete(path, headers=self.headers).status_code, 200
        )
        self.assertEqual(User.get_by_id("alice").native_push_devices, [])
        self.assertEqual(
            self.client.post(
                "/notifications/apns/register", headers=self.headers, json=self.body
            ).status_code,
            503,
        )

    def test_extra_provider_or_owner_fields_and_bad_tokens_are_rejected(self):
        for change in (
            {"username": "bob"},
            {"topic": "other"},
            {"endpoint": "https://other.invalid"},
            {"credentials": "no"},
            {"token": "abc"},
            {"environment": "other"},
        ):
            response = self.client.post(
                "/notifications/apns/register",
                headers=self.headers,
                json={**self.body, **change},
            )
            self.assertEqual(response.status_code, 422)

    def test_role_changes_camera_selection_and_revocation_use_current_database(self):
        response = self.client.post(
            "/notifications/apns/register",
            headers=self.headers,
            json={**self.body, "camera_ids": None},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(User.get_by_id("alice").native_push_devices[0]["camera_ids"])
        User.update(role="viewer").where(User.username == "alice").execute()
        response = self.client.post(
            "/notifications/apns/register",
            headers=self.headers,
            json={**self.body, "camera_ids": ["garden"]},
        )
        self.assertEqual(response.status_code, 200)
        User.update(role="front_only").where(User.username == "alice").execute()
        self.assertEqual(
            self.client.post(
                "/notifications/apns/register",
                headers=self.headers,
                json={**self.body, "camera_ids": ["garden"]},
            ).status_code,
            403,
        )
        User.update(role="revoked").where(User.username == "alice").execute()
        self.assertEqual(
            self.client.post(
                "/notifications/apns/test",
                headers=self.headers,
                json={"installation_id": self.body["installation_id"]},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.delete(
                "/notifications/apns/register/" + self.body["installation_id"],
                headers=self.headers,
            ).status_code,
            200,
        )

    def test_explicit_test_is_installation_owned_and_acceptance_is_not_delivery(self):
        self.body["camera_ids"] = []
        self.client.post(
            "/notifications/apns/register", headers=self.headers, json=self.body
        )
        with patch("frigate.api.notification.APNsTransport") as transport:
            transport.return_value.send = AsyncMock(
                return_value=[APNsDelivery(self.body["installation_id"], True)]
            )
            transport.return_value.close = AsyncMock()
            response = self.client.post(
                "/notifications/apns/test",
                headers=self.headers,
                json={"installation_id": self.body["installation_id"]},
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn("best-effort", response.json()["message"])
            response = self.client.post(
                "/notifications/apns/test",
                headers={"remote-user": "bob", "remote-role": "admin"},
                json={"installation_id": self.body["installation_id"]},
            )
            self.assertEqual(response.status_code, 404)


class TestAPNsConfiguration(unittest.TestCase):
    def test_default_is_disabled_and_partial_disabled_config_is_allowed(self):
        from frigate.config.apns import APNsConfig

        self.assertFalse(APNsConfig().enabled)
        self.assertFalse(APNsConfig(server_id="home-nvr").enabled)
        with self.assertRaises(ValidationError):
            APNsConfig(enabled=True)
        with self.assertRaises(ValidationError):
            APNsConfig(private_key_file="-----BEGIN PRIVATE KEY-----\nnot-a-path")

    def test_environment_identifiers_are_resolved_without_reading_private_key(self):
        from frigate.config.apns import APNsConfig

        with patch.dict(
            "frigate.util.env.FRIGATE_ENV_VARS",
            {"FRIGATE_APNS_TEAM_ID": "TEAMID1234", "FRIGATE_APNS_KEY_ID": "PUSHKEY123"},
        ):
            config = APNsConfig(
                enabled=True,
                server_id="home-nvr",
                topic="com.errantactions.frigatehorizon",
                team_id="{FRIGATE_APNS_TEAM_ID}",
                key_id="{FRIGATE_APNS_KEY_ID}",
                private_key_file="/not-mounted/synthetic.p8",
            )
        self.assertEqual(config.team_id, "TEAMID1234")
        self.assertEqual(config.key_id, "PUSHKEY123")
        self.assertFalse(config.include_details)

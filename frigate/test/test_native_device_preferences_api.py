"""Installation-owned native preference HTTP contracts using real signed sessions."""

import time
import unittest
from datetime import datetime
from unittest.mock import patch
from uuid import uuid4

from frigate.comms.apns_registry import APNsRegistry
from frigate.models import User
from frigate.test import test_native_push_images as image_tests


class TestNativeDevicePreferenceAPI(unittest.TestCase):
    def setUp(self):
        self.fixture = image_tests.TestNativeImageAPI()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.client = self.fixture.client
        self.headers = self.fixture.headers
        self.identity = self.fixture.body["installation_id"]
        self.base = f"/notifications/apns/devices/{self.identity}"

    def preferences(self):
        response = self.client.get(self.base + "/preferences", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def post(self, value):
        return self.client.post(
            self.base + "/preferences", headers=self.headers, json=value
        )

    def test_exact_default_wrapper_and_successful_revision_compare_swap(self):
        before = self.preferences()
        self.assertEqual(
            before,
            {
                "revision": 0,
                "camera_snooze_until": {},
                "quiet_hours": None,
                "health_alerts": False,
            },
        )
        quiet = {
            "enabled": True,
            "start_minute": 1320,
            "end_minute": 420,
            "timezone": "America/New_York",
            "days": [1, 2, 3, 4, 5],
        }
        response = self.post({**before, "quiet_hours": quiet})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json(), {**before, "revision": 1, "quiet_hours": quiet}
        )
        self.assertEqual(self.post(before).status_code, 409)
        self.assertEqual(self.preferences()["quiet_hours"], quiet)

    def test_health_is_explicit_opt_in_and_requires_negotiated_feature(self):
        before = self.preferences()
        self.assertEqual(self.post({**before, "health_alerts": True}).status_code, 409)
        response = self.client.post(
            "/notifications/apns/register",
            headers=self.headers,
            json={**self.fixture.body, "features": ["actions-v1", "health-v1"]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.preferences()["health_alerts"])
        response = self.post({**before, "health_alerts": True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["health_alerts"])
        self.assertEqual(response.json()["revision"], 1)

    def test_snooze_is_installation_local_bounded_and_cancelable(self):
        config = self.fixture.config
        original_rules = config.notifications.model_dump()
        original_camera_rules = config.cameras["front"].notifications.model_dump()
        now = time.time()
        response = self.client.post(
            self.base + "/snooze",
            headers=self.headers,
            json={"camera_id": "front", "seconds": 900},
        )
        self.assertEqual(response.status_code, 200, response.text)
        prefs = response.json()
        self.assertEqual(prefs["revision"], 1)
        self.assertGreaterEqual(prefs["camera_snooze_until"]["front"], now + 900)
        self.assertLess(prefs["camera_snooze_until"]["front"], time.time() + 901)
        response = self.client.post(
            self.base + "/snooze",
            headers=self.headers,
            json={"camera_id": "front", "seconds": 0},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["camera_snooze_until"], {})
        self.assertEqual(response.json()["revision"], 2)
        self.assertEqual(config.notifications.model_dump(), original_rules)
        self.assertEqual(
            config.cameras["front"].notifications.model_dump(), original_camera_rules
        )

    def test_current_camera_and_installation_selection_are_enforced(self):
        response = self.client.post(
            self.base + "/snooze",
            headers=self.headers,
            json={"camera_id": "garden", "seconds": 900},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            self.post(
                {
                    **self.preferences(),
                    "camera_snooze_until": {"garden": time.time() + 900},
                }
            ).status_code,
            403,
        )
        self.client.post(
            "/notifications/apns/register",
            headers=self.headers,
            json={**self.fixture.body, "camera_ids": []},
        )
        response = self.client.post(
            self.base + "/snooze",
            headers=self.headers,
            json={"camera_id": "front", "seconds": 900},
        )
        self.assertEqual(response.status_code, 403)

    def test_unowned_unregistered_and_revoked_sessions_cannot_change_preferences(self):
        bob_headers = {
            "remote-user": "bob",
            "remote-role": "admin",
            "Cookie": f"{self.fixture.config.auth.cookie_name}={self.fixture.token(username='bob')}",
        }
        self.assertEqual(
            self.client.get(
                self.base + "/preferences", headers=bob_headers
            ).status_code,
            404,
        )
        unknown = f"/notifications/apns/devices/{uuid4()}/preferences"
        self.assertEqual(
            self.client.get(unknown, headers=self.headers).status_code, 404
        )
        self.assertEqual(
            self.client.get(
                self.base + "/preferences",
                headers={"remote-user": "alice", "remote-role": "admin"},
            ).status_code,
            401,
        )
        User.update(password_changed_at=datetime.now()).where(
            User.username == "alice"
        ).execute()
        self.assertEqual(
            self.client.get(
                self.base + "/preferences", headers=self.headers
            ).status_code,
            401,
        )

    def test_oversized_snoozes_and_invalid_quiet_hours_fail_without_saving(self):
        before = self.preferences()
        self.assertEqual(
            self.post(
                {
                    **before,
                    "camera_snooze_until": {"front": time.time() + 7200},
                }
            ).status_code,
            422,
        )
        self.assertEqual(
            self.post(
                {
                    **before,
                    "quiet_hours": {
                        "enabled": True,
                        "start_minute": 10,
                        "end_minute": 10,
                        "timezone": "UTC",
                        "days": [1],
                    },
                }
            ).status_code,
            422,
        )
        for seconds in (True, 1, 1800, -1):
            self.assertEqual(
                self.client.post(
                    self.base + "/snooze",
                    headers=self.headers,
                    json={"camera_id": "front", "seconds": seconds},
                ).status_code,
                422,
            )
        self.assertEqual(self.preferences(), before)

    def test_password_revocation_during_installation_lookup_is_rechecked(self):
        registry = APNsRegistry(self.fixture.database.execute_sql)
        original_devices = registry.devices

        def revoke(username):
            devices = original_devices(username)
            User.update(password_changed_at=datetime.now()).where(
                User.username == "alice"
            ).execute()
            return devices

        with patch("frigate.api.notification._native_registry", return_value=registry):
            with patch.object(registry, "devices", side_effect=revoke):
                response = self.client.get(
                    self.base + "/preferences", headers=self.headers
                )
        self.assertEqual(response.status_code, 401, response.text)

    def test_preference_storage_errors_are_explicit_and_do_not_change_state(self):
        from peewee import OperationalError

        before = self.preferences()
        registry = APNsRegistry(self.fixture.database.execute_sql)
        with patch("frigate.api.notification._native_registry", return_value=registry):
            with patch.object(
                registry, "set_preferences", side_effect=OperationalError("synthetic")
            ):
                response = self.post(before)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.preferences(), before)

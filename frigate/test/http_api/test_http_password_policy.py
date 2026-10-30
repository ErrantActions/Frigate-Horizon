"""Password policy tests without production data or database migrations."""

import unittest
from unittest.mock import MagicMock

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from joserfc.jwk import OctKey
from peewee import SqliteDatabase

from frigate.api.auth import (
    hash_password,
    limiter,
    require_admin_by_default,
    router,
    validate_password_strength,
    verify_password,
)
from frigate.config import FrigateConfig
from frigate.models import User


class TestPasswordPolicy(unittest.TestCase):
    def setUp(self):
        self.db = SqliteDatabase(":memory:", check_same_thread=False, thread_safe=False)
        self.binding = self.db.bind_ctx([User])
        self.binding.__enter__()
        self.addCleanup(self.binding.__exit__, None, None, None)
        self.addCleanup(self.db.close)
        self.db.create_tables([User])
        self.app = FastAPI(dependencies=[Depends(require_admin_by_default())])
        self.app.frigate_config = FrigateConfig(mqtt={"enabled": False}, cameras={})
        self.app.config_publisher = MagicMock()
        self.app.jwt_token = OctKey.import_key(b"test-password-policy-key-00000000")
        self.app.state.limiter = limiter
        self.app.include_router(router)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.admin = {"remote-user": "admin", "remote-role": "admin"}
        self.viewer = {"remote-user": "viewer", "remote-role": "viewer"}

    def create_viewer(self, password="existing-test-password"):
        User.create(
            username="viewer",
            role="viewer",
            password_hash=hash_password(password),
            notification_tokens=[],
        )

    def test_accepts_every_nonempty_password_length(self):
        for password in ("x", "short", "elevenchars", "longer-test-password"):
            with self.subTest(length=len(password)):
                self.assertEqual(validate_password_strength(password), (True, None))
        self.assertEqual(
            validate_password_strength(""), (False, "Password cannot be empty")
        )

    def test_admin_can_create_user_with_one_character_password(self):
        response = self.client.post(
            "/users",
            headers=self.admin,
            json={"username": "short_user", "password": "x", "role": "viewer"},
        )
        self.assertEqual(response.status_code, 200)
        user = User.get_by_id("short_user")
        self.assertTrue(verify_password("x", user.password_hash))
        self.assertNotEqual(user.password_hash, "x")

    def test_empty_password_cannot_create_user(self):
        response = self.client.post(
            "/users",
            headers=self.admin,
            json={"username": "empty_user", "password": "", "role": "viewer"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(User.get_or_none(User.username == "empty_user"))

    def test_admin_can_set_short_password_and_login(self):
        self.create_viewer()
        response = self.client.put(
            "/users/viewer/password",
            headers=self.admin,
            json={"password": "x"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(verify_password("x", User.get_by_id("viewer").password_hash))
        response = self.client.post("/login", json={"user": "viewer", "password": "x"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("set-cookie", response.headers)

    def test_empty_password_cannot_replace_existing_password(self):
        self.create_viewer()
        before = User.get_by_id("viewer").password_hash
        response = self.client.put(
            "/users/viewer/password", headers=self.admin, json={"password": ""}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(before, User.get_by_id("viewer").password_hash)

    def test_self_service_still_checks_current_password_and_other_accounts(self):
        self.create_viewer()
        before = User.get_by_id("viewer").password_hash
        for body, expected in (
            ({"password": "x"}, 400),
            ({"password": "x", "old_password": "incorrect"}, 401),
        ):
            response = self.client.put(
                "/users/viewer/password", headers=self.viewer, json=body
            )
            self.assertEqual(response.status_code, expected)
            self.assertEqual(before, User.get_by_id("viewer").password_hash)
        response = self.client.put(
            "/users/admin/password", headers=self.viewer, json={"password": "x"}
        )
        self.assertEqual(response.status_code, 403)
        response = self.client.put(
            "/users/viewer/password",
            headers=self.viewer,
            json={"password": "x", "old_password": "existing-test-password"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(verify_password("x", User.get_by_id("viewer").password_hash))


if __name__ == "__main__":
    unittest.main()

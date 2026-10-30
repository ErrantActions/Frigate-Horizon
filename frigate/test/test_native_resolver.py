"""Queued native routing uses current authorization, independent of image support."""

import math
import unittest
from datetime import datetime
from unittest.mock import patch

from frigate.models import Event, ReviewSegment, User
from frigate.test import test_native_push_images as image_tests


class TestNativeResolver(unittest.TestCase):
    def setUp(self):
        self.fixture = image_tests.TestNativeImageAPI()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.config = self.fixture.config

    def resolve(self, kind="review", identifier="review-1", headers=None):
        return self.fixture.client.get(
            f"/notifications/apns/resolve/{kind}/{identifier}",
            headers=self.fixture.headers if headers is None else headers,
        )

    def test_exact_metadata_contract_does_not_depend_on_images_or_registration(self):
        self.config.apns.image_notifications = False
        self.fixture.app.detected_frames_processor = None
        User.update(native_push_devices=[]).execute()
        Event.update(thumbnail="").execute()
        ReviewSegment.update(
            thumb_path="/missing/thumbnail.webp",
            data={"native_person_best_shot": False},
        ).execute()
        with patch("frigate.api.notification.render_native_thumbnail") as render:
            for kind, identifier in (
                ("review", "review-1"),
                ("event", "event-1"),
                ("camera", "front"),
            ):
                response = self.resolve(kind, identifier)
                self.assertEqual(response.status_code, 200, response.text)
                value = response.json()
                self.assertEqual(
                    set(value),
                    {"server_id", "kind", "identifier", "camera", "timestamp"},
                )
                self.assertEqual(value["server_id"], "home-nvr")
                self.assertEqual(value["kind"], kind)
                self.assertEqual(value["identifier"], identifier)
                self.assertEqual(value["camera"], "front")
                if kind == "camera":
                    self.assertIsNone(value["timestamp"])
                else:
                    self.assertTrue(math.isfinite(value["timestamp"]))
                self.assertEqual(response.headers["cache-control"], "private, no-store")
            render.assert_not_called()

    def test_current_database_role_not_issued_or_forwarded_admin_role(self):
        Event.update(camera="garden").execute()
        self.assertEqual(self.resolve("event", "event-1").status_code, 403)
        User.update(role="viewer").where(User.username == "alice").execute()
        self.assertEqual(self.resolve("event", "event-1").status_code, 200)
        User.update(role="revoked").where(User.username == "alice").execute()
        self.assertEqual(self.resolve().status_code, 403)

    def test_revoked_password_deleted_user_and_header_only_sessions_fail(self):
        self.assertEqual(
            self.resolve(
                headers={"remote-user": "alice", "remote-role": "admin"}
            ).status_code,
            401,
        )
        User.update(password_changed_at=datetime.now()).where(
            User.username == "alice"
        ).execute()
        self.assertEqual(self.resolve().status_code, 401)
        User.delete().where(User.username == "alice").execute()
        self.assertEqual(self.resolve().status_code, 401)

    def test_unknown_references_and_non_routing_kinds_are_not_resolved(self):
        self.assertEqual(self.resolve(identifier="unknown").status_code, 404)
        self.assertEqual(self.resolve("camera", "garden").status_code, 403)
        self.assertEqual(self.resolve("test", "anything").status_code, 422)
        self.assertEqual(self.resolve("health", "anything").status_code, 422)

    def test_role_change_during_metadata_lookup_is_rechecked(self):
        def metadata(*args):
            User.update(role="revoked").where(User.username == "alice").execute()
            return "front", 1000.0

        with patch(
            "frigate.api.notification._native_notification_reference",
            side_effect=metadata,
        ):
            self.assertEqual(self.resolve().status_code, 403)

    def test_reference_reassignment_during_resolution_requires_retry(self):
        with patch(
            "frigate.api.notification._native_notification_reference",
            side_effect=[("front", 1000.0), ("garden", 1000.0)],
        ):
            self.assertEqual(self.resolve().status_code, 409)

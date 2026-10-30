"""Installation preference CAS, backwards compatibility and timezone behavior."""

import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from frigate.comms.apns_registry import APNsRegistry, NativePushDevice
from frigate.comms.native_preferences import (
    DevicePreferences,
    NativeSnoozeBody,
    QuietHours,
)


class TestQuietHours(unittest.TestCase):
    def quiet(self, **values):
        return QuietHours.model_validate(
            {
                "enabled": True,
                "start_minute": 22 * 60,
                "end_minute": 7 * 60,
                "timezone": "America/New_York",
                "days": [5],
                **values,
            }
        )

    def epoch(self, value):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()

    def test_cross_midnight_is_owned_by_start_day_and_end_is_exclusive(self):
        quiet = self.quiet()
        for value, expected in (
            ("2026-10-31T01:59:00Z", False),
            ("2026-10-31T02:00:00Z", True),
            ("2026-10-31T10:59:00Z", True),
            ("2026-10-31T11:00:00Z", False),
            ("2026-11-01T02:00:00Z", False),
        ):
            self.assertEqual(quiet.active(self.epoch(value)), expected, value)

    def test_dst_repeated_hour_is_quiet_both_times(self):
        quiet = self.quiet(days=[7], start_minute=60, end_minute=120)
        self.assertTrue(quiet.active(self.epoch("2026-11-01T05:30:00Z")))
        self.assertTrue(quiet.active(self.epoch("2026-11-01T06:30:00Z")))
        self.assertFalse(quiet.active(self.epoch("2026-11-01T07:00:00Z")))

    def test_dst_skipped_hour_does_not_extend_quiet_interval(self):
        quiet = self.quiet(days=[7], start_minute=60, end_minute=180)
        self.assertTrue(quiet.active(self.epoch("2026-03-08T06:59:00Z")))
        self.assertFalse(quiet.active(self.epoch("2026-03-08T07:00:00Z")))

    def test_same_day_disabled_and_invalid_configurations(self):
        quiet = self.quiet(timezone="UTC", days=[1], start_minute=60, end_minute=120)
        self.assertTrue(quiet.active(self.epoch("2026-10-26T01:00:00Z")))
        self.assertFalse(quiet.active(self.epoch("2026-10-26T02:00:00Z")))
        self.assertFalse(
            self.quiet(enabled=False).active(self.epoch("2026-10-31T03:00:00Z"))
        )
        for values in (
            {"timezone": "not/a/zone"},
            {"days": [0]},
            {"days": [8]},
            {"days": []},
            {"days": [1, 1]},
            {"start_minute": 1440},
            {"start_minute": 420, "end_minute": 420},
            {"days": [True]},
        ):
            with self.assertRaises(ValidationError):
                self.quiet(**values)

    def test_snooze_and_quiet_hours_only_filter_own_installation(self):
        preferences = DevicePreferences(camera_snooze_until={"front": 500})
        self.assertFalse(preferences.allows("front", 499))
        self.assertTrue(preferences.allows("front", 500))
        self.assertTrue(preferences.allows("back", 499))
        self.assertTrue(DevicePreferences().allows("front", 499))
        self.assertFalse(DevicePreferences().health_alerts)
        for value in (True, -1, 10, 3601):
            with self.assertRaises(ValidationError):
                NativeSnoozeBody(camera_id="front", seconds=value)
        for timestamp in (True, -1, float("inf"), float("nan")):
            with self.assertRaises(ValidationError):
                DevicePreferences(camera_snooze_until={"front": timestamp})


class TestNativePreferenceRegistry(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript(
            'CREATE TABLE "user" (username TEXT PRIMARY KEY,role TEXT,'
            "native_push_devices TEXT NOT NULL DEFAULT '[]');"
            "INSERT INTO user(username,role) VALUES ('alice','viewer'),('bob','admin');"
        )
        self.registry = APNsRegistry(self.db.execute)
        self.identifier = str(uuid4())

    def device(self, **values):
        return NativePushDevice(
            installation_id=self.identifier,
            token=values.pop("token", "a" * 64),
            environment="production",
            camera_ids=None,
            registered_at=values.pop("registered_at", 100),
            **values,
        )

    def test_old_devices_default_to_no_features_no_health_no_quiet_hours(self):
        value = {
            "installation_id": self.identifier,
            "token": "a" * 64,
            "environment": "production",
            "camera_ids": None,
            "registered_at": 100,
        }
        self.db.execute(
            "UPDATE user SET native_push_devices=? WHERE username='alice'",
            (json.dumps([value]),),
        )
        device = self.registry.devices("alice")[1][0]
        self.assertEqual(device.features, [])
        self.assertEqual(
            device.preferences.model_dump(),
            {
                "revision": 0,
                "camera_snooze_until": {},
                "quiet_hours": None,
                "health_alerts": False,
            },
        )

    def test_malformed_installation_rows_raise_explicit_storage_errors(self):
        for value in ({}, [None], ["invalid"], [{}]):
            with self.subTest(value=value):
                self.db.execute(
                    "UPDATE user SET native_push_devices=? WHERE username='alice'",
                    (json.dumps(value),),
                )
                with self.assertRaisesRegex(ValueError, "registration storage"):
                    self.registry.devices("alice")

    def test_revision_and_owner_role_are_compared_atomically(self):
        self.registry.register("alice", self.device(features=["health-v1"]))
        updated = DevicePreferences(revision=1, health_alerts=True)
        self.assertFalse(
            self.registry.set_preferences("bob", "admin", self.identifier, 0, updated)
        )
        self.assertFalse(
            self.registry.set_preferences("alice", "admin", self.identifier, 0, updated)
        )
        self.assertTrue(
            self.registry.set_preferences(
                "alice", "viewer", self.identifier, 0, updated
            )
        )
        self.assertFalse(
            self.registry.set_preferences(
                "alice", "viewer", self.identifier, 0, updated
            )
        )
        self.assertEqual(self.registry.devices("alice")[1][0].preferences, updated)

    def test_rotation_and_same_account_renewal_keep_preferences_but_account_change_resets(
        self,
    ):
        self.registry.register("alice", self.device(features=["health-v1"]))
        prefs = DevicePreferences(
            revision=1, health_alerts=True, camera_snooze_until={"front": 500}
        )
        self.assertTrue(
            self.registry.set_preferences("alice", "viewer", self.identifier, 0, prefs)
        )
        self.registry.register("alice", self.device(token="b" * 64, registered_at=200))
        current = self.registry.devices("alice")[1][0]
        self.assertEqual(current.preferences, prefs)
        self.assertEqual(current.features, [])
        self.registry.register("bob", self.device(token="c" * 64, registered_at=300))
        self.assertEqual(self.registry.devices("alice")[1], [])
        self.assertEqual(
            self.registry.devices("bob")[1][0].preferences, DevicePreferences()
        )

    def test_registration_scope_change_cannot_accept_a_stale_preference_write(self):
        self.registry.register("alice", self.device(features=["health-v1"]))
        old = self.registry.devices("alice")[1][0]
        self.registry.register("alice", self.device(registered_at=200))
        self.assertFalse(
            self.registry.set_preferences(
                "alice",
                "viewer",
                self.identifier,
                0,
                DevicePreferences(revision=1, health_alerts=True),
                expected_registered_at=old.registered_at,
            )
        )
        current = self.registry.devices("alice")[1][0]
        self.assertEqual(current.preferences.revision, 0)
        self.assertFalse(current.preferences.health_alerts)

    def test_concurrent_revision_writers_have_exactly_one_winner(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.db"
            with closing(sqlite3.connect(path)) as db:
                db.executescript(
                    'CREATE TABLE "user" (username TEXT PRIMARY KEY,role TEXT,'
                    "native_push_devices TEXT NOT NULL DEFAULT '[]');"
                    "INSERT INTO user(username,role) VALUES ('alice','viewer');"
                )
                APNsRegistry(db.execute).register("alice", self.device())
                db.commit()

            def update(index):
                with closing(sqlite3.connect(path, timeout=10)) as db, db:
                    return APNsRegistry(db.execute).set_preferences(
                        "alice",
                        "viewer",
                        self.identifier,
                        0,
                        DevicePreferences(
                            revision=1, camera_snooze_until={"front": 1000 + index}
                        ),
                    )

            with ThreadPoolExecutor(max_workers=8) as pool:
                self.assertEqual(sum(pool.map(update, range(16))), 1)

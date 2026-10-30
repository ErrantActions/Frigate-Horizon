"""Health never equates missing telemetry or idle inference with a healthy recorder."""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from peewee import SqliteDatabase

from frigate.comms.apns_registry import APNsRegistry, NativePushDevice
from frigate.comms.native_health import NativeHealthMonitor
from frigate.comms.native_preferences import DevicePreferences
from frigate.const import RECORD_DIR
from frigate.models import User


class TestNativeHealth(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 1000.0
        record = SimpleNamespace(
            enabled=True,
            continuous=SimpleNamespace(days=3),
            sub=SimpleNamespace(enabled=False, continuous=SimpleNamespace(days=0)),
        )
        self.config = SimpleNamespace(
            cameras={
                name: SimpleNamespace(enabled=True, record=record)
                for name in ("front", "back")
            },
            auth=SimpleNamespace(
                enabled=True, roles={"admin": [], "front_only": ["front"]}
            ),
            apns=SimpleNamespace(enabled=True),
            storage=SimpleNamespace(enabled=False),
        )
        self.stats = {
            "service": {
                "last_updated": self.now,
                "uptime": 300,
                "storage": {RECORD_DIR: {"free": 500, "total": 1000}},
            },
            "processes": {
                name: {"pid": i + 10}
                for i, name in enumerate(("recording", "review_segment", "go2rtc"))
            },
            "detectors": {"coral1": {"pid": 20}, "coral2": {"pid": 21}},
            "cameras": {
                "front": {
                    "detection_fps": 0,
                    "camera_fps": 0,
                    "capture_mode": "recording_only",
                }
            },
        }
        self.app = SimpleNamespace(
            frigate_config=self.config,
            storage_maintainer=None,
            dispatcher=None,
            stats_emitter=SimpleNamespace(
                get_latest_stats=lambda: self.stats,
                stats_tracking={"detectors": {"coral1": object(), "coral2": object()}},
            ),
        )
        self.monitor = NativeHealthMonitor(self.app, clock=lambda: self.now)
        self.monitor._recording_progress = lambda config: {
            f"{name}:main": self.now - 10 for name in config.cameras
        }
        self.enterContext(
            patch("frigate.comms.native_health.psutil.pid_exists", return_value=True)
        )
        self.now = 1201
        self.stats["service"]["last_updated"] = self.now

    async def test_complete_real_recordings_and_alive_processes_ignore_zero_idle_detection_fps(
        self,
    ):
        report = await self.monitor.collect()
        self.assertTrue(report.checks_complete)
        self.assertEqual(report.issues, [])

    async def test_missing_stale_or_invalid_telemetry_never_looks_complete(self):
        del self.stats["service"]["last_updated"]
        report = await self.monitor.collect()
        self.assertFalse(report.checks_complete)
        self.stats["service"]["last_updated"] = self.now - 31
        self.assertFalse((await self.monitor.collect()).checks_complete)
        self.stats["service"]["last_updated"] = self.now
        del self.stats["processes"]["recording"]
        self.assertFalse((await self.monitor.collect()).checks_complete)

    async def test_missing_one_detector_and_invalid_pid_or_storage_are_unknown(self):
        del self.stats["detectors"]["coral2"]
        self.assertFalse((await self.monitor.collect()).checks_complete)
        self.stats["detectors"]["coral2"] = {"pid": 21}
        for value in (True, 1.5, 2**40, float("inf")):
            self.stats["processes"]["recording"]["pid"] = value
            self.assertFalse((await self.monitor.collect()).checks_complete)
        self.stats["processes"]["recording"]["pid"] = 10
        self.stats["service"]["storage"][RECORD_DIR]["free"] = 1001
        self.assertFalse((await self.monitor.collect()).checks_complete)

    async def test_absent_root_stats_and_nonfinite_or_future_recordings_are_unknown(
        self,
    ):
        self.app.stats_emitter.get_latest_stats = lambda: None
        self.assertFalse((await self.monitor.collect()).checks_complete)
        self.app.stats_emitter.get_latest_stats = lambda: self.stats
        for value in (float("nan"), float("inf"), -1, True, self.now + 600):
            self.monitor._recording_progress = lambda config: {
                "front:main": value,
                "back:main": self.now - 10,
            }
            self.assertFalse((await self.monitor.collect()).checks_complete)

    async def test_actual_stalled_recording_is_scoped_and_keeps_stable_first_seen(self):
        self.monitor._recording_progress = lambda config: {"front:main": self.now - 400}
        report = await self.monitor.collect()
        self.assertEqual(len(report.issues), 1)
        issue = report.issues[0]
        self.assertEqual(issue.kind, "recording_stalled")
        self.assertEqual(issue.camera, "front")
        self.assertEqual(issue.severity, "critical")
        first_seen, identity = issue.first_seen, issue.id
        self.now += 20
        self.stats["service"]["last_updated"] = self.now
        next_report = await self.monitor.collect()
        self.assertEqual(next_report.issues[0].id, identity)
        self.assertEqual(next_report.issues[0].first_seen, first_seen)
        scoped = self.monitor.scoped({"username": "alice", "role": "front_only"})
        self.assertEqual(len(scoped.issues), 1)
        self.assertEqual(scoped.issues[0].camera, "front")
        self.config.auth.roles["back_only"] = ["back"]
        self.assertEqual(
            self.monitor.scoped({"username": "bob", "role": "back_only"}).issues, []
        )

    async def test_global_storage_process_issues_are_not_leaked_to_camera_only_roles(
        self,
    ):
        self.stats["service"]["storage"][RECORD_DIR]["free"] = 1
        self.stats["processes"]["recording"]["pid"] = 0
        report = await self.monitor.collect()
        self.assertEqual(
            {issue.kind for issue in report.issues},
            {"local_storage_low", "process_recording"},
        )
        self.assertEqual(
            self.monitor.scoped({"username": "alice", "role": "front_only"}).issues, []
        )
        self.assertEqual(
            len(self.monitor.scoped({"username": "admin", "role": "admin"}).issues), 2
        )

    async def test_queued_issue_is_rejected_after_condition_recovers_or_sample_stales(
        self,
    ):
        self.monitor._recording_progress = lambda config: {"front:main": self.now - 400}
        report = await self.monitor.collect()
        issue = report.issues[0]
        self.assertTrue(
            self.monitor.current_issue(issue.id, issue.camera, issue.first_seen)
        )
        self.monitor._recording_progress = lambda config: {"front:main": self.now - 10}
        await self.monitor.collect()
        self.assertFalse(
            self.monitor.current_issue(issue.id, issue.camera, issue.first_seen)
        )
        self.now += 31
        self.assertFalse(
            self.monitor.current_issue(issue.id, issue.camera, issue.first_seen)
        )

    async def test_wrapper_has_required_nullable_camera_and_explicit_completeness(self):
        self.stats["service"]["storage"][RECORD_DIR]["free"] = 1
        value = (await self.monitor.collect()).model_dump()
        self.assertEqual(set(value), {"sampled_at", "checks_complete", "issues"})
        self.assertEqual(
            set(value["issues"][0]),
            {"id", "kind", "camera", "severity", "title", "detail", "first_seen"},
        )
        self.assertIsNone(value["issues"][0]["camera"])

    async def test_health_delivery_requires_feature_opt_in_debounce_cooldown_and_scope(
        self,
    ):
        database = SqliteDatabase(":memory:")
        self.addCleanup(database.close)
        self.enterContext(database.bind_ctx([User]))
        database.create_tables([User])
        User.create(
            username="alice",
            password_hash="unused",
            role="front_only",
            notification_tokens=[],
        )
        registry = APNsRegistry(database.execute_sql)
        installation = str(uuid4())
        device = NativePushDevice(
            installation,
            "a" * 64,
            "production",
            ["front"],
            self.now,
            features=["health-v1"],
        )
        registry.register("alice", device)
        self.monitor._recording_progress = lambda config: {
            "front:main": self.now - 400,
            "back:main": self.now - 10,
        }
        report = await self.monitor.collect()
        self.assertEqual(self.monitor._eligible(report, self.now), [])
        self.now += 31
        self.stats["service"]["last_updated"] = self.now
        report = await self.monitor.collect()
        self.assertEqual(self.monitor._eligible(report, self.now), [])
        self.assertTrue(
            registry.set_preferences(
                "alice",
                "front_only",
                installation,
                0,
                DevicePreferences(revision=1, health_alerts=True),
            )
        )
        eligible = self.monitor._eligible(report, self.now)
        self.assertEqual(
            [(user, target) for user, target, _ in eligible], [("alice", installation)]
        )
        key = ("alice", installation, report.issues[0].id)
        self.monitor.last_sent[key] = self.now
        self.assertEqual(self.monitor._eligible(report, self.now), [])
        self.now += 901
        self.stats["service"]["last_updated"] = self.now
        report = await self.monitor.collect()
        self.assertEqual(len(self.monitor._eligible(report, self.now)), 1)
        self.config.auth.roles["back_only"] = ["back"]
        User.update(role="back_only").execute()
        self.assertEqual(self.monitor._eligible(report, self.now), [])
        User.update(role="front_only").execute()
        registry.register(
            "alice",
            NativePushDevice(
                installation, "a" * 64, "production", ["front"], self.now, features=[]
            ),
        )
        self.assertEqual(self.monitor._eligible(report, self.now), [])


@unittest.skipIf(os.name == "nt", "Full HTTP auth imports require Linux")
class TestNativeHealthAPI(unittest.TestCase):
    def setUp(self):
        from unittest.mock import AsyncMock

        from frigate.comms.native_health import (
            NativeHealthMonitor,
            NativeHealthResponse,
        )
        from frigate.test import test_native_push_images as image_tests

        self.fixture = image_tests.TestNativeImageAPI()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.client = self.fixture.client
        self.monitor = NativeHealthMonitor(self.client.app)
        self.monitor.latest = NativeHealthResponse(
            sampled_at=100, checks_complete=False
        )
        self.monitor.collect = AsyncMock(return_value=self.monitor.latest)
        self.client.app.state.native_health = self.monitor

    def test_unknown_telemetry_remains_incomplete_and_private(self):
        response = self.client.get(
            "/notifications/apns/health", headers=self.fixture.headers
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            set(response.json()), {"sampled_at", "checks_complete", "issues"}
        )
        self.assertFalse(response.json()["checks_complete"])
        self.assertEqual(response.json()["issues"], [])
        self.assertEqual(response.headers["cache-control"], "private, no-store")

    def test_session_and_revocation_during_observation_are_rechecked(self):
        from datetime import datetime

        response = self.client.get(
            "/notifications/apns/health",
            headers={"remote-user": "alice", "remote-role": "admin"},
        )
        self.assertEqual(response.status_code, 401)

        async def revoke():
            User.update(password_changed_at=datetime.now()).where(
                User.username == "alice"
            ).execute()
            return self.monitor.latest

        self.monitor.collect.side_effect = revoke
        response = self.client.get(
            "/notifications/apns/health", headers=self.fixture.headers
        )
        self.assertEqual(response.status_code, 401, response.text)

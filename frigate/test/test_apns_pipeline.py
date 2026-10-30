"""Verify native delivery shares browser notification eligibility and failures."""

import asyncio
import queue
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from frigate.comms.webpush import PushNotification, WebPushClient


class TestNativePipeline(unittest.IsolatedAsyncioTestCase):
    async def test_native_provider_starts_without_enabling_camera_rules(self):
        from frigate.app import FrigateApp

        for enabled, authenticated, camera_enabled, expected in (
            (False, True, False, False),
            (True, False, False, False),
            (True, True, False, True),
            (False, True, True, True),
        ):
            app = FrigateApp.__new__(FrigateApp)
            config = SimpleNamespace(
                mqtt=SimpleNamespace(enabled=False),
                apns=SimpleNamespace(enabled=enabled),
                auth=SimpleNamespace(enabled=authenticated),
                cameras={
                    "front": SimpleNamespace(
                        enabled=True,
                        notifications=SimpleNamespace(enabled_in_config=camera_enabled),
                    )
                },
            )
            app.config_holder = SimpleNamespace(config=config)
            for name in (
                "stop_event",
                "inter_process_communicator",
                "camera_activity_communicator",
                "inter_config_updater",
                "onvif_controller",
                "ptz_metrics",
                "notice_registry",
            ):
                setattr(app, name, MagicMock())
            with (
                patch("frigate.app.WebPushClient") as push,
                patch("frigate.app.WebSocketClient"),
                patch("frigate.app.Dispatcher"),
            ):
                app.init_dispatcher()
                self.assertEqual(push.call_count, int(expected))

    def client(self):
        client = WebPushClient.__new__(WebPushClient)
        client.config = SimpleNamespace(
            auth=SimpleNamespace(enabled=True, roles={"viewer": []}),
            apns=SimpleNamespace(enabled=True),
            cameras={
                "front": SimpleNamespace(
                    enabled=True, notifications=SimpleNamespace(enabled=True)
                )
            },
        )
        client.is_camera_suspended = MagicMock(return_value=False)
        return client

    async def test_native_rechecks_database_camera_rules_and_suspension(self):
        notification = PushNotification("alice", {"camera": "front"}, "Title", "Body")
        for condition in ("allowed", "denied", "disabled", "suspended", "provider_off"):
            client = self.client()
            client.config.cameras["front"].notifications.enabled = (
                condition != "disabled"
            )
            client.config.apns.enabled = condition != "provider_off"
            client.is_camera_suspended.return_value = condition == "suspended"
            transport = SimpleNamespace(send=AsyncMock(return_value=[]))
            with (
                patch("frigate.comms.webpush.APNsRegistry") as registry,
                patch("frigate.comms.webpush.User"),
            ):
                registry.return_value.authorized_devices.return_value = (
                    [] if condition == "denied" else [object()]
                )
                await client._send_native_notification(notification, transport)
            self.assertEqual(
                transport.send.await_count, 1 if condition == "allowed" else 0
            )

    async def test_browser_provider_failure_does_not_skip_native(self):
        client = self.client()
        client.stop_event = threading.Event()
        client.notification_queue = queue.Queue()
        client.notification_queue.put(
            PushNotification("alice", {"camera": "front"}, "Title", "Body")
        )
        client._send_browser_notification = MagicMock(
            side_effect=RuntimeError("sensitive content")
        )

        async def native(*args):
            client.stop_event.set()

        client._send_native_notification = AsyncMock(side_effect=native)
        loop = asyncio.new_event_loop()
        try:
            with self.assertLogs("frigate.comms.webpush", level="ERROR") as logs:
                await asyncio.to_thread(client._notification_worker, loop, object())
            self.assertEqual(client._send_native_notification.await_count, 1)
            self.assertNotIn("sensitive content", "\n".join(logs.output))
        finally:
            loop.close()

    async def test_native_users_share_existing_cooldown_without_browser_email(self):
        client = self.client()
        client.config.notifications = SimpleNamespace(email="", cooldown=60)
        client.config.cameras["front"].notifications.cooldown = 30
        client.last_notification_time = 0
        client.last_camera_notification_time = {"front": 0}
        client.web_pushers = {}
        client.user_cameras = {"alice": {"front"}}
        client.check_registrations = MagicMock(
            side_effect=RuntimeError("browser provider unavailable")
        )
        client.cleanup_registrations = MagicMock()
        client.send_push_notification = MagicMock()
        payload = {
            "camera": "front",
            "event_id": "event-1",
            "name": "person",
            "type": "object",
            "score": 0.9,
        }
        client.send_trigger(payload)
        client.send_trigger(payload)
        self.assertEqual(client.send_push_notification.call_count, 1)
        self.assertEqual(
            client.send_push_notification.call_args.kwargs["user"], "alice"
        )

    async def test_best_person_alert_waits_for_end_before_consuming_shared_cooldown(
        self,
    ):
        client = self.client()
        client.config.notifications = SimpleNamespace(email="", cooldown=60)
        camera = client.config.cameras["front"]
        camera.name = "front"
        camera.snapshots = SimpleNamespace(native_person=True)
        camera.notifications.cooldown = 30
        client.last_notification_time = 0
        client.last_camera_notification_time = {"front": 0}
        client.web_pushers = {}
        client.user_cameras = {"alice": {"front"}}
        client.cleanup_registrations = MagicMock()
        client.send_push_notification = MagicMock()
        payload = {
            "type": "new",
            "after": {
                "camera": "front",
                "severity": "alert",
                "id": "review-person",
                "start_time": 1000,
                "thumb_path": "/media/frigate/clips/review/person.webp",
                "data": {
                    "objects": ["person"],
                    "sub_labels": [],
                    "zones": [],
                    "metadata": None,
                },
            },
        }
        with patch(
            "frigate.motion.native_person.person_review_window_matches",
            return_value=True,
        ):
            client.send_alert(payload)
            self.assertEqual(client.last_notification_time, 0)
            client.send_push_notification.assert_not_called()
            payload["type"] = "end"
            client.send_alert(payload)
            client.send_alert(payload)
        self.assertEqual(client.send_push_notification.call_count, 1)
        self.assertGreater(client.last_notification_time, 0)

    async def test_queued_browser_alert_rechecks_current_account_permissions(self):
        notification = PushNotification("alice", {"camera": "front"}, "Title", "Body")
        for role in ("viewer", "revoked", None):
            client = self.client()
            client.config.notifications = SimpleNamespace(email="owner@example.invalid")
            client.check_registrations = MagicMock()
            pusher = MagicMock()
            pusher.subscription_info = {
                "endpoint": "https://web.push.apple.com/example"
            }
            pusher.send.return_value.status_code = 201
            client.web_pushers = {"alice": [pusher]}
            client.claim_headers = {"https://web.push.apple.com": {}}
            with patch("frigate.comms.webpush.User.select") as query:
                query.return_value.where.return_value.first.return_value = (
                    SimpleNamespace(role=role) if role else None
                )
                client._send_browser_notification(notification)
            self.assertEqual(pusher.send.call_count, 1 if role == "viewer" else 0)

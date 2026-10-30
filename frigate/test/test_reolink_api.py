"""Native alarm-state integration never invents confidence or decodes video."""

import asyncio
import json
import logging
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from frigate.api.defs.request.onvif_body import OnvifConnectionBody
from frigate.comms.reolink_api import (
    NativeAlarmBridge,
    NativeAlarmClient,
    ReolinkApiError,
    ReolinkEventManager,
    parse_alarm_states,
    save_alarm_snapshot,
)


def ai(person=0, vehicle=0, animal=0):
    return {
        "channel": 0,
        "people": {"support": 1, "alarm_state": person},
        "vehicle": {"support": 1, "alarm_state": vehicle},
        "dog_cat": {"support": 1, "alarm_state": animal},
    }


class TestNativeStates(unittest.TestCase):
    def test_snapshot_availability_is_per_label_when_person_waits_for_coral(self):
        publish = MagicMock()
        bridge = NativeAlarmBridge("front", publish)
        bridge.observe(
            {"person": True, "vehicle": True, "animal": False},
            1000,
            snapshot_available={"vehicle"},
        )
        values = {
            call.args[0]["label"]: call.args[0]["snapshot_available"]
            for call in publish.call_args_list
        }
        self.assertEqual(values, {"person": False, "vehicle": True})

    @unittest.skipIf(
        sys.platform == "win32", "Native camera/config integration requires Linux"
    )
    def test_person_analysis_does_not_save_untracked_native_snapshot_orphans(self):
        camera = SimpleNamespace(
            enabled=True,
            detect=SimpleNamespace(enabled=False),
            snapshots=SimpleNamespace(native_person=True),
        )
        manager = ReolinkEventManager(
            lambda: SimpleNamespace(cameras={"front": camera})
        )
        manager.publisher = MagicMock()
        manager.person_analysis = MagicMock()
        manager.person_analysis.status.return_value = {"state": "ready"}
        manager.person_analysis.consume.return_value = True
        api = MagicMock()

        async def read():
            manager.stop_event.set()
            return {"person": True, "vehicle": False, "animal": False}

        api.read = AsyncMock(side_effect=read)
        api.snapshot = AsyncMock()
        api.logout = AsyncMock()
        with (
            patch("frigate.ptz.device_manager.connection_for", return_value=object()),
            patch("frigate.comms.reolink_api.NativeAlarmClient", return_value=api),
            patch.object(manager, "_reference_active", return_value=False),
            patch("frigate.comms.reolink_api.save_alarm_snapshot") as save,
        ):
            asyncio.run(manager._camera("front"))
        api.snapshot.assert_not_awaited()
        save.assert_not_called()
        manager.person_analysis.consume.assert_called()

    @unittest.skipIf(sys.platform == "win32", "Review integration requires Linux")
    def test_review_starts_and_ends_without_any_video_frame(self):
        import threading

        from frigate.comms.detections_updater import DetectionTypeEnum
        from frigate.test.test_review_flow import CAMERA, ReviewFlowTestCase
        from frigate.track.object_processing import ManualEventState

        fixture = ReviewFlowTestCase()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        maintainer = fixture.maintainer
        maintainer.stop_event = threading.Event()
        identity = "1000.000000-abcdef"
        with tempfile.TemporaryDirectory() as directory:
            thumb = Path(directory) / CAMERA / f"{identity}.webp"
            thumb.parent.mkdir()
            thumb.write_bytes(b"camera-encoded-thumbnail")
            Path(fixture.clips_dir.name, "review").mkdir()
            queue = [
                (
                    DetectionTypeEnum.api.value,
                    (
                        CAMERA,
                        1000,
                        {
                            "state": ManualEventState.start,
                            "event_id": identity,
                            "label": "person",
                            "end_time": None,
                            "source": "reolink",
                        },
                    ),
                ),
                (
                    DetectionTypeEnum.api.value,
                    (
                        CAMERA,
                        1005,
                        {
                            "state": ManualEventState.end,
                            "event_id": identity,
                            "end_time": 1005,
                        },
                    ),
                ),
            ]

            def update(timeout):
                if not queue:
                    maintainer.stop_event.set()
                    return None
                return queue.pop(0)

            maintainer.detection_subscriber.check_for_update.side_effect = update
            with (
                patch(
                    "frigate.review.maintainer.native_api_selected", return_value=True
                ),
                patch("frigate.review.maintainer.THUMB_DIR", directory),
            ):
                maintainer.run()
            reviews = fixture.reviews()
            self.assertEqual(reviews[0]["type"], "new")
            self.assertEqual(reviews[-1]["type"], "end")
            self.assertEqual(reviews[-1]["after"]["end_time"], 1005)
            self.assertIn("person", reviews[-1]["after"]["data"]["objects"])
            maintainer.frame_manager.get.assert_not_called()
            self.assertIsNone(maintainer.active_review_segments[CAMERA])

    def test_event_snapshot_uses_a_camera_still_and_writes_real_webp_files(self):
        import cv2
        import numpy as np

        image = np.full((360, 640, 3), 120, dtype=np.uint8)
        success, encoded = cv2.imencode(".jpg", image)
        self.assertTrue(success)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("frigate.const.THUMB_DIR", str(root / "thumbs")),
                patch("frigate.const.CLIPS_DIR", str(root)),
            ):
                save_alarm_snapshot("front", {"person": "event"}, encoded.tobytes())
            thumbnail = cv2.imread(str(root / "thumbs" / "front" / "event.webp"))
            self.assertEqual(thumbnail.shape[0], 175)
            self.assertTrue((root / "front-event-clean.webp").is_file())

    def test_http_log_redacts_tokens_and_does_not_log_credentials(self):
        from frigate.comms.reolink_api import _PrivateReolinkHttpLog

        record = logging.LogRecord(
            "httpx",
            logging.INFO,
            "",
            0,
            "HTTP Request: GET http://camera.invalid/cgi-bin/api.cgi?token=PrivateToken",
            (),
            None,
        )
        self.assertTrue(_PrivateReolinkHttpLog().filter(record))
        self.assertNotIn("PrivateToken", record.getMessage())
        self.assertNotIn("camera.invalid", record.getMessage())

    def test_refresh_keeps_one_event_alive_for_a_long_camera_alarm(self):
        publish = MagicMock()
        bridge = NativeAlarmBridge("front", publish)
        for timestamp in range(100, 500):
            bridge.observe(parse_alarm_states({"state": 1}, ai(1)), timestamp)
        self.assertEqual(bridge.starts, 1)
        self.assertEqual(bridge.ends, 0)
        self.assertEqual(len(bridge.active), 1)
        self.assertEqual(bridge.active["person"].last_seen, 499)

    def test_actual_firmware_shape_and_label_mapping(self):
        self.assertEqual(
            parse_alarm_states({"state": 1}, ai(1)),
            {
                "motion": True,
                "person": True,
                "vehicle": False,
                "animal": False,
            },
        )

    def test_unknown_missing_invalid_or_wrong_channel_cannot_be_quiet(self):
        for motion, value in (
            ({}, ai()),
            ({"state": True}, ai()),
            ({"state": 0}, {}),
            ({"state": 0}, {**ai(), "channel": 1}),
            ({"state": 0}, ai(2)),
        ):
            with self.assertRaises(ValueError):
                parse_alarm_states(motion, value)

    def test_alarm_start_refresh_clear_and_absent_confidence(self):
        publish = MagicMock()
        bridge = NativeAlarmBridge("front", publish)
        bridge.observe(parse_alarm_states({"state": 1}, ai(1)), 100)
        self.assertEqual(bridge.starts, 1)
        start = publish.call_args.args[0]
        self.assertEqual(start["label"], "person")
        self.assertIsNone(start["confidence"])
        self.assertEqual(start["source"], "reolink")
        bridge.observe(parse_alarm_states({"state": 1}, ai(1)), 104)
        self.assertEqual(publish.call_args.args[0]["action"], "refresh")
        self.assertEqual(bridge.starts, 1)
        bridge.observe(parse_alarm_states({"state": 0}, ai()), 104.5)
        self.assertIn("person", bridge.active)
        bridge.observe(parse_alarm_states({"state": 0}, ai()), 105)
        self.assertEqual(publish.call_args.args[0]["action"], "end")
        self.assertFalse(bridge.active)

    def test_motion_alone_does_not_become_person_or_review_alert(self):
        publish = MagicMock()
        NativeAlarmBridge("front", publish).observe(
            parse_alarm_states({"state": 1}, ai()),
            100,
        )
        publish.assert_not_called()

    def test_reference_does_not_duplicate_original_object_events(self):
        publish = MagicMock()
        bridge = NativeAlarmBridge("front", publish, reference=True)
        bridge.observe(parse_alarm_states({"state": 1}, ai(1, 1)), 100)
        bridge.observe(parse_alarm_states({"state": 0}, ai()), 101)
        publish.assert_not_called()
        self.assertEqual(len(bridge.history), 3)

    def test_disconnect_ends_camera_episodes_and_new_true_state_restarts(self):
        publish = MagicMock()
        bridge = NativeAlarmBridge("front", publish)
        value = parse_alarm_states({"state": 1}, ai(1))
        bridge.observe(value, 100)
        first = bridge.active["person"].event_id
        bridge.close(102)
        self.assertFalse(bridge.active)
        bridge.observe(value, 103)
        self.assertNotEqual(bridge.active["person"].event_id, first)

    @unittest.skipIf(
        sys.platform == "win32", "The event processor requires Linux resource and IPC"
    )
    def test_processor_does_not_invent_snapshots_or_confidence(self):
        from frigate.track.object_processing import TrackedObjectProcessor

        processor = SimpleNamespace(
            camera_states={
                "front": SimpleNamespace(
                    camera_config=SimpleNamespace(
                        record=SimpleNamespace(event_pre_capture=5, enabled=True)
                    ),
                    save_manual_event_image=MagicMock(),
                )
            },
            onvif_manual_deadlines={},
            ongoing_manual_events={},
            reolink_manual_event_ids=set(),
            plugin_manual_event_ids=set(),
            event_sender=MagicMock(),
            detection_publisher=MagicMock(),
        )
        TrackedObjectProcessor.create_manual_event(
            processor,
            (
                time.time(),
                "front",
                "person",
                "test",
                True,
                None,
                None,
                15,
                "reolink",
                {},
                None,
            ),
        )
        event = processor.event_sender.publish.call_args.args[0][4]
        self.assertIsNone(event["score"])
        self.assertFalse(event["has_snapshot"])
        self.assertEqual(event["type"], "reolink")
        processor.camera_states["front"].save_manual_event_image.assert_not_called()
        self.assertIn("test", processor.onvif_manual_deadlines)

    def test_manager_reports_stale_instead_of_healthy_on_old_data(self):
        manager = ReolinkEventManager(lambda: None)
        manager.statuses["front"] = {"state": "running", "updated_at": time.time() - 10}
        self.assertEqual(manager.snapshot("front")["state"], "stale")


class TestNativeHttpClient(unittest.IsolatedAsyncioTestCase):
    async def test_token_authentication_and_reauthentication_on_minus_six(self):
        logins, reads = 0, 0
        commands = []

        def handle(request):
            nonlocal logins, reads
            body = json.loads(request.content)[0]
            command = body["cmd"]
            commands.append(command)
            code, value, error = 0, {}, None
            if command == "Login":
                logins += 1
                value = {
                    "Token": {"name": "PrivateToken" + str(logins), "leaseTime": 3600}
                }
                self.assertEqual(body["param"]["User"]["password"], "private-password")
            elif command == "GetMdState":
                reads += 1
                if reads == 1:
                    code, error = 1, {"rspCode": -6}
                else:
                    value = {"state": 0}
            elif command == "GetAiState":
                value = ai()
            return httpx.Response(
                200,
                json=[
                    {
                        "cmd": command,
                        "code": code,
                        "value": value,
                        "error": error,
                    }
                ],
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            api = NativeAlarmClient(
                OnvifConnectionBody(
                    host="camera.invalid", username="test", password="private-password"
                ),
                client,
            )
            value = await api.read()
            self.assertFalse(value["person"])
            self.assertEqual(logins, 2)
            await api.logout()
            self.assertIsNone(api.token)
        self.assertEqual(commands[-1], "Logout")

    async def test_http_and_vendor_errors_are_not_silent_false_states(self):
        def handle(request):
            command = json.loads(request.content)[0]["cmd"]
            if command == "Login":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "cmd": command,
                            "code": 0,
                            "value": {
                                "Token": {"name": "PrivateToken", "leaseTime": 3600}
                            },
                        }
                    ],
                )
            return httpx.Response(
                200,
                json=[
                    {
                        "cmd": command,
                        "code": 1,
                        "error": {"rspCode": -9},
                    }
                ],
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            api = NativeAlarmClient(OnvifConnectionBody(host="camera.invalid"), client)
            with self.assertRaises(ReolinkApiError):
                await api.read()


class TestNativeHttpRedirects(unittest.IsolatedAsyncioTestCase):
    async def test_no_redirect_or_external_endpoint_is_accepted_as_success(self):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(302)),
            follow_redirects=False,
        ) as client:
            api = NativeAlarmClient(OnvifConnectionBody(host="camera.invalid"), client)
            with self.assertRaises(ReolinkApiError):
                await api.read()


@unittest.skipIf(sys.platform == "win32", "Review pipeline requires Linux")
class TestNativeReviewRouting(unittest.TestCase):
    def feed(self, label, review_config=""):
        import threading

        from frigate.comms.detections_updater import DetectionTypeEnum
        from frigate.test.test_review_flow import CAMERA, ReviewFlowTestCase
        from frigate.track.object_processing import ManualEventState

        fixture = ReviewFlowTestCase()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        maintainer = fixture._make_maintainer(review_config)
        maintainer.stop_event = threading.Event()
        Path(fixture.clips_dir.name, "review").mkdir(exist_ok=True)
        identity = "1000.000000-abcdef"
        queue = [
            (
                DetectionTypeEnum.api.value,
                (
                    CAMERA,
                    1000,
                    {
                        "state": ManualEventState.start,
                        "event_id": identity,
                        "label": label,
                        "end_time": None,
                        "source": "reolink",
                    },
                ),
            ),
            (
                DetectionTypeEnum.api.value,
                (
                    CAMERA,
                    1002,
                    {
                        "state": ManualEventState.end,
                        "event_id": identity,
                        "end_time": 1002,
                        "source": "reolink",
                    },
                ),
            ),
        ]

        def update(timeout):
            if not queue:
                maintainer.stop_event.set()
                return None
            return queue.pop(0)

        maintainer.detection_subscriber.check_for_update.side_effect = update
        with (
            patch("frigate.review.maintainer.native_api_selected", return_value=True),
            self.assertNoLogs("frigate.review.maintainer", level="ERROR"),
        ):
            maintainer.run()
        maintainer.frame_manager.get.assert_not_called()
        self.assertFalse(maintainer.indefinite_events.get(CAMERA))
        return [
            json.loads(call.args[1])
            for call in maintainer.requestor.send_data.call_args_list
            if call.args[0] == "reviews"
        ]

    def test_vehicle_starts_and_ends_as_a_detection_without_video(self):
        reviews = self.feed("vehicle")
        self.assertEqual(reviews[0]["type"], "new")
        self.assertEqual(reviews[0]["after"]["severity"], "detection")
        self.assertEqual(reviews[-1]["type"], "end")
        self.assertEqual(reviews[-1]["after"]["end_time"], 1002)

    def test_configured_person_alert_is_preserved(self):
        self.assertEqual(self.feed("person")[0]["after"]["severity"], "alert")

    def test_zone_required_person_alarm_does_not_claim_zone_entry(self):
        reviews = self.feed(
            "person",
            """
    review:
      alerts:
        required_zones: [driveway]
""",
        )
        self.assertEqual(reviews[0]["after"]["severity"], "detection")

    def test_disabled_detection_is_not_promoted_or_left_open(self):
        self.assertEqual(
            self.feed(
                "vehicle",
                """
    review:
      detections:
        enabled: False
""",
            ),
            [],
        )

    def test_filtered_native_label_and_end_are_ignored_cleanly(self):
        self.assertEqual(
            self.feed(
                "vehicle",
                """
    review:
      detections:
        labels: [person]
""",
            ),
            [],
        )

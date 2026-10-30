"""Existing external-event pipeline behavior for opt-in ONVIF signals, with mocks."""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from frigate.track.object_processing import ManualEventState, TrackedObjectProcessor


class TestOnvifManualEventPipeline(unittest.TestCase):
    def setUp(self):
        self.processor = object.__new__(TrackedObjectProcessor)
        self.camera = SimpleNamespace(enabled=True)
        self.processor.config = SimpleNamespace(cameras={"front": self.camera})
        self.processor.camera_states = {
            "front": SimpleNamespace(
                camera_config=SimpleNamespace(
                    record=SimpleNamespace(enabled=True, event_pre_capture=5)
                ),
                save_manual_event_image=MagicMock(),
            )
        }
        self.processor.event_sender = MagicMock()
        self.processor.detection_publisher = MagicMock()
        self.processor.ongoing_manual_events = {}
        self.processor.reolink_manual_event_ids = set()
        self.processor.plugin_manual_event_ids = set()
        self.processor.onvif_manual_deadlines = {}

    def create(self, source="onvif", duration=30):
        with (
            patch("frigate.track.object_processing.time.time", return_value=1000),
            patch("frigate.track.object_processing.time.monotonic", return_value=100),
        ):
            self.processor.create_manual_event(
                (
                    1000,
                    "front",
                    "onvif_motion",
                    "native-id",
                    True,
                    0,
                    None,
                    duration,
                    source,
                    {},
                    None,
                )
            )

    def test_onvif_uses_manual_review_start_and_preserves_non_object_source(self):
        self.create()
        event = self.processor.event_sender.publish.call_args.args[0][4]
        self.assertEqual(event["type"], "onvif")
        self.assertEqual(event["label"], "onvif_motion")
        self.assertEqual(event["draw"], {})
        self.assertIsNone(event["end_time"])
        manual = self.processor.detection_publisher.publish.call_args.args[0][2]
        self.assertEqual(manual["state"], ManualEventState.start)
        self.assertIn("native-id", self.processor.onvif_manual_deadlines)

    def test_native_end_clears_the_independent_watchdog(self):
        self.create()
        self.processor.end_manual_event(("native-id", 1002))
        self.assertNotIn("native-id", self.processor.onvif_manual_deadlines)
        self.assertNotIn("native-id", self.processor.ongoing_manual_events)
        manual = self.processor.detection_publisher.publish.call_args.args[0][2]
        self.assertEqual(manual["state"], ManualEventState.end)

    def test_reolink_source_is_preserved_through_the_independent_expiry(self):
        self.create(source="reolink")
        self.assertIn("native-id", self.processor.reolink_manual_event_ids)
        with patch("frigate.track.object_processing.time.monotonic", return_value=131):
            self.processor.expire_onvif_events(1031)
        manual = self.processor.detection_publisher.publish.call_args.args[0][2]
        self.assertEqual(manual["state"], ManualEventState.end)
        self.assertEqual(manual["source"], "reolink")
        self.assertFalse(self.processor.reolink_manual_event_ids)

    def test_watchdog_expires_a_source_without_an_end_packet(self):
        self.create()
        with patch("frigate.track.object_processing.time.monotonic", return_value=131):
            self.processor.expire_onvif_events(1031)
        end = self.processor.event_sender.publish.call_args.args[0][4]
        self.assertEqual(end["end_time"], 1030)
        self.assertEqual(self.processor.onvif_manual_deadlines, {})

    def test_disabling_camera_ends_native_event_before_deadline(self):
        self.create()
        self.camera.enabled = False
        with patch("frigate.track.object_processing.time.monotonic", return_value=101):
            self.processor.expire_onvif_events(1001)
        self.assertEqual(self.processor.onvif_manual_deadlines, {})
        self.assertEqual(
            self.processor.event_sender.publish.call_args.args[0][4]["end_time"], 1001
        )

    def test_ordinary_finite_manual_api_events_keep_existing_behavior(self):
        self.create(source="api")
        event = self.processor.event_sender.publish.call_args.args[0][4]
        self.assertEqual(event["end_time"], 1030)
        manual = self.processor.detection_publisher.publish.call_args.args[0][2]
        self.assertEqual(manual["state"], ManualEventState.complete)
        self.assertEqual(self.processor.onvif_manual_deadlines, {})

    def test_invalid_native_expiry_does_not_publish(self):
        for duration in (None, 0, 301, True, float("nan")):
            with self.subTest(duration=duration):
                self.create(duration=duration)
        self.processor.event_sender.publish.assert_not_called()

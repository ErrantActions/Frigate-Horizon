"""Motion wakes AI briefly without restarting continuous recording processes."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from frigate.plugins import inference
from frigate.plugins.inference import EventInferenceConfig, EventInferenceController


class TestEventInferenceLease(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.patch = patch.object(inference, "LEASE_PATH", self.root / "leases.json")
        self.patch.start()
        self.addCleanup(self.patch.stop)
        inference._read_leases.cache_clear()
        self.now = 100.0
        self.controller = EventInferenceController(self.root, clock=lambda: self.now)
        self.controller.initialize()

    def test_disabled_default_does_not_start_on_motion(self):
        self.assertFalse(self.controller.trigger("FrontDoor", "motion"))
        self.assertFalse(inference.lease_status("FrontDoor", self.now)["active"])

    def test_motion_window_expires_at_exactly_30_seconds(self):
        self.controller.configure("FrontDoor", EventInferenceConfig(enabled=True))
        self.assertTrue(self.controller.trigger("FrontDoor", "motion"))
        self.assertTrue(inference.lease_status("FrontDoor", 129.999)["active"])
        self.assertFalse(inference.lease_status("FrontDoor", 130)["active"])
        self.assertFalse(inference.lease_status("another_camera", 110)["active"])

    def test_doorbell_press_does_not_wake_ai(self):
        self.controller.configure("FrontDoor", EventInferenceConfig(enabled=True))
        self.assertFalse(self.controller.trigger("FrontDoor", "doorbell"))
        self.assertFalse(inference.lease_status("FrontDoor", self.now)["active"])

    def test_new_motion_extends_from_the_new_trigger(self):
        self.controller.configure("FrontDoor", EventInferenceConfig(enabled=True))
        self.controller.trigger("FrontDoor", "motion")
        self.now = 120
        self.controller.trigger("FrontDoor", "motion")
        self.assertTrue(inference.lease_status("FrontDoor", 149.999)["active"])
        self.assertFalse(inference.lease_status("FrontDoor", 150)["active"])

    def test_settings_alone_do_not_start_inference(self):
        self.controller.configure("FrontDoor", EventInferenceConfig(enabled=True))
        self.assertFalse(inference.lease_status("FrontDoor", self.now)["active"])

    def test_disabling_revokes_the_window_immediately(self):
        self.controller.configure("FrontDoor", EventInferenceConfig(enabled=True))
        self.controller.trigger("FrontDoor", "motion")
        self.controller.configure("FrontDoor", EventInferenceConfig(enabled=False))
        self.assertFalse(inference.lease_status("FrontDoor", 110)["active"])

    def test_restart_restores_preferences_but_never_a_trigger(self):
        self.controller.configure("FrontDoor", EventInferenceConfig(enabled=True))
        self.controller.trigger("FrontDoor", "motion")
        replacement = EventInferenceController(self.root, clock=lambda: self.now)
        replacement.initialize()
        self.assertTrue(replacement.preferences["FrontDoor"].enabled)
        self.assertFalse(inference.lease_status("FrontDoor", 110)["active"])

    def test_invalid_long_lease_fails_closed_with_an_error(self):
        inference.LEASE_PATH.write_text(
            json.dumps(
                {
                    "FrontDoor": {"started": 100, "expires": 1000},
                }
            )
        )
        with self.assertLogs("frigate.plugins.inference", level="ERROR"):
            self.assertFalse(inference.lease_status("FrontDoor", 110)["active"])


@unittest.skipIf(sys.platform == "win32", "Camera pipeline requires Linux")
class TestEventInferenceCameraPipeline(unittest.TestCase):
    def test_new_camera_with_no_learned_regions_still_gets_ai_scan(self):
        from frigate.video.detect import event_inference_regions

        regions = event_inference_regions(
            (720, 1280), 320, [[{"x": 0, "y": 0, "sizes": []}]]
        )
        self.assertTrue(regions)
        self.assertLessEqual(regions[0][0], 0)
        self.assertLessEqual(regions[0][1], 0)
        self.assertGreaterEqual(regions[0][2], 1280)
        self.assertGreaterEqual(regions[0][3], 720)

    def test_capture_permission_is_independent_of_storage_mode(self):
        from frigate.motion import record_only

        with (
            patch.object(record_only, "recording_only", return_value=True),
            patch.object(record_only, "plugin_inference_active", return_value=True),
        ):
            self.assertTrue(record_only.detection_capture_active("FrontDoor"))
            self.assertTrue(record_only.recording_only("FrontDoor"))

    def test_detection_command_has_no_recording_outputs(self):
        from frigate.motion.record_only import event_detection_command
        from frigate.test.test_compressed_live import config

        camera = config()
        original = camera.model_dump()
        command = event_detection_command(camera)
        self.assertIn("pipe:", command)
        self.assertNotIn("-segment_time", command)
        self.assertNotIn("-segment_format", command)
        self.assertEqual(camera.model_dump(), original)

    def test_expiry_stops_only_the_decoder(self):
        from frigate.video.ffmpeg import CameraWatchdog

        watchdog = CameraWatchdog.__new__(CameraWatchdog)
        watchdog.config = SimpleNamespace(name="FrontDoor", enabled=True)
        decoder = MagicMock()
        decoder.poll.return_value = None
        recorder = MagicMock()
        watchdog.ffmpeg_detect_process = decoder
        watchdog.ffmpeg_other_processes = [{"process": recorder}]
        watchdog.capture_thread = MagicMock()
        watchdog.capture_thread.is_alive.return_value = False
        watchdog.logger = MagicMock()
        watchdog.ffmpeg_pid = SimpleNamespace(value=123)
        watchdog.camera_fps = SimpleNamespace(value=5)
        watchdog.skipped_fps = SimpleNamespace(value=0)
        watchdog.detect_status = MagicMock()
        with (
            patch("frigate.video.ffmpeg.recording_only", return_value=True),
            patch("frigate.video.ffmpeg.plugin_inference_active", return_value=False),
        ):
            self.assertTrue(watchdog._sync_event_inference())
        decoder.terminate.assert_called_once()
        recorder.terminate.assert_not_called()
        recorder.kill.assert_not_called()
        self.assertEqual(len(watchdog.ffmpeg_other_processes), 1)
        self.assertIsNone(watchdog.ffmpeg_detect_process)
        self.assertEqual(watchdog.ffmpeg_pid.value, 0)

    def test_motion_starts_only_one_detection_process(self):
        from frigate.test.test_compressed_live import config
        from frigate.video.ffmpeg import CameraWatchdog

        watchdog = CameraWatchdog.__new__(CameraWatchdog)
        watchdog.config = config()
        watchdog.ffmpeg_detect_process = None
        recorder = MagicMock()
        watchdog.ffmpeg_other_processes = [{"process": recorder}]
        watchdog.ffmpeg_pid = SimpleNamespace(value=0)
        for name in (
            "logger",
            "logpipe",
            "frame_size",
            "shm_frame_count",
            "frame_index",
            "frame_shape",
            "frame_queue",
            "camera_fps",
            "skipped_fps",
            "stop_event",
        ):
            setattr(watchdog, name, MagicMock())
        with (
            patch("frigate.video.ffmpeg.recording_only", return_value=True),
            patch("frigate.video.ffmpeg.plugin_inference_active", return_value=True),
            patch("frigate.video.ffmpeg.detection_capture_active", return_value=True),
            patch("frigate.video.ffmpeg.start_or_restart_ffmpeg") as start,
            patch("frigate.video.ffmpeg.CameraCaptureRunner") as capture,
        ):
            self.assertTrue(watchdog._sync_event_inference())
            self.assertFalse(watchdog._sync_event_inference())
        start.assert_called_once()
        command = start.call_args.args[0]
        self.assertIn("pipe:", command)
        self.assertNotIn("-segment_time", command)
        capture.return_value.start.assert_called_once()
        recorder.terminate.assert_not_called()
        recorder.kill.assert_not_called()
        self.assertEqual(watchdog.ffmpeg_other_processes, [{"process": recorder}])
        self.assertFalse(watchdog.detect_process_records_sub)

    def test_decoder_hang_is_bounded_without_stopping_recorder(self):
        from frigate.video.ffmpeg import CameraWatchdog

        watchdog = CameraWatchdog.__new__(CameraWatchdog)
        watchdog.config = SimpleNamespace(name="FrontDoor", enabled=True)
        decoder = MagicMock()
        decoder.poll.return_value = None
        decoder.wait.side_effect = [subprocess.TimeoutExpired("ffmpeg", 3), 0]
        recorder = MagicMock()
        watchdog.ffmpeg_detect_process = decoder
        watchdog.ffmpeg_other_processes = [{"process": recorder}]
        watchdog.capture_thread = None
        watchdog.logger = MagicMock()
        watchdog.ffmpeg_pid = SimpleNamespace(value=123)
        watchdog.camera_fps = SimpleNamespace(value=5)
        watchdog.skipped_fps = SimpleNamespace(value=0)
        watchdog.detect_status = MagicMock()
        with (
            patch("frigate.video.ffmpeg.recording_only", return_value=True),
            patch("frigate.video.ffmpeg.plugin_inference_active", return_value=False),
        ):
            watchdog._sync_event_inference()
        decoder.kill.assert_called_once()
        recorder.kill.assert_not_called()

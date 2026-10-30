"""Disabling legacy decoding must retain stream-copy recordings, not stop whole cameras."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from frigate.motion.record_only import (
    RecordingOnlyMode,
    configuration,
    recording_metadata_cutoff,
    recording_only,
    recording_only_commands,
)
from frigate.test.test_camera_watchdog import build_watchdog
from frigate.test.test_compressed_live import config


class TestRecordingOnly(unittest.TestCase):
    def test_watchdog_does_not_start_a_legacy_decoder_in_recording_only_mode(self):
        watchdog = build_watchdog()
        watchdog.capture_thread = MagicMock()
        with (
            patch("frigate.video.ffmpeg.detection_capture_active", return_value=False),
            patch("frigate.video.ffmpeg.start_or_restart_ffmpeg") as start,
        ):
            watchdog.start_ffmpeg_detect()
        start.assert_not_called()
        self.assertIsNone(watchdog.capture_thread)
        self.assertEqual(watchdog.ffmpeg_pid.value, 0)
        self.assertEqual(watchdog.camera_fps.value, 0)

    def tearDown(self):
        configuration.cache_clear()

    def test_normal_build_does_not_change_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "frigate.motion.record_only.MODE_PATH", Path(directory) / "absent.json"
            ):
                self.assertFalse(recording_only("native_test"))
                self.assertEqual(recording_metadata_cutoff(config(), 123), 123)

    def test_explicit_mode_removes_decode_without_changing_recording_config(self):
        camera = config()
        camera.record.continuous.days = 17
        original = camera.model_dump()
        mode = RecordingOnlyMode(mode="recording_only", cameras={"native_test"})
        with patch("frigate.motion.record_only.configuration", return_value=mode):
            commands = recording_only_commands(camera)
        self.assertEqual(camera.model_dump(), original)
        self.assertEqual(len(commands), 1)
        args = commands[0]["cmd"]
        self.assertNotIn("detect", commands[0]["roles"])
        self.assertNotIn("pipe:", args)
        self.assertNotIn("-hwaccel", args)
        self.assertEqual(args[args.index("-c:v") + 1], "copy")

    def test_continuous_ingestion_does_not_wait_for_fake_frames(self):
        camera = config()
        camera.record.continuous.days = 17
        mode = RecordingOnlyMode(mode="recording_only", cameras={"native_test"})
        with (
            patch("frigate.motion.record_only.configuration", return_value=mode),
            patch("frigate.motion.record_only.time.time", return_value=1000),
        ):
            self.assertEqual(recording_metadata_cutoff(camera, 0), 1000)

    def test_motion_only_retention_cannot_silently_discard_recordings(self):
        camera = config()
        camera.record.continuous.days = 0
        mode = RecordingOnlyMode(mode="recording_only", cameras={"native_test"})
        with patch("frigate.motion.record_only.configuration", return_value=mode):
            with self.assertRaises(ValueError):
                recording_only_commands(camera)
            with self.assertRaises(ValueError):
                recording_metadata_cutoff(camera, 0)

    def test_unselected_camera_remains_unchanged(self):
        mode = RecordingOnlyMode(mode="recording_only", cameras={"another_camera"})
        with patch("frigate.motion.record_only.configuration", return_value=mode):
            self.assertFalse(recording_only("native_test"))
            self.assertEqual(recording_only_commands(config()), [])


if __name__ == "__main__":
    unittest.main()

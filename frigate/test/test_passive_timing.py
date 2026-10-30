"""Passive calibration must not interrupt the restored original detector."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from frigate.motion.record_only import TimingCalibrationOptions
from frigate.motion.timing_calibration import (
    TemporaryTimingTuner,
    timing_recommendations,
)
from frigate.test.test_compressed_live import config


class TestPassiveTiming(unittest.TestCase):
    def test_existing_detection_is_never_replaced_by_a_temporary_decode_lease(self):
        tuner = TemporaryTimingTuner({"native_test": config()})
        tuner.camera, tuner.job_id = "native_test", "passive"
        options = TimingCalibrationOptions(
            camera="native_test", job_id="passive", reference_mode="existing_detector"
        )
        first = True

        def request(path, body=None):
            nonlocal first
            if body is not None:
                return {"state": "subscribed" if body["enabled"] else "stopped"}
            if path == "stats":
                return {
                    "cameras": {
                        "native_test": {
                            "camera_fps": 5,
                            "process_fps": 5,
                            "detection_enabled": True,
                            "ffmpeg_pid": 100,
                        },
                        "other": {
                            "camera_fps": 5,
                            "process_fps": 5,
                            "detection_enabled": True,
                            "ffmpeg_pid": 101,
                        },
                    }
                }
            state = "stopped" if first else "subscribed"
            first = False
            return {"state": state, "events": []}

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "untouched.json"
            path.write_text("existing unrelated lease")
            with (
                patch("frigate.motion.timing_calibration.TIMING_CONTROL_PATH", path),
                patch("frigate.motion.timing_calibration._ReferenceSocket"),
                patch(
                    "frigate.motion.timing_calibration.time.monotonic",
                    side_effect=[0, 0, 0, 1, 1, 31],
                ),
                patch(
                    "frigate.motion.timing_calibration.time.time",
                    side_effect=[1000, 1000, 1001, 1001],
                ),
                patch.object(tuner, "_request", side_effect=request),
                patch.object(tuner, "_save"),
                patch.object(tuner.stop_event, "wait", return_value=False),
            ):
                tuner._run(options, 30)
            self.assertEqual(path.read_text(), "existing unrelated lease")
        self.assertEqual(tuner.phase, "complete")
        self.assertTrue(tuner.other_cameras_decoded)
        self.assertFalse(tuner.owns_lease)
        self.assertFalse(tuner.snapshot()["detector_settings_changed"])

    def test_timing_is_not_fabricated_without_real_episodes(self):
        result = timing_recommendations([], [])
        self.assertEqual(result["status"], "insufficient_data")
        self.assertFalse(result["timers_applied"])

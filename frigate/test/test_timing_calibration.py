"""Bounded detector leases and conservative timing estimates without cameras."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from frigate.motion.record_only import (
    RecordingOnlyMode,
    TimingCalibrationOptions,
    recording_only,
)
from frigate.motion.timing_calibration import (
    TemporaryTimingTuner,
    _ReferenceSocket,
    timing_recommendations,
)
from frigate.test.test_camera_watchdog import build_watchdog
from frigate.test.test_compressed_live import config
from frigate.track.norfair_tracker import NorfairTracker
from frigate.track.object_processing import TrackedObjectProcessor
from frigate.video.detect import clear_tracking_state


class TestTemporaryTimingCalibration(unittest.TestCase):
    def test_tracking_memory_is_cleared_without_fake_reference_frames(self):
        tracker = MagicMock(spec=NorfairTracker)
        for field in (
            "tracked_objects",
            "disappeared",
            "stationary_box_history",
            "positions",
            "track_id_map",
            "parked_vehicle_memory",
            "stationary_classifier",
        ):
            setattr(tracker, field, MagicMock())
        tracker.stationary_classifier.anchor_frames = {"object": 1}
        nested = SimpleNamespace(tracked_objects=[1])
        tracker.trackers = {"person": {"main": nested}}
        tracker.default_tracker = {}
        clear_tracking_state(tracker)
        tracker.tracked_objects.clear.assert_called_once()
        tracker.parked_vehicle_memory.clear.assert_called_once()
        tracker.stationary_classifier.reset.assert_called_once_with("object")
        self.assertEqual(nested.tracked_objects, [])

    def test_temporary_reference_end_closes_objects_but_keeps_camera_enabled(self):
        ended, activity = MagicMock(), MagicMock()
        tracked = SimpleNamespace(obj_data={})
        state = SimpleNamespace(
            previous_frame_id="frame",
            current_frame_time=100,
            tracked_objects={"object": tracked},
            callbacks={"end": [ended], "camera_activity": [activity]},
        )
        TrackedObjectProcessor.force_end_all_events(
            SimpleNamespace(), "native_test", state, camera_enabled=True
        )
        self.assertIn("end_time", tracked.obj_data)
        ended.assert_called_once()
        self.assertTrue(activity.call_args.args[1]["enabled"])
        self.assertEqual(activity.call_args.args[1]["objects"], [])

    def options(self, duration=900):
        return TimingCalibrationOptions(
            camera="native_test", job_id="test-reference", duration_seconds=duration
        )

    def test_only_selected_camera_can_decode_and_lease_expires_exactly(self):
        mode = RecordingOnlyMode(
            mode="recording_only",
            cameras={"native_test", "other"},
            all_enabled_cameras=True,
            timing_calibration=self.options(30),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lease.json"
            with (
                patch("frigate.motion.record_only.configuration", return_value=mode),
                patch("frigate.motion.record_only.TIMING_CONTROL_PATH", path),
            ):
                self.assertTrue(recording_only("native_test"))
                path.write_text(
                    json.dumps(
                        {
                            "camera": "native_test",
                            "job_id": "test-reference",
                            "started_monotonic": 100,
                            "expires_monotonic": 130,
                        }
                    )
                )
                with patch(
                    "frigate.motion.record_only.time.monotonic", return_value=101
                ):
                    self.assertFalse(recording_only("native_test"))
                    self.assertTrue(recording_only("other"))
                with patch(
                    "frigate.motion.record_only.time.monotonic", return_value=130
                ):
                    self.assertTrue(recording_only("native_test"))
                path.write_text("invalid")
                with self.assertLogs("frigate.motion.record_only", level="ERROR"):
                    self.assertTrue(recording_only("native_test"))

    def test_capture_watchdog_rebuilds_record_only_processes_at_expiry(self):
        watchdog = build_watchdog()
        watchdog.was_recording_only = False
        with (
            patch.object(watchdog, "_update_enabled_state", return_value=True),
            patch.object(watchdog, "_check_config_updates", return_value={}),
            patch.object(watchdog, "start_all_ffmpeg") as start,
            patch.object(watchdog, "stop_all_ffmpeg") as stop,
            patch.object(watchdog, "_reset_segment_times"),
            patch.object(watchdog.stop_event, "wait", side_effect=[False, True]),
            patch("frigate.video.ffmpeg.recording_only", return_value=True),
            patch("frigate.video.ffmpeg.time.sleep"),
        ):
            watchdog.run()
        self.assertTrue(watchdog.was_recording_only)
        self.assertEqual(start.call_count, 2)
        self.assertEqual(stop.call_count, 2)

    def test_recommendations_require_five_independent_closed_episodes(self):
        reference, native = [], []
        for index in range(5):
            offset = index * 50
            reference.append(
                {"kind": "object", "start": offset + 7, "end": offset + 22}
            )
            native.append(
                {"channel": "motion", "start": offset + 10, "end": offset + 20}
            )
        partial = timing_recommendations(reference[:4], native[:4])
        self.assertEqual(partial["status"], "insufficient_data")
        self.assertIsNone(partial["channels"]["motion"]["recommended_timers"])
        result = timing_recommendations(reference, native)
        self.assertEqual(result["status"], "recommendations_available")
        timers = result["channels"]["motion"]["recommended_timers"]
        self.assertEqual(timers["pre_roll_seconds"], 4)
        self.assertEqual(timers["post_hold_seconds"], 3)
        self.assertFalse(result["timers_applied"])

    def test_ambiguous_objects_spanning_multiple_camera_alarms_do_not_tune(self):
        result = timing_recommendations(
            [{"kind": "object", "start": 0, "end": 100}],
            [
                {"channel": "a", "start": 10, "end": 20},
                {"channel": "a", "start": 30, "end": 40},
            ],
        )
        self.assertEqual(result["status"], "insufficient_data")
        self.assertEqual(result["channels"], {})

    def test_camera_initialization_is_not_a_real_motion_edge(self):
        tuner = TemporaryTimingTuner({"native_test": config()})
        tuner.camera = "native_test"
        event = {
            "topic": "motion",
            "received_at": 100,
            "kind": "motion",
            "operation": "Initialized",
            "active": True,
            "source_key": "opaque",
        }
        tuner.observe_native(event)
        self.assertEqual(tuner.native_open, {})
        tuner.observe_native({**event, "operation": "Changed", "received_at": 101})
        tuner.observe_native(
            {**event, "operation": "Changed", "received_at": 105, "active": False}
        )
        self.assertEqual(len(tuner.native), 1)
        self.assertEqual(tuner.native[0]["end"] - tuner.native[0]["start"], 4)

    def test_malformed_reference_fails_instead_of_looking_quiet(self):
        tuner = TemporaryTimingTuner({})
        _ReferenceSocket.received_message(
            SimpleNamespace(owner=tuner, job_id=tuner.job_id),
            SimpleNamespace(data=b"not-json"),
        )
        self.assertTrue(tuner.stop_event.is_set())
        self.assertEqual(tuner.error, "invalid_reference_message")

    def test_start_rejects_wrong_camera_and_overlong_reference(self):
        tuner = TemporaryTimingTuner({"native_test": config()})
        mode = RecordingOnlyMode(
            mode="recording_only",
            cameras={"native_test"},
            timing_calibration=self.options(),
        )
        with patch(
            "frigate.motion.timing_calibration.timing_calibration_options",
            return_value=mode.timing_calibration,
        ):
            with self.assertRaises(ValueError):
                tuner.start("other", 900)
            with self.assertRaises(ValueError):
                tuner.start("native_test", 901)

    def test_deadline_cleans_up_the_decode_lease_and_camera_subscription(self):
        tuner = TemporaryTimingTuner({"native_test": config()})
        tuner.camera, tuner.job_id = "native_test", "job"

        def request(path, body=None):
            if body is not None:
                return {"state": "subscribed" if body["enabled"] else "stopped"}
            return {"state": "stopped" if request.first else "subscribed", "events": []}

        request.first = True

        def call(path, body=None):
            result = request(path, body)
            request.first = False
            return result

        with tempfile.TemporaryDirectory() as directory:
            lease = Path(directory) / "lease.json"
            with (
                patch("frigate.motion.timing_calibration.TIMING_CONTROL_PATH", lease),
                patch("frigate.motion.timing_calibration._ReferenceSocket") as socket,
                patch(
                    "frigate.motion.timing_calibration.time.monotonic",
                    side_effect=[0, 0, 0, 31],
                ),
                patch("frigate.motion.timing_calibration.time.time", return_value=1000),
                patch.object(tuner, "_request", side_effect=call) as calls,
                patch.object(tuner, "_save"),
                patch.object(tuner.stop_event, "wait", return_value=False),
            ):
                tuner._run(self.options(), 30)
            self.assertFalse(lease.exists())
            self.assertEqual(tuner.phase, "complete")
            self.assertEqual(calls.call_args.args[1]["enabled"], False)
            socket.return_value.close.assert_called_once()

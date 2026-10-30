"""Real zone rasterization and YUV luma adaptation without publishing real events."""

import asyncio
import multiprocessing as mp
import queue
import unittest
from datetime import timedelta
from unittest.mock import MagicMock, patch

import numpy as np

from frigate.config import FrigateConfig
from frigate.config.profile_manager import ProfileManager
from frigate.const import REPLAY_CAMERA_PREFIX
from frigate.environment.messages import glare_configuration_hash
from frigate.environment.runtime import EnvironmentManager
from frigate.environment.video import CameraGlareMonitor
from frigate.test.test_environment_weather import NOW
from frigate.video.detect import process_frames


class TestCameraGlareMonitor(unittest.TestCase):
    def setUp(self):
        self.config = FrigateConfig(
            mqtt={"enabled": False},
            profiles={"glare": {"friendly_name": "Glare"}},
            cameras={
                "front": {
                    "ffmpeg": {
                        "inputs": [
                            {
                                "path": "rtsp://camera.invalid/test",
                                "roles": ["detect", "record"],
                            }
                        ]
                    },
                    "detect": {"height": 100, "width": 100, "fps": 5},
                    "zones": {"driveway": {"coordinates": "0,0,1,0,1,1,0,1"}},
                    "profiles": {"glare": {"motion": {"threshold": 40}}},
                    "environment": {
                        "enabled": True,
                        "glare": {
                            "enabled": True,
                            "profile": "glare",
                            "zones": ["driveway"],
                        },
                    },
                }
            },
        )
        self.camera = self.config.cameras["front"]
        self.publisher_class = self.enterContext(
            patch("frigate.environment.video.GlareSignalPublisher")
        )
        self.publisher = self.publisher_class.return_value
        self.frame = np.full((150, 100), 30, dtype=np.uint8)
        self.frame[100:] = 255

    def test_yuv_adaptation_emits_glare_metadata_without_changing_shared_pixels(self):
        monitor = CameraGlareMonitor(self.camera, (100, 100))
        initial = self.frame.copy()
        bright = self.frame.copy()
        bright[15:35, 15:35] = 255
        with patch("frigate.environment.video.time.monotonic", side_effect=[0, 1]):
            monitor.update(self.frame, 100)
            monitor.update(bright, 101)
        self.assertTrue(np.array_equal(self.frame, initial))
        signals = [call.args[0] for call in self.publisher.publish.call_args_list]
        glare = [signal for signal in signals if signal.kind == "glare"]
        self.assertEqual(len(glare), 1)
        self.assertEqual(glare[0].zones, ("driveway",))
        self.assertEqual(
            glare[0].configuration_hash, glare_configuration_hash(self.camera)
        )
        self.assertFalse(hasattr(glare[0], "image"))

    def test_ptz_motion_is_not_treated_as_transient_glare(self):
        monitor = CameraGlareMonitor(self.camera, (100, 100))
        bright = self.frame.copy()
        bright[15:35, 15:35] = 255
        with patch("frigate.environment.video.time.monotonic", side_effect=[0, 1]):
            monitor.update(self.frame, 100)
            monitor.update(bright, 101, camera_moving=True)
        self.assertFalse(
            any(
                call.args[0].kind == "glare"
                for call in self.publisher.publish.call_args_list
            )
        )

    def test_unchanged_zone_updates_do_not_discard_the_adaptive_baseline(self):
        monitor = CameraGlareMonitor(self.camera, (100, 100))
        detector = monitor.detector
        monitor.reconfigure(self.camera)
        self.assertIs(monitor.detector, detector)

    def test_invalid_frame_is_reported_without_raising_into_object_detection(self):
        monitor = CameraGlareMonitor(self.camera, (100, 100))
        with self.assertLogs("frigate.environment.video", level="ERROR"):
            monitor.update(np.zeros((10, 10), dtype=np.uint8), 100)
        self.assertIsNone(monitor.detector)
        signal = self.publisher.publish.call_args.args[0]
        self.assertEqual(signal.kind, "unavailable")
        self.assertEqual(signal.error_code, "invalid_glare_frame")

    def test_disabled_and_replay_cameras_do_not_publish_or_allocate_masks(self):
        self.camera.environment.enabled = False
        monitor = CameraGlareMonitor(self.camera, (100, 100))
        self.assertIsNone(monitor.detector)
        self.publisher_class.assert_not_called()
        self.camera.environment.enabled = True
        self.camera.name = f"{REPLAY_CAMERA_PREFIX}front"
        replay = CameraGlareMonitor(self.camera, (100, 100))
        self.assertIsNone(replay.detector)
        self.publisher_class.assert_not_called()

    def test_frame_processing_keeps_existing_queue_output_and_passes_ptz_context(self):
        self.camera.detect.enabled = False
        self.camera.onvif.autotracking.enabled = True
        frame_queue = MagicMock()
        frame_queue.get.side_effect = [("frame", 101.0), queue.Empty]
        frame_manager = MagicMock()
        frame_manager.get.return_value = self.frame
        motion = MagicMock()
        motion.detect.return_value = []
        tracker = MagicMock()
        tracker.tracked_objects = {}
        output = MagicMock()
        output.full.return_value = False
        object_detector = MagicMock()
        object_detector.fps.eps.return_value = 0.0
        metrics = MagicMock()
        ptz = MagicMock()
        ptz.start_time.value = 100.0
        ptz.stop_time.value = 0.0
        stop = MagicMock()
        stop.is_set.return_value = False
        requestor = MagicMock()
        with (
            patch("frigate.video.detect.CameraConfigUpdateSubscriber") as subscriber,
            patch("frigate.video.detect.CameraGlareMonitor") as monitor,
        ):
            subscriber.return_value.check_for_updates.return_value = {}
            process_frames(
                requestor,
                frame_queue,
                (100, 100),
                self.config.model_for_camera("front"),
                self.camera,
                frame_manager,
                motion,
                object_detector,
                tracker,
                output,
                metrics,
                stop,
                ptz,
                [],
                exit_on_empty=True,
            )
        monitor.return_value.update.assert_called_once()
        self.assertTrue(monitor.return_value.update.call_args.kwargs["camera_moving"])
        monitor.return_value.stop.assert_called_once()
        motion.detect.assert_called_once()
        output.put.assert_called_once()
        queued = output.put.call_args.args[0]
        self.assertEqual(queued[:3], ("front", "frame", 101.0))
        self.assertEqual(queued[3:], ({}, [], []))

    def test_detected_glare_reaches_profile_runtime_and_restores_base_after_lease(self):
        monitor = CameraGlareMonitor(self.camera, (100, 100))
        bright = self.frame.copy()
        bright[15:35, 15:35] = 255
        with patch("frigate.environment.video.time.monotonic", side_effect=[0, 1]):
            monitor.update(self.frame, NOW.timestamp())
            monitor.update(bright, NOW.timestamp() + 1)
        signal = next(
            call.args[0]
            for call in self.publisher.publish.call_args_list
            if call.args[0].kind == "glare"
        )

        async def apply():
            profiles = ProfileManager(self.config, MagicMock())
            manager = EnvironmentManager(self.config, profiles, mp.Event())
            self.assertTrue(
                manager.receive_glare_signal(signal, NOW + timedelta(seconds=1), 1)
            )
            await manager.tick(NOW + timedelta(seconds=1), 1)
            self.assertEqual(
                manager.camera_status("front", NOW).applied_profiles, ("glare",)
            )
            self.assertEqual(self.camera.motion.threshold, 40)
            await manager.tick(NOW + timedelta(seconds=31), 31)
            self.assertEqual(manager.camera_status("front", NOW).applied_profiles, ())
            self.assertEqual(self.camera.motion.threshold, 30)

        asyncio.run(apply())


if __name__ == "__main__":
    unittest.main()

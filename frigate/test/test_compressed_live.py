"""Image-scoped native trial selection, private media credentials and recording independence."""

import io
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
from pydantic import ValidationError

from frigate.config import FrigateConfig
from frigate.motion.compressed_live import (
    NativeActivation,
    NativeCameraCapture,
    activation,
    full_native_fallback_command,
    native_enabled,
    native_input,
    recording_commands,
    start_native_source,
)


def config():
    return FrigateConfig(
        mqtt={"enabled": False},
        cameras={
            "native_test": {
                "ffmpeg": {
                    "inputs": [
                        {
                            "path": "rtsp://account:private_value@127.0.0.1:8554/test",
                            "roles": ["detect", "record"],
                        }
                    ]
                },
                "detect": {"width": 3840, "height": 2160, "fps": 5},
                "record": {"enabled": True},
            }
        },
    ).cameras["native_test"]


class TestNativeLiveSelection(unittest.TestCase):
    def test_motion_distance_and_area_options_are_explicit(self):
        camera = config()
        camera.motion.compressed_min_distance = 2
        camera.motion.compressed_max_distance = 20
        camera.motion.compressed_min_activity = 0.01
        process = MagicMock()
        with patch(
            "frigate.motion.compressed_live.subprocess.Popen", return_value=process
        ) as spawn:
            start_native_source(camera, 2)
        command = spawn.call_args.args[0]
        self.assertEqual(float(command[command.index("--min-motion") + 1]), 2)
        self.assertEqual(float(command[command.index("--max-motion") + 1]), 20)
        self.assertEqual(camera.motion.compressed_min_activity, 0.01)

    def test_distance_defaults_preserve_original_extractor_threshold(self):
        camera = config()
        self.assertEqual(camera.motion.compressed_min_distance, 0.5)
        self.assertIsNone(camera.motion.compressed_max_distance)
        self.assertEqual(camera.motion.compressed_min_activity, 0)

    def test_invalid_or_reversed_distance_settings_are_rejected(self):
        from frigate.config.camera.motion import MotionConfig

        for values in (
            {"compressed_min_distance": -1},
            {"compressed_min_distance": float("nan")},
            {"compressed_min_distance": 2, "compressed_max_distance": 1},
            {"compressed_min_distance": 2, "compressed_max_distance": 2},
            {"compressed_max_distance": 0},
            {"compressed_min_activity": 1.1},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                MotionConfig(**values)

    def test_fallback_is_off_by_default_and_user_can_enable_it(self):
        camera = config()
        self.assertFalse(camera.motion.compressed_fallback)
        camera.motion.compressed_fallback = True
        self.assertTrue(camera.motion.compressed_fallback)

    def test_extractor_error_does_not_silently_enable_pixel_fallback(self):
        capture = self.fallback_capture()
        capture.frame_manager = MagicMock()
        capture.frame_manager.frame_times.return_value = np.zeros(2)
        capture._run_full_fallback = MagicMock()
        session = MagicMock()
        session.run.side_effect = EOFError("test extractor failed")
        with (
            patch(
                "frigate.motion.compressed_live.NativeCaptureSession",
                return_value=session,
            ),
            patch(
                "frigate.motion.compressed_live.activation",
                return_value=NativeActivation(
                    mode="experimental_native4k_gated", cameras={"native_test"}
                ),
            ),
            self.assertLogs("frigate.motion.compressed_live", level="ERROR") as logs,
        ):
            capture.run()
        capture._run_full_fallback.assert_not_called()
        self.assertIn("fallback is disabled", logs.output[0])
        self.assertEqual(capture.pipeline["native_capture"]["state"], "failed")
        self.assertFalse(capture.pipeline["native_capture"]["fallback_enabled"])

    def test_explicit_user_fallback_option_is_respected(self):
        capture = self.fallback_capture()
        capture.config.motion.compressed_fallback = True
        capture.frame_manager = MagicMock()
        capture.frame_manager.frame_times.return_value = np.zeros(2)
        capture._run_full_fallback = MagicMock()
        session = MagicMock()
        session.run.side_effect = EOFError("test extractor failed")
        with (
            patch(
                "frigate.motion.compressed_live.NativeCaptureSession",
                return_value=session,
            ),
            patch(
                "frigate.motion.compressed_live.activation",
                return_value=NativeActivation(
                    mode="experimental_native4k_gated", cameras={"native_test"}
                ),
            ),
            self.assertLogs("frigate.motion.compressed_live", level="ERROR"),
        ):
            capture.run()
        capture._run_full_fallback.assert_called_once()

    def tearDown(self):
        activation.cache_clear()

    def test_default_images_do_not_enable_native_trial(self):
        self.assertEqual(config().motion.backend, "pixel")
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "frigate.motion.compressed_live.ACTIVATION",
                Path(directory) / "absent.json",
            ):
                self.assertFalse(native_enabled(config()))

    def test_malformed_activation_is_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text('{"mode":"cpu_decode_reuse","cameras":["native_test"]}')
            with (
                patch("frigate.motion.compressed_live.ACTIVATION", path),
                self.assertRaises(ValidationError),
            ):
                activation()

    def test_only_explicit_camera_names_are_selected(self):
        camera = config()
        camera.motion.backend = "compressed"
        with patch(
            "frigate.motion.compressed_live.activation",
            return_value=NativeActivation(
                mode="experimental_native4k_gated", cameras={"another_camera"}
            ),
        ):
            with self.assertRaisesRegex(ValueError, "without an enabled native"):
                native_enabled(camera)

    def test_no1080p_or_dynamic_analysis_copy_is_accepted(self):
        camera = config()
        self.assertIn("**********", repr(native_input(camera)))
        camera.detect.height = 1080
        with self.assertRaises(ValueError):
            native_input(camera)
        camera.detect.height = 2160
        camera.detect.dynamic_resolution.enabled = True
        with self.assertRaises(ValueError):
            native_input(camera)

    def test_recording_split_does_not_reencode_or_modify_config(self):
        camera = config()
        camera.motion.backend = "compressed"
        original = camera.model_dump()
        with patch(
            "frigate.motion.compressed_live.activation",
            return_value=NativeActivation(
                mode="experimental_native4k_gated", cameras={"native_test"}
            ),
        ):
            commands = recording_commands(camera)
        self.assertEqual(camera.model_dump(), original)
        self.assertEqual(len(commands), 1)
        args = commands[0]["cmd"]
        self.assertNotIn("pipe:", args)
        self.assertNotIn("-hwaccel", args)
        self.assertEqual(args[args.index("-c:v") + 1], "copy")

    def test_source_credentials_never_become_process_arguments(self):
        process = MagicMock()
        with patch(
            "frigate.motion.compressed_live.subprocess.Popen", return_value=process
        ) as spawn:
            start_native_source(config(), 2)
        argv = spawn.call_args.args[0]
        self.assertNotIn("private_value", " ".join(argv))
        self.assertNotIn("rtsp://", " ".join(argv))
        self.assertIn("--live-input", argv)
        self.assertIn("--framed", argv)
        process.stdin = None

    def test_ordinary_recording_topology_is_preserved(self):
        with patch("frigate.motion.compressed_live.activation", return_value=None):
            self.assertEqual(recording_commands(config()), [])

    def test_users_can_select_legacy_even_when_native_build_contains_the_camera(self):
        camera = config()
        with patch(
            "frigate.motion.compressed_live.activation",
            return_value=NativeActivation(
                mode="experimental_native4k_gated", cameras={"native_test"}
            ),
        ):
            self.assertFalse(native_enabled(camera))
            camera.motion.backend = "compressed"
            self.assertTrue(native_enabled(camera))

    def test_new_backend_does_not_reuse_or_enable_the_old_adaptive_tuner(self):
        camera = config()
        old = camera.motion.adaptive.model_dump()
        camera.motion.backend = "compressed"
        self.assertEqual(camera.motion.adaptive.model_dump(), old)
        camera.motion.backend = "pixel"
        self.assertEqual(camera.motion.adaptive.model_dump(), old)

    def test_unknown_motion_system_is_not_accepted(self):
        with self.assertRaises(ValidationError):
            FrigateConfig(
                mqtt={"enabled": False},
                cameras={
                    "test": {
                        "ffmpeg": {
                            "inputs": [
                                {"path": "rtsp://127.0.0.1/test", "roles": ["detect"]}
                            ]
                        },
                        "detect": {"width": 3840, "height": 2160},
                        "motion": {"backend": "automatic_rl"},
                    }
                },
            )

    def test_native_operator_activation_enables_separate_compressed_learning(self):
        activation = NativeActivation(
            mode="experimental_native4k_gated", cameras={"native_test"}
        )
        self.assertTrue(activation.compressed_tuner)

    def test_native_source_uses_main_recording_not_lower_resolution_detect(self):
        camera = config()
        original = camera.ffmpeg.inputs[0].model_copy(deep=True)
        original.roles = ["record"]
        substream = camera.ffmpeg.inputs[0].model_copy(deep=True)
        substream.roles = ["detect"]
        substream.path = "rtsp://127.0.0.1:8554/sub"
        camera.ffmpeg.inputs = [original, substream]
        camera.recreate_ffmpeg_cmds()
        self.assertEqual(
            native_input(camera).source.get_secret_value(),
            original.path,
        )

    def test_full_fallback_uses_main_source_and_native_dimensions(self):
        camera = config()
        original = camera.model_dump()
        command = full_native_fallback_command(camera)
        self.assertEqual(command[command.index("-i") + 1], camera.ffmpeg.inputs[0].path)
        self.assertEqual(command[-1], "pipe:")
        self.assertNotIn("1080", " ".join(command))
        self.assertIn("3840", " ".join(command))
        self.assertIn("2160", " ".join(command))
        self.assertNotIn("-c:v", command)
        self.assertEqual(camera.model_dump(), original)

    def fallback_capture(self):
        metrics = MagicMock()
        metrics.pipeline = {}
        source = MagicMock()
        source.poll.return_value = 0
        source.stdout.closed = False
        capture = NativeCameraCapture(
            config(),
            source,
            2,
            2,
            0,
            metrics,
            threading.Event(),
            on_fallback=MagicMock(),
        )
        capture.session = MagicMock()
        capture.emit = MagicMock()
        capture.frame_rate.eps = MagicMock(return_value=5.0)
        return capture

    def test_extractor_failure_fallback_is_owned_and_observable(self):
        capture = self.fallback_capture()
        size = 3840 * 2160 * 3 // 2
        pipe = io.BufferedReader(io.BytesIO(bytes([100]) * size))
        process = MagicMock()
        process.stdout = pipe
        process.pid = 12345
        process.poll.return_value = None

        def emit(frame):
            self.assertEqual(frame.yuv.shape, (3240, 3840))
            self.assertGreater(frame.timestamp + capture.epoch, 0)
            capture.local_stop.set()

        capture.emit.side_effect = emit
        with patch(
            "frigate.motion.compressed_live.subprocess.Popen", return_value=process
        ):
            capture._run_full_fallback()
        capture.session.close.assert_called_once()
        capture.source_process.stdout.close.assert_called_once()
        capture.on_fallback.assert_called_once_with(process)
        capture.emit.assert_called_once()
        status = capture.pipeline["native_capture"]
        self.assertEqual(status["backend"], "native4k_full_decode_fallback")
        self.assertEqual(status["reason"], "entropy_source_failed")
        self.assertIsNone(status["parser_pid"])
        process.terminate.assert_called_once()
        self.assertTrue(pipe.closed)

    def test_stop_interrupts_source_and_fallback_not_recording(self):
        capture = self.fallback_capture()
        capture.source_process.poll.return_value = None
        capture.fallback_process = MagicMock()
        capture.fallback_process.poll.return_value = None
        capture.stop()
        capture.source_process.terminate.assert_called_once()
        capture.fallback_process.terminate.assert_called_once()
        self.assertTrue(capture.local_stop.is_set())

    def test_fallback_short_frame_is_a_visible_failure(self):
        capture = self.fallback_capture()
        process = MagicMock()
        process.stdout = io.BufferedReader(io.BytesIO(b"short"))
        process.poll.return_value = 1
        with (
            patch(
                "frigate.motion.compressed_live.subprocess.Popen", return_value=process
            ),
            self.assertRaisesRegex(EOFError, "stopped producing frames"),
        ):
            capture._run_full_fallback()
        capture.emit.assert_not_called()


if __name__ == "__main__":
    unittest.main()

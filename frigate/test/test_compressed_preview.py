"""Live vector inspection never starts automatically, decodes pixels or changes camera settings."""

import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from frigate.api.auth import require_admin_by_default
from frigate.api.compressed_motion import router
from frigate.motion.compressed_live import native_media_input
from frigate.motion.compressed_preview import (
    BackgroundPreviewLease,
    VectorPreview,
    VectorPreviewManager,
    VectorPreviewOptions,
)
from frigate.motion.compressed_protocol import CompressedEvidence
from frigate.motion.compressed_tuner import CompressedNoiseEstimator
from frigate.motion.record_only import RecordingOnlyMode, TimingCalibrationOptions
from frigate.test.test_compressed_live import config


class TestVectorPreviewManager(unittest.TestCase):
    def test_custom_auto_preview_allows_calibration_before_hard_expiry(self):
        process = MagicMock()
        process.poll.return_value = 0
        with (
            patch(
                "frigate.motion.compressed_preview.subprocess.Popen",
                return_value=process,
            ) as spawn,
            patch("frigate.motion.compressed_preview.threading.Thread"),
            patch("frigate.motion.compressed_preview.threading.Timer") as timer,
        ):
            preview = VectorPreview(
                config(), VectorPreviewOptions(auto_tune=True, min_distance=10)
            )
            self.assertNotIn("--seconds", spawn.call_args.args[0])
            self.assertEqual(timer.call_args.args[0], 185)
            self.assertGreater(preview.snapshot()["expires_in"], 170)
            self.assertLessEqual(preview.snapshot()["expires_in"], 180)
            preview.stop()

    def test_learning_observes_every_frame_not_only_ui_snapshots(self):
        from frigate.test.test_compressed_tuner import sample

        events = [sample(index * 0.04) for index in range(25)]
        for event in events:
            event.vector_x = [0.0] * 576
            event.vector_y = [0.0] * 576
        process = MagicMock()
        process.poll.return_value = 0
        with (
            patch(
                "frigate.motion.compressed_preview.subprocess.Popen",
                return_value=process,
            ),
            patch("frigate.motion.compressed_preview.threading.Thread"),
            patch("frigate.motion.compressed_preview.threading.Timer"),
            patch(
                "frigate.motion.compressed_preview.read_extractor_events",
                return_value=iter(events),
            ),
            patch(
                "frigate.motion.compressed_preview.time.monotonic",
                return_value=100,
            ),
        ):
            preview = VectorPreview(config(), VectorPreviewOptions(auto_tune=True))
            preview.noise = MagicMock(wraps=CompressedNoiseEstimator())
            preview._read()
        self.assertEqual(preview.noise.observe.call_count, len(events))
        self.assertEqual(preview.latest["source_pts"], events[0].pts)

    def test_source_update_restarts_only_changed_camera_and_preserves_other_model(self):
        manager = VectorPreviewManager()
        first = config()
        second = first.model_copy(deep=True)
        second.name = "second_camera"
        workers = []

        def create(camera, options, *, background=False):
            worker = MagicMock()
            worker.source = native_media_input(camera)
            worker.stopped.is_set.return_value = False
            worker.noise = CompressedNoiseEstimator()
            workers.append(worker)
            return worker

        with (
            patch(
                "frigate.motion.compressed_preview.recording_only", return_value=True
            ),
            patch("frigate.motion.compressed_preview.Path.is_file", return_value=True),
            patch(
                "frigate.motion.compressed_preview.VectorPreview", side_effect=create
            ),
            patch("frigate.motion.compressed_preview.threading.Thread"),
        ):
            manager.start_background({first.name: first, second.name: second})
            from frigate.test.test_compressed_tuner import sample

            workers[1].noise.observe(sample(0))
            updated = first.model_copy(deep=True)
            updated.ffmpeg.inputs[0].path = "rtsp://127.0.0.1:8554/east"
            updated.recreate_ffmpeg_cmds()
            manager.update_cameras({first.name: updated, second.name: second})
            with patch.object(
                manager.stop_background, "wait", side_effect=[False, True]
            ):
                manager._supervise()
            self.assertEqual(len(workers), 3)
            workers[0].stop.assert_called_once()
            workers[1].stop.assert_not_called()
            self.assertIs(manager.background[second.name], workers[1])
            self.assertEqual(len(workers[1].noise.samples), 1)
            self.assertEqual(
                workers[2].source.source.get_secret_value(),
                "rtsp://127.0.0.1:8554/east",
            )
            manager.update_cameras({second.name: second})
            with patch.object(
                manager.stop_background, "wait", side_effect=[False, True]
            ):
                manager._supervise()
            workers[2].stop.assert_called_once()
            self.assertNotIn(first.name, manager.background)
            workers[1].stop.assert_not_called()
            manager.close()

    def test_background_learning_is_independent_per_camera_and_view_stop_does_not_stop_it(
        self,
    ):
        manager = VectorPreviewManager()
        first = config()
        second = first.model_copy(deep=True)
        second.name = "second_camera"
        workers = []

        def create(camera, options, *, background=False):
            self.assertTrue(background)
            worker = MagicMock()
            worker.camera = camera.name
            worker.noise = CompressedNoiseEstimator()
            worker.stopped.is_set.return_value = False
            worker.snapshot.return_value = {
                "camera": camera.name,
                "status": "running",
                "data": None,
            }
            workers.append(worker)
            return worker

        with (
            patch(
                "frigate.motion.compressed_preview.recording_only", return_value=True
            ),
            patch("frigate.motion.compressed_preview.Path.is_file", return_value=True),
            patch(
                "frigate.motion.compressed_preview.VectorPreview", side_effect=create
            ),
            patch("frigate.motion.compressed_preview.threading.Thread"),
        ):
            manager.start_background({first.name: first, second.name: second})
            self.assertIsNot(workers[0].noise, workers[1].noise)
            from frigate.test.test_compressed_tuner import sample

            workers[0].noise.observe(sample(0))
            self.assertEqual(len(workers[0].noise.samples), 1)
            self.assertEqual(len(workers[1].noise.samples), 0)
            result = manager.start(first, VectorPreviewOptions(auto_tune=True))
            self.assertIsInstance(manager.preview, BackgroundPreviewLease)
            manager.stop(first.name, result["session"])
            workers[0].stop.assert_not_called()
            workers[1].stop.assert_not_called()
            manager.close()
            workers[0].stop.assert_called_once()
            workers[1].stop.assert_called_once()

    def test_availability_does_not_start_any_source_or_decoder(self):
        manager = VectorPreviewManager()
        with (
            patch("frigate.motion.compressed_preview.Path.is_file", return_value=True),
            patch("frigate.motion.compressed_preview.subprocess.Popen") as spawn,
        ):
            self.assertTrue(manager.status("camera", None)["available"])
        spawn.assert_not_called()

    def test_explicit_start_is_bounded_and_credentials_are_not_arguments(self):
        process = MagicMock()
        process.poll.return_value = 0
        with (
            patch(
                "frigate.motion.compressed_preview.subprocess.Popen",
                return_value=process,
            ) as spawn,
            patch("frigate.motion.compressed_preview.threading.Thread"),
            patch("frigate.motion.compressed_preview.threading.Timer"),
        ):
            preview = VectorPreview(
                config(), VectorPreviewOptions(min_distance=2, max_distance=10)
            )
            command = spawn.call_args.args[0]
            self.assertIn("--min-motion", command)
            self.assertEqual(command[command.index("--seconds") + 1], "120")
            self.assertNotIn("rtsp://", " ".join(command))
            self.assertNotIn("private_value", " ".join(command))
            self.assertTrue(preview.snapshot()["preview_only"])
            self.assertFalse(preview.snapshot()["pixels_decoded"])
            preview.stop()

    def test_one_preview_only_and_stop_requires_matching_camera_and_session(self):
        manager = VectorPreviewManager()
        worker = MagicMock()
        worker.id, worker.camera = "a" * 32, "native_test"
        worker.stopped.is_set.return_value = False
        worker.snapshot.return_value = {"camera": "native_test", "session": worker.id}
        with (
            patch("frigate.motion.compressed_preview.Path.is_file", return_value=True),
            patch(
                "frigate.motion.compressed_preview.VectorPreview", return_value=worker
            ),
        ):
            manager.start(config(), VectorPreviewOptions())
            with self.assertRaises(RuntimeError):
                manager.start(config(), VectorPreviewOptions())
            with self.assertRaises(KeyError):
                manager.stop("another_camera", worker.id)
            worker.stop.assert_not_called()
            manager.stop("native_test", worker.id)
            worker.stop.assert_called_once()

    def test_stale_cache_is_not_presented_as_live_vectors(self):
        process = MagicMock()
        process.poll.return_value = 0
        with (
            patch(
                "frigate.motion.compressed_preview.subprocess.Popen",
                return_value=process,
            ),
            patch("frigate.motion.compressed_preview.threading.Thread"),
            patch("frigate.motion.compressed_preview.threading.Timer"),
        ):
            preview = VectorPreview(config(), VectorPreviewOptions())
            preview.latest = {"updated_at": time.time() - 10}
            self.assertEqual(preview.snapshot()["status"], "stale")
            self.assertIsNone(preview.snapshot()["data"])
            preview.stop()

    def test_invalid_ranges_and_fake_direction_arrays_are_rejected(self):
        for values in (
            {"min_distance": 2, "max_distance": 1},
            {"min_distance": float("nan")},
            {"min_activity": 2},
        ):
            with self.assertRaises(ValidationError):
                VectorPreviewOptions(**values)
        from frigate.test.test_compressed_protocol import evidence

        value = evidence()
        value["vector_x"] = [0.0] * 576
        with self.assertRaises(ValidationError):
            CompressedEvidence.model_validate(value)
        value["vector_y"] = [0.0] * 576
        self.assertIsNotNone(CompressedEvidence.model_validate(value).vector_x)
        value["vector_y"][0] = float("nan")
        with self.assertRaises(ValidationError):
            CompressedEvidence.model_validate(value)


class TestCompressedPreviewApi(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI(dependencies=[Depends(require_admin_by_default())])
        self.manager = MagicMock()
        self.manager.status.return_value = {
            "camera": "native_test",
            "available": True,
            "status": "stopped",
            "data": None,
        }
        self.app.state.vector_previews = self.manager
        self.app.frigate_config = SimpleNamespace(
            cameras={"native_test": config()},
            auth=SimpleNamespace(
                roles={"admin": ["native_test"], "viewer": ["native_test"]}
            ),
            proxy=SimpleNamespace(separator=","),
        )
        self.app.include_router(router)
        self.client = self.enterContext(TestClient(self.app))
        self.headers = {"remote-user": "synthetic", "remote-role": "admin"}

    def test_get_never_starts_a_worker_and_is_not_cacheable(self):
        response = self.client.get(
            "/native_test/compressed-motion-preview", headers=self.headers
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers["cache-control"])
        self.manager.start.assert_not_called()

    def test_timing_reference_is_admin_only_bounded_and_explicit(self):
        mode = RecordingOnlyMode(
            mode="recording_only",
            cameras={"native_test"},
            timing_calibration=TimingCalibrationOptions(
                camera="native_test", job_id="timing-test"
            ),
        )
        tuner = MagicMock()
        tuner.snapshot.return_value = {"camera": "native_test", "phase": "idle"}
        tuner.start.return_value = {"camera": "native_test", "phase": "preparing"}
        self.app.state.timing_calibration = tuner
        with patch(
            "frigate.api.compressed_motion.timing_calibration_options",
            return_value=mode.timing_calibration,
        ):
            response = self.client.get(
                "/native_test/timing-calibration", headers=self.headers
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn("no-store", response.headers["cache-control"])
            tuner.start.assert_not_called()
            response = self.client.post(
                "/native_test/timing-calibration",
                json={"duration_seconds": 901},
                headers=self.headers,
            )
            self.assertEqual(response.status_code, 422)
            tuner.start.assert_not_called()
            response = self.client.post(
                "/native_test/timing-calibration",
                json={"duration_seconds": 900},
                headers=self.headers,
            )
            self.assertEqual(response.status_code, 200)
            tuner.start.assert_called_once_with("native_test", 900)
            response = self.client.get(
                "/native_test/timing-calibration",
                headers={"remote-user": "synthetic", "remote-role": "viewer"},
            )
            self.assertEqual(response.status_code, 403)
            response = self.client.delete(
                "/native_test/timing-calibration", headers=self.headers
            )
            self.assertEqual(response.status_code, 200)
            tuner.close.assert_called_once()

    def test_only_authenticated_administrators_can_inspect(self):
        self.assertEqual(
            self.client.get("/native_test/compressed-motion-preview").status_code, 403
        )
        self.assertEqual(
            self.client.get(
                "/native_test/compressed-motion-preview",
                headers={"remote-user": "synthetic", "remote-role": "viewer"},
            ).status_code,
            403,
        )
        self.manager.status.assert_not_called()

    def test_explicit_start_validates_bounds_before_starting_source(self):
        response = self.client.post(
            "/native_test/compressed-motion-preview",
            json={"min_distance": 5, "max_distance": 2},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 422)
        self.manager.start.assert_not_called()

    def test_missing_binary_and_busy_worker_are_explicit(self):
        self.manager.start.side_effect = FileNotFoundError()
        response = self.client.post(
            "/native_test/compressed-motion-preview", json={}, headers=self.headers
        )
        self.assertEqual(response.status_code, 503)
        self.manager.start.side_effect = RuntimeError()
        self.assertEqual(
            self.client.post(
                "/native_test/compressed-motion-preview", json={}, headers=self.headers
            ).status_code,
            409,
        )

    def test_unknown_camera_and_expired_session_are_rejected(self):
        self.assertEqual(
            self.client.get(
                "/missing/compressed-motion-preview", headers=self.headers
            ).status_code,
            404,
        )
        self.manager.status.side_effect = KeyError()
        self.assertEqual(
            self.client.get(
                "/native_test/compressed-motion-preview?session=" + "a" * 32,
                headers=self.headers,
            ).status_code,
            404,
        )


if __name__ == "__main__":
    unittest.main()

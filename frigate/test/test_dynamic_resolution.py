"""Analysis raster bounds, hysteresis, I420 planes and canonical detection coordinates."""

import unittest
from contextlib import ExitStack
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2
import numpy as np
from pydantic import ValidationError

from frigate.config import DetectConfig, FrigateConfig, ModelConfig
from frigate.config.camera.detect import DynamicResolutionConfig
from frigate.video.detect import detect, process_frames
from frigate.video.dynamic_resolution import (
    AnalysisSize,
    DynamicResolutionController,
    resize_analysis_frame,
)


class TestDynamicResolutionConfig(unittest.TestCase):
    def test_disabled_by_default_and_native_maximum_is_explicitly_optional(self):
        config = DetectConfig()
        self.assertFalse(config.dynamic_resolution.enabled)
        self.assertIsNone(config.dynamic_resolution.max_height)

    def test_rejects_reversed_misaligned_and_nonpositive_bounds(self):
        for data in (
            {"min_height": 1080, "max_height": 720},
            {"min_height": 0},
            {"max_height": 721},
            {"min_height": 161},
        ):
            with self.subTest(data=data), self.assertRaises(ValidationError):
                DynamicResolutionConfig(**data)

    def test_enabled_range_cannot_upscale_a_capture_stream(self):
        with self.assertRaises(ValidationError):
            DetectConfig(
                width=1280,
                height=720,
                dynamic_resolution={"enabled": True, "min_height": 1080},
            )
        config = DynamicResolutionConfig(enabled=True, max_height=2160)
        with self.assertRaises(ValueError):
            config.validate_capture_height(1080)
        with self.assertRaises(ValueError):
            config.validate_capture_height(None)
        with self.assertRaises(ValidationError):
            DetectConfig(dynamic_resolution={"enabled": True}, fps=0)

    def test_fixed_range_and_equal_bounds_are_valid(self):
        cfg = DetectConfig(
            width=3840,
            height=2160,
            dynamic_resolution={
                "enabled": True,
                "min_height": 1080,
                "max_height": 1080,
            },
        )
        self.assertEqual(cfg.dynamic_resolution.max_height, 1080)


class TestDynamicResolutionController(unittest.TestCase):
    def controller(self, **kwargs):
        """Create a full-HD-to-4K controller without wall-clock waits."""
        return DynamicResolutionController(
            (2160, 3840),
            DynamicResolutionConfig(enabled=True, min_height=720, **kwargs),
            5,
            0,
        )

    def sample(self, controller, start, end, work, skipped=0):
        """Replay continuous measured processing rather than jumping timers."""
        heights = []
        for tick in range(int(start * 5), int(end * 5)):
            now = tick / 5
            controller.observe(work, skipped, now)
            heights.append(controller.select(now).height)
        return heights

    def test_starts_at_maximum_and_does_not_react_to_startup_load(self):
        controller = self.controller()
        heights = self.sample(controller, 0, 30, 0.5, 0.2)
        self.assertEqual(set(heights), {2160})

    def test_sustained_overload_steps_down_but_never_below_floor(self):
        controller = self.controller()
        heights = self.sample(controller, 0, 100, 0.5, 0.2)
        self.assertEqual(sorted(set(heights)), [720, 1080, 1440, 2160])
        self.assertEqual(controller.select(100).height, 720)
        self.assertEqual(controller.reason, "minimum")

    def test_short_spikes_and_midrange_load_do_not_cause_oscillation(self):
        controller = self.controller()
        self.sample(controller, 0, 40, 0.1)
        self.sample(controller, 40, 44, 0.1, 0.1)
        self.sample(controller, 44, 60, 0.14)
        self.assertEqual(controller.select(60).height, 2160)

    def test_recovery_requires_a_full_healthy_hold_per_step(self):
        controller = self.controller()
        self.sample(controller, 0, 100, 0.5, 0.2)
        self.sample(controller, 100, 150, 0.02)
        self.assertEqual(controller.select(150).height, 720)
        self.sample(controller, 150, 190, 0.02)
        self.assertEqual(controller.select(190).height, 1080)
        self.sample(controller, 190, 350, 0.02)
        self.assertEqual(controller.select(350).height, 2160)
        self.assertEqual(controller.reason, "maximum")

    def test_no_recovery_credit_for_a_missing_camera_feed(self):
        controller = self.controller()
        self.sample(controller, 0, 100, 0.5, 0.2)
        self.sample(controller, 100, 120, 0.02)
        self.assertEqual(controller.select(1000).height, 720)
        self.assertIsNone(controller.healthy_since)

    def test_disabled_mode_retains_exact_capture_geometry(self):
        controller = DynamicResolutionController(
            (1082, 1922),
            DynamicResolutionConfig(),
            5,
            0,
        )
        self.sample(controller, 0, 100, 2, 1)
        self.assertEqual(
            controller.select(100),
            AnalysisSize(1922, 1082, 1922, 1082),
        )
        self.assertEqual(controller.reason, "disabled")

    def test_user_ceiling_and_equal_bounds_are_enforced_from_first_frame(self):
        controller = self.controller(max_height=1080)
        self.assertEqual(controller.select(0).height, 1080)
        self.sample(controller, 0, 100, 0.5, 0.2)
        self.assertEqual(controller.select(100).height, 720)
        pinned = DynamicResolutionController(
            (2160, 3840),
            DynamicResolutionConfig(enabled=True, min_height=1080, max_height=1080),
            5,
            0,
        )
        self.sample(pinned, 0, 200, 2, 1)
        self.assertEqual(pinned.select(200).height, 1080)

    def test_bad_load_measurements_are_not_treated_as_successful_recovery(self):
        controller = self.controller()
        for work, skipped in ((float("nan"), 0), (1, float("nan")), (-1, 0), (1, 2)):
            with (
                self.subTest(work=work, skipped=skipped),
                self.assertRaises(ValueError),
            ):
                controller.observe(work, skipped, 0)


class TestAnalysisRaster(unittest.TestCase):
    def test_native_buffer_is_not_copied_or_modified(self):
        frame = np.zeros((540, 640), dtype=np.uint8)
        frame.flags.writeable = False
        self.assertIs(
            resize_analysis_frame(frame, AnalysisSize(640, 360, 640, 360)), frame
        )

    def test_resizes_y_u_and_v_independently_with_correct_i420_layout(self):
        source = np.empty((540, 640), dtype=np.uint8)
        flat = source.reshape(-1)
        flat[: 640 * 360] = 64
        flat[640 * 360 : 640 * 360 * 5 // 4] = 92
        flat[640 * 360 * 5 // 4 :] = 176
        before = source.copy()
        resized = resize_analysis_frame(source, AnalysisSize(320, 180, 640, 360))
        target = resized.reshape(-1)
        self.assertEqual(resized.shape, (270, 320))
        self.assertTrue(np.all(target[: 320 * 180] == 64))
        self.assertTrue(np.all(target[320 * 180 : 320 * 180 * 5 // 4] == 92))
        self.assertTrue(np.all(target[320 * 180 * 5 // 4 :] == 176))
        np.testing.assert_array_equal(before, source)
        self.assertEqual(
            cv2.cvtColor(resized, cv2.COLOR_YUV2RGB_I420).shape, (180, 320, 3)
        )

    def test_geometry_mismatch_is_an_explicit_error(self):
        with self.assertRaises(ValueError):
            resize_analysis_frame(
                np.zeros((540, 640), dtype=np.uint8),
                AnalysisSize(320, 180, 1280, 720),
            )

    def test_minimum_crop_increases_as_analysis_resolution_decreases(self):
        self.assertEqual(AnalysisSize(3840, 2160, 3840, 2160).region_minimum(320), 320)
        self.assertEqual(AnalysisSize(1280, 720, 3840, 2160).region_minimum(320), 960)

    def test_crops_cover_the_original_region_even_at_image_edges(self):
        size = AnalysisSize(852, 480, 3840, 2160)
        for region in (
            (1, 3, 321, 323),
            (3500, 1900, 3900, 2300),
            (-80, -60, 320, 340),
        ):
            with self.subTest(region=region):
                crop, extent = size.project_region(region)
                self.assertTrue(all(point % 4 == 0 for point in crop))
                self.assertEqual(crop[2] - crop[0], crop[3] - crop[1])
                self.assertLessEqual(extent[0], region[0])
                self.assertLessEqual(extent[1], region[1])
                self.assertGreaterEqual(extent[2], region[2])
                self.assertGreaterEqual(extent[3], region[3])

    def test_detections_return_to_canonical_coordinates_before_object_filters(self):
        model = ModelConfig(width=320, height=320)
        config = DetectConfig(width=3840, height=2160)
        canonical_box = (1210, 410, 1450, 650)
        region = (1100, 300, 1580, 780)
        for width, height in ((3840, 2160), (1920, 1080), (852, 480)):
            size = AnalysisSize(width, height, 3840, 2160)
            extent = region if size.native else size.project_region(region)[1]
            x1, y1, x2, y2 = extent
            normalized = (
                (canonical_box[1] - y1) / (y2 - y1),
                (canonical_box[0] - x1) / (x2 - x1),
                (canonical_box[3] - y1) / (y2 - y1),
                (canonical_box[2] - x1) / (x2 - x1),
            )
            detector = Mock()
            detector.detect.return_value = [("car", 0.9, normalized)]
            with (
                self.subTest(height=height),
                patch("frigate.video.detect.create_tensor_input") as tensor,
                patch(
                    "frigate.video.detect.is_object_filtered", return_value=False
                ) as filtered,
            ):
                frame = np.zeros((height * 3 // 2, width), dtype=np.uint8)
                results = detect(
                    config, detector, frame, model, region, ["car"], {}, size
                )
                self.assertIs(tensor.call_args.args[0], frame)
                for actual, expected in zip(results[0][2], canonical_box):
                    self.assertLessEqual(abs(actual - expected), 1)
                self.assertEqual(filtered.call_args.args[0], results[0])

    def test_rejects_detections_entirely_inside_out_of_frame_padding(self):
        size = AnalysisSize(1280, 720, 3840, 2160)
        detector = Mock()
        detector.detect.return_value = [("car", 0.9, (0.0, 0.0, 0.1, 0.1))]
        with (
            patch("frigate.video.detect.create_tensor_input"),
            patch("frigate.video.detect.is_object_filtered") as filtered,
        ):
            results = detect(
                DetectConfig(width=3840, height=2160),
                detector,
                np.zeros((1080, 1280), np.uint8),
                ModelConfig(width=320, height=320),
                (-960, -960, 960, 960),
                ["car"],
                {},
                size,
            )
        self.assertEqual(results, [])
        filtered.assert_not_called()


class TestDynamicResolutionPipeline(unittest.TestCase):
    def test_worker_resizes_real_analysis_frames_then_disables_live_without_changing_capture(
        self,
    ):
        self.run_pipeline_case(all_output_full=False)

    def test_fully_blocked_output_reports_zero_published_rate(self):
        self.run_pipeline_case(all_output_full=True)

    def run_pipeline_case(self, all_output_full):
        """Replay real analysis frames while isolating every external publisher."""
        config = FrigateConfig(
            mqtt={"enabled": False},
            cameras={
                "camera": {
                    "ffmpeg": {
                        "inputs": [
                            {"path": "rtsp://example.test/video", "roles": ["detect"]}
                        ]
                    },
                    "detect": {
                        "enabled": True,
                        "width": 640,
                        "height": 360,
                        "dynamic_resolution": {
                            "enabled": True,
                            "min_height": 160,
                            "max_height": 360,
                        },
                    },
                },
            },
        ).cameras["camera"]
        frame = np.full((540, 640), 128, dtype=np.uint8)
        frame.flags.writeable = False
        frames = Queue()
        for index in range(80):
            frames.put((f"frame-{index}", float(index)))

        def value(initial=0):
            return SimpleNamespace(value=initial)

        reports = []

        class Reports(dict):
            def __setitem__(self, key, data):
                reports.append(data)
                super().__setitem__(key, data)

        metrics = SimpleNamespace(
            frame_queue=frames,
            detection_frame=value(),
            process_fps=value(),
            detection_fps=value(),
            camera_fps=value(5),
            skipped_fps=value(0.5),
            dynamic_resolution=Reports(snapshot=None),
            pipeline={"snapshot": None},
        )
        frame_manager = Mock()
        frame_manager.get.return_value = frame
        tracker = Mock()
        tracker.tracked_objects = {}
        tracker.untracked_object_boxes = []
        motion = Mock()
        motion.detect.return_value = [(20, 20, 100, 100)]
        motion.is_calibrating.return_value = False
        detector = Mock()
        detector.detect.return_value = []
        detector.fps.eps.return_value = 5
        output = Mock()
        output.full.side_effect = lambda: all_output_full or clock["frame"] % 4 == 0
        stop = Mock()
        stop.is_set.return_value = False
        clock = {"frame": -1, "calls": 0}
        sizes = []

        def updates():
            clock["frame"] += 1
            clock["calls"] = 0
            if clock["frame"] == 40:
                config.detect.width, config.detect.height = 1280, 720
                config.detect.dynamic_resolution = DynamicResolutionConfig(
                    enabled=True,
                    min_height=720,
                    max_height=720,
                )
                return {"detect": ["camera"]}
            if clock["frame"] == 50:
                config.detect.width, config.detect.height = 640, 360
                config.detect.dynamic_resolution = DynamicResolutionConfig(
                    enabled=True,
                    min_height=160,
                    max_height=360,
                )
                return {"detect": ["camera"]}
            if clock["frame"] == 60:
                config.detect.dynamic_resolution.enabled = False
                return {"detect": ["camera"]}
            return {}

        def monotonic():
            clock["calls"] += 1
            return max(0, clock["frame"] + 1) + (clock["calls"] - 1) * 0.4

        def resize(source, size):
            sizes.append(size)
            self.assertIs(source, frame)
            return resize_analysis_frame(source, size)

        with ExitStack() as stack:
            subscriber = stack.enter_context(
                patch("frigate.video.detect.CameraConfigUpdateSubscriber")
            )
            subscriber.return_value.check_for_updates.side_effect = updates
            stack.enter_context(patch("frigate.video.detect.CameraGlareMonitor"))
            stack.enter_context(patch("frigate.video.detect.DroppedFrameTracker"))
            fps = stack.enter_context(patch("frigate.video.detect.EventsPerSecond"))
            rates = [Mock(), Mock(), Mock()]
            for rate, rate_value in zip(
                rates, (0 if all_output_full else 5, 5, 5 if all_output_full else 1.25)
            ):
                rate.eps.return_value = rate_value
            fps.side_effect = rates
            stack.enter_context(
                patch("frigate.video.detect.time.monotonic", side_effect=monotonic)
            )
            stack.enter_context(
                patch("frigate.video.detect.resize_analysis_frame", side_effect=resize)
            )
            stack.enter_context(
                patch("frigate.video.detect.get_startup_regions", return_value=[])
            )
            stack.enter_context(
                patch("frigate.video.detect.get_cluster_candidates", return_value=[[0]])
            )
            cluster = stack.enter_context(
                patch(
                    "frigate.video.detect.get_cluster_region",
                    return_value=(0, 0, 320, 320),
                )
            )
            stack.enter_context(
                patch(
                    "frigate.video.detect.get_cluster_region_from_grid",
                    return_value=(0, 0, 320, 320),
                )
            )
            process_frames(
                Mock(),
                frames,
                (360, 640),
                ModelConfig(width=320, height=320),
                config,
                frame_manager,
                motion,
                detector,
                tracker,
                output,
                metrics,
                stop,
                SimpleNamespace(frame_time=value()),
                [],
                exit_on_empty=True,
            )
        self.assertEqual(len(sizes), 80)
        self.assertEqual((sizes[0].width, sizes[0].height), (640, 360))
        self.assertIn(160, [size.height for size in sizes[35:60]])
        self.assertTrue(all(size.native for size in sizes[60:]))
        self.assertTrue(
            all(call.args[1] == (540, 640) for call in frame_manager.get.call_args_list)
        )
        self.assertEqual((config.detect.width, config.detect.height), (640, 360))
        self.assertTrue(
            all(
                call.args[0].shape == (1, 320, 320, 3)
                for call in detector.detect.call_args_list
            )
        )
        self.assertIn(724, [call.args[1] for call in cluster.call_args_list])
        self.assertFalse(metrics.dynamic_resolution["snapshot"]["enabled"])
        self.assertIsNone(metrics.dynamic_resolution["snapshot"]["error"])
        self.assertTrue(
            any(report["error"] == "capture_geometry_pending" for report in reports)
        )
        self.assertEqual(metrics.dynamic_resolution["snapshot"]["height"], 360)
        self.assertTrue(np.all(frame == 128))
        self.assertEqual(rates[1].update.call_count, 80)
        self.assertEqual(rates[2].update.call_count, 80 if all_output_full else 20)
        self.assertEqual(output.put.call_count, 0 if all_output_full else 60)
        self.assertEqual(
            metrics.pipeline["snapshot"]["output_dropped_fps"],
            5 if all_output_full else 1.25,
        )
        self.assertEqual(metrics.process_fps.value, 0 if all_output_full else 5)

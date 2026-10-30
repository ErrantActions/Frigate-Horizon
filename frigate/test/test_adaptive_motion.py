"""Per-cell PID safety, independent profiles and persistence regression tests."""

import hashlib
import tempfile
import time
import unittest
from collections import OrderedDict
from pathlib import Path
from unittest.mock import patch

import numpy as np

from frigate.config.camera.motion import AdaptiveMotionConfig, motion_analysis_shape
from frigate.config.config import RuntimeMotionConfig
from frigate.motion.adaptive import (
    AdaptiveMotionProfile,
    MotionProfileKey,
    gust_band_bounds,
)
from frigate.motion.adaptive_runtime import (
    AdaptiveMotionRuntime,
    MotionSample,
    atomic_json,
    cache_path,
)
from frigate.motion.alignment import CameraViewMonitor


class TestAdaptiveMotion(unittest.TestCase):
    def setUp(self):
        self.settings = AdaptiveMotionConfig(
            columns=4,
            rows=4,
            minimum_observation_seconds=60,
            minimum_episodes=2,
            episode_gap_seconds=10,
            learning_seconds=300,
            quiet_seconds=30,
            decay_seconds=300,
            probe_interval_seconds=300,
            probe_seconds=1,
        )
        self.profile = AdaptiveMotionProfile(self.settings)
        self.profile.activate(1000)

    def test_grid_defaults_and_config_bounds(self):
        defaults = AdaptiveMotionConfig()
        self.assertFalse(defaults.enabled)
        self.assertEqual(defaults.mode, "shadow")
        self.assertEqual((defaults.columns, defaults.rows), (500, 500))
        self.assertEqual(defaults.wind_band_mph, 10)
        for values in (
            {"short_edge": 501},
            {"rows": 0},
            {"timezone": "not/a/timezone"},
            {"probe_seconds": 20, "probe_interval_seconds": 20},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                AdaptiveMotionConfig(**values)

    def test_short_edge_scales_grid_and_analysis_for_landscape_portrait_and_square(
        self,
    ):
        for shape, expected in (
            ((1080, 1920), (500, 889)),
            ((1920, 1080), (889, 500)),
            ((1080, 1080), (500, 500)),
        ):
            with self.subTest(shape=shape):
                config = RuntimeMotionConfig(
                    frame_shape=shape,
                    frame_height=100,
                    adaptive={"enabled": True, "mode": "active", "short_edge": 500},
                )
                self.assertEqual(
                    (config.adaptive.rows, config.adaptive.columns), expected
                )
                self.assertEqual(motion_analysis_shape(config, shape), expected)
                self.assertEqual(config.frame_height, 100)

    def test_requested_grid_is_limited_by_real_native_pixels(self):
        config = RuntimeMotionConfig(
            frame_shape=(360, 640),
            frame_height=100,
            adaptive={"enabled": True, "mode": "active", "short_edge": 500},
        )
        self.assertEqual((config.adaptive.rows, config.adaptive.columns), (360, 640))
        self.assertEqual(motion_analysis_shape(config, (360, 640)), (360, 640))
        self.assertEqual(config.adaptive.short_edge, 500)

    def test_disabled_analysis_preserves_existing_resolution(self):
        config = RuntimeMotionConfig(frame_shape=(1080, 1920), frame_height=100)
        self.assertEqual(motion_analysis_shape(config, (1080, 1920)), (100, 177))

    def test_vertical_grid_count_uses_current_frame_height_not_pixels(self):
        for shape, expected in (
            ((1080, 1920), (500, 889)),
            ((1920, 1080), (500, 281)),
            ((1080, 1080), (500, 500)),
        ):
            with self.subTest(shape=shape):
                config = RuntimeMotionConfig(
                    frame_shape=shape,
                    frame_height=100,
                    adaptive={"enabled": True, "grid_height": 500},
                )
                self.assertEqual(
                    (config.adaptive.rows, config.adaptive.columns), expected
                )
                self.assertEqual(motion_analysis_shape(config, shape), expected)
                self.assertEqual(config.frame_height, 100)

    def test_vertical_count_overrides_legacy_computed_dimensions(self):
        settings = AdaptiveMotionConfig(
            grid_height=100,
            short_edge=500,
            columns=889,
            rows=500,
        )
        settings.resolve_grid((1080, 1920))
        self.assertEqual((settings.rows, settings.columns), (100, 178))

    def test_legacy_dimensions_choose_their_shorter_edge_until_explicitly_overridden(
        self,
    ):
        settings = AdaptiveMotionConfig(columns=64, rows=36)
        settings.resolve_grid((1080, 1920))
        self.assertEqual(
            (settings.rows, settings.columns, settings.short_edge), (36, 64, 36)
        )
        settings = AdaptiveMotionConfig(columns=64, rows=36, short_edge=100)
        settings.resolve_grid((1080, 1920))
        self.assertEqual((settings.rows, settings.columns), (100, 178))

    def test_half_hour_wind_and_direction_are_independent(self):
        first = MotionProfileKey.at(0, "UTC", 0, 0, 16)
        self.assertEqual(first.window, 0)
        self.assertNotEqual(first, MotionProfileKey.at(1800, "UTC", 0, 0, 16))
        self.assertEqual(first, MotionProfileKey.at(86400, "UTC", 0, 360, 16))
        self.assertNotEqual(first, MotionProfileKey.at(0, "UTC", 5, 0, 16))
        unknown = MotionProfileKey.at(0, "UTC", None, None, 16)
        self.assertIsNone(unknown.gust_band_mph)
        self.assertNotEqual(first, unknown)

    def test_single_crossing_does_not_train_suppression(self):
        moving = np.zeros((4, 4), np.float32)
        moving[1, 1] = 1
        for timestamp in range(1001, 1020):
            self.profile.update(moving, timestamp)
        self.assertEqual(self.profile.episodes[1, 1], 1)

    def test_low_wind_bands_are_independent_of_selected_width(self):
        for width in (5, 10):
            for mph, expected in (
                (0, (0, 5)),
                (4.999, (0, 5)),
                (5, (5, 10)),
                (9.999, (5, 10)),
                (10, (10, 10 + width)),
                (15, (15, 20) if width == 5 else (10, 20)),
                (20, (20, 20 + width)),
            ):
                with self.subTest(width=width, mph=mph):
                    self.assertEqual(gust_band_bounds(mph, width), expected)
                    key = MotionProfileKey.at(
                        0, "UTC", mph / 2.2369362921, 0, 16, width
                    )
                    self.assertEqual(key.gust_band_mph, expected[0])

    def test_invalid_wind_bounds_are_explicit(self):
        for mph, width in ((-1, 10), (float("nan"), 10), (float("inf"), 5), (1, 0)):
            with self.subTest(mph=mph, width=width), self.assertRaises(ValueError):
                gust_band_bounds(mph, width)
        self.assertEqual(float(self.profile.adjustment.max()), 0)

    def test_missing_frames_and_pauses_do_not_count_as_quiet(self):
        zeros = np.zeros((4, 4), np.float32)
        self.profile.update(zeros, 1100)
        self.profile.update(zeros, 1101, paused=True)
        self.assertEqual(float(self.profile.quiet.max()), 0)
        self.assertEqual(float(self.profile.observed.max()), 0)

    def test_useful_track_hold_is_local_and_restores_baseline(self):
        self.profile.adjustment.fill(8)
        self.profile.hold([(25, 25, 50, 50)], (100, 100), 1001)
        effective = self.profile.effective_adjustment(1002)
        self.assertEqual(effective[1, 1], 0)
        self.assertEqual(effective[3, 3], 8)
        self.profile.update(np.ones((4, 4), np.float32), 1002)
        self.assertEqual(self.profile.observed[1, 1], 0)
        self.assertGreater(self.profile.observed[3, 3], 0)

    def test_quiet_speeds_decay_toward_baseline(self):
        self.profile.adjustment.fill(8)
        self.profile.quiet[1, 1] = self.settings.quiet_seconds
        self.profile.update(np.zeros((4, 4), np.float32), 1001)
        self.assertLess(self.profile.adjustment[1, 1], self.profile.adjustment[0, 0])
        self.assertGreaterEqual(self.profile.adjustment.min(), 0)

    def test_reactivation_decays_without_creating_quiet_or_derivative_spike(self):
        self.profile.adjustment.fill(8)
        self.profile.integral.fill(4)
        self.profile.derivative.fill(50)
        self.profile.activate(2000)
        self.assertLess(float(self.profile.adjustment.max()), 8)
        self.assertEqual(float(self.profile.derivative.max()), 0)
        self.assertEqual(float(self.profile.quiet.max()), 0)

    def test_periodic_baseline_probe_restores_all_cells(self):
        self.profile.adjustment.fill(8)
        self.assertEqual(float(self.profile.effective_adjustment(1200).max()), 0)
        self.assertEqual(float(self.profile.effective_adjustment(1202).max()), 8)

    def test_increases_respect_slew_and_total_limits(self):
        self.profile.observed.fill(10000)
        self.profile.episodes.fill(100)
        self.profile.activity.fill(1)
        self.profile.previous_error.fill(0.9)
        self.profile.update(np.ones((4, 4), np.float32), 1001)
        self.assertLessEqual(
            float(self.profile.adjustment.max()),
            self.settings.max_change_per_minute / 60 + 1e-6,
        )
        self.assertLessEqual(
            float(self.profile.adjustment.max()), self.settings.max_adjustment
        )

    def test_invalid_evidence_is_explicit(self):
        for values in (np.zeros((3, 4)), np.full((4, 4), np.nan)):
            with self.assertRaises(ValueError):
                self.profile.update(values, 1001)


class TestAdaptivePersistence(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.settings = AdaptiveMotionConfig(columns=4, rows=4)
        self.runtime = AdaptiveMotionRuntime.__new__(AdaptiveMotionRuntime)
        self.runtime.settings = self.settings
        self.runtime.directory = self.directory
        self.runtime.base_directory = self.directory
        self.runtime.state_directory = self.directory
        self.runtime.initialized = True
        self.runtime.profiles = OrderedDict()
        self.runtime.camera = "synthetic"
        self.runtime.frame_shape = (100, 100)
        self.runtime.baseline = 30
        self.runtime.error = None
        self.runtime.updated_at = 1000
        self.runtime.adjustment = None
        self.key = MotionProfileKey(0, None, None)

    def test_alignment_recovery_retires_history_before_resuming_learning(self):
        random = np.random.default_rng(42)
        original = random.integers(0, 255, (100, 100), dtype=np.uint8)
        replacement = random.integers(0, 255, (100, 100), dtype=np.uint8)
        self.runtime.alignment = CameraViewMonitor()
        self.runtime.alignment.check(original)
        self.runtime.alignment_loaded = True
        profile = self.runtime._load(self.key, 1000)
        profile.adjustment.fill(8)
        self.runtime.adjustment = profile.adjustment.copy()
        self.runtime.key = self.key
        sample = MotionSample(
            1001, np.zeros((100, 100), np.uint8), [], False, False, replacement
        )
        for _ in range(5):
            self.assertFalse(self.runtime._check_alignment(sample))
            self.assertEqual(self.runtime.directory, self.directory)
        self.assertFalse(self.runtime._check_alignment(sample))
        self.assertEqual(self.runtime.alignment_warning, "camera_view_changed")
        self.assertNotEqual(self.runtime.directory, self.directory)
        self.assertTrue((self.directory / "view.json").is_file())
        self.assertFalse(self.runtime.profiles)
        self.assertIsNone(self.runtime.adjustment)
        self.assertIsNone(self.runtime.key)
        self.assertTrue(self.runtime._check_alignment(sample))
        self.assertIsNone(self.runtime.alignment_warning)
        fresh = self.runtime._load(self.key, 1002)
        self.assertEqual(float(fresh.adjustment.max()), 0)
        self.assertEqual(float(fresh.observed.max()), 0)

    def test_saved_window_resumes_independently(self):
        profile = self.runtime._load(self.key, 1000)
        profile.adjustment.fill(6)
        self.runtime._save(self.key, profile)
        self.runtime.profiles.clear()
        other = self.runtime._load(MotionProfileKey(1, None, None), 1010)
        self.assertEqual(float(other.adjustment.max()), 0)
        restored = self.runtime._load(self.key, 1020)
        self.assertGreater(float(restored.adjustment.min()), 5)
        self.assertLess(float(restored.adjustment.max()), 6)

    def test_grid_warm_start_does_not_multiply_evidence(self):
        profile = self.runtime._load(self.key, 1000)
        profile.adjustment.fill(6)
        profile.episodes.fill(50)
        profile.observed.fill(10000)
        self.runtime._save(self.key, profile)
        self.runtime.profiles.clear()
        self.runtime.settings = self.settings.model_copy(
            update={"columns": 8, "rows": 8}
        )
        inherited = self.runtime._load(self.key, 1001)
        self.assertEqual(inherited.adjustment.shape, (8, 8))
        self.assertTrue(inherited.inherited)
        self.assertGreater(float(inherited.adjustment.min()), 0)
        self.assertEqual(float(inherited.episodes.max()), 0)
        self.assertEqual(float(inherited.observed.max()), 0)
        self.assertEqual(float(inherited.derivative.max()), 0)
        self.assertEqual(len(list(self.directory.glob("*.previous"))), 1)

    def test_corrupt_profile_never_becomes_successful_baseline(self):
        (self.directory / self.key.filename).write_bytes(b"corrupt")
        with self.assertRaises(ValueError):
            self.runtime._load(self.key, 1000)

    def test_warm_start_prior_survives_restart_without_fake_episode_counts(self):
        profile = self.runtime._load(self.key, 1000)
        profile.adjustment.fill(4)
        profile.inherited = True
        self.runtime._save(self.key, profile)
        self.runtime.profiles.clear()
        restored = self.runtime._load(self.key, 1001)
        self.assertTrue(restored.inherited)
        self.assertEqual(float(restored.episodes.max()), 0)

    def test_direction_interpolation_wraps_and_unknown_neighbors_use_baseline(self):
        self.runtime.settings = self.settings.model_copy(
            update={"minimum_observation_seconds": 60}
        )
        for sector in (15, 0):
            key = MotionProfileKey(0, 0, sector)
            profile = self.runtime._load(key, 1000)
            profile.adjustment.fill(8)
            profile.observed.fill(100)
        weather = {"gust_mps": 0, "direction_degrees": 348.75}
        output = self.runtime._interpolate(MotionProfileKey(0, 0, 0), weather, 1001)
        self.assertAlmostEqual(float(output[0, 0]), 8, places=2)
        weather["gust_mps"] = 2.5 / 2.2369362921
        output = self.runtime._interpolate(MotionProfileKey(0, 0, 0), weather, 1001)
        self.assertAlmostEqual(float(output[0, 0]), 4, places=2)

    def test_low_wind_interpolation_uses_the_mandatory_five_mph_split(self):
        self.runtime.settings = self.settings.model_copy(
            update={"minimum_observation_seconds": 60, "wind_band_mph": 10}
        )
        for band, adjustment in ((0, 2), (5, 6), (10, 10), (20, 12)):
            profile = self.runtime._load(MotionProfileKey(0, band, 0), 1000)
            profile.adjustment.fill(adjustment)
            profile.observed.fill(100)
        for mph, expected in ((0, 2), (2.5, 4), (5, 6), (7.5, 8), (10, 10), (15, 11)):
            weather = {"gust_mps": mph / 2.2369362921, "direction_degrees": 0}
            key = MotionProfileKey.at(0, "UTC", weather["gust_mps"], 0, 16, 10)
            actual = self.runtime._interpolate(key, weather, 1000)
            with self.subTest(mph=mph):
                self.assertAlmostEqual(float(actual[0, 0]), expected, places=4)

    def test_shadow_never_applies_learned_adjustment(self):
        self.runtime.adjustment = np.full((4, 4), 12, np.float32)
        self.runtime.updated_at = 1000
        with patch("frigate.motion.adaptive_runtime.time.time", return_value=1001):
            result = self.runtime.threshold(np.full((10, 10), 35, np.uint8), [])
        self.assertTrue((result == 255).all())

    def test_active_holds_and_stale_state_fall_back_to_configured_threshold(self):
        self.runtime.settings = self.settings.model_copy(update={"mode": "active"})
        self.runtime.adjustment = np.full((4, 4), 12, np.float32)
        with patch("frigate.motion.adaptive_runtime.time.time", return_value=1001):
            result = self.runtime.threshold(
                np.full((10, 10), 35, np.uint8), [(0, 0, 50, 50)]
            )
        self.assertEqual(result[1, 1], 255)
        self.assertEqual(result[8, 8], 0)
        with patch("frigate.motion.adaptive_runtime.time.time", return_value=1020):
            result = self.runtime.threshold(np.full((10, 10), 35, np.uint8), [])
        self.assertTrue((result == 255).all())


class TestAdaptiveWorker(unittest.TestCase):
    def test_split_wind_bands_do_not_relabel_legacy_ten_mph_history(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(
            patch("frigate.motion.adaptive_runtime.CONFIG_DIR", str(directory))
        )
        self.enterContext(
            patch("frigate.motion.adaptive_runtime.threading.Thread.start")
        )
        for width in (5, 10):
            settings = AdaptiveMotionConfig(wind_band_mph=width)
            runtime = AdaptiveMotionRuntime(
                "synthetic", settings, (100, 100), 30, "test-view"
            )
            old_identity = hashlib.sha256(
                f"test-view:(100, 100):30:UTC:{width}:16".encode()
            ).hexdigest()[:24]
            legacy = runtime.state_directory / old_identity
            legacy.mkdir(parents=True)
            marker = legacy / "legacy-history"
            marker.write_bytes(b"existing wider-band observations")
            if width == 10:
                self.assertNotEqual(runtime.directory, legacy)
            else:
                self.assertEqual(runtime.directory, legacy)
            self.assertEqual(marker.read_bytes(), b"existing wider-band observations")

    def test_actual_worker_publishes_local_diagnostics_and_persists_state(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(
            patch(
                "frigate.motion.adaptive_runtime.CONFIG_DIR", str(directory / "config")
            )
        )
        self.enterContext(
            patch("frigate.motion.adaptive_runtime.CACHE_DIR", str(directory / "cache"))
        )
        settings = AdaptiveMotionConfig(enabled=True, columns=8, rows=8)
        runtime = AdaptiveMotionRuntime(
            "synthetic", settings, (100, 100), 30, "test-view"
        )
        self.addCleanup(runtime.stop)
        now = time.time()
        atomic_json(
            cache_path("synthetic", "request"),
            {
                "timestamp": now,
                "field": "effective",
                "resolution": 8,
                "column": 1,
                "row": 1,
            },
        )
        random = np.random.default_rng(42)
        reference = random.integers(0, 255, (100, 100), dtype=np.uint8)
        for offset in (0, 1):
            runtime.samples.put(
                MotionSample(
                    now + offset,
                    np.zeros((100, 100), np.uint8),
                    [],
                    False,
                    False,
                    reference,
                ),
                timeout=2,
            )
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                path = cache_path("synthetic", "snapshot")
                if path.exists():
                    import json

                    snapshot = json.loads(path.read_text())
                    if snapshot["updated_at"] >= now + offset:
                        break
                runtime.stopped.wait(0.01)
            else:
                self.fail("Adaptive worker did not publish a diagnostic snapshot")
        self.assertEqual(snapshot["mode"], "shadow")
        self.assertFalse(snapshot["applied"])
        self.assertEqual(snapshot["grid"]["values"], [30.0] * 64)
        self.assertEqual(snapshot["grid"]["minimum"], 30)
        self.assertEqual(snapshot["grid"]["maximum"], 30)
        self.assertIn("cell", snapshot)
        runtime.stop()
        self.assertTrue(list((directory / "config").rglob("window-*.npz")))
        self.assertTrue(list((directory / "config").rglob("reference.npy")))
        profile = next(iter(runtime.profiles.values()))
        profile.adjustment.fill(0)
        profile.adjustment[0, 0] = 8
        atomic_json(
            cache_path("synthetic", "request"),
            {
                "timestamp": now + 2,
                "field": "adjustment",
                "resolution": 4,
            },
        )
        runtime._publish(
            MotionSample(now + 2, np.zeros((100, 100), np.uint8), [], False, False),
            profile,
            {"state": "unknown"},
        )
        import json

        overview = json.loads(cache_path("synthetic", "snapshot").read_text())
        self.assertEqual(overview["grid"]["minimum"], 0)
        self.assertEqual(overview["grid"]["maximum"], 8)
        self.assertEqual(max(overview["grid"]["values"]), 2)

    def test_default_500_by_500_controller_computes_finite_local_state(self):
        settings = AdaptiveMotionConfig()
        profile = AdaptiveMotionProfile(settings)
        profile.activate(1000)
        motion = np.zeros((500, 500), np.float32)
        motion[200:250, 200:250] = 1
        profile.update(motion, 1001)
        self.assertEqual(profile.adjustment.shape, (500, 500))
        self.assertTrue(np.isfinite(profile.activity).all())
        self.assertGreater(profile.activity[220, 220], 0)
        self.assertEqual(profile.activity[0, 0], 0)

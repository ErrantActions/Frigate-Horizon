"""Preview autotuning learns only compressed noise and does not silently hide unfamiliar activity."""

import unittest

from frigate.motion.compressed_tuner import CompressedNoiseEstimator
from frigate.test.test_compressed_tuner import sample


class TestCompressedNoiseEstimator(unittest.TestCase):
    def trained(self, value=0.01):
        tuner = CompressedNoiseEstimator(
            minimum_samples=10, capacity=30, quiet_seconds=4
        )
        result = None
        for index in range(20):
            result = tuner.observe(sample(index * 0.5, value=value))
        return tuner, result

    def test_repeated_compressed_noise_is_removed_after_warmup(self):
        _, result = self.trained()
        self.assertEqual(result["status"], "ready")
        self.assertGreater(result["vector_floor"][50], 0.01)
        self.assertEqual(result["vector_signal"][50], 0)
        self.assertEqual(result["coded_signal"][50], 0)
        self.assertFalse(result["pixels_used"])
        self.assertFalse(result["applies_to_live_detector"])

    def test_warmup_keeps_raw_activity_visible(self):
        tuner = CompressedNoiseEstimator()
        result = tuner.observe(sample(0, value=0.02))
        self.assertEqual(result["status"], "learning")
        self.assertEqual(result["vector_floor"][50], 0)
        self.assertAlmostEqual(result["vector_signal"][50], 0.02)

    def test_new_foreground_and_neighbors_are_held_out_of_noise_learning(self):
        tuner, _ = self.trained()
        result = tuner.observe(sample(10, value=0.5))
        self.assertGreater(result["held_cells"], 0)
        self.assertGreater(result["vector_floor"][50], 0)
        self.assertAlmostEqual(result["vector_signal"][50], 0.5)
        moved = tuner.observe(sample(10.5, index=51, value=0.5))
        self.assertGreater(moved["vector_floor"][51], 0)
        self.assertGreater(moved["held_cells"], 1)

    def test_unknowns_and_intra_frames_never_become_quiet_noise_samples(self):
        tuner = CompressedNoiseEstimator()
        for index in range(30):
            evidence = sample(index * 0.5, unknown="vectors_unavailable")
            evidence.picture_type = "I"
            result = tuner.observe(evidence)
        self.assertEqual(result["samples"], 0)
        self.assertEqual(result["status"], "learning")
        self.assertEqual(result["vector_floor"][50], 0)

    def test_saturated_activity_cannot_calibrate_itself_as_quiet(self):
        tuner, result = self.trained(value=0.8)
        self.assertFalse(tuner.ready[:, 1, 18].any())
        self.assertEqual(result["vector_floor"][50], 0)
        self.assertAlmostEqual(result["vector_signal"][50], 0.8)

    def test_gaps_and_geometry_changes_drop_old_observations(self):
        tuner, _ = self.trained()
        result = tuner.observe(sample(30))
        self.assertEqual(result["status"], "learning")
        self.assertEqual(result["samples"], 1)
        result = tuner.observe(sample(30.5, width=1920))
        self.assertEqual(result["samples"], 1)

    def test_memory_and_invalid_observation_bounds_are_explicit(self):
        tuner = CompressedNoiseEstimator(minimum_samples=10, capacity=20)
        for index in range(100):
            result = tuner.observe(sample(index * 0.5))
        self.assertLessEqual(result["samples"], 20)
        with self.assertRaises(ValueError):
            CompressedNoiseEstimator(minimum_samples=5)
        with self.assertRaises(ValueError):
            CompressedNoiseEstimator(minimum_samples=60, capacity=10)

    def test_default_baseline_requires_two_quiet_minutes_not_twenty_frames(self):
        tuner = CompressedNoiseEstimator()
        for index in range(3000):
            result = tuner.observe(sample(index * 0.04, value=0.01))
        self.assertEqual(result["status"], "learning")
        result = tuner.observe(sample(120, value=0.01))
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["vector_signal"][50], 0)
        self.assertEqual(result["baseline_seconds"], 120)

    def test_one_frame_attack_and_release_at_native_frame_rate(self):
        tuner, _ = self.trained()
        for timestamp in (10, 10.04, 10.08):
            result = tuner.observe(sample(timestamp, value=0.5))
            self.assertAlmostEqual(result["vector_signal"][50], 0.5)
            self.assertTrue(result["wake_required"])
        result = tuner.observe(sample(10.12, value=0.01))
        self.assertEqual(result["vector_signal"][50], 0)
        self.assertFalse(result["wake_required"])
        self.assertGreater(result["held_cells"], 0)

    def test_long_foreground_does_not_replace_learned_baseline(self):
        tuner, _ = self.trained()
        for index in range(200):
            result = tuner.observe(sample(10 + index * 0.5, value=0.2))
        self.assertAlmostEqual(result["vector_signal"][50], 0.2)
        self.assertGreater(result["vector_floor"][50], 0)
        result = tuner.observe(sample(110, value=0.01))
        self.assertEqual(result["vector_signal"][50], 0)

    def test_boot_event_pauses_only_affected_cells_quiet_calibration(self):
        tuner = CompressedNoiseEstimator(
            minimum_samples=10, capacity=30, quiet_seconds=8
        )
        for index in range(15):
            tuner.observe(sample(index * 0.5, value=0.01))
        duration = tuner.quiet_duration[:, 1, 18].copy()
        for index in range(15, 31):
            result = tuner.observe(sample(index * 0.5, value=0.5))
            self.assertAlmostEqual(result["vector_signal"][50], 0.5)
        self.assertEqual(tuner.quiet_duration[:, 1, 18].tolist(), duration.tolist())
        self.assertFalse(tuner.ready[:, 1, 18].any())
        self.assertTrue(tuner.ready[:, 0, 0].all())
        for index in range(31, 34):
            result = tuner.observe(sample(index * 0.5, value=0.01))
        self.assertEqual(result["vector_signal"][50], 0)

    def test_busy_coded_channel_does_not_block_vector_noise_calibration(self):
        tuner = CompressedNoiseEstimator(
            minimum_samples=10, capacity=30, quiet_seconds=4
        )
        for index in range(20):
            evidence = sample(index * 0.5, value=0.01)
            evidence.coded_activity = [0.8] * 576
            result = tuner.observe(evidence)
        self.assertEqual(result["vector_signal"][50], 0)
        self.assertAlmostEqual(result["coded_signal"][50], 0.8)
        self.assertTrue(result["wake_required"])

    def test_bootstrap_keeps_small_noise_fluctuations_in_robust_statistics(self):
        tuner = CompressedNoiseEstimator(
            minimum_samples=10, capacity=30, quiet_seconds=12
        )
        for index in range(25):
            value = 0.01 if index < 10 or index % 2 == 0 else 0.03
            result = tuner.observe(sample(index * 0.5, value=value))
        self.assertTrue(tuner.ready[:, 1, 18].all())
        self.assertEqual(result["vector_signal"][50], 0)

    def test_unknown_evidence_requires_wake_even_when_activity_is_zero(self):
        tuner, _ = self.trained()
        result = tuner.observe(sample(10, value=0, unknown="vectors_unavailable"))
        self.assertTrue(result["wake_required"])
        self.assertFalse(result["reliable"])
        self.assertEqual(result["vector_floor"][50], 0)

    def test_cameras_do_not_share_baselines(self):
        first, _ = self.trained(value=0.01)
        second, _ = self.trained(value=0.1)
        self.assertGreater(
            first.observe(sample(10, value=0.1))["vector_signal"][50], 0.09
        )
        self.assertEqual(second.observe(sample(10, value=0.1))["vector_signal"][50], 0)


if __name__ == "__main__":
    unittest.main()

"""Compressed-only spatial recurrence learns sustained movement without using image pixels."""

import math
import unittest

import numpy as np

from frigate.motion.compressed_protocol import CompressedEvidence
from frigate.motion.compressed_tuner import MAX_OFFSET, CompressedMotionTuner


def sample(timestamp, *, index=50, value=0.003, unknown=None, width=3840):
    vector = [0.0] * 576
    vector[index] = value
    return CompressedEvidence(
        type="frame",
        index=0,
        pts=round(timestamp * 1000),
        relative_seconds=timestamp,
        width=width,
        height=2160,
        picture_type="P",
        vector_count=32400,
        moving_vectors=1,
        coverage=1,
        vectors_sha256=None,
        uncertainty=unknown,
        time_base=(1, 1000),
        profile_idc=100,
        cabac=1,
        activity=vector,
        coded_activity=vector,
    )


class TestCompressedMotionTuner(unittest.TestCase):
    def test_regular_keyframes_do_not_reset_compressed_background_learning(self):
        tuner = CompressedMotionTuner(minimum_observation_seconds=5, learning_seconds=2)
        for index in range(600):
            observed = sample(index * 0.2)
            if index % 10 == 0:
                observed.picture_type = "I"
                observed.vector_count = 0
                observed.uncertainty = "vectors_unavailable"
                observed.coded_activity = [1.0] * 576
            result = tuner.observe(observed, suppression_allowed=True)
        self.assertEqual(tuner.resets, 0)
        self.assertGreater(tuner.samples, 500)
        self.assertGreater(result.coded.flat[50], 0)

    def train(self, tuner, count=600, *, gust=False):
        result = None
        for index in range(count):
            timestamp = index * 0.2
            value = 0.003 if not gust else 0.004 + 0.001 * math.sin(timestamp / 8)
            result = tuner.observe(
                sample(timestamp, value=value), suppression_allowed=True
            )
        return result

    def test_continuous_background_learns_without_stop_start_episodes_or_pixels(self):
        tuner = CompressedMotionTuner(minimum_observation_seconds=5, learning_seconds=2)
        result = self.train(tuner)
        self.assertGreater(result.vector.flat[50], 0)
        self.assertGreater(result.coded.flat[50], 0)
        self.assertLessEqual(result.vector.max(), MAX_OFFSET)
        self.assertEqual(
            tuner.snapshot()["input"], "motion_vectors_and_coded_block_activity"
        )
        self.assertFalse(tuner.snapshot()["pixel_data_used_for_learning"])
        self.assertFalse(tuner.snapshot()["pixel_history_reused"])

    def test_learning_does_not_automatically_authorize_suppression(self):
        tuner = CompressedMotionTuner(minimum_observation_seconds=5, learning_seconds=2)
        self.train(tuner)
        result = tuner.observe(sample(120), suppression_allowed=False)
        self.assertFalse(np.any(result.vector))
        self.assertFalse(np.any(result.coded))

    def test_new_spatial_path_is_not_suppressed_by_a_busy_background_cell(self):
        tuner = CompressedMotionTuner(minimum_observation_seconds=5, learning_seconds=2)
        self.train(tuner)
        for step in range(1, 10):
            result = tuner.observe(
                sample(120 + step * 0.2, index=50 + step, value=0.05),
                suppression_allowed=True,
            )
            self.assertEqual(result.vector.flat[50 + step], 0)
            self.assertEqual(result.coded.flat[50 + step], 0)

    def test_larger_unseen_gust_wakes_instead_of_instantly_raising_tolerance(self):
        tuner = CompressedMotionTuner(minimum_observation_seconds=5, learning_seconds=2)
        self.train(tuner, gust=True)
        result = tuner.observe(sample(120.2, value=0.2), suppression_allowed=True)
        self.assertEqual(result.vector.flat[50], 0)
        self.assertEqual(result.coded.flat[50], 0)

    def test_repeated_multi_second_gusts_have_bounded_history(self):
        tuner = CompressedMotionTuner(minimum_observation_seconds=5, learning_seconds=2)
        self.train(tuner, count=900, gust=True)
        self.assertLessEqual(len(tuner.patterns), 300)
        self.assertEqual(tuner.snapshot()["pattern_history_seconds"], 60)
        self.assertGreater(tuner.snapshot()["repeating_pattern_cells"], 0)
        self.assertLessEqual(tuner.adjustment.max(), MAX_OFFSET)

    def test_protected_objects_and_periodic_probes_have_no_offsets(self):
        tuner = CompressedMotionTuner(minimum_observation_seconds=5, learning_seconds=2)
        self.train(tuner)
        probe = tuner.observe(sample(120), suppression_allowed=True)
        self.assertFalse(np.any(probe.vector))
        result = tuner.observe(
            sample(120.2), protected_activity=True, suppression_allowed=True
        )
        self.assertFalse(np.any(result.vector))
        self.assertEqual(tuner.reason, "protected_activity")

    def test_source_gaps_geometry_and_global_change_reset_learning(self):
        tuner = CompressedMotionTuner(minimum_observation_seconds=5, learning_seconds=2)
        self.train(tuner)
        result = tuner.observe(sample(130), suppression_allowed=True)
        self.assertFalse(np.any(result.vector))
        self.assertGreater(tuner.resets, 0)
        tuner.observe(sample(130.2, width=1920), suppression_allowed=True)
        self.assertGreater(tuner.resets, 1)
        observed = sample(130.4, width=1920)
        observed.activity = [0.5] * 576
        result = tuner.observe(observed, suppression_allowed=True)
        self.assertFalse(np.any(result.vector))

    def test_unknown_evidence_does_not_learn_or_earn_quiet_history(self):
        tuner = CompressedMotionTuner()
        for index in range(50):
            result = tuner.observe(
                sample(index * 0.2, unknown="vectors_unavailable"),
                suppression_allowed=True,
            )
        self.assertEqual(tuner.samples, 0)
        self.assertFalse(np.any(result.coded))

    def test_instances_and_default_warmup_do_not_reuse_other_learning(self):
        learned = CompressedMotionTuner(
            minimum_observation_seconds=5, learning_seconds=2
        )
        self.train(learned)
        fresh = CompressedMotionTuner()
        result = self.train(fresh, count=100)
        self.assertFalse(np.any(result.vector))
        self.assertEqual(fresh.minimum_observation_seconds, 300)
        self.assertFalse(np.any(CompressedMotionTuner().adjustment))


if __name__ == "__main__":
    unittest.main()

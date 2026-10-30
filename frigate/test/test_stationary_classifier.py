"""Tests for stationary object classification thresholds."""

import unittest
from unittest.mock import patch

import cv2
import numpy as np

from frigate.track.stationary_classifier import (
    DEFAULT_OBJECT_THRESHOLDS,
    DYNAMIC_OBJECT_THRESHOLDS,
    NON_STATIONARY_OBJECT_THRESHOLDS,
    STATIONARY_OBJECT_THRESHOLDS,
    StationaryMotionClassifier,
    StationaryThresholds,
    get_stationary_threshold,
)


class TestStationaryMotionClassifier(unittest.TestCase):
    def setUp(self) -> None:
        random = np.random.default_rng(42)
        bgr = random.integers(24, 230, (240, 320, 3), dtype=np.uint8)
        self.frame = cv2.cvtColor(bgr, cv2.COLOR_BGR2YUV_I420)
        self.box = (80, 60, 200, 180)
        self.classifier = StationaryMotionClassifier()
        self.classifier.ensure_anchor("car", self.frame, self.box)

    def test_constant_small_anchor_offset_is_not_cumulative_drift(self):
        with (
            patch.object(
                cv2,
                "matchTemplate",
                return_value=np.array([[0.88]], dtype=np.float32),
            ),
            patch.object(
                cv2,
                "phaseCorrelate",
                return_value=((2.4, 0), 1.0),
            ),
        ):
            for _ in range(10):
                self.assertTrue(self.classifier.evaluate("car", self.frame, self.box))

    def test_changed_pixels_in_expanded_box_are_not_ignored(self):
        changed = self.frame.copy()
        changed[60:180, 200:260] = 255
        expanded = (80, 60, 260, 180)
        results = [self.classifier.evaluate("car", changed, expanded) for _ in range(3)]
        self.assertFalse(results[-1])

    def test_unchanged_scene_with_different_box_sizes_stays_stationary(self):
        for box in ((100, 74, 180, 170), (70, 50, 220, 190), self.box) * 3:
            self.assertTrue(self.classifier.evaluate("car", self.frame, box))

    def test_actual_image_translation_becomes_active(self):
        moved = self.frame.copy()
        moved[:240] = cv2.warpAffine(
            self.frame[:240],
            np.float32([[1, 0, 18], [0, 1, 0]]),
            (320, 240),
        )
        moved_box = (98, 60, 218, 180)
        self.assertFalse(
            [self.classifier.evaluate("car", moved, moved_box) for _ in range(3)][-1]
        )

    def test_gradual_anchor_relative_displacement_is_detected(self):
        with patch.object(
            cv2, "matchTemplate", return_value=np.array([[0.88]], dtype=np.float32)
        ):
            results = []
            for fraction in (0.01, 0.02, 0.03, 0.04, 0.05):
                with patch.object(
                    cv2,
                    "phaseCorrelate",
                    return_value=((fraction * self.classifier.CROP_SIZE, 0), 1.0),
                ):
                    results.append(
                        self.classifier.evaluate("car", self.frame, self.box)
                    )
        self.assertEqual(results, [True, True, True, True, False])

    def test_brightness_offset_does_not_imply_translation(self):
        brighter = self.frame.copy()
        brighter[:240] = np.clip(brighter[:240].astype(np.int16) + 15, 0, 255).astype(
            np.uint8
        )
        self.assertTrue(self.classifier.evaluate("car", brighter, self.box))

    def test_disjoint_detection_is_not_hidden_by_the_old_reference(self):
        self.assertFalse(
            self.classifier.evaluate("car", self.frame, (220, 60, 310, 180))
        )

    def test_unknown_reference_does_not_claim_stationary_proof(self):
        self.assertFalse(self.classifier.evaluate("unknown", self.frame, self.box))

    def test_geometry_change_clears_reference(self):
        self.assertFalse(
            self.classifier.evaluate("car", np.zeros((180, 160), np.uint8), self.box)
        )
        self.assertNotIn("car", self.classifier.anchor_frames)
        self.assertNotIn("car", self.classifier.anchor_crops)

    def test_reference_budget_falls_back_and_reset_releases_memory(self):
        self.classifier.MAX_REFERENCES = 2
        self.classifier.ensure_anchor("second", self.frame, self.box)
        with self.assertLogs("frigate.track.stationary_classifier", level="WARNING"):
            self.classifier.ensure_anchor("third", self.frame, self.box)
        self.assertNotIn("third", self.classifier.anchor_frames)
        self.assertFalse(self.classifier.evaluate("third", self.frame, self.box))
        self.classifier.reset("car")
        self.assertNotIn("car", self.classifier.anchor_boxes)
        self.assertNotIn("car", self.classifier.changed_counts)
        self.classifier.ensure_anchor("third", self.frame, self.box)
        self.assertIn("third", self.classifier.anchor_frames)

    def test_scene_reference_is_bounded_for_landscape_and_portrait(self):
        for height, width in ((2160, 3840), (3840, 2160)):
            frame = np.full((height * 3 // 2, width), 128, np.uint8)
            classifier = StationaryMotionClassifier()
            classifier.ensure_anchor("large", frame, (0, 0, width, height))
            reference = classifier.anchor_frames["large"]
            self.assertLessEqual(reference.shape[0] * 2 // 3, 640)
            self.assertLessEqual(reference.shape[1], 640)

    def test_nonfinite_comparison_cannot_keep_an_object_stationary(self):
        with patch.object(
            cv2, "matchTemplate", return_value=np.array([[np.nan]], dtype=np.float32)
        ):
            self.assertFalse(self.classifier.evaluate("car", self.frame, self.box))


class TestStationaryThresholds(unittest.TestCase):
    def test_known_labels_return_expected_singletons(self) -> None:
        self.assertIs(get_stationary_threshold("package"), STATIONARY_OBJECT_THRESHOLDS)
        self.assertIs(get_stationary_threshold("car"), DYNAMIC_OBJECT_THRESHOLDS)
        self.assertIs(
            get_stationary_threshold("license_plate"),
            NON_STATIONARY_OBJECT_THRESHOLDS,
        )

    def test_unknown_label_returns_shared_default(self) -> None:
        # an unknown label must reuse the shared default instance, not allocate
        # a fresh one on every call (this runs per object per frame)
        first = get_stationary_threshold("person")
        second = get_stationary_threshold("dog")
        self.assertIs(first, DEFAULT_OBJECT_THRESHOLDS)
        self.assertIs(second, DEFAULT_OBJECT_THRESHOLDS)

    def test_default_matches_a_fresh_instance(self) -> None:
        # the shared default must be value-equivalent to the previous
        # per-call StationaryThresholds()
        self.assertEqual(DEFAULT_OBJECT_THRESHOLDS, StationaryThresholds())


if __name__ == "__main__":
    unittest.main()

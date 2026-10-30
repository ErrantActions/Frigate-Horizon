"""Camera movement checks reject weak or localized feature evidence."""

import unittest

import cv2
import numpy as np

from frigate.motion.alignment import CameraViewMonitor


class TestCameraViewMonitor(unittest.TestCase):
    def setUp(self):
        random = np.random.default_rng(42)
        self.frame = cv2.GaussianBlur(
            random.integers(0, 255, (160, 240), dtype=np.uint8), (3, 3), 0
        )
        self.monitor = CameraViewMonitor()
        self.assertEqual(self.monitor.check(self.frame).state, "warming_up")

    def test_unchanged_background_is_stable(self):
        self.assertEqual(self.monitor.check(self.frame.copy()).state, "stable")

    def test_exposure_changes_do_not_make_a_stationary_view_uncertain(self):
        darker = (self.frame.astype(np.float32) * 0.5 + 10).astype(np.uint8)
        self.assertEqual(self.monitor.check(darker).state, "stable")

    def test_exposure_change_does_not_hide_actual_camera_movement(self):
        darker = (self.frame.astype(np.float32) * 0.5 + 10).astype(np.uint8)
        shifted = cv2.warpAffine(
            darker,
            np.float32([[1, 0, 4], [0, 1, 3]]),
            (self.frame.shape[1], self.frame.shape[0]),
            borderMode=cv2.BORDER_REFLECT,
        )
        self.assertEqual(
            [self.monitor.check(shifted).state for _ in range(3)],
            ["checking", "checking", "moved"],
        )

    def test_sustained_coherent_shift_invalidates_old_view(self):
        shifted = cv2.warpAffine(
            self.frame,
            np.float32([[1, 0, 4], [0, 1, 3]]),
            (self.frame.shape[1], self.frame.shape[0]),
            borderMode=cv2.BORDER_REFLECT,
        )
        states = [self.monitor.check(shifted).state for _ in range(3)]
        self.assertEqual(states, ["checking", "checking", "moved"])
        self.assertEqual(self.monitor.check(shifted).state, "stable")

    def test_passing_foreground_object_does_not_move_the_camera(self):
        changed = self.frame.copy()
        changed[40:100, 90:130] = 255
        excluded = np.zeros_like(self.frame, dtype=bool)
        excluded[40:100, 90:130] = True
        for _ in range(4):
            self.assertEqual(self.monitor.check(changed, excluded).state, "stable")

    def test_insufficient_features_are_uncertain_not_stationary_proof(self):
        monitor = CameraViewMonitor()
        uniform = np.zeros_like(self.frame)
        monitor.check(uniform)
        self.assertEqual(monitor.check(uniform).state, "uncertain")

    def test_unmatchable_reference_recovers_only_after_sustained_stability(self):
        random = np.random.default_rng(91)
        replacement = cv2.GaussianBlur(
            random.integers(0, 255, self.frame.shape, dtype=np.uint8), (3, 3), 0
        )
        for _ in range(5):
            self.assertEqual(self.monitor.check(replacement).state, "uncertain")
            np.testing.assert_array_equal(self.monitor.reference, self.frame)
        self.assertEqual(self.monitor.check(replacement).state, "moved")
        self.assertEqual(self.monitor.check(replacement).state, "stable")

    def test_changing_unmatchable_views_do_not_recover(self):
        random = np.random.default_rng(91)
        for _ in range(12):
            changing = cv2.GaussianBlur(
                random.integers(0, 255, self.frame.shape, dtype=np.uint8), (3, 3), 0
            )
            self.assertEqual(self.monitor.check(changing).state, "uncertain")
        np.testing.assert_array_equal(self.monitor.reference, self.frame)

    def test_recovery_requires_distributed_unexcluded_features(self):
        random = np.random.default_rng(91)
        replacement = cv2.GaussianBlur(
            random.integers(0, 255, self.frame.shape, dtype=np.uint8), (3, 3), 0
        )
        excluded = np.ones_like(self.frame, dtype=bool)
        excluded[: self.frame.shape[0] // 2, : self.frame.shape[1] // 2] = False
        for _ in range(10):
            self.assertEqual(
                self.monitor.check(replacement, excluded).state, "uncertain"
            )
        np.testing.assert_array_equal(self.monitor.reference, self.frame)

    def test_original_view_return_cancels_recovery(self):
        random = np.random.default_rng(91)
        replacement = cv2.GaussianBlur(
            random.integers(0, 255, self.frame.shape, dtype=np.uint8), (3, 3), 0
        )
        for _ in range(4):
            self.monitor.check(replacement)
        self.assertEqual(self.monitor.check(self.frame).state, "stable")
        for _ in range(5):
            self.assertEqual(self.monitor.check(replacement).state, "uncertain")
        self.assertEqual(self.monitor.check(replacement).state, "moved")

    def test_recovery_does_not_follow_a_continuously_moving_candidate(self):
        random = np.random.default_rng(91)
        replacement = cv2.GaussianBlur(
            random.integers(0, 255, self.frame.shape, dtype=np.uint8), (3, 3), 0
        )
        for offset in range(12):
            shifted = cv2.warpAffine(
                replacement,
                np.float32([[1, 0, offset], [0, 1, 0]]),
                (replacement.shape[1], replacement.shape[0]),
                borderMode=cv2.BORDER_REFLECT,
            )
            self.assertEqual(self.monitor.check(shifted).state, "uncertain")
        np.testing.assert_array_equal(self.monitor.reference, self.frame)

    def test_textureless_replacement_cannot_recover(self):
        uniform = np.zeros_like(self.frame)
        for _ in range(10):
            self.assertEqual(self.monitor.check(uniform).state, "uncertain")
        np.testing.assert_array_equal(self.monitor.reference, self.frame)

    def test_invalid_frame_shape_is_explicit(self):
        with self.assertRaises(ValueError):
            self.monitor.check(np.zeros((10, 10, 3), dtype=np.uint8))

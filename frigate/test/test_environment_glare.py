"""Synthetic frame thresholds for glare, not vehicle-identification claims."""

import unittest
from dataclasses import dataclass

import numpy as np

from frigate.environment.glare import GlareDetector


@dataclass
class Settings:
    enabled: bool = True
    brightness_threshold: int = 220
    minimum_bright_fraction: float = 0.03
    minimum_fraction_increase: float = 0.02
    minimum_luminance_increase: int = 20
    maximum_frame_gap_seconds: float = 5
    baseline_seconds: float = 30
    cooldown_seconds: float = 30
    global_change_fraction: float = 0.5


class TestTransientGlare(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()
        self.mask = np.zeros((100, 100), dtype=np.bool_)
        self.mask[10:40, 10:40] = True
        self.detector = GlareDetector(self.settings, {"driveway": self.mask})
        self.dark = np.full((100, 100), 30, dtype=np.uint8)

    def bright(self):
        frame = self.dark.copy()
        frame[15:25, 15:25] = 255
        return frame

    def test_detects_sudden_localized_brightness_and_not_a_steady_plateau(self):
        self.assertEqual(self.detector.update(self.dark, 0), ())
        self.assertEqual(self.detector.update(self.bright(), 1), ("driveway",))
        for tick in range(2, 35):
            self.assertEqual(self.detector.update(self.bright(), tick), ())

    def test_existing_nighttime_lights_establish_a_baseline_without_triggering(self):
        for tick in range(35):
            self.assertEqual(self.detector.update(self.bright(), tick), ())

    def test_global_flash_and_exposure_changes_are_excluded(self):
        self.detector.update(self.dark, 0)
        self.assertEqual(
            self.detector.update(np.full((100, 100), 255, dtype=np.uint8), 1), ()
        )
        self.assertEqual(self.detector.update(self.dark, 2), ())

    def test_gradual_threshold_crossing_is_not_sudden_glare(self):
        for tick, value in enumerate(range(180, 241)):
            frame = self.dark.copy()
            frame[15:25, 15:25] = value
            self.assertEqual(self.detector.update(frame, tick), ())

    def test_dawn_dusk_context_excludes_a_local_transition(self):
        self.detector.update(self.dark, 0)
        self.assertEqual(
            self.detector.update(self.bright(), 1, lighting_transition=True), ()
        )
        self.assertEqual(self.detector.update(self.bright(), 2), ())

    def test_stream_gap_recalibrates_instead_of_triggering_on_reconnection(self):
        self.detector.update(self.dark, 0)
        self.assertEqual(self.detector.update(self.bright(), 10), ())
        self.assertEqual(self.detector.update(self.bright(), 11), ())

    def test_cooldown_blocks_repeated_flashes_but_expires_at_its_boundary(self):
        self.settings.baseline_seconds = 0.1
        self.detector.update(self.dark, 0)
        self.assertEqual(self.detector.update(self.bright(), 1), ("driveway",))
        self.detector.update(self.dark, 2)
        self.assertEqual(self.detector.update(self.bright(), 3), ())
        for tick in range(4, 31):
            self.detector.update(self.dark, tick)
        self.assertEqual(self.detector.update(self.bright(), 31), ("driveway",))

    def test_retains_its_own_previous_frame_when_input_memory_is_reused(self):
        frame = self.dark.copy()
        self.detector.update(frame, 0)
        frame[15:25, 15:25] = 255
        self.assertEqual(self.detector.update(frame, 1), ("driveway",))

    def test_brightness_outside_configured_zones_does_not_trigger(self):
        self.detector.update(self.dark, 0)
        frame = self.dark.copy()
        frame[60:70, 60:70] = 255
        self.assertEqual(self.detector.update(frame, 1), ())

    def test_exact_bright_area_and_luma_thresholds_are_measured(self):
        previous = self.dark.copy()
        previous[self.mask] = 200
        self.detector.update(previous, 0)
        frame = previous.copy()
        frame[10, 10:37] = 220
        self.assertEqual(self.detector.update(frame, 1), ("driveway",))
        detector = GlareDetector(self.settings, {"driveway": self.mask})
        previous[self.mask] = 201
        detector.update(previous, 0)
        frame = previous.copy()
        frame[10, 10:37] = 220
        self.assertEqual(detector.update(frame, 1), ())

    def test_global_exclusion_includes_its_exact_fraction_boundary(self):
        self.detector.update(self.dark, 0)
        frame = self.dark.copy()
        frame[:50] = 255
        self.assertEqual(self.detector.update(frame, 1), ())

    def test_rejects_invalid_masks_frames_and_clocks(self):
        with self.assertRaises(ValueError):
            GlareDetector(
                self.settings, {"empty": np.zeros((100, 100), dtype=np.bool_)}
            )
        with self.assertRaises(ValueError):
            self.detector.update(np.zeros((10, 10), dtype=np.uint8), 0)
        with self.assertRaises(ValueError):
            self.detector.update(self.dark.astype(np.float32), 0)
        self.detector.update(self.dark, 10)
        with self.assertRaises(ValueError):
            self.detector.update(self.dark, 9)


if __name__ == "__main__":
    unittest.main()

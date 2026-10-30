"""Parked vehicle appearance matching and tracker continuity regressions."""

import unittest
from unittest.mock import MagicMock, patch

import cv2
import numpy as np

from frigate.camera import PTZMetrics
from frigate.config import FrigateConfig
from frigate.track.norfair_tracker import NorfairTracker
from frigate.track.parked_vehicle_memory import ParkedVehicleMemory

BOX = (80, 60, 240, 200)


def make_frame(seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    bgr = rng.integers(24, 230, (240, 320, 3), dtype=np.uint8)
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2YUV_I420)


class TestParkedVehicleMemory(unittest.TestCase):
    def setUp(self) -> None:
        self.memory = ParkedVehicleMemory()
        self.frame = make_frame()
        self.memory.validate(self.frame, 100, 120)
        self.remember()

    def remember(self, id: str = "original", track_id: str = "old") -> None:
        self.memory.remember(id, track_id, "car", BOX, self.frame, 100)

    def match(self, label: str = "car", box=BOX, active=None, candidates=None):
        return self.memory.match(
            label,
            box,
            active or set(),
            candidates if candidates is not None else [(label, box)],
        )

    def test_matching_vehicle_reuses_original_id(self):
        self.memory.validate(self.frame, 101, 120)
        self.assertEqual(self.match(), "original")

    def test_small_box_jitter_is_allowed(self):
        self.assertEqual(self.match(box=(84, 62, 244, 202)), "original")

    def test_different_position_is_not_a_match(self):
        self.assertIsNone(self.match(box=(0, 0, 80, 60)))

    def test_different_label_is_not_a_match(self):
        self.assertIsNone(self.match(label="person"))

    def test_currently_detected_vehicle_cannot_be_claimed_twice(self):
        self.assertIsNone(self.match(active={"old"}))

    def test_two_memories_are_ambiguous(self):
        self.remember("other", "older")
        self.assertIsNone(self.match())

    def test_two_detections_are_ambiguous(self):
        self.assertIsNone(self.match(candidates=[("car", BOX), ("car", BOX)]))

    def test_changed_appearance_discards_memory(self):
        self.memory.validate(make_frame(2), 101, 120)
        self.assertIsNone(self.match())

    def test_different_color_discards_memory_with_identical_structure(self):
        recolored = self.frame.copy()
        recolored[240:] = 32
        self.memory.validate(recolored, 101, 120)
        self.assertIsNone(self.match())

    def test_small_brightness_shift_keeps_memory(self):
        brighter = self.frame.copy()
        brighter[:240] = np.clip(brighter[:240].astype(np.int16) + 2, 0, 255).astype(
            np.uint8
        )
        self.memory.validate(brighter, 101, 120)
        self.assertEqual(self.match(), "original")

    def test_expiration_uses_elapsed_time(self):
        self.memory.validate(self.frame, 220, 120)
        self.assertEqual(self.match(), "original")
        self.memory.validate(self.frame, 220.001, 120)
        self.assertIsNone(self.match())

    def test_clock_reversal_clears_memory(self):
        self.memory.validate(self.frame, 99, 120)
        self.assertIsNone(self.match())

    def test_frame_geometry_change_clears_memory(self):
        self.memory.validate(np.zeros((180, 160), dtype=np.uint8), 101, 120)
        self.assertIsNone(self.match())

    def test_uniform_crop_is_not_remembered(self):
        self.memory.clear()
        uniform = np.full_like(self.frame, 128)
        self.memory.remember("uniform", "old", "car", BOX, uniform, 100)
        self.assertFalse(self.memory.references)

    def test_small_or_invalid_crops_are_not_remembered(self):
        self.memory.clear()
        for box in ((0, 0, 8, 8), (-1, 0, 20, 20), (30, 30, 20, 20)):
            self.memory.remember("invalid", "old", "car", box, self.frame, 100)
        self.assertFalse(self.memory.references)

    def test_people_are_not_remembered(self):
        self.memory.clear()
        self.memory.remember("person", "old", "person", BOX, self.frame, 100)
        self.assertFalse(self.memory.references)

    def test_memory_is_bounded(self):
        for i in range(100):
            self.remember(str(i), str(i))
        self.assertEqual(len(self.memory.references), self.memory.MAX_OBJECTS)

    def test_color_conversion_is_bounded_even_for_4k_frames(self):
        frame = np.full((3240, 3840), 128, dtype=np.uint8)
        frame[:2160] = np.linspace(20, 220, 3840, dtype=np.uint8)
        memory = ParkedVehicleMemory()
        memory.validate(frame, 100, 120)
        with patch(
            "frigate.track.parked_vehicle_memory.get_histogram",
            wraps=__import__(
                "frigate.util.image", fromlist=["get_histogram"]
            ).get_histogram,
        ) as histogram:
            memory.remember("large", "old", "car", (0, 0, 3840, 2160), frame, 100)
            self.assertEqual(histogram.call_args.args[0].shape, (96, 64))


class TestParkedVehicleTracker(unittest.TestCase):
    def setUp(self) -> None:
        self.time = 1000.0
        config = FrigateConfig(
            **{
                "mqtt": {"enabled": False},
                "cameras": {
                    "driveway": {
                        "ffmpeg": {
                            "inputs": [
                                {
                                    "path": "rtsp://example.test/video",
                                    "roles": ["detect"],
                                }
                            ]
                        },
                        "detect": {
                            "enabled": True,
                            "width": 320,
                            "height": 240,
                            "min_initialized": 2,
                            "max_disappeared": 4,
                            "stationary": {
                                "threshold": 4,
                                "parked_vehicle_memory": True,
                            },
                        },
                        "objects": {"track": ["car", "person"]},
                    }
                },
            }
        )
        self.tracker = NorfairTracker(config.cameras["driveway"], PTZMetrics())
        self.tracker.frame_manager = MagicMock()
        self.tracker.frame_manager.get.return_value = make_frame()

    def frames(self, count: int, label: str | None = "car", box=BOX):
        for _ in range(count):
            self.time += 0.2
            detections = (
                [(label, 0.95, box, 22400, 1.14, (0, 0, 320, 240))]
                if label is not None
                else []
            )
            self.tracker.match_and_update("frame", self.time, detections)

    def park(self) -> str:
        self.frames(15)
        self.assertEqual(len(self.tracker.tracked_objects), 1)
        id = next(iter(self.tracker.tracked_objects))
        self.assertIn(id, self.tracker.parked_vehicle_memory.references)
        return id

    def test_original_id_survives_tracker_expiration_and_reacquisition(self):
        id = self.park()
        old_track_id = next(iter(self.tracker.track_id_map))
        self.frames(15, None)
        self.assertIn(id, self.tracker.tracked_objects)
        self.frames(15)
        self.assertEqual(set(self.tracker.tracked_objects), {id})
        self.assertNotIn(old_track_id, self.tracker.track_id_map)
        self.assertEqual(len(self.tracker.track_id_map), 1)
        self.assertEqual(self.tracker.tracked_objects[id]["position_changes"], 0)

    def test_deregister_releases_bounded_appearance_references(self):
        id = self.park()
        track_id = next(iter(self.tracker.track_id_map))
        self.assertIn(id, self.tracker.stationary_classifier.anchor_frames)
        self.tracker.deregister(id, track_id)
        self.assertNotIn(id, self.tracker.stationary_classifier.anchor_frames)
        self.assertNotIn(id, self.tracker.stationary_classifier.anchor_crops)
        self.assertNotIn(id, self.tracker.stationary_classifier.anchor_boxes)
        self.assertNotIn(id, self.tracker.stationary_classifier.changed_counts)

    def test_static_vehicle_box_jitter_does_not_create_initial_movement(self):
        self.frames(3)
        for index in range(40):
            self.frames(1, box=BOX if index % 2 else (120, 85, 220, 185))
        self.assertEqual(len(self.tracker.tracked_objects), 1)
        obj = next(iter(self.tracker.tracked_objects.values()))
        self.assertEqual(obj["position_changes"], 0)
        self.assertGreaterEqual(
            obj["motionless_count"], self.tracker.detect_config.stationary.threshold
        )

    def test_initial_appearance_guard_does_not_apply_to_people(self):
        self.frames(3, "person")
        for index in range(40):
            self.frames(1, "person", box=BOX if index % 2 else (120, 85, 220, 185))
        self.assertTrue(
            any(
                obj["position_changes"] > 0
                for obj in self.tracker.tracked_objects.values()
            )
        )
        self.assertFalse(self.tracker.stationary_classifier.anchor_frames)

    def test_disabled_classifier_keeps_initial_geometric_movement(self):
        self.tracker.camera_config.detect.stationary.classifier = False
        self.frames(3)
        for index in range(40):
            self.frames(1, box=BOX if index % 2 else (120, 85, 220, 185))
        self.assertTrue(
            any(
                obj["position_changes"] > 0
                for obj in self.tracker.tracked_objects.values()
            )
        )
        self.assertFalse(self.tracker.stationary_classifier.anchor_frames)

    def test_arriving_vehicle_is_active_before_settling_into_a_parked_track(self):
        random = np.random.default_rng(9)
        background = random.integers(20, 60, (240, 320, 3), dtype=np.uint8)
        vehicle = random.integers(70, 230, (80, 80, 3), dtype=np.uint8)
        active_seen = False
        for offset in range(0, 64, 4):
            image = background.copy()
            image[70:150, 40 + offset : 120 + offset] = vehicle
            self.tracker.frame_manager.get.return_value = cv2.cvtColor(
                image, cv2.COLOR_BGR2YUV_I420
            )
            self.frames(1, box=(40 + offset, 70, 120 + offset, 150))
            active_seen |= any(
                obj["position_changes"] > 0
                for obj in self.tracker.tracked_objects.values()
            )
        self.assertTrue(active_seen)
        self.frames(20, box=(100, 70, 180, 150))
        self.assertTrue(
            any(
                obj["motionless_count"]
                >= self.tracker.detect_config.stationary.threshold
                for obj in self.tracker.tracked_objects.values()
            )
        )

    def test_parked_vehicle_departure_still_becomes_active(self):
        random = np.random.default_rng(9)
        background = random.integers(20, 60, (240, 320, 3), dtype=np.uint8)
        vehicle = random.integers(70, 230, (80, 80, 3), dtype=np.uint8)
        image = background.copy()
        image[70:150, 40:120] = vehicle
        self.tracker.frame_manager.get.return_value = cv2.cvtColor(
            image, cv2.COLOR_BGR2YUV_I420
        )
        self.frames(20, box=(40, 70, 120, 150))
        self.assertTrue(
            any(
                obj["motionless_count"]
                >= self.tracker.detect_config.stationary.threshold
                for obj in self.tracker.tracked_objects.values()
            )
        )
        for offset in range(4, 64, 4):
            image = background.copy()
            image[70:150, 40 + offset : 120 + offset] = vehicle
            self.tracker.frame_manager.get.return_value = cv2.cvtColor(
                image, cv2.COLOR_BGR2YUV_I420
            )
            self.frames(1, box=(40 + offset, 70, 120 + offset, 150))
        self.assertTrue(
            any(
                obj["position_changes"] > 0
                for obj in self.tracker.tracked_objects.values()
            )
        )

    def test_reidentification_removes_superseded_track_in_either_order(self):
        for new_first in (False, True):
            with self.subTest(new_first=new_first):
                self.setUp()
                id = self.park()
                tracker = self.tracker.get_tracker("car")
                old = tracker.tracked_objects[0]
                replacement = MagicMock()
                replacement.global_id = "replacement"
                replacement.estimate = np.array([BOX[:2], BOX[2:]])
                replacement.estimate_velocity = np.zeros((2, 2))
                replacement.last_detection.data = {
                    **old.last_detection.data,
                    "frame_time": self.time + 0.2,
                }
                objects = [replacement, old] if new_first else [old, replacement]
                with patch.object(tracker, "update", return_value=objects):
                    self.frames(1)
                self.assertEqual(self.tracker.track_id_map, {"replacement": id})
                self.assertEqual(set(self.tracker.tracked_objects), {id})
                self.assertNotIn(old, tracker.tracked_objects)

    def test_missing_frames_do_not_claim_fresh_detector_results(self):
        id = self.park()
        last_detection_time = self.tracker.tracked_objects[id]["frame_time"]
        self.frames(15, None)
        self.assertEqual(
            self.tracker.tracked_objects[id]["frame_time"], last_detection_time
        )
        self.assertGreater(self.tracker.disappeared[id], 0)

    def test_changed_vehicle_at_same_position_gets_new_id(self):
        id = self.park()
        self.frames(15, None)
        self.tracker.frame_manager.get.return_value = make_frame(2)
        self.frames(15)
        self.assertNotIn(id, self.tracker.tracked_objects)
        self.assertEqual(len(self.tracker.tracked_objects), 1)

    def test_departed_vehicle_is_not_held(self):
        id = self.park()
        self.tracker.frame_manager.get.return_value = np.full(
            (360, 320), 128, dtype=np.uint8
        )
        self.frames(15, None)
        self.assertNotIn(id, self.tracker.tracked_objects)

    def test_memory_timeout_ends_track(self):
        id = self.park()
        self.time += 121
        self.frames(15, None)
        self.assertNotIn(id, self.tracker.tracked_objects)

    def test_disabled_feature_keeps_original_expiration_behavior(self):
        self.tracker.detect_config.stationary.parked_vehicle_memory = False
        self.frames(15)
        self.assertFalse(self.tracker.parked_vehicle_memory.references)
        self.frames(15, None)
        self.assertFalse(self.tracker.tracked_objects)

    def test_disabling_feature_releases_held_tracks(self):
        id = self.park()
        self.frames(15, None)
        self.tracker.detect_config.stationary.parked_vehicle_memory = False
        self.frames(1, None)
        self.assertNotIn(id, self.tracker.tracked_objects)

    def test_runtime_config_replacement_applies_without_restart(self):
        id = self.park()
        self.frames(15, None)
        stationary = self.tracker.camera_config.detect.stationary.model_copy(
            update={"parked_vehicle_memory": False}
        )
        self.tracker.camera_config.detect = (
            self.tracker.camera_config.detect.model_copy(
                update={"stationary": stationary}
            )
        )
        self.frames(1, None)
        self.assertNotIn(id, self.tracker.tracked_objects)

    def test_person_tracking_does_not_acquire_parked_memory(self):
        self.frames(15, "person")
        self.assertFalse(self.tracker.parked_vehicle_memory.references)
        self.frames(15, None)
        self.assertFalse(self.tracker.tracked_objects)

    def test_person_can_be_detected_while_vehicle_memory_is_held(self):
        id = self.park()
        self.frames(15, "person")
        self.assertIn(id, self.tracker.tracked_objects)
        self.assertEqual(
            sorted(obj["label"] for obj in self.tracker.tracked_objects.values()),
            ["car", "person"],
        )

    def test_memory_does_not_enable_disabled_stationary_classifier(self):
        self.tracker.detect_config.stationary.classifier = False
        self.park()
        self.assertFalse(self.tracker.stationary_classifier.anchor_crops)

    def test_low_confidence_vehicle_does_not_acquire_memory(self):
        self.tracker.camera_config.objects.filters["car"].threshold = 0.99
        self.frames(15)
        self.assertFalse(self.tracker.parked_vehicle_memory.references)

    def test_memory_defaults_and_duration_bounds(self):
        stationary_type = type(self.tracker.detect_config.stationary)
        self.assertFalse(stationary_type().parked_vehicle_memory)
        self.assertEqual(stationary_type().parked_vehicle_memory_seconds, 120)
        for seconds in (0, -1, 601):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                stationary_type(parked_vehicle_memory_seconds=seconds)

    def test_missing_image_releases_memory(self):
        id = self.park()
        self.frames(15, None)
        self.tracker.frame_manager.get.return_value = None
        self.frames(1, None)
        self.assertNotIn(id, self.tracker.tracked_objects)

    def test_disabled_detection_releases_held_vehicle(self):
        id = self.park()
        self.frames(15, None)
        self.tracker.camera_config.detect.enabled = False
        self.frames(1, None)
        self.assertNotIn(id, self.tracker.tracked_objects)

    def test_explicit_stationary_limit_prevents_memory(self):
        self.tracker.detect_config.stationary.max_frames.objects["car"] = 5
        self.frames(15)
        self.assertFalse(self.tracker.parked_vehicle_memory.references)

    def test_enabling_stationary_limit_releases_existing_memory(self):
        id = self.park()
        self.frames(15, None)
        self.tracker.detect_config.stationary.max_frames.objects["car"] = 5
        self.frames(1, None)
        self.assertNotIn(id, self.tracker.tracked_objects)

    def test_autotracking_disables_memory(self):
        self.park()
        self.tracker.camera_config.onvif.autotracking.enabled = True
        self.tracker.ptz_motion_estimator = MagicMock()
        self.tracker.ptz_motion_estimator.motion_estimator.return_value = None
        self.frames(1, None)
        self.assertFalse(self.tracker.parked_vehicle_memory.references)


if __name__ == "__main__":
    unittest.main()

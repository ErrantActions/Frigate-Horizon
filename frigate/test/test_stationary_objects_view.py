"""Read-only stationary snapshots and stationary-only image annotations."""

import copy
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np

from frigate.camera.state import CameraState


def tracked_object(identifier="100-abcdef", active=False, false_positive=False):
    """Build only the tracker fields read by the diagnostic snapshot."""
    data = {
        "id": identifier,
        "label": "car",
        "box": (10, 10, 50, 50),
        "motionless_count": 75,
        "position_changes": 0,
        "start_time": 80.0,
        "frame_time": 100.0,
    }
    rendered = {
        **data,
        "stationary": not active,
        "false_positive": false_positive,
        "score": 0.8,
        "computed_score": 0.85,
        "area": 1600,
        "current_estimated_speed": 0,
        "current_attributes": [],
    }
    return SimpleNamespace(
        active=active,
        false_positive=false_positive,
        computed_score=0.85,
        obj_data=data,
        to_dict=lambda: rendered,
    )


class TestStationaryObjectSnapshot(unittest.TestCase):
    def setUp(self):
        self.state = CameraState.__new__(CameraState)
        self.state.current_frame_lock = threading.Lock()
        self.state.current_frame_time = 100.0
        self.state.camera_config = SimpleNamespace(
            detect=SimpleNamespace(stationary=SimpleNamespace(threshold=50))
        )
        self.state.tracked_objects = {
            "stationary": tracked_object(),
            "moving": tracked_object("100-moving", active=True),
            "false": tracked_object("100-false", false_positive=True),
        }

    def test_snapshot_excludes_moving_and_false_positive_tracks_without_mutation(self):
        original = {
            key: copy.deepcopy(value.obj_data)
            for key, value in self.state.tracked_objects.items()
        }
        result = self.state.get_stationary_objects()
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["frame_time"], 100)
        self.assertEqual(
            result["objects"][0],
            {
                "id": "100-abcdef",
                "label": "car",
                "box": (10, 10, 50, 50),
                "score": 0.85,
                "stationary_frames": 75,
                "position_changes": 0,
                "tracked_seconds": 20.0,
                "last_tracking_update": 100.0,
                "updated_in_current_frame": True,
            },
        )
        self.assertEqual(
            original,
            {key: value.obj_data for key, value in self.state.tracked_objects.items()},
        )
        result["objects"][0]["box"] = (0, 0, 1, 1)
        self.assertEqual(
            self.state.tracked_objects["stationary"].obj_data["box"], (10, 10, 50, 50)
        )

    def test_older_track_update_is_not_reported_as_current(self):
        self.state.tracked_objects["stationary"].obj_data["frame_time"] = 90
        obj = self.state.get_stationary_objects()["objects"][0]
        self.assertFalse(obj["updated_in_current_frame"])
        self.assertEqual(obj["last_tracking_update"], 90)

    def test_new_movement_is_not_hidden_by_an_older_stationary_flag(self):
        self.state.tracked_objects["stationary"].obj_data["motionless_count"] = 0
        self.assertEqual(self.state.get_stationary_objects()["objects"], [])

    def test_snapshot_is_bounded_and_keeps_the_total(self):
        self.state.tracked_objects = {
            str(index): tracked_object(f"100-{index:04}") for index in range(125)
        }
        result = self.state.get_stationary_objects()
        self.assertEqual(result["total"], 125)
        self.assertEqual(len(result["objects"]), 100)
        self.assertEqual(result, self.state.get_stationary_objects())

    def test_no_frame_is_explicitly_unavailable(self):
        self.state.current_frame_time = 0
        self.state.tracked_objects = {}
        self.assertIsNone(self.state.get_stationary_objects()["frame_time"])

    def test_stationary_only_drawing_does_not_change_the_default_overlay(self):
        frame = np.zeros((100, 100, 3), np.uint8)
        self.state._current_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2YUV_I420)
        self.state.motion_boxes = []
        self.state.regions = []
        self.state.camera_config = SimpleNamespace(
            onvif=SimpleNamespace(autotracking=SimpleNamespace(enabled=False)),
            detect=SimpleNamespace(stationary=SimpleNamespace(threshold=50)),
        )
        self.state.model = SimpleNamespace(colormap={})
        with patch("frigate.camera.state.draw_box_with_label") as draw:
            self.state.get_current_frame(
                {"bounding_boxes": True, "stationary_only": True}
            )
            self.assertEqual(draw.call_count, 1)
            self.assertEqual(draw.call_args.args[5], "car abcdef")
            self.assertTrue(draw.call_args.args[6].startswith("85%"))
            draw.reset_mock()
            self.state.get_current_frame({"bounding_boxes": True})
            self.assertEqual(draw.call_count, 3)

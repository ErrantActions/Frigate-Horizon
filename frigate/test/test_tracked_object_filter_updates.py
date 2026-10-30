"""In-flight candidates must not kill shared processing after a live object-filter edit."""

import unittest
from types import SimpleNamespace

from frigate.config import FilterConfig
from frigate.track.tracked_object import TrackedObject


class TestTrackedObjectFilterUpdates(unittest.TestCase):
    def candidate(self, accepted: bool = False) -> TrackedObject:
        value = TrackedObject.__new__(TrackedObject)
        value.false_positive = not accepted
        value.obj_data = {"label": "car"}
        value.computed_score = 0.9
        value.camera_config = SimpleNamespace(
            name="test_camera",
            objects=SimpleNamespace(filters={"car": FilterConfig(threshold=0.7)}),
        )
        return value

    def test_removed_filter_rejects_unconfirmed_candidate_without_keyerror(self):
        candidate = self.candidate()
        candidate.camera_config.objects.filters.clear()
        with self.assertLogs(
            "frigate.track.tracked_object", level="WARNING"
        ) as observed:
            self.assertTrue(candidate._is_false_positive())
        self.assertIn("removed while a candidate track was active", observed.output[0])

    def test_accepted_event_remains_accepted_when_filter_is_removed(self):
        candidate = self.candidate(accepted=True)
        candidate.camera_config.objects.filters.clear()
        self.assertFalse(candidate._is_false_positive())

    def test_existing_filter_keeps_threshold_behavior(self):
        candidate = self.candidate()
        self.assertFalse(candidate._is_false_positive())
        candidate.computed_score = 0.5
        self.assertTrue(candidate._is_false_positive())

    def test_live_threshold_change_is_used_for_unconfirmed_candidates(self):
        candidate = self.candidate()
        candidate.camera_config.objects.filters["car"].threshold = 0.95
        self.assertTrue(candidate._is_false_positive())


if __name__ == "__main__":
    unittest.main()

"""Verify person-only Coral grants, bounded decoding and unchanged alert eligibility."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from frigate.motion import native_person as person
from frigate.plugins import inference
from frigate.plugins.inference import EventInferenceConfig, EventInferenceController


class TestPersonWindowFiles(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for module, attribute, path in (
            (person, "LEASE_PATH", self.root / "person-lease.json"),
            (person, "WINDOWS_PATH", self.root / "windows.json"),
            (inference, "LEASE_PATH", self.root / "motion-lease.json"),
        ):
            mock = patch.object(module, attribute, path)
            mock.start()
            self.addCleanup(mock.stop)
        self.now = 100.0
        self.wall = 1000.0
        self.clock_patch = patch.object(person.time, "monotonic", lambda: self.now)
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)
        person._read_windows.cache_clear()
        inference._read_leases.cache_clear()
        self.window = {
            "started": 100.0,
            "expires": 130.0,
            "wall_start": 1000.0,
            "wall_end": 1030.0,
        }
        self.camera = SimpleNamespace(
            name="front", snapshots=SimpleNamespace(native_person=True)
        )

    def grant(self):
        person.WINDOWS_PATH.write_text(json.dumps({"front": [self.window]}))
        person.LEASE_PATH.write_text(
            json.dumps({"front": {"started": 100, "expires": 130}})
        )

    def test_native_and_motion_lease_files_never_overwrite_each_other(self):
        motion = EventInferenceController(self.root / "motion", clock=lambda: self.now)
        native = EventInferenceController(
            self.root / "person", clock=lambda: self.now, lease_path=person.LEASE_PATH
        )
        motion.initialize()
        motion.configure("ring", EventInferenceConfig(enabled=True))
        motion.trigger("ring", "motion")
        before = inference.LEASE_PATH.read_bytes()
        native.initialize()
        native.configure(
            "front", EventInferenceConfig(enabled=True, triggers=["person"])
        )
        self.assertFalse(native.trigger("front", "motion"))
        self.assertTrue(native.trigger("front", "person"))
        self.assertTrue(native.status("front")["active"])
        native.revoke("front")
        self.assertFalse(native.status("front")["active"])
        self.assertEqual(inference.LEASE_PATH.read_bytes(), before)
        self.assertTrue(motion.status("ring")["active"])

    def test_both_lease_and_verified_window_are_required_with_exact_deadline(self):
        self.grant()
        self.assertTrue(person.native_person_active("front", 129.999))
        self.assertFalse(person.native_person_active("front", 130))
        self.assertFalse(person.native_person_active("ring", 110))
        person.WINDOWS_PATH.unlink()
        self.assertFalse(person.native_person_active("front", 110))

    def test_oversized_or_invalid_grants_fail_closed(self):
        self.grant()
        self.window["expires"] = 131
        person.WINDOWS_PATH.write_text(json.dumps({"front": [self.window]}))
        with self.assertLogs("frigate.motion.native_person", "ERROR"):
            self.assertFalse(person.native_person_active("front", 110))
        person.WINDOWS_PATH.write_text("x" * (129 * 1024))
        with self.assertLogs("frigate.motion.native_person", "ERROR"):
            self.assertFalse(person.native_person_active("front", 110))

    def test_only_confirmed_person_and_real_in_window_best_frame_can_save(self):
        self.grant()
        obj = SimpleNamespace(
            false_positive=False,
            obj_data={"label": "person"},
            thumbnail_data={"frame_time": 1015},
        )
        self.now = 135
        self.assertTrue(person.native_person_snapshot_allowed(self.camera, obj))
        for label, false_positive, frame, enabled in (
            ("car", False, 1015, True),
            ("person", True, 1015, True),
            ("person", False, 999, True),
            ("person", False, 1031, True),
            ("person", False, 1015, False),
        ):
            obj.obj_data["label"] = label
            obj.false_positive = false_positive
            obj.thumbnail_data["frame_time"] = frame
            self.camera.snapshots.native_person = enabled
            self.assertFalse(person.native_person_snapshot_allowed(self.camera, obj))
        self.camera.snapshots.native_person = True
        self.now = 251
        self.assertFalse(person.person_window_matches("front", 1015))

    def test_person_joining_older_vehicle_review_still_waits_for_best_shot(self):
        self.grant()
        payload = {
            "type": "update",
            "after": {"start_time": 900, "data": {"objects": ["vehicle", "person"]}},
        }
        self.assertTrue(person.defer_person_alert(self.camera, payload))
        self.now = 132
        self.assertTrue(person.person_window_finishing("front", 900))

    def test_only_new_mode_defers_person_alerts_and_never_end_or_genai(self):
        self.grant()
        payload = {
            "type": "new",
            "after": {"start_time": 1005, "data": {"objects": ["person"]}},
        }
        self.assertTrue(person.defer_person_alert(self.camera, payload))
        for kind in ("end", "genai"):
            payload["type"] = kind
            self.assertFalse(person.defer_person_alert(self.camera, payload))
        payload["type"] = "new"
        payload["after"]["data"]["objects"] = ["car"]
        self.assertFalse(person.defer_person_alert(self.camera, payload))
        payload["after"]["data"]["objects"] = ["person"]
        self.camera.snapshots.native_person = False
        self.assertFalse(person.defer_person_alert(self.camera, payload))


@unittest.skipIf(
    sys.platform == "win32", "Native camera/config integration requires Linux"
)
class TestPersonController(TestPersonWindowFiles):
    def setUp(self):
        super().setUp()
        self.camera.enabled = True
        self.camera.detect = SimpleNamespace(enabled=False)
        self.model = SimpleNamespace(
            devices=["edgetpu:pci:0", "edgetpu:pci:1"], merged_labelmap={0: "person"}
        )
        self.config = SimpleNamespace(
            cameras={"front": self.camera}, model_for_camera=lambda _name: self.model
        )
        for name in (
            "frigate.comms.reolink_api.native_api_selected",
            "frigate.motion.record_only.recording_only",
        ):
            mock = patch(name, return_value=True)
            mock.start()
            self.addCleanup(mock.stop)
        self.controller = person.NativePersonController(
            lambda: self.config,
            root=self.root / "controller",
            clock=lambda: self.now,
            wall_clock=lambda: self.wall,
        )
        self.controller.initialize()

    def start(self, identity="1000.000000-abcdef", label="person", source="reolink"):
        return self.controller.consume(
            {
                "camera": "front",
                "event_id": identity,
                "action": "start",
                "label": label,
                "source": source,
                "timestamp": self.wall,
            }
        )

    def test_refreshes_repeated_starts_and_another_episode_cannot_extend_window(self):
        self.assertTrue(self.start())
        self.now = 120
        self.wall = 1020
        self.assertTrue(self.start())
        self.assertTrue(
            self.controller.consume(
                {
                    "camera": "front",
                    "event_id": "1000.000000-abcdef",
                    "action": "refresh",
                    "timestamp": self.wall,
                }
            )
        )
        self.assertTrue(self.start("1020.000000-abcdef"))
        self.assertEqual(self.controller.history["front"][0]["expires"], 130)
        self.now = 130
        self.assertFalse(person.native_person_active("front"))
        self.assertTrue(self.start("1030.000000-abcdef"))
        self.assertEqual(self.controller.history["front"][-1]["expires"], 160)

    def test_motion_doorbell_vehicle_animal_and_unverified_sources_never_trigger(self):
        for label in ("motion", "doorbell", "vehicle", "animal"):
            self.assertFalse(self.start(label=label))
        self.assertFalse(self.start(source="plugin"))
        self.assertFalse(person.native_person_active("front"))

    def test_native_support_coral_model_and_recording_only_are_mandatory(self):
        for target in (
            "frigate.comms.reolink_api.native_api_selected",
            "frigate.motion.record_only.recording_only",
        ):
            with (
                patch(target, return_value=False),
                self.assertLogs("frigate.motion.native_person", "WARNING"),
            ):
                self.controller.sync()
                self.assertFalse(self.start())
        for devices, labels in (
            (["cpu"], {0: "person"}),
            (["edgetpu:pci:0", "cpu"], {0: "person"}),
            ([], {0: "person"}),
            (["edgetpu:pci:0"], {0: "car"}),
        ):
            self.model.devices, self.model.merged_labelmap = devices, labels
            self.controller.sync()
            self.assertFalse(self.start())

    def test_disabling_or_removing_camera_revokes_snapshot_and_decoder_permissions(
        self,
    ):
        self.start()
        self.camera.snapshots.native_person = False
        self.controller.sync()
        self.assertFalse(person.native_person_active("front"))
        self.assertFalse(person.person_window_matches("front", 1005))
        self.camera.snapshots.native_person = True
        self.wall = 1001
        self.start("1001.000000-abcdef")
        del self.config.cameras["front"]
        self.controller.sync()
        self.assertFalse(person.native_person_active("front"))

    def test_restart_shutdown_and_invalid_identity_never_grant_capture(self):
        with self.assertRaises(ValueError):
            self.start("made-up-id")
        self.start()
        self.controller.close()
        self.assertFalse(person.native_person_active("front"))
        self.controller.initialize()
        self.assertFalse(person.native_person_active("front"))
        self.assertFalse(person.person_window_matches("front", 1005))

    def test_recording_mode_is_preserved_and_person_grants_can_wake_non_plugin_camera(
        self,
    ):
        from frigate.motion import record_only

        with patch.object(record_only, "plugin_recording_only", return_value=False):
            self.assertFalse(record_only.detection_capture_active("front"))
            self.start()
            self.assertTrue(record_only.detection_capture_active("front"))
            self.assertTrue(record_only.recording_only("front"))


class TestPersonBestShotCompletion(unittest.TestCase):
    def setUp(self):
        from frigate.review.maintainer import (
            PendingReviewSegment,
            ReviewSegmentMaintainer,
        )
        from frigate.review.types import SeverityEnum

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.camera = SimpleNamespace(
            name="front",
            snapshots=SimpleNamespace(
                native_person=True, enabled=False, required_zones=["entry"]
            ),
        )
        self.segment = PendingReviewSegment(
            "front",
            1005,
            SeverityEnum.alert,
            {"track-1": "person", "track-2": "person"},
            {},
            [],
            set(),
        )
        self.segment.frame_path = str(self.root / "review.webp")
        self.maintainer = ReviewSegmentMaintainer.__new__(ReviewSegmentMaintainer)
        self.maintainer.config = SimpleNamespace(cameras={"front": self.camera})
        self.maintainer.active_review_segments = {"front": self.segment}
        self.maintainer.indefinite_events = {}
        self.maintainer.forcibly_end_segment = MagicMock()
        self.rows = [
            SimpleNamespace(
                id="track-1",
                end_time=1030,
                has_snapshot=True,
                top_score=0.8,
                data={"snapshot": {"frame_time": 1010}},
            ),
            SimpleNamespace(
                id="track-2",
                end_time=1030,
                has_snapshot=True,
                top_score=0.95,
                data={"snapshot": {"frame_time": 1025}},
            ),
        ]
        for target, replacement in (
            ("frigate.review.maintainer.CLIPS_DIR", str(self.root)),
            ("frigate.motion.native_person.person_review_window_matches", True),
            ("frigate.motion.native_person.person_window_finishing", False),
        ):
            mock = (
                patch(target, return_value=replacement)
                if not isinstance(replacement, str)
                else patch(target, replacement)
            )
            mock.start()
            self.addCleanup(mock.stop)

    def test_existing_manual_review_remembers_person_ownership_after_history_prunes(
        self,
    ):
        self.segment.start_time = 900
        self.assertTrue(self.maintainer._native_person_review_owned(self.segment))
        with patch(
            "frigate.motion.native_person.person_review_window_matches",
            return_value=False,
        ):
            self.assertTrue(self.maintainer._native_person_review_owned(self.segment))
        self.assertFalse(self.segment.native_person_best_shot)

    def test_short_review_cutoff_does_not_close_before_person_window(self):
        from frigate.test.test_review_flow import CAMERA, ReviewFlowTestCase

        fixture = ReviewFlowTestCase()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        camera = fixture.maintainer.config.cameras[CAMERA]
        camera.snapshots.native_person = True
        camera.detect.enabled = False
        camera.review.alerts.cutoff_time = 5
        with patch(
            "frigate.motion.native_person.native_person_active", return_value=True
        ):
            fixture.feed(
                (1000, [fixture.tracked("person", "person", 1000)]), (1010, [])
            )
        self.assertIsNotNone(fixture.maintainer.active_review_segments[CAMERA])
        self.assertNotIn("end", [review["type"] for review in fixture.reviews()])

    def finish(self):
        with patch("frigate.review.maintainer.Event") as model:
            model.select.return_value.where.return_value = self.rows
            self.maintainer._finish_native_person_reviews()

    def test_completed_review_uses_real_cached_best_person_snapshot_not_last_preview(
        self,
    ):
        import cv2
        import numpy as np

        for index, row in enumerate(self.rows):
            image = np.full((360, 640, 3), 50 + index * 150, dtype=np.uint8)
            self.assertTrue(
                cv2.imwrite(str(self.root / f"front-{row.id}-clean.webp"), image)
            )
        self.finish()
        self.assertTrue(self.segment.native_person_best_shot)
        self.assertEqual(self.segment.thumb_time, 1025)
        actual = cv2.imread(self.segment.frame_path)
        self.assertEqual(actual.shape[:2], (180, 320))
        self.assertGreater(actual.mean(), 180)
        self.maintainer.forcibly_end_segment.assert_called_once_with("front")
        self.assertTrue(self.segment.get_data(True)["data"]["native_person_best_shot"])

    def test_waits_for_real_track_end_and_never_extends_decoding_to_finish(self):
        self.rows[0].end_time = None
        with patch(
            "frigate.motion.native_person.person_window_finishing", return_value=True
        ):
            self.finish()
        self.maintainer.forcibly_end_segment.assert_not_called()
        self.rows = []
        self.finish()
        self.assertFalse(self.segment.native_person_best_shot)
        self.maintainer.forcibly_end_segment.assert_called_once_with("front")

    def test_no_frame_reads_or_forced_end_when_ordinary_manual_event_still_active(self):
        self.maintainer.indefinite_events = {"front": {"other": "vehicle"}}
        with patch("frigate.review.maintainer.cv2.imread") as reader:
            self.finish()
        reader.assert_not_called()
        self.maintainer.forcibly_end_segment.assert_not_called()
        self.assertFalse(self.segment.native_person_best_shot)

    def test_snapshot_zone_and_false_positive_rules_still_apply_to_stationary_people(
        self,
    ):
        from frigate.track.object_processing import TrackedObjectProcessor

        processor = TrackedObjectProcessor.__new__(TrackedObjectProcessor)
        obj = SimpleNamespace(
            false_positive=False,
            obj_data={"position_changes": 0, "id": "person"},
            entered_zones=[],
        )
        with patch(
            "frigate.motion.native_person.native_person_snapshot_allowed",
            return_value=True,
        ):
            self.assertFalse(processor.should_save_snapshot(self.camera, obj))
            obj.entered_zones = ["entry"]
            self.assertTrue(processor.should_save_snapshot(self.camera, obj))
            obj.false_positive = True
            self.assertFalse(processor.should_save_snapshot(self.camera, obj))
        obj.false_positive = False
        with patch(
            "frigate.motion.native_person.native_person_snapshot_allowed",
            return_value=False,
        ):
            self.assertFalse(processor.should_save_snapshot(self.camera, obj))

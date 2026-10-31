"""Storage pressure must preserve protected footage and prefer ordinary footage."""

import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from peewee import SqliteDatabase

from frigate.api.event import set_retain
from frigate.config import FrigateConfig, RetainModeEnum
from frigate.models import (
    ArchiveTransfer,
    Event,
    RecordingLease,
    Recordings,
    ReviewSegment,
)
from frigate.record.cleanup import RecordingCleanup
from frigate.record.policy import delete_automatic_recording
from frigate.storage import StorageMaintainer
from frigate.test.test_recording_policy import _RecordingPolicyFixture


class TestEmergencyRecordingProtection(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.db = SqliteDatabase(":memory:")
        models = [Event, Recordings, ReviewSegment, RecordingLease, ArchiveTransfer]
        self.enterContext(self.db.bind_ctx(models))
        self.addCleanup(self.db.close)
        with (
            patch.object(Event.end_time, "null", True),
            patch.object(ReviewSegment.end_time, "null", True),
        ):
            self.db.create_tables(models)
        self.config = FrigateConfig(
            mqtt={"enabled": False},
            cameras={
                name: {
                    "ffmpeg": {
                        "inputs": [
                            {
                                "path": "rtsp://camera.invalid/test",
                                "roles": ["detect", "record"],
                            }
                        ]
                    },
                    "detect": {"height": 1080, "width": 1920, "fps": 5},
                }
                for name in ("front", "back")
            },
        )
        self.enterContext(patch("frigate.storage.CameraConfigUpdateSubscriber"))
        self.enterContext(
            patch("frigate.record.archive.storage_lock", lambda: nullcontext())
        )
        self.storage = StorageMaintainer(self.config, MagicMock())
        self.enterContext(
            patch.object(self.storage, "expected_hourly_bandwidth", return_value=1.0)
        )

    def recording(self, name, start=100, camera="front", tier="local", size=2):
        path = Path(self.directory) / f"{name}.mp4"
        path.write_bytes(b"x" * int(size * (2**20)))
        return Recordings.create(
            id=name,
            camera=camera,
            path=str(path),
            start_time=start,
            end_time=start + 10,
            duration=10,
            segment_size=size,
            storage_tier=tier,
        )

    def event(self, name, start=100, end=110, camera="front", saved=True, clip=True):
        return Event.create(
            id=name,
            camera=camera,
            label="person",
            start_time=start,
            end_time=end,
            top_score=1,
            score=1,
            false_positive=False,
            zones=[],
            thumbnail="",
            region=[],
            box=[],
            area=0,
            retain_indefinitely=saved,
            has_clip=clip,
            plus_id="",
            model_hash="",
            detector_type="",
            model_type="",
            data={},
        )

    def assert_retained(self, recording):
        self.assertTrue(Path(recording.path).exists())
        self.assertTrue(
            Recordings.select().where(Recordings.id == recording.id).exists()
        )

    def test_never_falls_back_to_deleting_protected_footage(self):
        saved = self.recording("saved")
        self.event("saved-event")
        self.storage.reduce_storage_consumption()
        self.assert_retained(saved)
        self.assertTrue(self.storage.retention_unmet)

    def test_protection_does_not_depend_on_has_clip(self):
        saved = self.recording("saved")
        self.event("saved-event", clip=False)
        self.storage.reduce_storage_consumption()
        self.assert_retained(saved)

    def test_deletes_newer_ordinary_before_older_unsaved_event(self):
        event = self.recording("event", start=100)
        ordinary = self.recording("ordinary", start=1000)
        self.event("unsaved-event", saved=False)
        self.storage.reduce_storage_consumption()
        self.assert_retained(event)
        self.assertFalse(Path(ordinary.path).exists())

    def test_review_footage_is_last_resort_even_without_legacy_event(self):
        reviewed = self.recording("reviewed", start=100)
        ordinary = self.recording("ordinary", start=1000)
        ReviewSegment.create(
            id="review",
            camera="front",
            severity="alert",
            start_time=100,
            end_time=110,
            thumb_path="thumb.webp",
            data={},
        )
        self.storage.reduce_storage_consumption()
        self.assert_retained(reviewed)
        self.assertFalse(Path(ordinary.path).exists())

    def test_other_camera_event_does_not_protect_unrelated_recording(self):
        protected = self.recording("front-protected", start=100, size=0.25)
        unrelated = self.recording("back-ordinary", start=100, camera="back")
        self.event("front-event")
        self.storage.reduce_storage_consumption()
        self.assert_retained(protected)
        self.assertFalse(Path(unrelated.path).exists())
        self.assertFalse(self.storage.retention_unmet)

    def test_can_reclaim_unsaved_event_when_no_ordinary_footage_remains(self):
        unsaved = self.recording("unsaved")
        self.event("unsaved-event", saved=False)
        self.storage.reduce_storage_consumption()
        self.assertFalse(Path(unsaved.path).exists())
        self.assertFalse(
            Recordings.select().where(Recordings.id == unsaved.id).exists()
        )

    def test_never_uses_archive_footage_for_local_emergency_reclamation(self):
        archived = self.recording("archived", tier="archive")
        self.storage.reduce_storage_consumption()
        self.assert_retained(archived)

    def test_reclaims_actual_file_size_instead_of_rounded_legacy_metadata(self):
        first = self.recording("first")
        second = self.recording("second", start=1000)
        Recordings.update(segment_size=0.01).where(Recordings.id == first.id).execute()
        self.storage.reduce_storage_consumption()
        self.assertFalse(Path(first.path).exists())
        self.assert_retained(second)
        self.assertFalse(self.storage.retention_unmet)

    def test_deleting_disjoint_ordinary_segments_does_not_clear_saved_clip_flag(self):
        protected = self.recording("protected", start=200)
        self.recording("ordinary-before", start=100, size=0.5)
        self.recording("ordinary-after", start=1000, size=2)
        saved = self.event("saved-event", start=200, end=210)
        self.storage.reduce_storage_consumption()
        self.assert_retained(protected)
        self.assertTrue(Event.get_by_id(saved.id).has_clip)

    def test_regular_retention_preserves_saved_footage_and_preview_coverage(self):
        recording = self.recording("saved")
        self.event("saved-event", clip=False)
        cleanup = RecordingCleanup(self.config, MagicMock())
        _, kept = cleanup.expire_existing_camera_recordings(
            "main",
            1000,
            1000,
            RetainModeEnum.all,
            RetainModeEnum.all,
            self.config.cameras["front"],
            [],
        )
        self.assert_retained(recording)
        self.assertEqual(kept, [(100, 110)])

    def test_regular_retention_protects_saved_pre_capture(self):
        recording = self.recording("saved", start=90)
        self.event("saved-event", start=105, end=110, clip=False)
        self.config.cameras["front"].record.alerts.pre_capture = 5
        cleanup = RecordingCleanup(self.config, MagicMock())
        cleanup.expire_existing_camera_recordings(
            "main",
            1000,
            1000,
            RetainModeEnum.all,
            RetainModeEnum.all,
            self.config.cameras["front"],
            [],
        )
        self.assert_retained(recording)

    def test_emergency_disable_is_honored_without_an_archive(self):
        self.config.storage.enabled = False
        self.config.storage.emergency_delete = False
        self.storage.config_subscriber.check_for_updates.return_value = {}
        with (
            patch.object(self.storage, "calculate_camera_bandwidth"),
            patch.object(
                self.storage, "check_storage_needs_cleanup", return_value=True
            ),
            patch.object(self.storage, "reduce_storage_consumption") as reclaim,
        ):
            self.storage._maintain_once()
        reclaim.assert_not_called()
        self.assertTrue(self.storage.retention_unmet)


class TestRetainRequestContract(_RecordingPolicyFixture):
    """Exercise request injection and conflict responses without external services."""

    _use_file_database = True

    def setUp(self):
        super().setUp()
        app = FastAPI()
        app.frigate_config = FrigateConfig(mqtt={"enabled": False}, cameras={})
        app.post("/events/{event_id}/retain")(set_retain)
        self.client = self.enterContext(TestClient(app))

    def test_successful_retain_prevents_automatic_deletion(self):
        recording = self.recording()
        self.event(saved=False)
        response = self.client.post("/events/event/retain")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Event.get_by_id("event").retain_indefinitely)
        self.assertIsNone(delete_automatic_recording(recording.id, "emergency"))
        self.assertTrue(Path(recording.path).exists())

    def test_pending_deletion_returns_conflict_without_claiming_protection(self):
        self.recording()
        self.event(saved=False)
        Recordings.update(delete_requested=True, delete_reason="retention").execute()
        response = self.client.post("/events/event/retain")
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json()["success"])
        self.assertFalse(Event.get_by_id("event").retain_indefinitely)

    def test_unknown_event_returns_not_found(self):
        self.assertEqual(self.client.post("/events/missing/retain").status_code, 404)

    def test_other_camera_deletion_does_not_block_retaining_this_event(self):
        self.recording(camera="back")
        self.event(saved=False)
        Recordings.update(delete_requested=True, delete_reason="retention").execute()
        self.assertEqual(self.client.post("/events/event/retain").status_code, 200)
        self.assertTrue(Event.get_by_id("event").retain_indefinitely)


if __name__ == "__main__":
    unittest.main()

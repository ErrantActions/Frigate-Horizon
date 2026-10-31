"""Capture-window and protected-deletion rules using isolated SQLite and real files."""

import importlib
import sqlite3
import tempfile
import time
import unittest
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from peewee import SqliteDatabase

from frigate.models import (
    ArchiveTransfer,
    Event,
    RecordingLease,
    Recordings,
    ReviewSegment,
)
from frigate.record.archive import request_recording_deletion
from frigate.record.policy import (
    CapturePadding,
    RecordingDeletionInProgress,
    RecordingWindows,
    delete_automatic_recording,
    recording_event_windows,
    recording_is_protected,
    retain_event,
)


class TestRecordingWindows(unittest.TestCase):
    def test_merges_nested_and_touching_windows_without_losing_later_coverage(self):
        windows = RecordingWindows(
            [
                ("front", 10, 20),
                ("front", 12, 15),
                ("front", 20, 30),
                ("front", 40, 50),
            ]
        )
        self.assertTrue(windows.overlaps("front", 29, 31))
        self.assertTrue(windows.overlaps("front", 30, 30))
        self.assertFalse(windows.overlaps("front", 31, 39))
        self.assertTrue(windows.overlaps("front", 39, 41))

    def test_does_not_match_another_camera(self):
        self.assertFalse(RecordingWindows([("front", 10, 20)]).overlaps("back", 10, 20))

    def test_ongoing_windows_keep_future_segments_event_associated(self):
        self.assertTrue(
            RecordingWindows([("front", 10, None)]).overlaps("front", 100, 110)
        )

    def test_rejects_invalid_windows_and_segment_intervals(self):
        for start, end in ((20, 10), (float("nan"), 20), (10, float("inf"))):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                RecordingWindows([("front", start, end)])
        with self.assertRaises(ValueError):
            RecordingWindows([]).overlaps("front", 20, 10)


class _RecordingPolicyFixture(unittest.TestCase):
    _use_file_database = False

    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.db = SqliteDatabase(
            str(Path(self.directory) / "recordings.db")
            if self._use_file_database
            else ":memory:"
        )
        models = [Event, Recordings, ReviewSegment, RecordingLease, ArchiveTransfer]
        self.enterContext(self.db.bind_ctx(models))
        self.addCleanup(self.db.close)
        with (
            patch.object(Event.end_time, "null", True),
            patch.object(ReviewSegment.end_time, "null", True),
        ):
            self.db.create_tables(models)
        self.enterContext(
            patch("frigate.record.archive.storage_lock", lambda: nullcontext())
        )

    def recording(self, name="recording", camera="front", start=100, tier="local"):
        path = Path(self.directory) / f"{name}.mp4"
        path.write_bytes(b"recording" * 100)
        return Recordings.create(
            id=name,
            camera=camera,
            path=str(path),
            start_time=start,
            end_time=start + 10,
            duration=10,
            segment_size=999,
            storage_tier=tier,
        )

    def event(
        self, name="event", saved=True, clip=True, start=100, end=110, camera="front"
    ):
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


class TestRecordingDeletionPolicy(_RecordingPolicyFixture):
    def test_active_recording_lease_prevents_local_deletion(self):
        recording = self.recording()
        RecordingLease.create(
            id="active-reader",
            camera=recording.camera,
            start_time=recording.start_time,
            end_time=recording.end_time,
            expires=time.time() + 300,
        )
        self.assertIsNone(delete_automatic_recording(recording.id, "emergency"))
        self.assertTrue(Path(recording.path).is_file())
        self.assertFalse(Recordings.get_by_id(recording.id).delete_requested)

    def test_saved_event_with_cleared_clip_flag_still_protects_content(self):
        recording = self.recording()
        self.event(clip=False)
        self.assertIsNone(delete_automatic_recording(recording.id, "emergency"))
        self.assertTrue(Path(recording.path).exists())
        self.assertFalse(Recordings.get_by_id(recording.id).delete_requested)

    def test_pre_and_post_capture_are_protected(self):
        self.event(start=100, end=110)
        padding = CapturePadding(5, 10, 0, 0)
        self.assertTrue(recording_is_protected("front", 90, 95, padding))
        self.assertTrue(recording_is_protected("front", 120, 130, padding))
        self.assertFalse(recording_is_protected("front", 120.01, 130, padding))
        self.assertFalse(recording_is_protected("back", 100, 110, padding))

    def test_ongoing_saved_events_protect_later_segments(self):
        self.event(end=None)
        self.assertTrue(recording_is_protected("front", 1000, 1010))

    def test_reclaims_file_size_and_updates_only_overlapping_unsaved_clip_flags(self):
        recording = self.recording()
        self.event(saved=False)
        self.event("other-camera", camera="back", saved=False)
        self.event("disjoint", start=200, end=210, saved=False)
        self.assertEqual(
            delete_automatic_recording(recording.id, "emergency"), 900 / (2**20)
        )
        self.assertFalse(Path(recording.path).exists())
        self.assertFalse(
            Recordings.select().where(Recordings.id == recording.id).exists()
        )
        self.assertFalse(Event.get_by_id("event").has_clip)
        self.assertTrue(Event.get_by_id("other-camera").has_clip)
        self.assertTrue(Event.get_by_id("disjoint").has_clip)

    def test_missing_content_removes_stale_metadata_without_claiming_reclaimed_space(
        self,
    ):
        recording = self.recording()
        Path(recording.path).unlink()
        self.assertEqual(delete_automatic_recording(recording.id, "retention"), 0)
        self.assertFalse(
            Recordings.select().where(Recordings.id == recording.id).exists()
        )

    def test_does_not_delete_remote_or_explicitly_claimed_rows(self):
        recording = self.recording(tier="archive")
        self.assertIsNone(delete_automatic_recording(recording.id, "retention"))
        Recordings.update(
            storage_tier="local", delete_requested=True, delete_reason="user"
        ).execute()
        self.assertIsNone(delete_automatic_recording(recording.id, "emergency"))
        self.assertTrue(Path(recording.path).exists())

    def test_rechecks_protection_after_acquiring_the_path_lock(self):
        recording = self.recording()
        self.event(saved=False)

        @contextmanager
        def saved_before_lock():
            Event.update(retain_indefinitely=True).execute()
            yield

        with patch("frigate.record.archive.storage_lock", saved_before_lock):
            self.assertIsNone(delete_automatic_recording(recording.id, "retention"))
        self.assertTrue(Path(recording.path).exists())

    def test_permission_failure_is_logged_and_preserves_a_retryable_intent(self):
        recording = self.recording()
        with (
            patch.object(Path, "unlink", side_effect=PermissionError("denied")),
            self.assertLogs("frigate.record.policy", level="ERROR"),
        ):
            self.assertIsNone(delete_automatic_recording(recording.id, "retention"))
        self.assertTrue(Path(recording.path).exists())
        pending = Recordings.get_by_id(recording.id)
        self.assertTrue(pending.delete_requested)
        self.assertEqual(pending.delete_reason, "retention")

    def test_retain_rejects_an_in_progress_capture_window_deletion(self):
        self.recording(start=90)
        self.event(start=105, end=110, saved=False)
        Recordings.update(delete_requested=True, delete_reason="retention").execute()
        with self.assertRaises(RecordingDeletionInProgress):
            retain_event("event", CapturePadding(5, 0, 0, 0))
        self.assertFalse(Event.get_by_id("event").retain_indefinitely)

    def test_retain_commit_prevents_later_automatic_deletion(self):
        recording = self.recording()
        self.event(saved=False)
        retain_event("event")
        self.assertIsNone(delete_automatic_recording(recording.id, "emergency"))
        self.assertTrue(Path(recording.path).exists())

    def test_explicit_local_deletion_claim_blocks_a_late_retain_request(self):
        recording = self.recording()
        self.event(saved=False)
        self.assertTrue(request_recording_deletion(Path(recording.path)))
        self.assertFalse(Path(recording.path).exists())
        self.assertEqual(Recordings.get_by_id(recording.id).delete_reason, "user")
        with self.assertRaises(RecordingDeletionInProgress):
            retain_event("event")

    def test_review_classification_uses_its_own_severity_padding(self):
        ReviewSegment.create(
            id="review",
            camera="front",
            severity="detection",
            start_time=100,
            end_time=110,
            thumb_path="thumb.webp",
            data={},
        )
        windows = recording_event_windows(
            {"front": CapturePadding(10, 20, 3, 4)}, CapturePadding()
        )
        self.assertTrue(windows.overlaps("front", 96, 97))
        self.assertFalse(windows.overlaps("front", 90, 96))
        self.assertTrue(windows.overlaps("front", 114, 120))
        self.assertFalse(windows.overlaps("front", 114.01, 120))

    def test_unrelated_field_save_does_not_clear_concurrent_protection(self):
        self.event(saved=False)
        stale = Event.get_by_id("event")
        retain_event("event")
        stale.plus_id = "uploaded"
        stale.save(only=[Event.plus_id])
        self.assertTrue(Event.get_by_id("event").retain_indefinitely)


class TestRecordingDeletionMigration(unittest.TestCase):
    def test_migration_preserves_legacy_intents_without_guessing_their_reason(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute(
            "CREATE TABLE recordings (id TEXT PRIMARY KEY, delete_requested INTEGER)"
        )
        db.execute("INSERT INTO recordings VALUES ('legacy', 1)")
        migration = importlib.import_module(
            "migrations.041_add_recording_deletion_reason"
        )
        migrator = SimpleNamespace(sql=db.execute)
        migration.migrate(migrator, db)
        self.assertEqual(
            db.execute(
                "SELECT delete_requested, delete_reason FROM recordings"
            ).fetchone(),
            (1, None),
        )
        migration.rollback(migrator, db)
        self.assertEqual(
            db.execute("SELECT delete_requested FROM recordings").fetchone(), (1,)
        )


if __name__ == "__main__":
    unittest.main()

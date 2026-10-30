"""Archive lifecycle tests using real files and an isolated SQLite database."""

import importlib
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from peewee import SqliteDatabase

from frigate.models import (
    ArchiveTransfer,
    Event,
    RecordingLease,
    Recordings,
    ReviewSegment,
)
from frigate.record.archive import copy_verified, publish_archive_link
from frigate.record.archive_store import (
    PreparedArchiveMove,
    archive_retention_days,
    expire_recording,
    move_recording,
    publish_recording_copies,
    recover_archive_links,
)


class TestArchiveStore(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=".")
        self.addCleanup(self.directory.cleanup)
        self.local = Path(self.directory.name).resolve() / "recordings"
        self.archive = Path(self.directory.name).resolve() / "archive"
        self.local.mkdir()
        self.archive.mkdir()
        self.db = SqliteDatabase(":memory:")
        self.db.bind(
            [Recordings, Event, ReviewSegment, RecordingLease, ArchiveTransfer]
        )
        self.db.create_tables(
            [Recordings, Event, ReviewSegment, RecordingLease, ArchiveTransfer]
        )
        self.addCleanup(self.db.close)
        alerts = SimpleNamespace(
            pre_capture=5,
            post_capture=10,
            retain=SimpleNamespace(days=365, mode="active_objects"),
        )
        detections = SimpleNamespace(
            pre_capture=3,
            post_capture=4,
            retain=SimpleNamespace(days=120, mode="motion"),
        )
        self.record = SimpleNamespace(
            continuous=SimpleNamespace(days=7),
            motion=SimpleNamespace(days=14),
            alerts=alerts,
            detections=detections,
            sub=SimpleNamespace(
                continuous=SimpleNamespace(days=180),
                motion=SimpleNamespace(days=200),
                alerts=SimpleNamespace(days=400, mode="all"),
                detections=SimpleNamespace(days=240, mode="active_objects"),
            ),
            event_pre_capture=5,
            get_review_pre_capture=lambda severity: (
                alerts.pre_capture if severity == "alert" else detections.pre_capture
            ),
            get_review_post_capture=lambda severity: (
                alerts.post_capture if severity == "alert" else detections.post_capture
            ),
        )
        self.camera = SimpleNamespace(record=self.record)
        for target, value in (
            ("frigate.record.archive.RECORD_DIR", str(self.local)),
            ("frigate.record.archive_store.RECORD_DIR", str(self.local)),
            ("frigate.record.archive_store.storage_lock", lambda: nullcontext()),
            ("frigate.record.leases.storage_lock", lambda: nullcontext()),
            ("frigate.record.archive.storage_lock", lambda: nullcontext()),
            ("frigate.record.archive_store.fsync_directory", lambda path: None),
            ("frigate.record.archive.fsync_directory", lambda path: None),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def recording(self, age_days=100, **overrides):
        source = self.local / "2026-01-01" / "00" / "front" / "00.00.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"recording-content")
        end = time.time() - age_days * 86400
        return Recordings.create(
            id="recording",
            camera="front",
            path=str(source),
            start_time=end - 10,
            end_time=end,
            duration=10,
            motion=0,
            objects=0,
            dBFS=0,
            **overrides,
        )

    def archive_recording(self, *, link=True, **overrides):
        recording = self.recording(storage_tier="archive", **overrides)
        source = Path(recording.path)
        target = self.archive / source.relative_to(self.local)
        target.parent.mkdir(parents=True)
        source.replace(target)
        if link:
            try:
                source.symlink_to(target)
            except OSError as error:
                if getattr(error, "winerror", None) == 1314:
                    self.skipTest("Windows requires symbolic-link privileges")
                raise
        return recording, source, target

    def move(self, recording, cancelled=lambda: False):
        return move_recording(recording, self.archive, self.archive, 0, cancelled)

    def expire(self, recording, days=90):
        return expire_recording(
            recording, self.archive, self.archive, days, time.time()
        )

    def review(self, recording, severity="alert", **overrides):
        fields = {
            "start_time": recording.start_time,
            "end_time": recording.end_time,
        }
        fields.update(overrides)
        return ReviewSegment.create(
            id="review",
            camera=recording.camera,
            severity=severity,
            thumb_path="thumb.webp",
            data={},
            **fields,
        )

    def saved_event(self, recording):
        return Event.create(
            id="saved",
            camera=recording.camera,
            label="person",
            start_time=recording.start_time,
            end_time=recording.end_time,
            top_score=1,
            score=1,
            false_positive=False,
            zones=[],
            thumbnail="",
            region=[],
            box=[],
            area=0,
            retain_indefinitely=True,
            plus_id="",
            model_hash="",
            detector_type="",
            model_type="",
            data={},
        )

    def test_failed_copy_keeps_local_recording_and_metadata(self):
        recording = self.recording()
        with patch(
            "frigate.record.archive_store.copy_verified",
            side_effect=OSError("full"),
        ):
            with self.assertRaises(OSError):
                self.move(recording)
        self.assertTrue(Path(recording.path).is_file())
        self.assertEqual(Recordings.get_by_id(recording.id).storage_tier, "local")

    def test_publish_happens_after_verification_and_before_database_update(self):
        recording = self.recording()

        def publish(source, target):
            self.assertEqual(source.read_bytes(), target.read_bytes())
            self.assertEqual(Recordings.get_by_id(recording.id).storage_tier, "local")

        with patch(
            "frigate.record.archive_store.publish_archive_link",
            side_effect=publish,
        ) as publication:
            self.assertTrue(self.move(recording))
        publication.assert_called_once()
        self.assertEqual(Recordings.get_by_id(recording.id).storage_tier, "archive")
        self.assertEqual(Recordings.get_by_id(recording.id).archive_bytes, 17)

    def test_failed_publication_keeps_source_and_local_metadata(self):
        recording = self.recording()
        with patch(
            "frigate.record.archive_store.publish_archive_link",
            side_effect=OSError("cannot create link"),
        ):
            with self.assertRaises(OSError):
                self.move(recording)
        self.assertEqual(Path(recording.path).read_bytes(), b"recording-content")
        self.assertEqual(Recordings.get_by_id(recording.id).storage_tier, "local")

    def test_cleanup_during_copy_does_not_publish_an_orphan(self):
        recording = self.recording()

        def copy_then_expire(source, destination, rate, cancelled):
            copy_verified(source, destination, rate, cancelled)
            source.unlink()
            Recordings.delete().where(Recordings.id == recording.id).execute()

        with patch(
            "frigate.record.archive_store.copy_verified",
            side_effect=copy_then_expire,
        ):
            self.assertFalse(self.move(recording))
        self.assertEqual(list(self.archive.rglob("*.mp4")), [])

    def test_cancel_after_copy_preserves_local_recording(self):
        recording = self.recording()
        cancelled = False

        def copy_then_cancel(source, destination, rate, check):
            nonlocal cancelled
            copy_verified(source, destination, rate, check)
            cancelled = True

        with patch(
            "frigate.record.archive_store.copy_verified",
            side_effect=copy_then_cancel,
        ):
            self.assertFalse(self.move(recording, lambda: cancelled))
        self.assertEqual(Path(recording.path).read_bytes(), b"recording-content")
        self.assertEqual(list(self.archive.rglob("*.mp4")), [])

    def test_changed_source_is_not_replaced(self):
        recording = self.recording()

        def copy_then_change(source, destination, rate, cancelled):
            copy_verified(source, destination, rate, cancelled)
            source.write_bytes(b"changed-content-with-a-different-size")

        with patch(
            "frigate.record.archive_store.copy_verified",
            side_effect=copy_then_change,
        ):
            with self.assertRaises(OSError):
                self.move(recording)
        self.assertEqual(
            Path(recording.path).read_bytes(), b"changed-content-with-a-different-size"
        )
        self.assertEqual(Recordings.get_by_id(recording.id).storage_tier, "local")

    def test_transfer_retains_stable_playback_path(self):
        recording = self.recording()
        try:
            self.assertTrue(self.move(recording))
        except OSError as error:
            if getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows requires symbolic-link privileges")
            raise
        saved = Recordings.get_by_id(recording.id)
        self.assertEqual(saved.path, recording.path)
        self.assertEqual(saved.storage_tier, "archive")
        self.assertTrue(Path(saved.path).is_symlink())
        self.assertEqual(Path(saved.path).read_bytes(), b"recording-content")

    def test_durable_file_publication_does_not_hold_sqlite_writer_transaction(self):
        recording = self.recording()

        def publish_without_writer_lock(source, destination):
            self.assertFalse(self.db.in_transaction())
            return publish_archive_link(source, destination)

        with patch(
            "frigate.record.archive_store.publish_archive_link",
            side_effect=publish_without_writer_lock,
        ):
            self.assertTrue(self.move(recording))
        self.assertEqual(Recordings.get_by_id(recording.id).storage_tier, "archive")

    def prepare_publication_batch(self, count=40):
        prepared = []
        for index in range(count):
            source = self.local / "2026-01-01" / "00" / "front" / f"00.{index:02}.mp4"
            destination = self.archive / source.relative_to(self.local)
            source.parent.mkdir(parents=True, exist_ok=True)
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"verified-content")
            destination.write_bytes(source.read_bytes())
            recording = Recordings.create(
                id=f"batch-{index}",
                camera="front",
                path=str(source),
                start_time=index * 10,
                end_time=index * 10 + 10,
                duration=10,
                segment_size=1,
            )
            prepared.append(
                PreparedArchiveMove(
                    recording, source, destination, destination, source.stat()
                )
            )
        return prepared

    def test_publication_metadata_is_bounded_and_reports_progress_outside_writer_lock(
        self,
    ):
        prepared = self.prepare_publication_batch()
        statements = []
        self.db.connection().set_trace_callback(statements.append)

        def progress():
            self.assertFalse(self.db.in_transaction())

        moved = publish_recording_copies(
            prepared,
            lambda: False,
            on_progress=progress,
        )
        self.assertEqual(len(moved), 40)
        self.assertEqual(sum(sql == "BEGIN IMMEDIATE" for sql in statements), 2)
        self.assertEqual(
            Recordings.select().where(Recordings.storage_tier == "archive").count(), 40
        )

    def test_later_metadata_group_failure_leaves_all_verified_links_recoverable(self):
        prepared = self.prepare_publication_batch()
        count = 0

        def metadata(_row):
            nonlocal count
            count += 1
            if count == 30:
                raise OSError("Injected metadata callback failure")

        with self.assertRaisesRegex(OSError, "Injected metadata"):
            publish_recording_copies(prepared, lambda: False, on_commit=metadata)
        self.assertEqual(
            Recordings.select().where(Recordings.storage_tier == "archive").count(), 25
        )
        self.assertTrue(
            all(item.source.read_bytes() == b"verified-content" for item in prepared)
        )
        recover_archive_links(self.archive)
        self.assertEqual(
            Recordings.select().where(Recordings.storage_tier == "archive").count(), 40
        )

    def test_recovery_repairs_link_published_before_database_commit(self):
        recording, source, target = self.archive_recording()
        Recordings.update(storage_tier="local").execute()
        recover_archive_links(self.archive)
        self.assertEqual(Recordings.get_by_id(recording.id).storage_tier, "archive")
        self.assertEqual(source.read_bytes(), target.read_bytes())

    def test_startup_recovery_reports_progress_without_skipping_repairs(self):
        recording, source, target = self.archive_recording()
        Recordings.update(storage_tier="local").execute()
        progress = Mock()
        with patch(
            "frigate.record.archive_store.time.monotonic", side_effect=[0, 6, 6]
        ):
            recover_archive_links(self.archive, progress=progress)
        self.assertEqual(progress.call_count, 2)
        self.assertEqual(Recordings.get_by_id(recording.id).storage_tier, "archive")
        self.assertEqual(source.read_bytes(), target.read_bytes())

    def test_expiry_removes_archive_and_database_without_a_local_anchor(self):
        recording, source, target = self.archive_recording(link=False)
        self.assertTrue(self.expire(recording))
        self.assertFalse(os.path.lexists(source))
        self.assertFalse(target.exists())
        self.assertFalse(
            Recordings.select().where(Recordings.id == recording.id).exists()
        )

    def test_expiry_removes_both_tiers_and_database_row(self):
        recording, source, target = self.archive_recording()
        self.assertTrue(self.expire(recording))
        self.assertFalse(os.path.lexists(source))
        self.assertFalse(target.exists())
        self.assertFalse(
            Recordings.select().where(Recordings.id == recording.id).exists()
        )

    def test_failed_archive_deletion_is_persisted_and_retried(self):
        recording, source, target = self.archive_recording(link=False)
        original_unlink = Path.unlink

        def fail_archive(path, *args, **kwargs):
            if path == target:
                raise OSError("NFS disconnected")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", fail_archive):
            with self.assertRaises(OSError):
                self.expire(recording)
        saved = Recordings.get_by_id(recording.id)
        self.assertTrue(saved.delete_requested)
        self.assertTrue(target.exists())
        # A later retention increase cannot strand an already-started deletion.
        self.assertTrue(self.expire(saved, days=365))
        self.assertFalse(target.exists())

    def test_plain_local_file_blocks_archive_expiration(self):
        recording = self.recording(storage_tier="archive")
        with self.assertRaises(OSError):
            self.expire(recording)
        self.assertEqual(Path(recording.path).read_bytes(), b"recording-content")
        self.assertTrue(
            Recordings.select().where(Recordings.id == recording.id).exists()
        )

    def test_retention_uses_capture_age_not_archive_modification_time(self):
        recording, source, target = self.archive_recording(link=False, age_days=100)
        self.assertGreater(target.stat().st_mtime, time.time() - 60)
        self.assertTrue(self.expire(recording))

    def test_recent_recording_is_not_expired_at_retention_boundary(self):
        recording = self.recording(age_days=90)
        self.assertFalse(
            expire_recording(
                recording,
                self.archive,
                self.archive,
                90,
                recording.end_time + 90 * 86400,
            )
        )
        self.assertTrue(
            Recordings.select().where(Recordings.id == recording.id).exists()
        )

    def test_matching_review_retention_does_not_protect_unrelated_footage(self):
        recording = self.recording()
        self.assertEqual(archive_retention_days(recording, self.camera, 90), 90)
        self.review(recording)
        self.assertEqual(archive_retention_days(recording, self.camera, 90), 90)
        recording.objects = 1
        self.assertEqual(archive_retention_days(recording, self.camera, 90), 365)

    def test_sub_stream_retention_does_not_extend_main_stream(self):
        recording = self.recording()
        self.assertEqual(archive_retention_days(recording, self.camera, 90), 90)
        recording.stream_type = "sub"
        self.assertEqual(archive_retention_days(recording, self.camera, 90), 180)
        recording.motion = 1
        self.assertEqual(archive_retention_days(recording, self.camera, 90), 200)

    def test_sub_stream_alert_mode_and_days_are_independent(self):
        recording = self.recording(stream_type="sub")
        self.review(recording)
        self.assertEqual(archive_retention_days(recording, self.camera, 90), 400)

    def test_review_pre_capture_is_included_at_exact_boundary(self):
        recording = self.recording()
        recording.objects = 1
        review = self.review(
            recording,
            start_time=recording.end_time + self.record.alerts.pre_capture,
            end_time=recording.end_time + 20,
        )
        self.assertEqual(archive_retention_days(recording, self.camera, 90), 365)
        ReviewSegment.update(start_time=review.start_time + 0.01).execute()
        self.assertEqual(archive_retention_days(recording, self.camera, 90), 90)

    def test_review_post_capture_is_included_at_exact_boundary(self):
        recording = self.recording()
        recording.objects = 1
        review = self.review(
            recording,
            start_time=recording.start_time - 20,
            end_time=recording.start_time - self.record.alerts.post_capture,
        )
        self.assertEqual(archive_retention_days(recording, self.camera, 90), 365)
        ReviewSegment.update(end_time=review.end_time - 0.01).execute()
        self.assertEqual(archive_retention_days(recording, self.camera, 90), 90)

    def test_saved_event_prevents_normal_expiry(self):
        recording, source, target = self.archive_recording(link=False)
        self.saved_event(recording)
        for has_clip in (True, False):
            with self.subTest(has_clip=has_clip):
                Event.update(has_clip=has_clip).execute()
                self.assertFalse(self.expire(recording))
                self.assertTrue(target.exists())
                self.assertTrue(
                    Recordings.select().where(Recordings.id == recording.id).exists()
                )

    def test_retention_intent_does_not_bypass_saved_protection(self):
        recording, _, target = self.archive_recording(
            link=False, delete_requested=True, delete_reason="retention"
        )
        self.saved_event(recording)
        self.assertFalse(self.expire(recording))
        self.assertTrue(target.exists())

    def test_ambiguous_legacy_intent_does_not_bypass_saved_protection(self):
        recording, _, target = self.archive_recording(link=False, delete_requested=True)
        self.saved_event(recording)
        self.assertFalse(self.expire(recording))
        self.assertTrue(target.exists())

    def test_explicit_user_deletion_cannot_remove_saved_footage(self):
        recording, _, target = self.archive_recording(
            link=False, delete_requested=True, delete_reason="user"
        )
        self.saved_event(recording)
        self.assertFalse(self.expire(recording))
        self.assertTrue(target.exists())
        self.assertTrue(
            Recordings.select().where(Recordings.id == recording.id).exists()
        )

    def test_started_unprotected_deletion_finishes_after_retention_changes(self):
        recording, _, target = self.archive_recording(
            link=False, age_days=10, delete_requested=True, delete_reason="retention"
        )
        self.assertTrue(self.expire(recording, days=90))
        self.assertFalse(
            Recordings.select().where(Recordings.id == recording.id).exists()
        )
        self.assertFalse(target.exists())

    def test_explicit_deletion_is_processed_before_retention_age(self):
        recording, source, target = self.archive_recording(
            link=False, age_days=1, delete_requested=True
        )
        self.assertTrue(self.expire(recording))
        self.assertFalse(target.exists())
        self.assertFalse(os.path.lexists(source))
        self.assertFalse(
            Recordings.select().where(Recordings.id == recording.id).exists()
        )


class TestArchiveMigration(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.execute(
            "CREATE TABLE recordings (id TEXT PRIMARY KEY, path TEXT, "
            "start_time REAL, end_time REAL)"
        )
        self.db.execute(
            "INSERT INTO recordings VALUES (?, ?, ?, ?)",
            ("existing", "/media/frigate/recordings/existing.mp4", 1, 11),
        )
        self.migration = importlib.import_module(
            "migrations.040_add_recording_storage_tier"
        )
        self.migrator = SimpleNamespace(sql=self.db.execute)

    def test_existing_footage_defaults_to_local_without_changing_paths(self):
        self.migration.migrate(self.migrator, self.db)
        self.assertEqual(
            self.db.execute(
                "SELECT path, storage_tier, delete_requested FROM recordings"
            ).fetchone(),
            ("/media/frigate/recordings/existing.mp4", "local", 0),
        )

    def test_new_rows_can_omit_archive_fields(self):
        self.migration.migrate(self.migrator, self.db)
        self.db.execute(
            "INSERT INTO recordings (id, path, start_time, end_time) VALUES (?, ?, ?, ?)",
            ("new", "new.mp4", 12, 22),
        )
        self.assertEqual(
            self.db.execute(
                "SELECT storage_tier, delete_requested FROM recordings WHERE id = 'new'"
            ).fetchone(),
            ("local", 0),
        )

    def test_oldest_first_lookup_uses_index_without_temporary_sort(self):
        self.migration.migrate(self.migrator, self.db)
        plan = self.db.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM recordings "
            "WHERE storage_tier = 'local' ORDER BY start_time LIMIT 1"
        ).fetchall()
        details = " ".join(row[3] for row in plan)
        self.assertIn("recordings_tier_start", details)
        self.assertNotIn("TEMP B-TREE", details)

    def test_expiration_lookup_uses_index_without_temporary_sort(self):
        self.migration.migrate(self.migrator, self.db)
        plan = self.db.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM recordings "
            "WHERE storage_tier = 'archive' AND delete_requested = 0 "
            "AND end_time < 100 ORDER BY end_time, id LIMIT 1000"
        ).fetchall()
        details = " ".join(row[3] for row in plan)
        self.assertIn("recordings_tier_expiration", details)
        self.assertNotIn("TEMP B-TREE", details)

    def test_schema_rollback_preserves_existing_recordings(self):
        self.migration.migrate(self.migrator, self.db)
        self.migration.rollback(self.migrator, self.db)
        columns = [row[1] for row in self.db.execute("PRAGMA table_info(recordings)")]
        self.assertEqual(columns, ["id", "path", "start_time", "end_time"])
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM recordings").fetchone()[0], 1
        )


if __name__ == "__main__":
    unittest.main()

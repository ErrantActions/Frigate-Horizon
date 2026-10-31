"""Mirror lifecycle unit tests with real original bytes and local SQLite state."""

import ast
import importlib
import os
import shutil
import tempfile
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import mock_open, patch

from peewee import SqliteDatabase, fn
from pydantic import ValidationError

from frigate.models import (
    ArchiveTransfer,
    Event,
    RecordingLease,
    Recordings,
    ReviewSegment,
)
from frigate.record.archive import request_recording_deletion
from frigate.record.archive_limits import archive_owned_bytes, recover_archive_transfers
from frigate.record.archive_store import (
    archive_candidates,
    audit_mirror_copy,
    copy_recording_unverified,
    expire_recording,
    move_recording,
    publish_recording_copies,
    verify_recording_copy,
)
from frigate.record.local_mirror import (
    cleanup_local_mirrors,
    release_local_copy,
    validate_local_mirror_mount,
)
from frigate.record.policy import delete_automatic_recording
from frigate.util.storage import recording_storage_usage, storage_capacity
from generate_config_translations import load_storage_model

StorageConfig = load_storage_model()


class MirrorFixture(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory(dir="."))
        self.root = Path(self.directory).resolve()
        self.local, self.remote = self.root / "recordings", self.root / "archive"
        self.local.mkdir()
        self.remote.mkdir()
        self.database = SqliteDatabase(
            str(self.root / "recordings.db"), pragmas={"synchronous": "FULL"}
        )
        self.models = [
            Recordings,
            ArchiveTransfer,
            Event,
            RecordingLease,
            ReviewSegment,
        ]
        self.enterContext(self.database.bind_ctx(self.models))
        self.database.create_tables(self.models)
        self.addCleanup(self.database.close)
        migration = importlib.import_module(
            "migrations.047_add_recording_mirror_cleanup"
        )
        migration.migrate(SimpleNamespace(sql=self.database.execute_sql), self.database)
        self.now = time.time()
        self.number = 0
        record = SimpleNamespace(
            alerts=SimpleNamespace(pre_capture=0, post_capture=0),
            detections=SimpleNamespace(pre_capture=0, post_capture=0),
        )
        self.config = SimpleNamespace(
            storage=StorageConfig(enabled=True, mode="mirror"),
            record=record,
            cameras={"front": SimpleNamespace(record=record)},
        )
        self.config.storage.archive_path = str(self.remote)
        for module in ("archive", "archive_store", "leases", "local_mirror"):
            self.enterContext(
                patch(f"frigate.record.{module}.storage_lock", lambda: nullcontext())
            )
        self.enterContext(patch("frigate.record.archive.RECORD_DIR", str(self.local)))
        self.enterContext(
            patch("frigate.record.archive_store.RECORD_DIR", str(self.local))
        )
        # Windows unit tests cannot certify Linux directory fsync. The image
        # integration suite below repeats the transport with real directory fsync.
        if os.name == "nt":
            self.enterContext(patch("frigate.record.archive.fsync_directory"))
            self.enterContext(patch("frigate.record.archive_store.fsync_directory"))
            self.enterContext(patch("frigate.record.archive_limits.fsync_directory"))

    def recording(self, age_hours=1, **overrides):
        self.number += 1
        path = self.local / "2026-10-31" / "00" / "front" / f"{self.number}.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bytes(range(256)) * 1024)
        end = self.now - age_hours * 3600
        values = dict(
            id=f"mirror-{self.number}",
            camera="front",
            path=str(path),
            start_time=end - 10,
            end_time=end,
            duration=10,
            segment_size=path.stat().st_size / 2**20,
            motion=0,
            objects=0,
            dBFS=0,
        )
        values.update(overrides)
        return Recordings.create(**values)

    def mirror(self, row, cancelled=lambda: False):
        return move_recording(
            row, self.remote, self.remote, 0, cancelled, preserve_local=True
        )

    def release(self, row):
        try:
            return release_local_copy(row.id, self.remote)
        except OSError as error:
            if getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows symbolic-link privileges unavailable")
            raise

    def event(self, row, saved=True):
        return Event.create(
            id=f"event-{row.id}",
            camera=row.camera,
            label="person",
            start_time=row.start_time,
            end_time=row.end_time,
            retain_indefinitely=saved,
            has_clip=True,
            top_score=1,
            score=1,
            false_positive=False,
            zones=[],
            thumbnail="",
            region=[],
            box=[],
            area=0,
            plus_id="",
            model_hash="",
            detector_type="",
            model_type="",
            data={},
        )


class TestRecordingMirror(MirrorFixture):
    def test_actual_local_sampler_evaluates_numeric_usage_in_move_and_mirror_modes(
        self,
    ):
        # Execute the unchanged method source without importing Linux process APIs.
        # This tests real SQLite aggregates and local filesystem sampling, not a
        # replacement resource module or a simulated query implementation.
        path = Path(__file__).parents[1] / "record" / "archive_manager.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        process = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "ArchiveProcess"
        )
        method = next(
            node
            for node in process.body
            if isinstance(node, ast.FunctionDef) and node.name == "sample_local"
        )
        namespace = {
            "Recordings": Recordings,
            "fn": fn,
            "time": time,
            "shutil": shutil,
            "Path": Path,
            "RECORD_DIR": str(self.local),
            "storage_capacity": storage_capacity,
        }
        exec(
            compile(ast.Module(body=[method], type_ignores=[]), str(path), "exec"),
            namespace,
        )
        unverified, verified = self.recording(), self.recording(age_hours=2)
        self.mirror(verified)
        sampler = SimpleNamespace(config=self.config, snapshot={})
        sampler.report = lambda **values: sampler.snapshot.update(values)
        for mode, backlog in (("move", 0), ("mirror", 256 * 1024)):
            with self.subTest(mode=mode):
                self.config.storage.mode = mode
                local, _ = namespace["sample_local"](sampler)
                self.assertEqual(local, unverified.segment_size + verified.segment_size)
                self.assertEqual(sampler.mirror_backlog_bytes, backlog)
                self.assertEqual(
                    sampler.snapshot["capacity"]["local"]["usage_bytes"], 512 * 1024
                )

    def test_success_retains_identical_original_and_charges_both_copies(self):
        row = self.recording()
        before = Path(row.path).read_bytes()
        self.assertTrue(self.mirror(row))
        current = Recordings.get_by_id(row.id)
        self.assertEqual(current.storage_tier, "local")
        self.assertEqual(current.archive_bytes, len(before))
        self.assertFalse(Path(row.path).is_symlink())
        self.assertEqual(Path(row.path).read_bytes(), before)
        self.assertEqual(
            (self.remote / Path(row.path).relative_to(self.local)).read_bytes(), before
        )
        self.assertEqual(archive_owned_bytes(), len(before))
        totals, cameras = recording_storage_usage()
        for tier in ("local", "archive"):
            self.assertEqual(totals[tier]["usage_mib"], len(before) / 2**20)
            self.assertEqual(cameras["front"][tier]["recordings"], 1)

    def test_failed_readback_preserves_unverified_original(self):
        row = self.recording()
        pending = copy_recording_unverified(
            row,
            self.remote,
            self.remote,
            0,
            lambda: False,
            Path(row.path).stat().st_size,
            lambda *_: None,
            None,
        )
        pending.copy.partial.write_bytes(b"corrupt")
        with self.assertRaisesRegex(OSError, "verification failed"):
            verify_recording_copy(pending, 0, lambda: False, lambda *_: None, None)
        self.assertIsNone(Recordings.get_by_id(row.id).archive_bytes)
        self.assertTrue(Path(row.path).is_file())
        self.assertFalse(pending.move.destination.exists())
        self.assertEqual([item.id for item in archive_candidates(10)], [row.id])

    def test_cancelled_copy_keeps_original_and_no_verified_state(self):
        row = self.recording()
        self.assertFalse(self.mirror(row, lambda: True))
        self.assertIsNone(Recordings.get_by_id(row.id).archive_bytes)
        self.assertTrue(Path(row.path).is_file())
        self.assertEqual(RecordingLease.select().count(), 0)

    def test_cancelled_readback_and_publication_retain_original(self):
        row = self.recording()
        pending = copy_recording_unverified(
            row,
            self.remote,
            self.remote,
            0,
            lambda: False,
            Path(row.path).stat().st_size,
            lambda *_: None,
            None,
        )
        self.assertIsNone(
            verify_recording_copy(pending, 0, lambda: True, lambda *_: None, None)
        )
        self.assertTrue(Path(row.path).is_file())
        pending = copy_recording_unverified(
            row,
            self.remote,
            self.remote,
            0,
            lambda: False,
            Path(row.path).stat().st_size,
            lambda *_: None,
            None,
        )
        prepared = verify_recording_copy(
            pending, 0, lambda: False, lambda *_: None, None
        )
        self.assertEqual(
            publish_recording_copies([prepared], lambda: True, preserve_local=True), []
        )
        self.assertTrue(Path(row.path).is_file())
        self.assertFalse(prepared.destination.exists())
        self.assertIsNone(Recordings.get_by_id(row.id).archive_bytes)

    def test_restart_deduplicates_committed_mirror(self):
        row = self.recording()
        self.assertTrue(self.mirror(row))
        self.database.close()
        self.database.connect()
        self.assertEqual(list(archive_candidates(10)), [])
        self.assertEqual(archive_owned_bytes(), Path(row.path).stat().st_size)

    def test_committed_mirror_with_stale_reservation_recovers_without_recopy_or_local_loss(
        self,
    ):
        row = self.recording()
        self.mirror(row)
        ArchiveTransfer.create(id=row.id, path=row.path, size=256 * 1024)
        self.assertTrue(recover_archive_transfers(self.remote, self.remote))
        self.assertEqual(Recordings.get_by_id(row.id).storage_tier, "local")
        self.assertFalse(Path(row.path).is_symlink())
        self.assertEqual(list(archive_candidates(10)), [])
        self.assertEqual(archive_owned_bytes(), 256 * 1024)

    def test_interrupted_missing_mirror_audit_is_requeued_during_recovery(self):
        row = self.recording()
        self.mirror(row)
        target = self.remote / Path(row.path).relative_to(self.local)
        target.unlink()
        ArchiveTransfer.create(id=row.id, path=row.path, size=0)
        self.assertTrue(recover_archive_transfers(self.remote, self.remote))
        self.assertIsNone(Recordings.get_by_id(row.id).archive_bytes)
        self.assertTrue(Path(row.path).is_file())
        self.assertEqual([item.id for item in archive_candidates(10)], [row.id])

    def test_crash_after_readback_before_commit_recovers_and_recopies(self):
        row = self.recording()
        ArchiveTransfer.create(
            id=row.id, path=row.path, size=Path(row.path).stat().st_size
        )
        pending = copy_recording_unverified(
            row,
            self.remote,
            self.remote,
            0,
            lambda: False,
            Path(row.path).stat().st_size,
            lambda *_: None,
            None,
        )
        prepared = verify_recording_copy(
            pending, 0, lambda: False, lambda *_: None, None
        )
        self.assertTrue(prepared.destination.exists())
        self.assertTrue(recover_archive_transfers(self.remote, self.remote))
        self.assertFalse(prepared.destination.exists())
        self.assertTrue(Path(row.path).is_file())
        self.assertTrue(self.mirror(Recordings.get_by_id(row.id)))

    def test_normal_retention_never_discards_unverified_outage_backlog(self):
        row = self.recording(age_hours=24 * 500)
        self.assertIsNone(
            delete_automatic_recording(row.id, "retention", preserve_unverified=True)
        )
        cleanup_local_mirrors(self.config, self.now)
        cleanup_local_mirrors(self.config, self.now + 24 * 3600)
        self.assertTrue(Path(row.path).is_file())
        self.assertTrue(Recordings.select().where(Recordings.id == row.id).exists())

    def test_outage_recovery_catches_up_missing_and_unverified_only(self):
        known, backlog = self.recording(), self.recording(age_hours=2)
        self.assertTrue(self.mirror(known))
        with patch(
            "frigate.record.archive_store.copy_verified", side_effect=OSError("offline")
        ):
            with self.assertRaises(OSError):
                self.mirror(backlog)
        self.assertEqual([row.id for row in archive_candidates(10)], [backlog.id])
        self.assertTrue(self.mirror(backlog))
        target = self.remote / Path(known.path).relative_to(self.local)
        target.unlink()
        audit_mirror_copy(Recordings.get_by_id(known.id), self.remote)
        self.assertEqual([row.id for row in archive_candidates(10)], [known.id])
        self.assertTrue(self.mirror(Recordings.get_by_id(known.id)))
        self.assertEqual(list(archive_candidates(10)), [])

    def test_wrong_length_audit_requeues_without_discarding_original(self):
        row = self.recording()
        self.mirror(row)
        target = self.remote / Path(row.path).relative_to(self.local)
        target.write_bytes(b"truncated")
        audit_mirror_copy(Recordings.get_by_id(row.id), self.remote)
        self.assertIsNone(Recordings.get_by_id(row.id).archive_bytes)
        self.assertFalse(target.exists())
        self.assertTrue(Path(row.path).is_file())

    def test_minimum_12_hour_boundary_and_daily_24_hour_schedule(self):
        old = self.recording(age_hours=13)
        boundary = self.recording(age_hours=12)
        young = self.recording(age_hours=11)
        for row in (old, boundary, young):
            self.mirror(row)
        self.database.execute_sql(
            "INSERT INTO recordingmirrorstate(id,last_cleanup) VALUES(1,?)",
            (self.now - 24 * 3600,),
        )
        cleanup_local_mirrors(self.config, self.now - 1)
        self.assertFalse(Path(old.path).is_symlink())
        try:
            cleanup_local_mirrors(self.config, self.now)
        except OSError as error:
            if getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows symbolic-link privileges unavailable")
            raise
        self.assertTrue(Path(old.path).is_symlink())
        self.assertFalse(Path(boundary.path).is_symlink())
        self.assertFalse(Path(young.path).is_symlink())
        self.database.close()
        self.database.connect()
        cleanup_local_mirrors(self.config, self.now + 24 * 3600 - 1)
        self.assertFalse(Path(boundary.path).is_symlink())
        cleanup_local_mirrors(self.config, self.now + 24 * 3600)
        self.assertTrue(Path(boundary.path).is_symlink())
        self.assertTrue(Path(young.path).is_symlink())

    def test_daily_selection_boundary_and_restart_checkpoint_without_filesystem_publication(
        self,
    ):
        old, boundary = self.recording(age_hours=13), self.recording(age_hours=12)
        for row in (old, boundary):
            self.mirror(row)
        self.database.execute_sql(
            "INSERT INTO recordingmirrorstate(id,last_cleanup) VALUES(1,?)",
            (self.now - 24 * 3600,),
        )

        def metadata_only(identifier, *_args):
            Recordings.update(storage_tier="archive").where(
                Recordings.id == identifier
            ).execute()
            return 0

        with patch(
            "frigate.record.local_mirror.release_local_copy", side_effect=metadata_only
        ) as release:
            cleanup_local_mirrors(self.config, self.now - 1)
            release.assert_not_called()
            cleanup_local_mirrors(self.config, self.now)
            self.assertEqual(
                [call.args[0] for call in release.call_args_list], [old.id]
            )
            self.database.close()
            self.database.connect()
            cleanup_local_mirrors(self.config, self.now + 24 * 3600 - 1)
            self.assertEqual(release.call_count, 1)
            cleanup_local_mirrors(self.config, self.now + 24 * 3600)
            self.assertEqual(
                [call.args[0] for call in release.call_args_list], [old.id, boundary.id]
            )

    def test_saved_evidence_and_active_leases_preserve_both_copies(self):
        saved, leased = self.recording(age_hours=100), self.recording(age_hours=100)
        self.event(saved)
        for row in (saved, leased):
            self.mirror(row)
        RecordingLease.create(
            id="reader",
            camera=leased.camera,
            start_time=leased.start_time,
            end_time=leased.end_time,
            expires=self.now + 3600,
        )
        for row in (saved, leased):
            self.assertIsNone(self.release(row))
            self.assertIsNone(delete_automatic_recording(row.id, "emergency"))
            self.assertFalse(
                expire_recording(
                    Recordings.get_by_id(row.id), self.remote, self.remote, 0, self.now
                )
            )
            self.assertFalse(Path(row.path).is_symlink())

    def test_emergency_release_keeps_remote_catalog_even_under_12_hours(self):
        row = self.recording(age_hours=1)
        self.mirror(row)
        self.assertEqual(self.release(row), row.segment_size)
        self.assertEqual(Recordings.get_by_id(row.id).storage_tier, "archive")
        self.assertTrue(Path(row.path).is_symlink())
        self.assertEqual(Path(row.path).read_bytes(), bytes(range(256)) * 1024)
        self.assertEqual(archive_owned_bytes(), 256 * 1024)

    def test_explicit_user_deletion_queues_both_copies_without_orphaning_remote(self):
        row = self.recording()
        self.mirror(row)
        self.assertFalse(request_recording_deletion(Path(row.path), "local"))
        current = Recordings.get_by_id(row.id)
        self.assertTrue(current.delete_requested)
        self.assertEqual(current.delete_reason, "user")
        self.assertTrue(Path(row.path).is_file())
        self.assertTrue(
            expire_recording(current, self.remote, self.remote, 90, self.now)
        )
        self.assertFalse(Path(row.path).exists())
        self.assertFalse(
            (self.remote / Path(row.path).relative_to(self.local)).exists()
        )
        self.assertIsNone(Recordings.get_or_none(Recordings.id == row.id))

    def test_normal_mirrored_retention_is_worker_owned(self):
        row = self.recording(age_hours=100)
        self.mirror(row)
        self.assertIsNone(delete_automatic_recording(row.id, "retention"))
        self.assertTrue(
            expire_recording(
                Recordings.get_by_id(row.id), self.remote, self.remote, 1, self.now
            )
        )
        self.assertFalse(Path(row.path).exists())
        self.assertEqual(archive_owned_bytes(), 0)

    def test_transfer_reservation_prevents_emergency_loss(self):
        row = self.recording(age_hours=100)
        ArchiveTransfer.create(id=row.id, path=row.path, size=256 * 1024)
        self.assertIsNone(delete_automatic_recording(row.id, "emergency"))
        self.assertTrue(Path(row.path).is_file())

    def test_emergency_can_discard_unverified_unsaved_footage_but_not_saved_evidence(
        self,
    ):
        ordinary, saved = self.recording(age_hours=100), self.recording(age_hours=200)
        event = self.event(ordinary, saved=False)
        self.event(saved)
        self.assertEqual(
            delete_automatic_recording(ordinary.id, "emergency"), ordinary.segment_size
        )
        self.assertFalse(Path(ordinary.path).exists())
        self.assertFalse(Event.get_by_id(event.id).has_clip)
        self.assertIsNone(delete_automatic_recording(saved.id, "emergency"))
        self.assertTrue(Path(saved.path).is_file())

    def test_default_move_still_releases_original_only_after_verification(self):
        row = self.recording()
        try:
            self.assertTrue(
                move_recording(row, self.remote, self.remote, 0, lambda: False)
            )
        except OSError as error:
            if getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows symbolic-link privileges unavailable")
            raise
        self.assertEqual(Recordings.get_by_id(row.id).storage_tier, "archive")
        self.assertTrue(Path(row.path).is_symlink())

    def test_migration_never_reclassifies_historical_nfs_rows(self):
        row = self.recording()
        migration = importlib.import_module(
            "migrations.047_add_recording_mirror_cleanup"
        )
        migration.rollback(
            SimpleNamespace(sql=self.database.execute_sql), self.database
        )
        migration.migrate(SimpleNamespace(sql=self.database.execute_sql), self.database)
        current = Recordings.get_by_id(row.id)
        self.assertEqual(current.storage_tier, "local")
        self.assertIsNone(current.archive_bytes)

    def test_config_default_and_original_quality_constraints(self):
        self.assertEqual(StorageConfig().mode, "move")
        for values in (
            {"local_copy_hours": 11.99},
            {"retain_days": 0.49},
            {"max_age_days": 0.49},
            {"compression": {"enabled": True}},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                StorageConfig(mode="mirror", **values)

    def test_local_mount_qualification_never_probes_nfs(self):
        for kind, accepted in (
            ("zfs", True),
            ("nfs4", False),
            ("overlay", False),
            ("tmpfs", False),
        ):
            mountinfo = f"1 0 0:1 / /media/frigate/recordings rw - {kind} source rw\n"
            with (
                self.subTest(kind=kind),
                patch("builtins.open", mock_open(read_data=mountinfo)),
                patch("frigate.const.RECORD_DIR", "/media/frigate/recordings"),
                patch.object(
                    Path, "stat", side_effect=AssertionError("must not probe files")
                ),
            ):
                if accepted:
                    validate_local_mirror_mount()
                else:
                    with self.assertRaises(OSError):
                        validate_local_mirror_mount()

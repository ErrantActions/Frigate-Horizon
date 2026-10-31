"""Actual Linux transport/fsync and emergency reclamation integration coverage."""

import os
import shutil
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from filelock import FileLock

from frigate.models import ArchiveTransfer, Recordings
from frigate.record.archive import ArchiveDrain
from frigate.record.archive_concurrency import ArchiveConcurrency
from frigate.record.archive_store import audit_mirror_copy
from frigate.record.local_mirror import release_local_copy
from frigate.test.test_recording_mirror import MirrorFixture

if os.name != "nt":
    from frigate.api.config_util import archive_location_locked
    from frigate.record.archive_manager import ArchiveProcess
    from frigate.storage import StorageMaintainer


@unittest.skipIf(
    os.name == "nt", "Requires actual Linux resource/process and directory fsync APIs"
)
class TestMirrorLinux(MirrorFixture):
    def worker(self):
        worker = object.__new__(ArchiveProcess)
        worker.config = self.config
        worker.config.storage.parallel_transfers = 2
        worker.config_revision = SimpleNamespace(value=1)
        worker.applied_revision = SimpleNamespace(value=1)
        worker.stop_event = threading.Event()
        worker.snapshot = {}
        worker.ingest_rate = None
        worker.local_usage_mib = sum(row.segment_size for row in Recordings.select())
        worker.mirror_backlog_bytes = int(worker.local_usage_mib * 2**20)
        worker.mirror_audit_cursor = None
        worker.batch = ArchiveDrain()
        worker.batch.start(
            worker.mirror_backlog_bytes, worker.mirror_backlog_bytes, 2, "mirror"
        )
        worker.adaptive = ArchiveConcurrency()
        worker.current_transfer = None
        worker.transfer_cancelled = lambda *_: False
        worker.ensure_archive_room = lambda *_: None
        worker.report = lambda **values: worker.snapshot.update(values)
        self.enterContext(
            patch(
                "frigate.record.archive_manager.pin_archive",
                return_value=nullcontext(self.remote),
            )
        )
        self.enterContext(
            patch(
                "frigate.record.archive_manager.storage_lock",
                lambda: FileLock(str(self.root / "storage.lock"), timeout=1),
            )
        )
        self.enterContext(
            patch("frigate.record.archive_manager.RECORD_DIR", str(self.local))
        )
        return worker

    def test_real_parallel_copy_fsync_readback_keeps_originals_and_deduplicates(self):
        rows = [self.recording(), self.recording(age_hours=2)]
        worker = self.worker()
        before = worker.local_usage_mib
        moved = worker.move_batch(rows, self.remote)
        self.assertEqual({row.id for row in moved}, {row.id for row in rows})
        self.assertEqual(worker.local_usage_mib, before)
        self.assertEqual(worker.mirror_backlog_bytes, 0)
        self.assertEqual(ArchiveTransfer.select().count(), 0)
        self.assertEqual(worker.batch.verified_files, 2)
        sampled_local, _ = worker.sample_local()
        self.assertEqual(sampled_local, before)
        self.assertEqual(worker.mirror_backlog_bytes, 0)
        for row in rows:
            self.assertFalse(Path(row.path).is_symlink())
            self.assertEqual(Recordings.get_by_id(row.id).storage_tier, "local")
            self.assertEqual(
                (self.remote / Path(row.path).relative_to(self.local)).read_bytes(),
                Path(row.path).read_bytes(),
            )
        self.assertEqual(
            worker.move_batch(
                [Recordings.get_by_id(row.id) for row in rows], self.remote
            ),
            [],
        )

    def test_failed_worker_verification_retains_reservation_and_original_for_recovery(
        self,
    ):
        row = self.recording()
        worker = self.worker()
        with patch(
            "frigate.record.archive_store.verify_archive_copy",
            side_effect=OSError("readback failed"),
        ):
            with self.assertRaises(OSError):
                worker.move_batch([row], self.remote)
        self.assertTrue(Path(row.path).is_file())
        self.assertIsNone(Recordings.get_by_id(row.id).archive_bytes)
        self.assertEqual(worker.batch.verified_files, 0)
        self.assertEqual(ArchiveTransfer.select().count(), 1)

    def test_hung_remote_audit_cannot_lock_out_local_emergency_reclamation(self):
        stalled, redundant = self.recording(age_hours=20), self.recording(age_hours=1)
        for row in (stalled, redundant):
            self.mirror(row)
        for module in ("archive_store", "local_mirror"):
            self.enterContext(
                patch(
                    f"frigate.record.{module}.storage_lock",
                    lambda: FileLock(str(self.root / "storage.lock"), timeout=1),
                )
            )
        entered, resume = threading.Event(), threading.Event()
        destination = self.remote / Path(stalled.path).relative_to(self.local)
        actual_stat = Path.stat
        failures = []

        def blocked_stat(path, *args, **kwargs):
            if path == destination:
                entered.set()
                if not resume.wait(5):
                    raise TimeoutError("test audit timed out")
            return actual_stat(path, *args, **kwargs)

        def audit():
            try:
                with self.database.connection_context():
                    audit_mirror_copy(Recordings.get_by_id(stalled.id), self.remote)
            except Exception as error:
                failures.append(error)

        with patch.object(Path, "stat", blocked_stat):
            thread = threading.Thread(target=audit)
            thread.start()
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(
                    release_local_copy(redundant.id, self.remote),
                    redundant.segment_size,
                )
                self.assertTrue(Path(stalled.path).is_file())
            finally:
                resume.set()
                thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])

    def test_hung_reservation_statfs_does_not_hold_local_path_or_sqlite_writer_locks(
        self,
    ):
        self.enterContext(
            patch(
                "frigate.record.local_mirror.storage_lock",
                lambda: FileLock(str(self.root / "storage.lock"), timeout=1),
            )
        )
        actual_usage = shutil.disk_usage
        for batch in (False, True):
            with self.subTest(batch=batch):
                source, redundant = self.recording(), self.recording(age_hours=1)
                self.mirror(redundant)
                worker = self.worker()
                entered, resume = threading.Event(), threading.Event()
                failures, admitted = [], []

                def blocked_usage(path):
                    self.assertFalse(self.database.in_transaction())
                    if path == self.remote:
                        entered.set()
                        if not resume.wait(5):
                            raise TimeoutError("test statfs timed out")
                    return actual_usage(path)

                def reserve():
                    try:
                        with self.database.connection_context():
                            if batch:
                                admitted.extend(
                                    worker._reserve_batch(
                                        [source], self.remote, self.remote
                                    )
                                )
                            else:
                                admitted.append(
                                    worker._reserve_recording(
                                        source, self.remote, self.remote
                                    )
                                )
                    except Exception as error:
                        failures.append(error)

                with patch(
                    "frigate.record.archive_manager.shutil.disk_usage", blocked_usage
                ):
                    thread = threading.Thread(target=reserve)
                    thread.start()
                    try:
                        self.assertTrue(entered.wait(3))
                        # Capture's metadata writer and emergency path publisher
                        # remain usable while the archive statfs is blocked.
                        Recordings.update(motion=1).where(
                            Recordings.id == source.id
                        ).execute()
                        self.assertEqual(
                            release_local_copy(redundant.id, self.remote),
                            redundant.segment_size,
                        )
                    finally:
                        resume.set()
                        thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(failures, [])
                self.assertEqual(len(admitted), 1)

    def test_emergency_prefers_verified_redundancy_then_oldest_ordinary_unsaved_events_last(
        self,
    ):
        ordinary = self.recording(age_hours=100)
        event = self.recording(age_hours=200)
        redundant = self.recording(age_hours=1)
        saved = self.recording(age_hours=300)
        self.event(event, saved=False)
        self.event(saved)
        self.mirror(redundant)
        storage = object.__new__(StorageMaintainer)
        storage.config = self.config
        storage.retention_unmet = False
        storage.expected_hourly_bandwidth = lambda: redundant.segment_size
        self.enterContext(patch("frigate.storage.validate_local_mirror_mount"))
        storage.reduce_storage_consumption()
        self.assertTrue(Path(redundant.path).is_symlink())
        self.assertTrue(Path(ordinary.path).is_file())
        self.assertTrue(Path(event.path).is_file())
        storage.reduce_storage_consumption()
        self.assertIsNone(Recordings.get_or_none(Recordings.id == ordinary.id))
        self.assertTrue(Path(event.path).is_file())
        storage.reduce_storage_consumption()
        self.assertIsNone(Recordings.get_or_none(Recordings.id == event.id))
        self.assertTrue(Path(saved.path).is_file())
        storage.reduce_storage_consumption()
        self.assertTrue(storage.retention_unmet)
        self.assertTrue(Path(saved.path).is_file())

    def test_config_identity_cannot_change_with_mirrors_or_orphan_reservations(self):
        row = self.recording()
        self.mirror(row)
        previous = SimpleNamespace(
            storage=self.config.storage.model_copy(update={"enabled": False})
        )
        updated = SimpleNamespace(
            storage=previous.storage.model_copy(
                update={"archive_path": str(self.root / "new-archive")}
            )
        )
        self.assertTrue(archive_location_locked(previous, updated))
        Recordings.delete_by_id(row.id)
        ArchiveTransfer.create(id="orphan", path=str(self.local / "orphan.mp4"), size=0)
        self.assertTrue(archive_location_locked(previous, updated))

"""Real bounded concurrent file publication with isolated on-disk SQLite metadata."""

import copy
import tempfile
import threading
import time
import unittest
from contextlib import nullcontext
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
from frigate.record.archive import ArchiveDrain
from frigate.record.archive_manager import ArchiveProcess
from frigate.record.archive_store import (
    copy_recording_unverified,
    verify_recording_copy,
)


class TestParallelArchive(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.local, self.archive = root / "recordings", root / "archive"
        self.local.mkdir()
        self.archive.mkdir()
        self.database = SqliteDatabase(
            str(root / "metadata.sqlite"),
            timeout=10,
            pragmas={"journal_mode": "wal", "synchronous": "FULL"},
        )
        models = [Recordings, Event, ReviewSegment, RecordingLease, ArchiveTransfer]
        self.enterContext(self.database.bind_ctx(models))
        self.database.create_tables(models)
        self.addCleanup(self.database.close)
        self.rows = []
        for index in range(2):
            source = self.local / f"{index}.mp4"
            source.write_bytes(bytes([index + 1]) * 65536)
            self.rows.append(
                Recordings.create(
                    id=str(index),
                    camera="cam",
                    path=str(source),
                    start_time=time.time() - 200 + index * 20,
                    end_time=time.time() - 190 + index * 20,
                    duration=10,
                    segment_size=65536 / 2**20,
                )
            )
        self.worker = object.__new__(ArchiveProcess)
        self.worker.config = SimpleNamespace(
            storage=SimpleNamespace(
                mount_id="test",
                transfer_mib_per_second=0,
                max_archive_gib=None,
                archive_reserve_gib=0,
                local_budget_gib=100,
                drain_percent=70,
                parallel_transfers=2,
            )
        )
        self.worker.config_revision = SimpleNamespace(value=1)
        self.worker.stop_event = threading.Event()
        self.worker.batch = ArchiveDrain()
        self.worker.batch.start(131072, 131072, 2)
        self.worker.current_transfer = None
        self.worker.local_usage_mib = 131072 / 2**20
        self.worker.ingest_rate = None
        self.worker.transfer_cancelled = lambda: False
        self.worker.ensure_archive_room = lambda *args: None
        self.reports = []
        self.worker.report = lambda **values: self.reports.append(copy.deepcopy(values))
        self.enterContext(patch("frigate.record.archive.RECORD_DIR", str(self.local)))
        self.enterContext(
            patch("frigate.record.archive_manager.storage_lock", nullcontext)
        )
        self.enterContext(
            patch("frigate.record.archive_store.storage_lock", nullcontext)
        )
        self.enterContext(patch("frigate.record.leases.storage_lock", nullcontext))
        self.enterContext(
            patch(
                "frigate.record.archive_manager.pin_archive",
                side_effect=lambda *args: nullcontext(self.archive),
            )
        )

    def test_real_parallel_publication_is_verified_and_uses_distinct_reservations(self):
        moved = self.worker.move_batch(self.rows, self.archive)
        self.assertEqual({row.id for row in moved}, {"0", "1"})
        self.assertEqual(self.worker.batch.verified_files, 2)
        self.assertEqual(ArchiveTransfer.select().count(), 0)
        self.assertEqual(RecordingLease.select().count(), 0)
        for index, row in enumerate(self.rows):
            current = Recordings.get_by_id(row.id)
            self.assertEqual(current.storage_tier, "archive")
            self.assertTrue(Path(row.path).is_symlink())
            self.assertEqual(Path(row.path).read_bytes(), bytes([index + 1]) * 65536)
        self.assertTrue(
            any(
                report["transfer"].get("parallel_transfers") == 2
                for report in self.reports
            )
        )

    def test_both_jobs_can_run_concurrently(self):
        barrier = threading.Barrier(2)
        active = set()
        lock = threading.Lock()

        def transfer(
            row, root, pinned, limit, cancelled, size, progress, limiter, **kwargs
        ):
            with lock:
                active.add(row.id)
            barrier.wait(timeout=3)
            self.assertEqual(len(active), 2)
            return copy_recording_unverified(
                row, root, pinned, limit, cancelled, size, progress, limiter, **kwargs
            )

        with patch(
            "frigate.record.archive_manager.copy_recording_unverified", transfer
        ):
            moved = self.worker.move_batch(self.rows, self.archive)
        self.assertEqual(len(moved), 2)

    def test_reservations_are_committed_as_a_group_before_copying(self):
        statements = []
        self.database.connection().set_trace_callback(statements.append)
        checks = []

        def transfer(*args, **kwargs):
            checks.append(ArchiveTransfer.select().count())
            self.assertEqual(sum(sql == "BEGIN IMMEDIATE" for sql in statements), 1)
            return copy_recording_unverified(*args, **kwargs)

        with patch(
            "frigate.record.archive_manager.copy_recording_unverified", transfer
        ):
            self.assertEqual(len(self.worker.move_batch(self.rows, self.archive)), 2)
        self.assertEqual(checks, [2, 2])
        self.assertEqual(ArchiveTransfer.select().count(), 0)

    def test_grouped_admission_does_not_probe_nfs_under_the_sqlite_writer_lock(self):
        def capacity(_path):
            self.assertFalse(self.database.in_transaction())
            return SimpleNamespace(free=1024 * 1024)

        with patch(
            "frigate.record.archive_manager.shutil.disk_usage", side_effect=capacity
        ):
            jobs = self.worker._reserve_batch(self.rows, self.archive, self.archive)
        self.assertEqual(len(jobs), 2)
        self.assertEqual(ArchiveTransfer.select().count(), 2)
        self.assertTrue(all(Path(row.path).is_file() for row in self.rows))

    def test_grouped_admission_rechecks_maintenance_before_creating_reservations(self):
        def prepare(*_args):
            ArchiveTransfer.get_or_create(
                id="maintenance",
                defaults={
                    "path": str(self.local / "maintenance.mp4"),
                    "size": 0,
                    "kind": "maintenance",
                },
            )
            return 65536

        with patch.object(self.worker, "_prepare_reservation", side_effect=prepare):
            with self.assertRaisesRegex(OSError, "archive_maintenance_pending"):
                self.worker._reserve_batch(self.rows, self.archive, self.archive)
        self.assertEqual(
            ArchiveTransfer.select().where(ArchiveTransfer.kind == "transfer").count(),
            0,
        )

    def test_grouped_admission_honors_the_exact_archive_quota(self):
        self.worker.config.storage.max_archive_gib = 65536 / 2**30
        jobs = self.worker._reserve_batch(self.rows, self.archive, self.archive)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(ArchiveTransfer.select().count(), 1)
        self.assertEqual(ArchiveTransfer.select().first().size, 65536)

    def test_rolling_queue_publishes_more_files_than_the_worker_count(self):
        for index in range(2, 8):
            path = self.local / f"{index}.mp4"
            path.write_bytes(bytes([index + 1]) * 65536)
            self.rows.append(
                Recordings.create(
                    id=str(index),
                    camera="cam",
                    path=str(path),
                    start_time=time.time() - 200 + index * 20,
                    end_time=time.time() - 190 + index * 20,
                    duration=10,
                    segment_size=65536 / 2**20,
                )
            )
        self.worker.batch.start(8 * 65536, 8 * 65536, 8)
        self.worker.local_usage_mib = 8 * 65536 / 2**20
        barrier = threading.Barrier(2)
        lock = threading.Lock()
        active, peak = 0, 0

        def transfer(*args, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                barrier.wait(timeout=5)
                return copy_recording_unverified(*args, **kwargs)
            finally:
                with lock:
                    active -= 1

        with patch(
            "frigate.record.archive_manager.copy_recording_unverified", transfer
        ):
            moved = self.worker.move_batch(self.rows, self.archive)
        self.assertEqual(len(moved), 8)
        self.assertEqual(peak, 2)
        self.assertEqual(self.worker.batch.verified_files, 8)
        self.assertEqual(ArchiveTransfer.select().count(), 0)
        self.assertEqual(RecordingLease.select().count(), 0)
        for index, row in enumerate(self.rows):
            self.assertTrue(Path(row.path).is_symlink())
            self.assertEqual(Path(row.path).read_bytes(), bytes([index + 1]) * 65536)

    def test_failure_cancels_other_jobs_and_retains_durable_reservations(self):
        def transfer(
            row, root, pinned, limit, cancelled, size, progress, limiter, **kwargs
        ):
            if row.id == "1":
                raise OSError("synthetic copy failure")
            deadline = time.monotonic() + 3
            while not cancelled():
                if time.monotonic() >= deadline:
                    self.fail("Other worker did not observe cancellation")
                time.sleep(0.01)
            return None

        with patch(
            "frigate.record.archive_manager.copy_recording_unverified", transfer
        ):
            with self.assertRaisesRegex(OSError, "synthetic copy failure"):
                self.worker.move_batch(self.rows, self.archive)
        self.assertEqual(self.worker.batch.verified_files, 0)
        self.assertEqual(ArchiveTransfer.select().count(), 2)
        self.assertTrue(all(not Path(row.path).is_symlink() for row in self.rows))

    def test_every_copy_finishes_before_any_verification_starts(self):
        copied = set()
        lock = threading.Lock()

        def copy_file(*args, **kwargs):
            result = copy_recording_unverified(*args, **kwargs)
            with lock:
                copied.add(args[0].id)
            return result

        def verify_file(*args):
            self.assertEqual(copied, {"0", "1"})
            self.assertEqual(ArchiveTransfer.select().count(), 2)
            self.assertTrue(all(not Path(row.path).is_symlink() for row in self.rows))
            return verify_recording_copy(*args)

        with (
            patch(
                "frigate.record.archive_manager.copy_recording_unverified", copy_file
            ),
            patch("frigate.record.archive_manager.verify_recording_copy", verify_file),
        ):
            self.assertEqual(len(self.worker.move_batch(self.rows, self.archive)), 2)

    def test_single_worker_also_copies_the_batch_before_readback(self):
        self.worker.config.storage.parallel_transfers = 1
        copied = []

        def copy_file(*args, **kwargs):
            result = copy_recording_unverified(*args, **kwargs)
            copied.append(args[0].id)
            return result

        def verify_file(*args):
            self.assertEqual(copied, ["0", "1"])
            return verify_recording_copy(*args)

        with (
            patch(
                "frigate.record.archive_manager.copy_recording_unverified", copy_file
            ),
            patch("frigate.record.archive_manager.verify_recording_copy", verify_file),
        ):
            self.assertEqual(len(self.worker.move_batch(self.rows, self.archive)), 2)

    def test_corruption_after_bulk_copy_keeps_all_hdd_originals(self):
        def verify_file(item, *args):
            self.assertIsNotNone(item.copy)
            item.copy.partial.write_bytes(b"corrupt")
            return verify_recording_copy(item, *args)

        with patch("frigate.record.archive_manager.verify_recording_copy", verify_file):
            with self.assertRaisesRegex(OSError, "verification failed"):
                self.worker.move_batch(self.rows, self.archive)
        self.assertEqual(self.worker.batch.verified_files, 0)
        self.assertEqual(ArchiveTransfer.select().count(), 2)
        self.assertTrue(all(not Path(row.path).is_symlink() for row in self.rows))
        self.assertFalse(list(self.archive.glob("*.partial")))

    def test_cancel_after_copy_does_not_start_readback_or_release_hdd(self):
        def copy_file(*args, **kwargs):
            result = copy_recording_unverified(*args, **kwargs)
            self.worker.stop_event.set()
            return result

        with (
            patch(
                "frigate.record.archive_manager.copy_recording_unverified", copy_file
            ),
            patch("frigate.record.archive_manager.verify_recording_copy") as verify,
        ):
            self.assertEqual(self.worker.move_batch(self.rows, self.archive), [])
        verify.assert_not_called()
        self.assertTrue(all(not Path(row.path).is_symlink() for row in self.rows))
        self.assertFalse(list(self.archive.glob("*.partial")))

    def test_batch_admission_accounts_for_reserved_physical_capacity(self):
        with patch(
            "frigate.record.archive_manager.shutil.disk_usage",
            return_value=SimpleNamespace(free=100000),
        ):
            moved = self.worker.move_batch(self.rows, self.archive)
        self.assertEqual(len(moved), 1)
        self.assertEqual(self.worker.batch.verified_files, 1)
        self.assertEqual(ArchiveTransfer.select().count(), 0)

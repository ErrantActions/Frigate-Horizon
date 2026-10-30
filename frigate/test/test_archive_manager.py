"""Isolated archive-worker integration checks, requiring the Linux runtime imports."""

import copy
import os
import pickle
import unittest
from contextlib import nullcontext
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock, patch

from frigate.models import ArchiveTransfer, RecordingLease, Recordings
from frigate.record.archive import ArchiveDrain
from frigate.test import test_archive_store as store_fixture

if os.name != "nt":
    from frigate.record.archive_manager import ArchiveProcess


@unittest.skipIf(os.name == "nt", "Archive worker requires Linux resource/process APIs")
class TestArchiveWorker(unittest.TestCase):
    recording = store_fixture.TestArchiveStore.recording

    def setUp(self):
        store_fixture.TestArchiveStore.setUp(self)
        self.db.bind([ArchiveTransfer, RecordingLease])
        self.db.create_tables([ArchiveTransfer, RecordingLease])

    def test_expiry_yields_only_after_completed_deletions_outside_transactions(self):
        self.recording(storage_tier="archive")
        worker = self.worker()
        worker.config.storage.retain_days = 1
        worker.config.storage.max_age_days = None
        worker.config.cameras = {}
        worker.config.record = self.record
        worker.transfer_cancelled = lambda *_args: False
        worker.stop_event = Mock()

        def yield_writer(seconds):
            self.assertEqual(seconds, 0.05)
            self.assertFalse(self.db.in_transaction())
            return False

        worker.stop_event.wait.side_effect = yield_writer
        for expired in (False, True):
            worker.expiry_cursor = None
            worker.stop_event.wait.reset_mock()
            with (
                self.subTest(expired=expired),
                patch(
                    "frigate.record.archive_manager.expire_recording",
                    return_value=expired,
                ) as expire,
            ):
                worker._expire_archive(self.archive, self.archive)
                expire.assert_called_once()
                self.assertEqual(worker.stop_event.wait.call_count, int(expired))

    def test_buffer_eta_uses_start_threshold_instead_of_the_full_budget(self):
        worker = self.worker()
        worker.batch.active = False
        worker.config.storage.start_percent = 60
        worker.config.storage.drain_percent = 40
        worker.ingest_rate = 2**20
        result = worker.batch_status(50 * 1024)
        self.assertEqual(result["buffer_seconds"], 10 * 1024)
        self.assertEqual(result["drain_target_bytes"], 40 * 2**30)
        self.assertEqual(worker.batch_status(65 * 1024)["buffer_seconds"], 0)
        self.assertEqual(worker.config.storage.local_budget_gib, 100)

    def worker(self):
        worker = object.__new__(ArchiveProcess)
        worker.config = SimpleNamespace(
            storage=SimpleNamespace(
                mount_id="test",
                transfer_mib_per_second=0,
                max_archive_gib=None,
                archive_reserve_gib=0,
                local_budget_gib=100,
                start_percent=100,
                drain_percent=90,
                parallel_transfers=1,
                adaptive_transfers=False,
            )
        )
        worker.batch = ArchiveDrain()
        worker.batch.start(17, 17, 1)
        worker.current_transfer = None
        worker.snapshot = {}
        worker.ingest_rate = None
        worker.local_usage_mib = 17 / 2**20
        worker.hourly_usage_mib = 0
        worker.local_sampled_at = 0
        worker.transfer_cancelled = lambda: False
        worker.ensure_archive_room = lambda *args: None
        self.reports = []

        def report(**values):
            worker.snapshot.update(values)
            self.reports.append(copy.deepcopy(worker.snapshot))

        worker.report = report
        self.enterContext(
            patch(
                "frigate.record.archive_manager.pin_archive",
                return_value=nullcontext(self.archive),
            )
        )
        self.enterContext(
            patch("frigate.record.archive_manager.storage_lock", nullcontext)
        )
        self.enterContext(
            patch("frigate.record.archive_manager.RECORD_DIR", str(self.local))
        )
        return worker

    def test_batch_commits_only_after_readback_publication_and_database_update(self):
        recording = self.recording(segment_size=17 / 2**20)
        worker = self.worker()
        with patch("frigate.record.archive_store.publish_archive_link"):
            self.assertTrue(worker.move_recording(recording, self.archive))
        phases = [
            row["transfer"] for row in self.reports if row.get("transfer") is not None
        ]
        for phase in ("copying", "verification", "commit"):
            snapshots = [row for row in phases if row["phase"] == phase]
            self.assertTrue(snapshots, phase)
            self.assertTrue(all(row["verified_files"] == 0 for row in snapshots))
        self.assertEqual(phases[-1]["verified_files"], 1)
        self.assertEqual(phases[-1]["verified_bytes"], 17)
        self.assertEqual(Recordings.get_by_id(recording.id).storage_tier, "archive")
        self.assertEqual(ArchiveTransfer.select().count(), 0)
        self.assertEqual(RecordingLease.select().count(), 0)
        self.assertEqual(worker.batch.progress_phase, "waiting")

    def test_byte_progress_never_rescans_all_local_recordings(self):
        recording = self.recording(segment_size=17 / 2**20)
        worker = self.worker()
        with (
            patch.object(self.db, "execute_sql", wraps=self.db.execute_sql) as execute,
            patch("frigate.record.archive_store.publish_archive_link"),
        ):
            self.assertTrue(worker.move_recording(recording, self.archive))
        queries = [call.args[0].upper() for call in execute.call_args_list]
        self.assertFalse(
            any("SUM(" in sql and "SEGMENT_SIZE" in sql for sql in queries)
        )

    def test_failed_readback_keeps_reservation_source_and_zero_committed_count(self):
        recording = self.recording(segment_size=17 / 2**20)
        worker = self.worker()
        with patch(
            "frigate.record.archive_store.copy_verified",
            side_effect=OSError("verification failed"),
        ):
            with self.assertRaises(OSError):
                worker.move_recording(recording, self.archive)
        self.assertEqual(worker.batch.verified_files, 0)
        self.assertEqual(worker.batch.verified_bytes, 0)
        self.assertEqual(ArchiveTransfer.select().count(), 1)
        self.assertEqual(RecordingLease.select().count(), 0)
        self.assertTrue(Path(recording.path).is_file())
        self.assertEqual(Recordings.get_by_id(recording.id).storage_tier, "local")
        self.assertEqual(worker.batch.progress_phase, "waiting")

    def test_blocked_work_does_not_publish_stale_copy_or_readback_rates(self):
        worker = self.worker()
        worker.snapshot = {
            "transfer": {
                "phase": "copying",
                "rate_bytes_per_second": 10,
                "copy_rate_bytes_per_second": 20,
                "verification_rate_bytes_per_second": 30,
                "eta_seconds": 10,
            }
        }
        status = worker.blocked_transfer()
        self.assertEqual(status["phase"], "waiting")
        for key in (
            "rate_bytes_per_second",
            "copy_rate_bytes_per_second",
            "verification_rate_bytes_per_second",
            "eta_seconds",
        ):
            self.assertIsNone(status[key])

    def test_local_capacity_resamples_ingest_during_an_active_drain(self):
        recording = self.recording(segment_size=3)
        worker = self.worker()
        self.assertEqual(worker.sample_local()[0], 3)
        Recordings.create(
            id="ingest",
            camera=recording.camera,
            path=str(self.local / "new.mp4"),
            start_time=recording.start_time + 20,
            end_time=recording.end_time + 20,
            duration=10,
            segment_size=4,
        )
        self.assertEqual(worker.sample_local()[0], 7)
        self.assertEqual(worker.batch.planned_bytes, 17)

    def test_progress_uses_cached_usage_but_explicit_resample_sees_ingest(self):
        recording = self.recording(segment_size=3)
        worker = self.worker()
        with patch("frigate.record.archive_manager.time.monotonic", return_value=100):
            self.assertEqual(worker.sample_local(force=False)[0], 3)
            Recordings.create(
                id="new-ingest",
                camera=recording.camera,
                path=str(self.local / "new-ingest.mp4"),
                start_time=recording.start_time + 20,
                end_time=recording.end_time + 20,
                duration=10,
                segment_size=4,
            )
            with patch.object(
                self.db, "execute_sql", wraps=self.db.execute_sql
            ) as execute:
                self.assertEqual(worker.sample_local(force=False)[0], 3)
                self.assertFalse(execute.called)
            self.assertEqual(worker.sample_local()[0], 7)

    def test_quota_is_revalidated_under_lock_before_durable_reservation(self):
        recording = self.recording(segment_size=17 / 2**20)
        worker = self.worker()
        worker.config.storage.max_archive_gib = 17 / 2**30
        ArchiveTransfer.create(id="existing", path="other.mp4", size=17)
        with self.assertRaisesRegex(OSError, "archive_capacity_changed"):
            worker.move_recording(recording, self.archive)
        self.assertTrue(Path(recording.path).is_file())
        self.assertEqual(ArchiveTransfer.select().count(), 1)
        self.assertEqual(worker.batch.verified_files, 0)

    def test_live_settings_update_does_not_abandon_an_active_drain_above_low_water(
        self,
    ):
        worker = self.worker()
        worker.camera_subscriber = None
        worker.updates = Queue()
        worker.updates.put((2, pickle.dumps(worker.config)))
        worker.applied_revision = SimpleNamespace(value=1)
        worker.capacity = Mock()
        self.assertTrue(worker.apply_updates())
        self.assertTrue(worker.batch.active)
        self.assertEqual(worker.batch.planned_bytes, 17)
        self.assertEqual(worker.applied_revision.value, 2)

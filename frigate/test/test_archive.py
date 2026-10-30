"""Filesystem-level archive safety tests without cameras or a network share."""

import io
import os
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

from peewee import SqliteDatabase

from frigate.config.storage import StorageConfig
from frigate.models import ArchiveTransfer, Event, RecordingLease, Recordings
from frigate.record.archive import (
    ArchiveDirectoryBatch,
    ArchiveDrain,
    apply_archive_retention,
    archive_destination,
    copy_unverified,
    copy_verified,
    delete_local_recording,
    needs_transport,
    pin_archive,
    prepare_archive_directory,
    publish_archive_link,
    request_recording_deletion,
    validate_mount,
    verify_archive_copy,
)


class TestArchivePressure(unittest.TestCase):
    def test_separate_start_and_stop_preserve_the_local_budget(self):
        for usage, draining, expected in (
            (599, False, False),
            (600, False, True),
            (500, True, True),
            (400, True, False),
            (401, False, False),
            (1000, False, True),
        ):
            with self.subTest(usage=usage, draining=draining):
                self.assertEqual(
                    needs_transport(usage, 1000, 40, draining, 10000, 10, 60),
                    expected,
                )

    def test_pressure_overrides_the_new_stop_threshold(self):
        self.assertTrue(needs_transport(100, 1000, 40, False, 19, 10, 60))
        self.assertFalse(needs_transport(100, 1000, 40, True, 20, 10, 60))

    def test_start_and_stop_configuration_requires_a_nonempty_band(self):
        legacy = StorageConfig(drain_percent=70)
        self.assertEqual(legacy.start_percent, 100)
        requested = StorageConfig(
            local_budget_gib=3048, start_percent=60, drain_percent=40
        )
        self.assertEqual(requested.local_budget_gib, 3048)
        for start, stop in ((40, 40), (40, 60), (0, 0), (101, 40), (60, -1)):
            with self.subTest(start=start, stop=stop), self.assertRaises(ValueError):
                StorageConfig(start_percent=start, drain_percent=stop)
        self.assertEqual(
            StorageConfig(start_percent=1, drain_percent=0).drain_percent, 0
        )

    def test_new_ingest_extends_fixed_plan_until_live_low_water(self):
        batch = ArchiveDrain()
        batch.start(1000, 100, 1)
        batch.committed(100, 100)
        status = batch.status(950, 900, None)
        self.assertEqual(status["initial_plan_bytes"], 100)
        self.assertEqual(status["verified_bytes"], 100)
        self.assertEqual(status["incoming_bytes"], 50)
        self.assertEqual(status["remaining_bytes"], 50)
        self.assertTrue(needs_transport(950, 1000, 90, batch.active, 10000, 0))
        self.assertFalse(needs_transport(900, 1000, 90, batch.active, 10000, 0))
        batch.active = False
        self.assertFalse(needs_transport(950, 1000, 90, batch.active, 10000, 0))

    def test_eta_requires_completed_samples_and_outpacing_ingest(self):
        batch = ArchiveDrain()
        with patch("frigate.record.archive.time.monotonic", return_value=100):
            batch.start(1000, 100, 2)
        batch.committed(50, 50)
        with patch("frigate.record.archive.time.monotonic", return_value=102):
            self.assertIsNone(batch.status(950, 900, 5)["eta_seconds"])
            batch.committed(50, 50)
            status = batch.status(950, 900, 5)
            self.assertEqual(status["rate_bytes_per_second"], 50)
            self.assertAlmostEqual(status["eta_seconds"], 50 / 45)
            self.assertIsNone(batch.status(950, 900, None)["eta_seconds"])
            status = batch.status(950, 900, 60)
            self.assertTrue(status["cannot_outpace_ingest"])
            self.assertIsNone(status["eta_seconds"])
        batch.active = False
        self.assertIsNone(batch.status(900, 900, 5)["rate_bytes_per_second"])

    def test_budget_triggers_at_exact_limit(self):
        self.assertTrue(needs_transport(1024, 1024, 90, False, 10000, 10))
        self.assertFalse(needs_transport(1023, 1024, 90, False, 10000, 10))

    def test_copy_and_readback_rates_accumulate_short_files_without_idle_time(self):
        batch = ArchiveDrain()
        with patch("frigate.record.archive.time.monotonic") as clock:
            clock.return_value = 0
            batch.start(100_000_000, 20_000_000, 2)
            for phase, amount, timestamp in (
                ("copying", 0, 10),
                ("copying", 10_000_000, 10.25),
                ("verification", 0, 10.5),
                ("verification", 10_000_000, 11),
                ("commit", 10_000_000, 11),
            ):
                clock.return_value = timestamp
                batch.progress(phase, amount)
            status = batch.status(100_000_000, 0, None)
            self.assertIsNone(status["copy_rate_bytes_per_second"])
            self.assertIsNone(status["verification_rate_bytes_per_second"])
            for phase, amount, timestamp in (
                ("waiting", 0, 11),
                ("copying", 0, 100),
                ("copying", 10_000_000, 100.5),
                ("verification", 0, 100.75),
                ("verification", 10_000_000, 101.25),
                ("commit", 10_000_000, 101.25),
            ):
                clock.return_value = timestamp
                batch.progress(phase, amount)
            status = batch.status(100_000_000, 0, None)
            self.assertEqual(status["copy_rate_bytes_per_second"], 16_000_000)
            self.assertEqual(status["verification_rate_bytes_per_second"], 20_000_000)
            self.assertEqual(status["verified_files"], 0)
            self.assertIsNone(status["eta_seconds"])
            batch.active = False
            self.assertIsNone(batch.status(0, 0, None)["copy_rate_bytes_per_second"])
            clock.return_value = 200
            batch.start(100, 100, 1)
            self.assertIsNone(batch.status(100, 0, None)["copy_rate_bytes_per_second"])

    def test_waiting_after_a_failed_copy_does_not_reduce_its_active_copy_rate(self):
        batch = ArchiveDrain()
        with patch("frigate.record.archive.time.monotonic") as clock:
            clock.return_value = 0
            batch.start(100, 100, 1)
            batch.progress("copying", 0)
            clock.return_value = 1
            batch.progress("copying", 1_000_000)
            clock.return_value = 2
            batch.progress("waiting", 0)
            clock.return_value = 200
            status = batch.status(100, 0, None)
            self.assertEqual(status["copy_rate_bytes_per_second"], 500_000)
            self.assertIsNone(status["verification_rate_bytes_per_second"])

    def test_hysteresis_drains_to_exact_target(self):
        self.assertTrue(needs_transport(950, 1000, 90, True, 10000, 10))
        self.assertFalse(needs_transport(950, 1000, 90, False, 10000, 10))
        self.assertFalse(needs_transport(900, 1000, 90, True, 10000, 10))

    def test_free_space_overrides_budget_and_target(self):
        self.assertTrue(needs_transport(100, 1000, 90, False, 19, 10))
        self.assertFalse(needs_transport(100, 1000, 90, False, 20, 10))

    def test_no_bandwidth_sample_does_not_invent_pressure(self):
        self.assertFalse(needs_transport(100, 1000, 90, False, 0, 0))


class TestArchiveFiles(unittest.TestCase):
    def setUp(self):
        self.db = SqliteDatabase(":memory:")
        self.enterContext(
            self.db.bind_ctx([Recordings, RecordingLease, ArchiveTransfer])
        )
        self.db.create_tables([Recordings, RecordingLease, ArchiveTransfer])
        self.addCleanup(self.db.close)
        self.directory = tempfile.TemporaryDirectory(dir=".")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.source = self.root / "local" / "segment.mp4"
        self.source.parent.mkdir()
        self.payload = b"recording-content" * 100000
        self.source.write_bytes(self.payload)
        self.destination = self.root / "archive" / "camera" / "segment.mp4"
        # Directory fsync is a Linux durability operation, not supported on Windows.
        self.fsync = patch("frigate.record.archive.fsync_directory")
        self.directory_sync = self.fsync.start()
        self.addCleanup(self.fsync.stop)

    def test_copy_is_verified_and_source_is_not_deleted(self):
        copy_verified(self.source, self.destination, 0, lambda: False)
        self.assertEqual(self.source.read_bytes(), self.payload)
        self.assertEqual(self.destination.read_bytes(), self.payload)
        self.assertFalse(self.destination.with_suffix(".mp4.partial").exists())
        self.directory_sync.assert_called_once_with(self.destination.parent)

    def test_batch_directory_flushes_are_reused_only_after_success(self):
        batch = ArchiveDirectoryBatch(self.root)
        directory = self.root / "batch" / "camera"
        batch.prepare(directory)
        self.assertEqual(self.directory_sync.call_count, 4)
        batch.prepare(directory)
        self.assertEqual(self.directory_sync.call_count, 4)
        ArchiveDirectoryBatch(self.root).prepare(directory)
        self.assertEqual(self.directory_sync.call_count, 8)

    def test_failed_directory_flush_is_retried(self):
        batch = ArchiveDirectoryBatch(self.root)
        directory = self.root / "retry"
        self.directory_sync.side_effect = OSError("synthetic fsync failure")
        with self.assertRaises(OSError):
            batch.prepare(directory)
        self.directory_sync.side_effect = None
        self.directory_sync.reset_mock()
        batch.prepare(directory)
        self.assertEqual(self.directory_sync.call_count, 2)

    @unittest.skipIf(os.name == "nt", "Linux directory symlinks")
    def test_cached_directory_is_still_checked_for_symlink_replacement(self):
        batch = ArchiveDirectoryBatch(self.root)
        directory = self.root / "replace"
        batch.prepare(directory)
        directory.rmdir()
        directory.symlink_to(self.source.parent, target_is_directory=True)
        with self.assertRaises(OSError):
            batch.prepare(directory)

    def test_deferred_copy_does_not_read_back_or_publish_before_verification(self):
        events = []
        copied = copy_unverified(
            self.source,
            self.destination,
            0,
            lambda: False,
            progress=lambda phase, amount, total: events.append(phase),
        )
        self.assertTrue(copied.partial.exists())
        self.assertFalse(self.destination.exists())
        self.assertEqual(set(events), {"copying"})
        self.assertEqual(self.source.read_bytes(), self.payload)
        verify_archive_copy(copied, 0, lambda: False)
        self.assertEqual(self.destination.read_bytes(), self.payload)
        self.assertFalse(copied.partial.exists())

    def test_deferred_verification_rejects_corruption_and_keeps_original(self):
        copied = copy_unverified(self.source, self.destination, 0, lambda: False)
        copied.partial.write_bytes(b"x" * len(self.payload))
        with self.assertRaisesRegex(OSError, "verification failed"):
            verify_archive_copy(copied, 0, lambda: False)
        self.assertEqual(self.source.read_bytes(), self.payload)
        self.assertFalse(self.destination.exists())
        self.assertFalse(copied.partial.exists())

    def test_deferred_verification_cancellation_preserves_original(self):
        copied = copy_unverified(self.source, self.destination, 0, lambda: False)
        with self.assertRaises(InterruptedError):
            verify_archive_copy(copied, 0, lambda: True)
        self.assertEqual(self.source.read_bytes(), self.payload)
        self.assertFalse(self.destination.exists())
        self.assertFalse(copied.partial.exists())

    def test_byte_progress_separates_copy_readback_and_commit(self):
        events = []

        def progress(phase, amount, total):
            events.append((phase, amount, total))
            self.assertEqual(total, len(self.payload))
            if phase != "commit":
                self.assertFalse(self.destination.exists())
            self.assertTrue(self.source.exists())

        copy_verified(
            self.source, self.destination, 0, lambda: False, progress=progress
        )
        copying = [amount for phase, amount, _ in events if phase == "copying"]
        readback = [amount for phase, amount, _ in events if phase == "verification"]
        self.assertEqual(copying, [0, 1024 * 1024, len(self.payload)])
        self.assertEqual(readback, copying)
        self.assertEqual(events[-1], ("commit", len(self.payload), len(self.payload)))

    def test_copy_completion_is_not_success_when_readback_fails(self):
        phases = []
        original_open = Path.open

        def corrupt_read(path, mode="r", *args, **kwargs):
            if path == self.destination.with_suffix(".mp4.partial") and mode == "rb":
                return io.BytesIO(b"x" * len(self.payload))
            return original_open(path, mode, *args, **kwargs)

        with patch.object(Path, "open", corrupt_read):
            with self.assertRaisesRegex(OSError, "verification failed"):
                copy_verified(
                    self.source,
                    self.destination,
                    0,
                    lambda: False,
                    progress=lambda phase, amount, total: phases.append(
                        (phase, amount)
                    ),
                )
        self.assertIn(("copying", len(self.payload)), phases)
        self.assertIn(("verification", len(self.payload)), phases)
        self.assertNotIn("commit", [phase for phase, _ in phases])
        self.assertFalse(self.destination.exists())

    def test_interruption_preserves_source_and_removes_partial(self):
        with self.assertRaises(InterruptedError):
            copy_verified(self.source, self.destination, 0, lambda: True)
        self.assertEqual(self.source.read_bytes(), self.payload)
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.destination.with_suffix(".mp4.partial").exists())

    def test_failed_flush_never_publishes_destination(self):
        with patch("frigate.record.archive.os.fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                copy_verified(self.source, self.destination, 0, lambda: False)
        self.assertTrue(self.source.exists())
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.destination.with_suffix(".mp4.partial").exists())

    def test_corrupted_destination_is_not_published(self):
        original_open = Path.open

        def corrupt_read(path, mode="r", *args, **kwargs):
            if path == self.destination.with_suffix(".mp4.partial") and mode == "rb":
                return io.BytesIO(b"corrupt")
            return original_open(path, mode, *args, **kwargs)

        with patch.object(Path, "open", corrupt_read):
            with self.assertRaises(OSError):
                copy_verified(self.source, self.destination, 0, lambda: False)
        self.assertEqual(self.source.read_bytes(), self.payload)
        self.assertFalse(self.destination.exists())

    def test_existing_archive_copy_is_replaced_only_after_verification(self):
        self.destination.parent.mkdir(parents=True)
        self.destination.write_bytes(b"previous-complete-copy")
        with self.assertRaises(InterruptedError):
            copy_verified(self.source, self.destination, 0, lambda: True)
        self.assertEqual(self.destination.read_bytes(), b"previous-complete-copy")

    def test_source_change_fails_verification(self):
        original_stat = Path.stat

        def changed_stat(path, *args, **kwargs):
            stat = original_stat(path, *args, **kwargs)
            if path == self.source:
                values = list(stat)
                values[6] += 1
                return os.stat_result(values)
            return stat

        with patch.object(Path, "stat", changed_stat):
            with self.assertRaises(OSError):
                copy_verified(self.source, self.destination, 0, lambda: False)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.source.read_bytes(), self.payload)

    def test_rate_limit_remains_cancellable(self):
        calls = 0

        def cancel():
            nonlocal calls
            calls += 1
            return calls >= 2

        with patch("frigate.record.archive.time.sleep") as sleep:
            with self.assertRaises(InterruptedError):
                copy_verified(self.source, self.destination, 0.001, cancel)
        self.assertFalse(self.destination.exists())
        self.assertTrue(self.source.exists())
        sleep.assert_not_called()

    def test_rate_limit_accounts_for_copy_and_verification_traffic(self):
        clock = 0.0

        def sleep(seconds):
            nonlocal clock
            clock += seconds

        with (
            patch("frigate.record.archive.time.monotonic", side_effect=lambda: clock),
            patch("frigate.record.archive.time.sleep", side_effect=sleep),
        ):
            copy_verified(self.source, self.destination, 1, lambda: False)
        expected_seconds = len(self.payload) * 2 / (1024 * 1024)
        self.assertGreaterEqual(clock, expected_seconds)

    def make_archive_link(self):
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        self.destination.write_bytes(self.payload)
        try:
            publish_archive_link(self.source, self.destination)
        except OSError as error:
            if getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows requires symbolic-link privileges")
            raise

    def test_stable_path_plays_archive_content(self):
        self.make_archive_link()
        self.assertTrue(self.source.is_symlink())
        self.assertEqual(self.source.readlink(), self.destination)
        self.assertEqual(self.source.read_bytes(), self.payload)
        self.assertFalse(self.source.with_suffix(".mp4.archive-link").exists())

    def test_local_cleanup_does_not_follow_archive_links(self):
        self.make_archive_link()
        with patch("frigate.record.archive.storage_lock", return_value=nullcontext()):
            self.assertFalse(delete_local_recording(self.source))
        self.assertEqual(self.destination.read_bytes(), self.payload)
        self.assertTrue(self.source.is_symlink())

    def test_missing_archive_does_not_remove_stable_link(self):
        self.make_archive_link()
        self.destination.unlink()
        self.assertFalse(self.source.exists())
        self.assertTrue(os.path.lexists(self.source))
        with patch("frigate.record.archive.storage_lock", return_value=nullcontext()):
            self.assertFalse(delete_local_recording(self.source))

    def test_missing_local_file_is_not_counted_as_reclaimed_space(self):
        self.source.unlink()
        with patch("frigate.record.archive.storage_lock", return_value=nullcontext()):
            with self.assertRaises(FileNotFoundError):
                delete_local_recording(self.source, missing_ok=False)

    def test_archive_path_preserves_recording_structure(self):
        with patch("frigate.record.archive.RECORD_DIR", str(self.source.parent)):
            self.assertEqual(
                archive_destination(self.source, self.root / "archive"),
                self.root / "archive" / "segment.mp4",
            )
            with self.assertRaises(ValueError):
                archive_destination(self.root / "outside.mp4", self.root / "archive")
            with self.assertRaises(ValueError):
                archive_destination(
                    self.source.parent / ".." / "outside.mp4",
                    self.root / "archive",
                )

    def test_new_archive_directories_are_flushed_before_copying(self):
        root = self.root / "archive"
        root.mkdir()
        target = root / "2026-01-01" / "00" / "front"
        prepare_archive_directory(target, root)
        self.assertTrue(target.is_dir())
        self.assertEqual(
            self.directory_sync.call_args_list,
            [
                call(root / "2026-01-01"),
                call(root),
                call(root / "2026-01-01" / "00"),
                call(root / "2026-01-01"),
                call(target),
                call(target.parent),
            ],
        )

    def test_directory_flush_failure_blocks_archive_preparation(self):
        root = self.root / "archive"
        root.mkdir()
        self.directory_sync.side_effect = OSError("directory sync failed")
        with self.assertRaises(OSError):
            prepare_archive_directory(root / "day" / "hour", root)
        self.assertFalse((root / "day" / "hour").exists())
        self.assertEqual(self.source.read_bytes(), self.payload)

    def test_existing_directories_are_flushed_again_after_interrupted_preparation(self):
        root = self.root / "archive"
        root.mkdir()
        target = root / "day"
        target.mkdir()
        prepare_archive_directory(target, root)
        self.directory_sync.assert_has_calls([call(target), call(root)])

    def test_directory_preparation_rejects_escape_and_non_directory(self):
        with self.assertRaises(ValueError):
            prepare_archive_directory(self.root / ".." / "outside", self.root)
        blocked = self.root / "blocked"
        blocked.write_bytes(b"not-a-directory")
        with self.assertRaises(OSError):
            prepare_archive_directory(blocked / "hour", self.root)


class TestArchiveMount(unittest.TestCase):
    def test_local_fallback_is_rejected_before_marker_access(self):
        mounts = "10 1 0:1 / /media/frigate/archive rw - ext4 /dev/sda rw\n"
        with patch("builtins.open", return_value=io.StringIO(mounts)):
            with patch.object(Path, "read_text") as marker:
                with self.assertRaises(OSError):
                    validate_mount(Path("/media/frigate/archive"), "archive-id")
                marker.assert_not_called()

    def test_valid_nfs_identity(self):
        mounts = "10 1 0:1 / /media/frigate/archive rw - nfs4 nas:/archive rw\n"
        root = Path("/media/frigate/archive")
        with (
            patch("builtins.open", return_value=io.StringIO(mounts)),
            patch.object(Path, "is_symlink", return_value=False),
            patch.object(Path, "resolve", return_value=root),
            patch.object(Path, "read_text", return_value="archive-id\n"),
        ):
            validate_mount(root, "archive-id")

    def test_nfs_with_wrong_identity_is_rejected(self):
        mounts = "10 1 0:1 / /media/frigate/archive rw - nfs nas:/archive rw\n"
        root = Path("/media/frigate/archive")
        with (
            patch("builtins.open", return_value=io.StringIO(mounts)),
            patch.object(Path, "is_symlink", return_value=False),
            patch.object(Path, "resolve", return_value=root),
            patch.object(Path, "read_text", return_value="wrong-share"),
        ):
            with self.assertRaises(OSError):
                validate_mount(root, "archive-id")

    def test_nested_local_mount_overrides_nfs_parent(self):
        mounts = (
            "10 1 0:1 / /media/frigate rw - nfs4 nas:/media rw\n"
            "11 10 8:1 / /media/frigate/archive rw - ext4 /dev/sda rw\n"
        )
        with (
            patch("builtins.open", return_value=io.StringIO(mounts)),
            patch.object(Path, "read_text") as marker,
        ):
            with self.assertRaises(OSError):
                validate_mount(Path("/media/frigate/archive"), "archive-id")
        marker.assert_not_called()

    def test_escaped_mountpoint_is_recognized(self):
        mounts = "10 1 0:1 / /media/frigate/nas\\040archive rw - nfs4 nas:/archive rw\n"
        root = Path("/media/frigate/nas archive")
        with (
            patch("builtins.open", return_value=io.StringIO(mounts)),
            patch.object(Path, "is_symlink", return_value=False),
            patch.object(Path, "resolve", return_value=root),
            patch.object(Path, "read_text", return_value="archive-id"),
        ):
            validate_mount(root, "archive-id")

    def test_pinned_mount_descriptor_is_closed_after_validation_failure(self):
        root = Path("/media/frigate/archive")
        with (
            patch("frigate.record.archive.os.O_DIRECTORY", 0, create=True),
            patch("frigate.record.archive.os.open", return_value=42),
            patch("frigate.record.archive.os.close") as close,
            patch(
                "frigate.record.archive.validate_mount",
                side_effect=OSError("unmounted"),
            ),
        ):
            with self.assertRaises(OSError):
                with pin_archive(root, "archive-id"):
                    self.fail("unverified mount was exposed to a writer")
        close.assert_called_once_with(42)

    def test_mount_replacement_during_validation_is_rejected(self):
        root = Path("/media/frigate/archive")
        with (
            patch("frigate.record.archive.os.O_DIRECTORY", 0, create=True),
            patch("frigate.record.archive.os.open", return_value=42),
            patch("frigate.record.archive.os.close") as close,
            patch("frigate.record.archive.validate_mount"),
            patch(
                "frigate.record.archive.os.fstat",
                return_value=SimpleNamespace(st_dev=1, st_ino=10),
            ),
            patch.object(
                Path, "stat", return_value=SimpleNamespace(st_dev=2, st_ino=20)
            ),
        ):
            with self.assertRaises(OSError):
                with pin_archive(root, "archive-id"):
                    self.fail("replaced mount was exposed to a writer")
        close.assert_called_once_with(42)


class TestArchiveMetadata(unittest.TestCase):
    def setUp(self):
        self.db = SqliteDatabase(":memory:")
        self.db.bind([Recordings, Event])
        self.db.create_tables([Recordings, Event])
        self.addCleanup(self.db.close)

    def recording(self, tier="local"):
        return Recordings.create(
            id="test-recording",
            camera="front",
            path="missing-local-anchor.mp4",
            start_time=1,
            end_time=11,
            duration=10,
            storage_tier=tier,
        )

    def config(self, enabled):
        return SimpleNamespace(
            storage=SimpleNamespace(enabled=enabled, retain_days=90),
            record=SimpleNamespace(archive_retention_days=0),
            cameras={
                "front": SimpleNamespace(
                    record=SimpleNamespace(archive_retention_days=0)
                )
            },
        )

    def test_existing_recordings_default_to_local(self):
        recording = self.recording()
        self.assertEqual(recording.storage_tier, "local")
        self.assertFalse(recording.delete_requested)

    def test_offline_archive_deletion_is_persisted_not_unlinked(self):
        recording = self.recording("archive")
        with patch("frigate.record.archive.delete_local_recording") as delete:
            self.assertFalse(
                request_recording_deletion(
                    Path(recording.path),
                    recording.storage_tier,
                )
            )
            delete.assert_not_called()
        saved = Recordings.get_by_id(recording.id)
        self.assertTrue(saved.delete_requested)
        self.assertEqual(saved.storage_tier, "archive")

    def test_uncommitted_link_is_repaired_when_deletion_is_requested(self):
        recording = self.recording()
        with (
            patch("frigate.record.archive.delete_local_recording", return_value=False),
            patch.object(Path, "is_symlink", return_value=True),
        ):
            self.assertFalse(request_recording_deletion(Path(recording.path)))
        saved = Recordings.get_by_id(recording.id)
        self.assertEqual(saved.storage_tier, "archive")
        self.assertTrue(saved.delete_requested)

    def test_enabling_archive_extends_metadata_lifetime(self):
        config = self.config(True)
        apply_archive_retention(config)
        self.assertEqual(config.record.archive_retention_days, 90)
        self.assertEqual(config.cameras["front"].record.archive_retention_days, 90)

    def test_disabling_transport_preserves_existing_archive_metadata(self):
        self.recording("archive")
        config = self.config(False)
        apply_archive_retention(config)
        self.assertEqual(config.cameras["front"].record.archive_retention_days, 90)

    def test_disabled_empty_archive_preserves_existing_behavior(self):
        config = self.config(False)
        apply_archive_retention(config)
        self.assertEqual(config.record.archive_retention_days, 0)
        self.assertEqual(config.cameras["front"].record.archive_retention_days, 0)


if __name__ == "__main__":
    unittest.main()

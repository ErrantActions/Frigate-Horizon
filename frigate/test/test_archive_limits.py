"""Exact archive admission, bounded recovery, and protected oldest-first expiry."""

import importlib.util
import sqlite3
import sys
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from frigate.models import ArchiveTransfer, RecordingLease, Recordings
from frigate.record.archive import archive_destination, copy_verified
from frigate.record.archive_limits import (
    ArchiveBlocked,
    ArchiveCapacity,
    archive_budget_bytes,
    archive_owned_bytes,
    backfill_archive_bytes,
    plan_archive_batch,
    recover_archive_transfers,
)
from frigate.record.archive_store import archive_retention_days, expire_recording
from frigate.record.leases import lease_recordings, release_recordings
from frigate.test import test_archive_store as store_fixture
from frigate.util.storage import (
    invalidate_storage_capacity,
    recording_storage_usage,
    storage_capacity,
)


class TestArchiveLimits(unittest.TestCase):
    setUp = store_fixture.TestArchiveStore.setUp
    saved_event = store_fixture.TestArchiveStore.saved_event

    def archived(self, identifier, size=16, age=1):
        source = self.local / f"{identifier}.mp4"
        destination = self.archive / source.name
        destination.write_bytes(b"x" * size)
        end = time.time() - age * 86400
        return Recordings.create(
            id=identifier,
            camera="front",
            path=str(source),
            start_time=end - 10,
            end_time=end,
            duration=10,
            storage_tier="archive",
            archive_bytes=size,
        )

    def config(self, budget=32, behavior="expire_oldest"):
        return SimpleNamespace(
            storage=SimpleNamespace(
                max_archive_gib=None if budget is None else budget / (2**30),
                archive_full_behavior=behavior,
            ),
            record=self.record,
            cameras={"front": self.camera},
        )

    def ensure(self, capacity, config, incoming=0, cancelled=lambda: False):
        capacity.ensure_room(
            config, self.archive, self.archive, incoming, cancelled, Mock()
        )

    def test_6tib_exact_boundary_includes_incoming_bytes(self):
        budget = archive_budget_bytes(6144)
        self.assertEqual(budget, 6 * (2**40))
        row = self.archived("huge")
        Recordings.update(archive_bytes=budget - 17).where(
            Recordings.id == row.id
        ).execute()
        config = self.config(budget, "pause")
        self.ensure(ArchiveCapacity(), config, 17)
        with self.assertRaisesRegex(ArchiveBlocked, "archive_capacity_paused"):
            self.ensure(ArchiveCapacity(), config, 18)
        self.assertEqual(Recordings.select().count(), 1)

    def test_oldest_ordinary_is_removed_before_older_unsaved_event(self):
        event = self.archived("event", age=10)
        store_fixture.TestArchiveStore.review(self, event)
        self.archived("ordinary", age=2)
        self.ensure(ArchiveCapacity(), self.config(), incoming=16)
        self.assertTrue(Recordings.select().where(Recordings.id == "event").exists())
        self.assertFalse(
            Recordings.select().where(Recordings.id == "ordinary").exists()
        )

    def test_unsaved_events_are_last_resort_after_complete_ordinary_pass(self):
        event = self.archived("event", age=10)
        store_fixture.TestArchiveStore.review(self, event)
        capacity = ArchiveCapacity()
        with self.assertRaisesRegex(ArchiveBlocked, "archive_cleanup_pending"):
            self.ensure(capacity, self.config(16), 16)
        self.ensure(capacity, self.config(16), 16)
        self.assertEqual(Recordings.select().count(), 0)

    def test_saved_and_leased_footage_block_budget_reduction(self):
        saved = self.archived("saved")
        self.saved_event(saved)
        leased = self.archived("leased", age=2)
        RecordingLease.create(
            id="reader",
            camera=leased.camera,
            start_time=leased.start_time,
            end_time=leased.end_time,
            expires=time.time() + 300,
        )
        capacity = ArchiveCapacity()
        with self.assertRaisesRegex(ArchiveBlocked, "archive_cleanup_pending"):
            self.ensure(capacity, self.config(1))
        with self.assertRaisesRegex(
            ArchiveBlocked, "archive_capacity_protected_or_leased"
        ):
            self.ensure(capacity, self.config(1))
        self.assertEqual(Recordings.select().count(), 2)
        self.assertEqual(len(list(self.archive.glob("*.mp4"))), 2)

    def test_180_day_boundary_and_saved_exception(self):
        row = self.archived("boundary", age=180)
        now = row.end_time + 180 * 86400
        self.assertFalse(expire_recording(row, self.archive, self.archive, 180, now))
        self.saved_event(row)
        self.assertFalse(
            expire_recording(row, self.archive, self.archive, 180, now + 1)
        )
        from frigate.models import Event

        Event.delete().execute()
        self.assertTrue(expire_recording(row, self.archive, self.archive, 180, now + 1))

    def test_explicit_maximum_overrides_longer_camera_retention_only_when_set(self):
        row = self.archived("long-camera")
        self.record.continuous.days = 365
        self.assertEqual(archive_retention_days(row, self.camera, 180), 365)
        self.assertEqual(archive_retention_days(row, self.camera, 180, 180), 180)

    def test_reduced_budget_expires_young_ordinary_footage(self):
        self.archived("young", age=1)
        self.ensure(ArchiveCapacity(), self.config(1))
        self.assertEqual(archive_owned_bytes(), 0)

    def test_incoming_larger_than_entire_budget_never_evicts(self):
        self.archived("keep")
        with self.assertRaisesRegex(ArchiveBlocked, "archive_incoming_exceeds_budget"):
            self.ensure(ArchiveCapacity(), self.config(16), 17)
        self.assertEqual(Recordings.select().count(), 1)

    def test_config_change_prevents_eviction(self):
        self.archived("keep")
        with self.assertRaisesRegex(ArchiveBlocked, "archive_settings_changed"):
            self.ensure(ArchiveCapacity(), self.config(1), cancelled=lambda: True)
        self.assertEqual(Recordings.select().count(), 1)

    def test_filesystem_full_is_not_reported_as_free_quota(self):
        with patch(
            "frigate.record.archive_limits.shutil.disk_usage",
            return_value=SimpleNamespace(free=0),
        ):
            with self.assertRaisesRegex(ArchiveBlocked, "archive_capacity_paused"):
                self.ensure(ArchiveCapacity(), self.config(None, "pause"), 1)

    def test_explicit_safety_reserve_applies_to_physical_admission_once(self):
        config = self.config(None, "pause")
        config.storage.archive_reserve_gib = 100 / 2**30
        with patch(
            "frigate.record.archive_limits.shutil.disk_usage",
            return_value=SimpleNamespace(free=110),
        ):
            self.ensure(ArchiveCapacity(), config, 10)
            with self.assertRaisesRegex(ArchiveBlocked, "archive_capacity_paused"):
                self.ensure(ArchiveCapacity(), config, 11)

    def test_plan_is_oldest_eligible_prefix_and_does_not_change_with_ingest(self):
        now = time.time()
        for identifier, age, size in (
            ("first", 100, 3),
            ("second", 90, 4),
            ("fresh", 0, 99),
        ):
            Recordings.create(
                id=identifier,
                camera="front",
                path=f"{identifier}.mp4",
                start_time=now - age - 10,
                end_time=now - age,
                duration=10,
                segment_size=size,
            )
        plan = plan_archive_batch(5 * 2**20)
        self.assertEqual(plan, (7 * 2**20, 2))
        Recordings.create(
            id="new",
            camera="front",
            path="new.mp4",
            start_time=now - 10,
            end_time=now,
            duration=10,
            segment_size=30,
        )
        self.assertEqual(plan, plan_archive_batch(5 * 2**20))

    def test_local_transfer_reservation_blocks_source_deletion_without_claim(self):
        row = store_fixture.TestArchiveStore.recording(self)
        ArchiveTransfer.create(id=row.id, path=row.path, size=17)
        from frigate.record.archive import (
            delete_local_recording,
            request_recording_deletion,
        )

        claim = Mock(return_value=True)
        self.assertFalse(delete_local_recording(Path(row.path), can_delete=claim))
        self.assertFalse(delete_local_recording(Path(row.path)))
        claim.assert_not_called()
        with self.assertRaises(BlockingIOError):
            request_recording_deletion(Path(row.path))
        saved = Recordings.get_by_id(row.id)
        self.assertEqual(saved.storage_tier, "local")
        self.assertFalse(saved.delete_requested)
        self.assertTrue(Path(row.path).exists())

    def test_local_reader_lease_blocks_cleanup_without_a_callback(self):
        row = store_fixture.TestArchiveStore.recording(self)
        from frigate.record.archive import delete_local_recording

        lease_recordings("reader", row.camera, row.start_time, row.end_time)
        self.assertFalse(delete_local_recording(Path(row.path)))
        release_recordings("reader")
        self.assertTrue(delete_local_recording(Path(row.path)))

    def test_source_lease_is_held_until_commit_then_released(self):
        row = store_fixture.TestArchiveStore.recording(self)
        from frigate.record.archive_store import move_recording
        from frigate.record.leases import recording_has_lease

        def copy(source, destination, rate, cancelled):
            self.assertTrue(recording_has_lease(row))
            copy_verified(source, destination, rate, cancelled)

        with (
            patch("frigate.record.archive_store.copy_verified", side_effect=copy),
            patch("frigate.record.archive_store.publish_archive_link"),
        ):
            self.assertTrue(
                move_recording(row, self.archive, self.archive, 0, lambda: False)
            )
        self.assertEqual(RecordingLease.select().count(), 0)

    def test_durable_reservation_blocks_archive_expiry_after_lease_expiration(self):
        row = self.archived("maintenance", size=17, age=200)
        ArchiveTransfer.create(id=row.id, path=row.path, size=34)
        Recordings.update(delete_requested=True, delete_reason="user").execute()
        self.assertFalse(
            expire_recording(row, self.archive, self.archive, 0, time.time())
        )
        self.assertTrue((self.archive / "maintenance.mp4").exists())
        self.assertEqual(Recordings.get_by_id(row.id).archive_bytes, 17)
        ArchiveTransfer.delete().execute()
        self.assertTrue(
            expire_recording(row, self.archive, self.archive, 0, time.time())
        )

    def test_tagged_maintenance_is_counted_but_never_recovered_as_a_transfer(self):
        from frigate.record.archive_limits import archive_maintenance_active

        row = self.archived("maintenance", size=17)
        partial = (self.archive / "maintenance.mp4").with_suffix(".mp4.partial")
        partial.write_bytes(b"maintenance-owned-staging")
        ArchiveTransfer.create(
            id=row.id, path=row.path, size=34, kind="compression", owner="owned-token"
        )
        self.assertTrue(recover_archive_transfers(self.archive, self.archive))
        self.assertTrue(archive_maintenance_active())
        self.assertEqual(archive_owned_bytes(), 51)
        with self.assertRaisesRegex(ArchiveBlocked, "archive_maintenance_pending"):
            self.ensure(ArchiveCapacity(), self.config(None, "pause"), 1)
        self.assertEqual(partial.read_bytes(), b"maintenance-owned-staging")
        self.assertEqual(ArchiveTransfer.get_by_id(row.id).owner, "owned-token")
        self.assertEqual(Recordings.get_by_id(row.id).archive_bytes, 17)

    def test_low_level_local_delete_refuses_saved_media_without_a_callback(self):
        row = store_fixture.TestArchiveStore.recording(self)
        self.saved_event(row)
        from frigate.record.archive import (
            delete_local_recording,
            request_recording_deletion,
        )
        from frigate.record.policy import RecordingProtected

        self.assertFalse(delete_local_recording(Path(row.path)))
        with self.assertRaises(RecordingProtected):
            request_recording_deletion(Path(row.path))
        self.assertTrue(Path(row.path).exists())
        self.assertFalse(Recordings.get_by_id(row.id).delete_requested)

    def test_low_level_archived_delete_never_records_intent_for_saved_media(self):
        row = self.archived("saved")
        self.saved_event(row)
        from frigate.record.archive import request_recording_deletion
        from frigate.record.policy import RecordingProtected

        with self.assertRaises(RecordingProtected):
            request_recording_deletion(Path(row.path), "archive")
        self.assertTrue((self.archive / "saved.mp4").exists())
        self.assertFalse(Recordings.get_by_id(row.id).delete_requested)

    def test_supplied_capture_padding_protects_local_and_archive_delete_paths(self):
        from frigate.models import Event
        from frigate.record.archive import request_recording_deletion
        from frigate.record.policy import CapturePadding, RecordingProtected

        for tier in ("local", "archive"):
            with self.subTest(tier=tier):
                Recordings.delete().execute()
                Event.delete().execute()
                row = (
                    store_fixture.TestArchiveStore.recording(self)
                    if tier == "local"
                    else self.archived("saved")
                )
                event = self.saved_event(row)
                Event.update(
                    start_time=row.end_time + 4, end_time=row.end_time + 10
                ).where(Event.id == event.id).execute()
                with self.assertRaises(RecordingProtected):
                    request_recording_deletion(
                        Path(row.path),
                        tier,
                        capture_padding=CapturePadding(alert_pre=5),
                    )
                self.assertFalse(Recordings.get_by_id(row.id).delete_requested)
                if tier == "local":
                    self.assertTrue(Path(row.path).exists())
                else:
                    Recordings.update(
                        delete_requested=True, delete_reason="user"
                    ).execute()
                    self.assertFalse(
                        expire_recording(
                            row,
                            self.archive,
                            self.archive,
                            0,
                            time.time(),
                            CapturePadding(alert_pre=5),
                        )
                    )
                    self.assertTrue((self.archive / "saved.mp4").exists())

    def test_unknown_negative_archive_lengths_are_not_negative_usage(self):
        self.archived("unknown")
        Recordings.update(archive_bytes=-1).execute()
        totals, _ = recording_storage_usage()
        self.assertEqual(totals["archive"]["usage_mib"], 0)
        self.assertEqual(totals["archive"]["unknown_size_recordings"], 1)

    def test_unknown_size_backfill_is_bounded_and_never_zero_on_failure(self):
        row = self.archived("unknown", size=17)
        Recordings.update(archive_bytes=None).execute()
        with self.assertRaisesRegex(ArchiveBlocked, "archive_accounting_pending"):
            archive_owned_bytes()
        self.assertTrue(backfill_archive_bytes(self.archive))
        self.assertEqual(archive_owned_bytes(), 17)
        Recordings.update(archive_bytes=None).execute()
        (self.archive / "unknown.mp4").unlink()
        with self.assertRaisesRegex(ArchiveBlocked, "archive_size_unavailable"):
            backfill_archive_bytes(self.archive)
        self.assertIsNone(Recordings.get_by_id(row.id).archive_bytes)

    def test_backfill_stops_at_bounded_batch_and_blocks_until_complete(self):
        self.archived("first", size=17)
        self.archived("second", size=18)
        Recordings.update(archive_bytes=None).execute()
        with patch("frigate.record.archive_limits.BACKFILL_BATCH", 1):
            self.assertFalse(backfill_archive_bytes(self.archive))
            self.assertTrue(backfill_archive_bytes(self.archive))
        self.assertEqual(archive_owned_bytes(), 35)

    def test_backfill_groups_full_commits_and_never_stats_under_writer_lock(self):
        for index in range(40):
            self.archived(f"legacy-{index}", size=16 + index)
        Recordings.update(archive_bytes=None).execute()
        statements = []
        self.db.connection().set_trace_callback(statements.append)

        def outside_transaction(source, root):
            self.assertFalse(self.db.in_transaction())
            return archive_destination(source, root)

        def progress():
            self.assertFalse(self.db.in_transaction())

        with patch(
            "frigate.record.archive_limits.archive_destination",
            side_effect=outside_transaction,
        ):
            self.assertTrue(backfill_archive_bytes(self.archive, progress=progress))
        self.assertEqual(sum(sql == "BEGIN IMMEDIATE" for sql in statements), 2)
        self.assertEqual(archive_owned_bytes(), sum(16 + index for index in range(40)))

    def test_crash_reservation_counts_orphans_until_durable_cleanup(self):
        source = self.local / "interrupted.mp4"
        ArchiveTransfer.create(id="gone", path=str(source), size=21)
        destination = self.archive / source.name
        destination.write_bytes(b"complete")
        partial = destination.with_suffix(".mp4.partial")
        partial.write_bytes(b"partial")
        self.assertEqual(archive_owned_bytes(), 21)
        with patch("frigate.record.archive_limits.fsync_directory"):
            self.assertTrue(recover_archive_transfers(self.archive, self.archive))
        self.assertEqual(archive_owned_bytes(), 0)
        self.assertFalse(destination.exists())
        self.assertFalse(partial.exists())

    def test_offline_recovery_preserves_reservation(self):
        source = self.local / "interrupted.mp4"
        ArchiveTransfer.create(id="gone", path=str(source), size=21)
        with patch.object(Path, "unlink", side_effect=OSError("NAS unavailable")):
            with self.assertRaises(OSError):
                recover_archive_transfers(self.archive, self.archive)
        self.assertEqual(archive_owned_bytes(), 21)

    def test_committed_transfer_recovery_does_not_delete_archive(self):
        row = self.archived("complete", size=17)
        ArchiveTransfer.create(id=row.id, path=row.path, size=17)
        self.assertEqual(archive_owned_bytes(), 34)
        with patch("frigate.record.archive_limits.fsync_directory"):
            self.assertTrue(recover_archive_transfers(self.archive, self.archive))
        self.assertEqual(archive_owned_bytes(), 17)
        self.assertTrue((self.archive / "complete.mp4").exists())

    def test_copy_cannot_write_past_reservation_when_source_grows(self):
        source = self.local / "growing.mp4"
        source.write_bytes(b"x" * 17)
        destination = self.archive / source.name
        with self.assertRaisesRegex(OSError, "exceeds its archive reservation"):
            copy_verified(source, destination, 0, lambda: False, 16)
        self.assertFalse(destination.exists())
        self.assertFalse(destination.with_suffix(".mp4.partial").exists())

    def test_exact_archive_aggregate_does_not_use_rounded_mib(self):
        self.archived("exact", size=17)
        Recordings.update(segment_size=999).execute()
        totals, _ = recording_storage_usage()
        self.assertEqual(totals["archive"]["usage_mib"], 17 / (2**20))
        Recordings.update(archive_bytes=None).execute()
        totals, _ = recording_storage_usage()
        self.assertEqual(totals["archive"]["unknown_size_recordings"], 1)
        self.assertEqual(totals["archive"]["usage_mib"], 0)

    def test_unset_quota_preserves_unlimited_admission_with_legacy_unknown_sizes(self):
        self.archived("legacy")
        Recordings.update(archive_bytes=None).execute()
        report = Mock()
        ArchiveCapacity().ensure_room(
            self.config(None),
            self.archive,
            self.archive,
            17,
            lambda: False,
            report,
        )
        self.assertNotIn("archive_usage_bytes", report.call_args.kwargs)

    def test_unset_quota_with_physical_room_skips_full_archive_accounting(self):
        report = Mock()
        with patch(
            "frigate.record.archive_limits.archive_owned_bytes",
            side_effect=AssertionError(
                "Full archive scan is unnecessary without a quota"
            ),
        ):
            ArchiveCapacity().ensure_room(
                self.config(None),
                self.archive,
                self.archive,
                17,
                lambda: False,
                report,
            )
        self.assertIsNone(report.call_args.kwargs["archive_budget_bytes"])
        self.assertEqual(report.call_args.kwargs["archive_reserved_bytes"], 17)
        self.assertNotIn("archive_usage_bytes", report.call_args.kwargs)

    def test_unset_quota_fast_admission_still_blocks_maintenance(self):
        ArchiveTransfer.create(
            id="maintenance",
            path=str(self.local / "maintenance.mp4"),
            size=0,
            kind="maintenance",
        )
        with self.assertRaisesRegex(ArchiveBlocked, "archive_maintenance_pending"):
            ArchiveCapacity().ensure_room(
                self.config(None),
                self.archive,
                self.archive,
                17,
                lambda: False,
                Mock(),
            )

    def test_original_media_lease_blocks_even_explicit_deletion_until_released(self):
        row = self.archived("reader")
        with patch("frigate.record.leases.storage_lock", nullcontext):
            lease_recordings("reader", row.camera, row.start_time, row.end_time)
        Recordings.update(delete_requested=True, delete_reason="user").execute()
        self.assertFalse(
            expire_recording(row, self.archive, self.archive, 0, time.time())
        )
        release_recordings("reader")
        self.assertTrue(
            expire_recording(row, self.archive, self.archive, 0, time.time())
        )


class TestArchiveLimitConfig(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Load only these Pydantic models, not Linux detector/runtime dependencies.
        root = Path(__file__).parents[1] / "config"
        for name in ("base", "compression", "storage"):
            spec = importlib.util.spec_from_file_location(
                f"_archive_limit_config.{name}", root / f"{name}.py"
            )
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
        cls.model = module.StorageConfig

    def test_old_config_defaults_are_unchanged(self):
        config = self.model()
        self.assertIsNone(config.max_age_days)
        self.assertIsNone(config.max_archive_gib)
        self.assertEqual(config.archive_full_behavior, "pause")
        self.assertEqual(config.retain_days, 90)
        self.assertTrue(config.emergency_delete)
        self.assertEqual(config.archive_reserve_gib, 0)
        self.assertFalse(config.compression.enabled)
        self.assertFalse(config.compression.window.enabled)

    def test_approved_limits_and_invalid_values(self):
        config = self.model(
            max_age_days=180,
            max_archive_gib=6144,
            archive_full_behavior="expire_oldest",
            retain_days=180,
        )
        self.assertEqual(archive_budget_bytes(config.max_archive_gib), 6597069766656)
        for field in ("max_age_days", "max_archive_gib"):
            for value in (0, -1, float("inf"), float("nan")):
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaises(ValueError),
                ):
                    self.model(**{field: value})
        with self.assertRaises(ValueError):
            self.model(archive_full_behavior="delete_protected")

    def test_migration_preserves_existing_rows_and_round_trips(self):
        import importlib

        migration = importlib.import_module("migrations.042_add_archive_limits")
        with sqlite3.connect(":memory:") as database:
            database.execute("CREATE TABLE recordings (id TEXT PRIMARY KEY)")
            database.execute("INSERT INTO recordings VALUES ('existing')")
            migrator = SimpleNamespace(sql=database.execute)
            migration.migrate(migrator, database)
            self.assertEqual(
                database.execute("SELECT id, archive_bytes FROM recordings").fetchone(),
                ("existing", None),
            )
            migration.rollback(migrator, database)
            self.assertEqual(
                database.execute("SELECT id FROM recordings").fetchone(), ("existing",)
            )


class TestDualCapacity(unittest.TestCase):
    def test_quota_and_physical_space_both_limit_admission(self):
        capacity = storage_capacity(
            30,
            100,
            1000,
            80,
            reserved_bytes=20,
            written_reservation_bytes=10,
            reserve_bytes=5,
        )
        self.assertEqual(capacity["budget_remaining_bytes"], 50)
        self.assertEqual(capacity["effective_headroom_bytes"], 50)
        self.assertEqual(capacity["usage_bytes"], 30)
        capacity = storage_capacity(
            30,
            100,
            1000,
            40,
            reserved_bytes=20,
            written_reservation_bytes=10,
            reserve_bytes=5,
        )
        self.assertEqual(capacity["effective_headroom_bytes"], 25)

    def test_unknown_usage_offline_and_stale_never_claim_quota_headroom(self):
        capacity = storage_capacity(None, 100, 1000, 800)
        self.assertIsNone(capacity["budget_remaining_bytes"])
        self.assertIsNone(capacity["effective_headroom_bytes"])
        self.assertEqual(capacity["filesystem_available_bytes"], 800)
        for state in ("offline", "stale"):
            invalid = invalidate_storage_capacity(capacity, state)
            self.assertEqual(invalid["state"], state)
            self.assertIsNone(invalid["effective_headroom_bytes"])
            self.assertEqual(invalid["filesystem_available_bytes"], 800)

    def test_unset_budget_is_not_unlimited_physical_space(self):
        capacity = storage_capacity(300, None, 1000, 50, reserve_bytes=10)
        self.assertIsNone(capacity["budget_remaining_bytes"])
        self.assertEqual(capacity["effective_headroom_bytes"], 40)
        capacity = storage_capacity(110, 100, 1000, 900)
        self.assertEqual(capacity["over_budget_bytes"], 10)
        self.assertEqual(capacity["effective_headroom_bytes"], 0)

    def test_shared_filesystem_reservation_does_not_charge_other_tier_quota(self):
        local = storage_capacity(
            20,
            100,
            1000,
            50,
            physical_reserved_bytes=30,
            written_reservation_bytes=10,
            reserve_bytes=5,
            filesystem_id="shared",
        )
        self.assertEqual(local["budget_remaining_bytes"], 80)
        self.assertEqual(local["reserved_bytes"], 0)
        self.assertEqual(local["effective_headroom_bytes"], 25)
        self.assertEqual(local["physical_reserved_bytes"], 30)

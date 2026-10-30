"""Generated-media and fault-injection coverage, not NVIDIA hardware certification."""

import asyncio
import importlib.util
import json
import threading
import time
import unittest
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from peewee import SqliteDatabase
from pydantic import ValidationError

from frigate.api.defs.request.compression_body import CompressionStartBody
from frigate.config.compression import StorageCompressionConfig
from frigate.models import ArchiveTransfer, Event, RecordingLease, Recordings
from frigate.record.audio_repair import RepairError, _digest
from frigate.record.compression import (
    CompressionError,
    RecordingCompression,
    fixed_rate,
)
from frigate.record.compression_manager import (
    CompressionConflict,
    CompressionManager,
    weekly_window,
)
from frigate.record.compression_storage import (
    CompressionStorage,
    reservation_schema_ready,
)
from frigate.test.test_audio_repair import AudioRepairFixture


class TestCompressionPolicy(unittest.TestCase):
    def test_actual_owner_migration_preserves_existing_transfer_reservations(self):
        database = SqliteDatabase(":memory:")
        try:
            database.connect()
            database.execute_sql(
                "CREATE TABLE archivetransfer(id TEXT,path TEXT,size INTEGER)"
            )
            database.execute_sql(
                "INSERT INTO archivetransfer VALUES('existing','recording.mp4',123)"
            )
            migration_path = (
                Path(__file__).resolve().parents[2]
                / "migrations"
                / "043_add_archive_reservation_owner.py"
            )
            specification = importlib.util.spec_from_file_location(
                "compression_owner_migration", migration_path
            )
            assert specification is not None and specification.loader is not None
            migration = importlib.util.module_from_spec(specification)
            specification.loader.exec_module(migration)
            migration.migrate(SimpleNamespace(sql=database.execute_sql), database)
            self.assertTrue(reservation_schema_ready(database))
            self.assertEqual(
                database.execute_sql(
                    "SELECT id,path,size,kind,owner FROM archivetransfer"
                ).fetchone(),
                ("existing", "recording.mp4", 123, "transfer", None),
            )
        finally:
            database.close()

    def test_reservation_admission_requires_both_real_schema_and_model_tags(self):
        database = SqliteDatabase(":memory:")
        try:
            database.connect()
            database.execute_sql(
                "CREATE TABLE archivetransfer(id TEXT,path TEXT,size INTEGER)"
            )
            self.assertFalse(reservation_schema_ready(database))
            database.execute_sql(
                "ALTER TABLE archivetransfer ADD COLUMN kind TEXT DEFAULT 'transfer'"
            )
            self.assertFalse(reservation_schema_ready(database))
            database.execute_sql("ALTER TABLE archivetransfer ADD COLUMN owner TEXT")
            self.assertEqual(
                reservation_schema_ready(database),
                {"kind", "owner"}.issubset(ArchiveTransfer._meta.fields),
            )
        finally:
            database.close()

    def test_safe_defaults_and_strict_request(self):
        policy = StorageCompressionConfig()
        self.assertFalse(policy.enabled)
        self.assertFalse(policy.window.enabled)
        self.assertEqual(policy.min_age_days, 14)
        self.assertEqual(policy.target_mbps, 4)
        self.assertEqual(policy.encoder, "hevc_nvenc")
        for value in ("ffmpeg", "libsvtav1;unsafe", "", "vp9"):
            with self.subTest(encoder=value), self.assertRaises(ValidationError):
                StorageCompressionConfig(encoder=value)
        for value in (0, 129):
            with self.assertRaises(ValidationError):
                StorageCompressionConfig(cpu_threads=value)
        for value in (0, 61):
            with self.assertRaises(ValidationError):
                StorageCompressionConfig(frame_rate=value)
        for value in (0, -1, float("inf"), float("nan"), 101):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                StorageCompressionConfig(target_mbps=value)
        for value in (
            {},
            {"confirm": False},
            {"confirm": 1},
            {"confirm": "true"},
            {"confirm": True, "path": "/media/file"},
        ):
            with self.assertRaises(ValidationError):
                CompressionStartBody.model_validate(value)

    def test_weekly_window_utc_crosses_midnight_and_is_bounded(self):
        policy = StorageCompressionConfig(
            window={
                "enabled": True,
                "weekday": 6,
                "start_hour": 23,
                "duration_minutes": 120,
            }
        )
        sunday = datetime(2026, 10, 18, 23, 30, tzinfo=UTC).timestamp()
        monday = datetime(2026, 10, 19, 0, 30, tzinfo=UTC).timestamp()
        self.assertEqual(weekly_window(policy, sunday), weekly_window(policy, monday))
        self.assertIsNone(weekly_window(policy, monday + 1800))
        self.assertIsNone(weekly_window(StorageCompressionConfig(), sunday))

    def test_fixed_rate_rejects_variable_packet_cadence(self):
        probe = {
            "video": {"index": 0, "r_frame_rate": "25/1", "avg_frame_rate": "25/1"},
            "packets": [
                {"stream_index": 0, "duration_time": "0.04", "pts_time": str(i * 0.04)}
                for i in range(3)
            ],
        }
        self.assertEqual(fixed_rate(probe), Fraction(25))
        probe["packets"][1]["pts_time"] = "0.06"
        with self.assertRaisesRegex(CompressionError, "variable-frame-rate"):
            fixed_rate(probe)


class CompressionFixture(AudioRepairFixture):
    def setUp(self):
        super().setUp()
        self.enterContext(self.db.bind_ctx([RecordingLease, ArchiveTransfer]))
        self.db.create_tables([RecordingLease, ArchiveTransfer])
        self.assertTrue(reservation_schema_ready(self.db))
        for module in (
            "frigate.record.compression_storage",
            "frigate.record.audio_repair",
            "frigate.record.leases",
        ):
            self.enterContext(patch(f"{module}.storage_lock", nullcontext))
        self.engine = RecordingCompression(
            self.db,
            self.recordings,
            self.cache,
            self.backup,
            self.ffmpeg,
            self.ffprobe,
            timeout=30,
            online=True,
            policy=StorageCompressionConfig(enabled=True, min_free_gib=0.25),
            readers_leased=True,
        )

    def protect(self, row, event_id):
        return Event.create(
            id=event_id,
            label="person",
            camera=row.camera,
            start_time=row.start_time,
            end_time=row.end_time,
            retain_indefinitely=True,
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

    def software_fixture_command(self, source, target, probe):
        """Generate a verified fixture, never a runtime encoder fallback."""
        command = [
            self.ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-copyts",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c:v",
            "libx265",
            "-preset",
            "ultrafast",
            "-crf",
            "40",
            "-x265-params",
            "log-level=error:pools=none:frame-threads=1:bframes=0",
            "-c:a",
            "copy",
            "-fps_mode",
            "cfr",
            "-r",
            str(fixed_rate(probe)),
            "-avoid_negative_ts",
            "disabled",
            str(target),
        ]
        return command


class TestCompression(CompressionFixture):
    def test_selected_encoders_use_matching_codec_tags_and_variable_bitrate(self):
        row = self.clip(codec="libx264", audio=False)
        source = Path(row.path)
        original = self.engine.probe(source)
        for encoder, codec, tag in (
            ("h264_nvenc", "h264", "avc1"),
            ("hevc_nvenc", "hevc", "hvc1"),
            ("av1_nvenc", "av1", "av01"),
            ("libx264", "h264", "avc1"),
            ("libx265", "hevc", "hvc1"),
            ("libsvtav1", "av1", "av01"),
            ("libaom-av1", "av1", "av01"),
        ):
            with self.subTest(encoder=encoder):
                self.engine.policy.encoder = encoder
                self.engine.policy.target_mbps = 5.5
                command = self.engine.command(
                    source, source.with_suffix(".output.mp4"), original
                )
                self.assertEqual(command[command.index("-c:v") + 1], encoder)
                self.assertEqual(command[command.index("-tag:v") + 1], tag)
                self.assertEqual(command[command.index("-b:v") + 1], "5500000")
                self.assertEqual(self.engine.policy.video_codec, codec)
                self.assertNotIn("-vf", command)
                if encoder == "libsvtav1":
                    self.assertNotIn("-maxrate", command)
                    self.assertNotIn("-bufsize", command)

    def test_explicit_frame_rate_is_required_to_normalize_variable_input(self):
        row = self.clip(codec="libx264", audio=False)
        source = Path(row.path)
        original = self.engine.probe(source)
        original["video"]["avg_frame_rate"] = "9/1"
        with self.assertRaisesRegex(CompressionError, "variable-frame-rate"):
            self.engine.command(source, source.with_suffix(".output.mp4"), original)
        self.engine.policy.frame_rate = 16
        self.engine.policy.encoder = "libsvtav1"
        command = self.engine.command(
            source, source.with_suffix(".output.mp4"), original
        )
        self.assertEqual(command[command.index("-r") + 1], "16")
        self.assertEqual(command[command.index("-vf") + 1], "fps=16,trim=end_frame=19")
        self.assertNotIn("-frames:v", command)

    def test_real_cpu_av1_commands_validate_and_store_the_selected_codec(self):
        for encoder, frame_rate, audio in (
            ("libsvtav1", None, False),
            ("libaom-av1", None, False),
            ("libsvtav1", 16, False),
            ("libsvtav1", None, True),
            ("libaom-av1", None, True),
            ("libsvtav1", 16, True),
        ):
            with self.subTest(encoder=encoder, frame_rate=frame_rate, audio=audio):
                row = self.clip(codec="libx264", audio=audio)
                source = Path(row.path)
                original = self.engine.probe(source)
                self.engine.policy.encoder = encoder
                self.engine.policy.target_mbps = 0.1
                self.engine.policy.cpu_threads = 2
                self.engine.policy.frame_rate = frame_rate
                target = source.with_suffix(".output.mp4")
                self.engine._run(self.engine.command(source, target, original))
                metadata = self.engine.validate(
                    original, self.engine.probe(target), target
                )
                self.assertEqual(metadata["video_codec"], "av1")
                self.assertEqual(
                    fixed_rate(self.engine.probe(target)), Fraction(frame_rate or 10)
                )
                self.assertEqual(Recordings.get_by_id(row.id).video_codec, "h264")
                target.unlink()

    def test_gpu_command_preserves_geometry_fixed_rate_and_absent_audio(self):
        row = self.clip(codec="libx264", audio=False)
        source = Path(row.path)
        command = self.engine.command(
            source, source.with_suffix(".output.mp4"), self.engine.probe(source)
        )
        self.assertEqual(command[command.index("-c:v") + 1], "hevc_nvenc")
        self.assertEqual(command[command.index("-rc") + 1], "vbr")
        self.assertEqual(command[command.index("-r") + 1], "10")
        self.assertIn("-an", command)
        self.assertNotIn("-vf", command)
        self.assertNotIn("libx265", command)

    def test_verified_smaller_output_preserves_capture_age_and_audio_presence(self):
        for audio in (False, True):
            with self.subTest(audio=audio):
                row = self.clip(codec="libx264", audio=audio)
                original = _digest(Path(row.path))
                snapshot = dict(row.__data__)
                with patch.object(
                    self.engine, "command", self.software_fixture_command
                ):
                    outcome, saved = self.engine.compress(row)
                self.assertEqual(outcome, "compressed")
                self.assertGreater(saved, 0)
                current = Recordings.get_by_id(row.id)
                self.assertEqual(current.start_time, snapshot["start_time"])
                self.assertEqual(current.end_time, snapshot["end_time"])
                self.assertEqual(current.storage_tier, "local")
                self.assertEqual(current.video_codec, "hevc")
                self.assertEqual(current.has_audio, audio)
                self.assertEqual(current.keyframes[0], 0)
                data = json.loads(self.engine.last_journal.read_text())
                self.assertEqual(data["state"], "cleaned")
                self.assertEqual(data["source_hash"], original)
                self.assertGreater(data["actual_saved_bytes"], 0)
                self.assertFalse(
                    list(Path(row.path).parent.glob(".frigate-compression-*"))
                )
                self.assertFalse(ArchiveTransfer.select().exists())

    def test_protection_age_active_reader_and_completion_fail_closed(self):
        row = self.clip(codec="libx264")
        self.protect(row, "saved")
        self.assertEqual(self.engine.skip_reason(row), "protected")
        Event.delete().execute()
        RecordingLease.create(
            id="reader",
            camera=row.camera,
            start_time=row.start_time,
            end_time=row.end_time,
            expires=time.time() + 60,
        )
        self.assertEqual(self.engine.skip_reason(row), "active-reader")
        RecordingLease.delete().execute()
        self.engine.completed = lambda _id: True
        self.assertEqual(self.engine.skip_reason(row), "already-compressed")
        self.engine.completed = lambda _id: False
        row.end_time = time.time() - 3600
        self.assertEqual(self.engine.skip_reason(row), "too-young")
        self.engine.readers_leased = False
        self.assertEqual(self.engine.skip_reason(row), "reader-leases-not-certified")

    def test_gpu_failure_never_falls_back_and_original_remains(self):
        row = self.clip(codec="libx264")
        before = _digest(Path(row.path))
        real_run = self.engine._run
        encodes = []

        def fail_gpu(command):
            if "hevc_nvenc" in command:
                encodes.append(command)
                raise RepairError("GPU unavailable")
            return real_run(command)

        with (
            patch.object(self.engine, "_run", fail_gpu),
            self.assertRaises(RepairError),
        ):
            self.engine.compress(row)
        self.assertEqual(len(encodes), 1)
        self.assertEqual(_digest(Path(row.path)), before)
        self.assertEqual(
            json.loads(self.engine.last_journal.read_text())["state"],
            "rolled-back-cleaned",
        )

    def test_not_smaller_output_is_never_published(self):
        row = self.clip(codec="libx264")
        before = _digest(Path(row.path))
        real_run = self.engine._run

        def same_size(command):
            if "hevc_nvenc" in command:
                Path(command[-1]).write_bytes(Path(row.path).read_bytes())
                return b""
            return real_run(command)

        with patch.object(self.engine, "_run", same_size):
            self.assertEqual(self.engine.compress(row), ("not-smaller", 0))
        self.assertEqual(_digest(Path(row.path)), before)

    def test_db_failure_rolls_back_replaced_file(self):
        row = self.clip(codec="libx264")
        before = _digest(Path(row.path))
        real_update = self.engine._update
        failed = False

        def fail_once(recording_id, metadata):
            nonlocal failed
            if not failed:
                failed = True
                raise RepairError("Injected metadata failure")
            return real_update(recording_id, metadata)

        with (
            patch.object(self.engine, "command", self.software_fixture_command),
            patch.object(self.engine, "_update", fail_once),
            self.assertRaises(RepairError),
        ):
            self.engine.compress(row)
        self.assertTrue(failed)
        self.assertEqual(_digest(Path(row.path)), before)
        self.assertEqual(Recordings.get_by_id(row.id).video_codec, "h264")

    def test_late_reader_and_late_saved_evidence_block_publication(self):
        for protection in ("lease", "event"):
            with self.subTest(protection=protection):
                row = self.clip(codec="libx264")
                original = _digest(Path(row.path))
                real_validate = self.engine.validate

                def protect(*args):
                    metadata = real_validate(*args)
                    if protection == "lease":
                        RecordingLease.create(
                            id="late-reader",
                            camera=row.camera,
                            start_time=row.start_time,
                            end_time=row.end_time,
                            expires=time.time() + 60,
                        )
                    else:
                        self.protect(row, "late-event")
                    return metadata

                with (
                    patch.object(self.engine, "command", self.software_fixture_command),
                    patch.object(self.engine, "validate", protect),
                    self.assertRaises(RepairError),
                ):
                    self.engine.compress(row)
                self.assertTrue(
                    RecordingLease.select()
                    .where(RecordingLease.id == "late-reader")
                    .exists()
                    if protection == "lease"
                    else Event.select().where(Event.id == "late-event").exists()
                )
                self.assertEqual(_digest(Path(row.path)), original)
                RecordingLease.delete().execute()
                Event.delete().execute()

    def test_crash_after_replace_is_recovered_from_durable_original(self):
        row = self.clip(codec="libx264")
        original = _digest(Path(row.path))
        with (
            patch.object(self.engine, "command", self.software_fixture_command),
            patch.object(
                self.engine, "_update", side_effect=SystemExit("simulated crash")
            ),
            self.assertRaises(SystemExit),
        ):
            self.engine.compress(row)
        self.assertNotEqual(_digest(Path(row.path)), original)
        self.assertEqual(self.engine.recover(self.engine.last_journal), "rolled-back")
        self.assertEqual(_digest(Path(row.path)), original)
        self.assertEqual(Recordings.get_by_id(row.id).video_codec, "h264")

    def test_disk_headroom_failure_does_not_start_encoder_or_create_journal(self):
        row = self.clip(codec="libx264")
        original = _digest(Path(row.path))
        usage = SimpleNamespace(free=1)
        with (
            patch(
                "frigate.record.compression_storage.shutil.disk_usage",
                return_value=usage,
            ),
            self.assertRaises(RepairError),
        ):
            self.engine.compress(row)
        self.assertEqual(_digest(Path(row.path)), original)
        self.assertFalse(self.engine.last_journal.exists())

    def test_stop_during_encoder_preserves_original_and_rolls_back_intent(self):
        row = self.clip(codec="libx264")
        original = _digest(Path(row.path))
        real_run = self.engine._run

        def stop_encoder(command):
            if "hevc_nvenc" in command:
                self.engine.cancel.set()
            return real_run(command)

        with (
            patch.object(self.engine, "_run", stop_encoder),
            self.assertRaises(RepairError),
        ):
            self.engine.compress(row)
        self.assertEqual(_digest(Path(row.path)), original)
        self.assertEqual(
            json.loads(self.engine.last_journal.read_text())["state"],
            "rolled-back-cleaned",
        )

    def test_validation_rejects_vfr_changed_resolution_and_missing_audio(self):
        row = self.clip(codec="libx264")
        original = self.engine.probe(Path(row.path))
        for field, value in (("width", 720), ("avg_frame_rate", "9/1")):
            modified = deepcopy(original)
            modified["video"][field] = value
            with self.assertRaises(RepairError):
                self.engine.validate(original, modified, Path(row.path))


class TestArchiveCompression(CompressionFixture):
    def setUp(self):
        super().setUp()
        self.archive = self.root / "archive"
        self.archive.mkdir()
        (self.archive / ".frigate-archive").write_text("test-archive")
        self.settings = SimpleNamespace(
            enabled=True,
            archive_path=str(self.archive),
            mount_id="test-archive",
            max_archive_gib=None,
        )
        self.engine.archive_settings_provider = lambda: self.settings

        @contextmanager
        def pinned(root, mount_id):
            verify(root, mount_id)
            yield root

        def verify(root, mount_id):
            if (root / ".frigate-archive").read_text() != mount_id:
                raise OSError("Test mount identity mismatch")

        self.enterContext(
            patch("frigate.record.compression_storage.pin_archive", pinned)
        )
        self.enterContext(
            patch("frigate.record.compression_storage.validate_mount", verify)
        )

    def archived_clip(self, audio=False):
        row = self.clip(codec="libx264", audio=audio)
        logical = Path(row.path)
        physical = self.archive / logical.relative_to(self.recordings)
        logical.replace(physical)
        try:
            logical.symlink_to(physical)
        except OSError:
            # Windows validation lacks symlink privileges. Only the stable-link
            # facade is mocked; NAS media, SQLite, encoding and fsync remain real.
            logical.touch()
            is_symlink, readlink = Path.is_symlink, Path.readlink
            self.enterContext(
                patch.object(
                    Path,
                    "is_symlink",
                    lambda path: True if path == logical else is_symlink(path),
                )
            )
            self.enterContext(
                patch.object(
                    Path,
                    "readlink",
                    lambda path: physical if path == logical else readlink(path),
                )
            )
        Recordings.update(
            storage_tier="archive",
            archive_bytes=physical.stat().st_size,
        ).where(Recordings.id == row.id).execute()
        return Recordings.get_by_id(row.id), physical

    def test_archive_replace_updates_exact_bytes_keeps_stable_link_and_frees_backup(
        self,
    ):
        row, physical = self.archived_clip(audio=True)
        original_size = physical.stat().st_size
        link = Path(row.path)
        original_link = link.lstat()
        with patch.object(self.engine, "command", self.software_fixture_command):
            outcome, saved = self.engine.compress(row)
        current = Recordings.get_by_id(row.id)
        self.assertEqual(outcome, "compressed")
        self.assertGreater(saved, 0)
        self.assertLess(current.archive_bytes, original_size)
        self.assertEqual(current.archive_bytes, physical.stat().st_size)
        self.assertEqual(current.segment_size, current.archive_bytes / 2**20)
        self.assertEqual(current.start_time, row.start_time)
        self.assertEqual(current.end_time, row.end_time)
        self.assertEqual(link.readlink(), physical)
        self.assertEqual(link.lstat().st_ino, original_link.st_ino)
        self.assertFalse(list(self.archive.glob(".frigate-compression-*")))
        self.assertFalse(ArchiveTransfer.select().exists())
        data = json.loads(self.engine.last_journal.read_text())
        self.assertEqual(data["state"], "cleaned")
        self.assertEqual(data["actual_saved_bytes"], saved)

    def test_archive_offline_does_not_create_output_backup_or_reservation(self):
        row, physical = self.archived_clip()
        original = _digest(physical)
        with (
            patch(
                "frigate.record.compression_storage.pin_archive",
                side_effect=OSError("Offline"),
            ),
            self.assertRaises(OSError),
        ):
            self.engine.compress(row)
        self.assertEqual(_digest(physical), original)
        self.assertFalse(ArchiveTransfer.select().exists())
        self.assertFalse(list(self.archive.glob(".frigate-compression-*")))

    def test_unknown_archive_bytes_waits_for_existing_accounting_backfill(self):
        row, physical = self.archived_clip()
        original = _digest(physical)
        row.archive_bytes = None
        self.assertEqual(self.engine.compress(row), ("archive-accounting-pending", 0))
        self.assertEqual(_digest(physical), original)
        self.assertFalse(ArchiveTransfer.select().exists())

    def test_archive_crash_after_replace_restores_original_and_exact_accounting(self):
        row, physical = self.archived_clip()
        original = _digest(physical)
        original_size = physical.stat().st_size
        with (
            patch.object(self.engine, "command", self.software_fixture_command),
            patch.object(
                self.engine, "_update", side_effect=SystemExit("simulated crash")
            ),
            self.assertRaises(SystemExit),
        ):
            self.engine.compress(row)
        self.assertNotEqual(_digest(physical), original)
        self.assertEqual(Recordings.get_by_id(row.id).archive_bytes, original_size)
        self.assertTrue(ArchiveTransfer.select().exists())
        self.assertEqual(self.engine.recover(self.engine.last_journal), "rolled-back")
        self.assertEqual(_digest(physical), original)
        self.assertEqual(Recordings.get_by_id(row.id).archive_bytes, original_size)
        self.assertFalse(ArchiveTransfer.select().exists())

    def test_archive_budget_does_not_evict_or_override_capture_age(self):
        row, physical = self.archived_clip()
        original = _digest(physical)
        self.settings.max_archive_gib = physical.stat().st_size / 2**30
        with (
            patch.object(self.engine, "command", self.software_fixture_command),
            self.assertRaises(RepairError),
        ):
            self.engine.compress(row)
        self.assertEqual(_digest(physical), original)
        self.assertFalse(ArchiveTransfer.select().exists())

    def test_archive_configuration_change_before_commit_rolls_back_and_releases(self):
        row, physical = self.archived_clip()
        original = _digest(physical)
        validate = self.engine.validate

        def change(*args):
            metadata = validate(*args)
            self.settings.mount_id = "changed-config"
            return metadata

        with (
            patch.object(self.engine, "command", self.software_fixture_command),
            patch.object(self.engine, "validate", change),
            self.assertRaises(RepairError),
        ):
            self.engine.compress(row)
        self.assertEqual(_digest(physical), original)
        self.assertEqual(
            Recordings.get_by_id(row.id).archive_bytes, physical.stat().st_size
        )
        self.assertFalse(ArchiveTransfer.select().exists())
        self.assertFalse(list(self.archive.glob(".frigate-compression-*")))

    def test_mount_swap_never_writes_new_volume_and_retains_old_original(self):
        row, physical = self.archived_clip()
        original = _digest(physical)
        old_root = self.root / "detached-archive"
        validate = self.engine.validate

        def swap(*args):
            metadata = validate(*args)
            self.archive.rename(old_root)
            self.archive.mkdir()
            (self.archive / ".frigate-archive").write_text("test-archive")
            return metadata

        with (
            patch.object(self.engine, "command", self.software_fixture_command),
            patch.object(self.engine, "validate", swap),
            self.assertRaises((RepairError, OSError)),
        ):
            self.engine.compress(row)
        self.assertEqual(_digest(old_root / physical.name), original)
        self.assertEqual(
            [path.name for path in self.archive.iterdir()], [".frigate-archive"]
        )
        self.assertTrue(ArchiveTransfer.select().exists())
        self.assertIn(
            json.loads(self.engine.last_journal.read_text())["state"],
            ("preparing", "ready"),
        )

    def test_archive_late_protection_blocks_lossy_publication(self):
        row, physical = self.archived_clip()
        original = _digest(physical)
        validate = self.engine.validate

        def protect(*args):
            metadata = validate(*args)
            self.protect(row, "late-archive-protection")
            return metadata

        with (
            patch.object(self.engine, "command", self.software_fixture_command),
            patch.object(self.engine, "validate", protect),
            self.assertRaises(RepairError),
        ):
            self.engine.compress(row)
        self.assertEqual(_digest(physical), original)
        self.assertFalse(ArchiveTransfer.select().exists())

    def test_no_allocated_savings_keeps_original_and_reports_zero(self):
        row, physical = self.archived_clip()
        original = _digest(physical)
        with (
            patch.object(self.engine, "command", self.software_fixture_command),
            patch(
                "frigate.record.compression_storage.allocated_bytes", return_value=65536
            ),
        ):
            self.assertEqual(self.engine.compress(row), ("no-allocation-savings", 0))
        self.assertEqual(_digest(physical), original)
        self.assertFalse(ArchiveTransfer.select().exists())

    def test_backup_cleanup_failure_reports_no_savings_and_recovers_owned_artifacts(
        self,
    ):
        row, physical = self.archived_clip()
        original_size = physical.stat().st_size
        remove = self.engine.storage._remove_owned

        def blocked(path, expected_hash=None):
            if path.name.endswith(".original.mp4"):
                raise OSError("Injected backup cleanup failure")
            return remove(path, expected_hash)

        with (
            patch.object(self.engine, "command", self.software_fixture_command),
            patch.object(self.engine.storage, "_remove_owned", blocked),
            self.assertRaises(OSError),
        ):
            self.engine.compress(row)
        data = json.loads(self.engine.last_journal.read_text())
        self.assertEqual(data["state"], "committed")
        self.assertNotIn("actual_saved_bytes", data)
        self.assertLess(physical.stat().st_size, original_size)
        self.assertTrue(ArchiveTransfer.select().exists())
        self.assertTrue(
            list(self.archive.glob("*.original.mp4"))
            or list(self.archive.glob(".*.original.mp4"))
        )
        self.assertEqual(self.engine.recover(self.engine.last_journal), "cleaned")
        self.assertFalse(ArchiveTransfer.select().exists())
        self.assertFalse(list(self.archive.glob(".frigate-compression-*")))
        self.assertGreater(
            json.loads(self.engine.last_journal.read_text())["actual_saved_bytes"], 0
        )

    def test_other_transfer_blocks_compression_without_touching_source(self):
        row, physical = self.archived_clip()
        original = _digest(physical)
        ArchiveTransfer.create(
            id="other", path=str(self.recordings / "other.mp4"), size=123
        )
        with (
            patch.object(self.engine, "command", self.software_fixture_command),
            self.assertRaises(RepairError),
        ):
            self.engine.compress(row)
        self.assertEqual(_digest(physical), original)
        self.assertEqual(ArchiveTransfer.get_by_id("other").size, 123)

    def test_recovery_waits_for_reader_before_restoring_original(self):
        row, physical = self.archived_clip()
        original = _digest(physical)
        with (
            patch.object(self.engine, "command", self.software_fixture_command),
            patch.object(
                self.engine, "_update", side_effect=SystemExit("simulated crash")
            ),
            self.assertRaises(SystemExit),
        ):
            self.engine.compress(row)
        output = _digest(physical)
        RecordingLease.create(
            id="new-reader",
            camera=row.camera,
            start_time=row.start_time,
            end_time=row.end_time,
            expires=time.time() + 60,
        )
        with self.assertRaisesRegex(RepairError, "active readers"):
            self.engine.recover(self.engine.last_journal)
        self.assertEqual(_digest(physical), output)
        self.assertTrue(ArchiveTransfer.select().exists())
        RecordingLease.delete().execute()
        self.engine.recover(self.engine.last_journal)
        self.assertEqual(_digest(physical), original)
        self.assertFalse(ArchiveTransfer.select().exists())


class TestCompressionManager(CompressionFixture):
    def test_job_policy_keeps_encoder_and_resource_snapshot(self):
        policy = self.config.storage.compression
        policy.encoder = "libsvtav1"
        policy.cpu_threads = 2
        policy.svt_preset = 10
        policy.frame_rate = 16
        policy.target_mbps = 5.5
        job = self.manager.create_job()
        policy.encoder = "libx264"
        policy.target_mbps = 1
        policy.cpu_threads = 4
        current = self.manager.get_job(job["id"])
        self.assertEqual(current["encoder"], "libsvtav1")
        self.assertEqual(current["video_codec"], "av1")
        self.assertEqual(current["target_mbps"], 5.5)
        self.assertEqual(current["cpu_threads"], 2)
        self.assertEqual(current["frame_rate"], 16)
        self.assertEqual(current["svt_preset"], 10)

    def setUp(self):
        super().setUp()
        capture = SimpleNamespace(pre_capture=0, post_capture=0)
        record = SimpleNamespace(alerts=capture, detections=capture)
        self.config = SimpleNamespace(
            database=SimpleNamespace(path=self.db.database),
            cameras={"front": SimpleNamespace(record=record, ffmpeg=SimpleNamespace())},
            storage=SimpleNamespace(
                compression=StorageCompressionConfig(enabled=True, max_files=1)
            ),
        )
        self.manager = self.make_manager()
        asyncio.run(self.manager.start())
        self.addCleanup(lambda: asyncio.run(self.manager.close()))

    def make_manager(self):
        return CompressionManager(
            lambda: self.config,
            root=self.root / "jobs",
            recordings_root=self.recordings,
            cache_root=self.cache,
            readers_leased=True,
            ffmpeg=self.ffmpeg,
            ffprobe=self.ffprobe,
        )

    def wait(self):
        self.manager._future.result(timeout=30)
        return self.manager.list_jobs()["jobs"][0]

    def test_disabled_and_uncertified_readers_block_job_creation(self):
        self.config.storage.compression.enabled = False
        with self.assertRaises(CompressionConflict):
            self.manager.create_job()
        self.config.storage.compression.enabled = True
        self.manager.readers_leased = False
        with self.assertRaises(CompressionConflict):
            self.manager.create_job()

    def test_real_media_commit_records_only_post_cleanup_net_savings(self):
        row = self.clip(codec="libx264")
        fixture_command = self.software_fixture_command

        def command(engine, source, target, probe):
            return fixture_command(source, target, probe)

        with patch.object(RecordingCompression, "command", command):
            self.manager.create_job()
            summary = self.wait()
        self.assertEqual(summary["compressed"], 1)
        self.assertEqual(summary["failed"], 0)
        self.assertGreater(summary["saved_bytes"], 0)
        self.assertTrue(self.manager._completed(row.id))
        self.assertFalse(ArchiveTransfer.select().exists())
        self.assertFalse(list(self.recordings.glob(".frigate-compression-*")))
        items = self.manager.list_items(summary["id"])
        self.assertEqual(items["total"], 1)
        self.assertEqual(items["items"][0]["saved_bytes"], summary["saved_bytes"])

    def test_cleanup_pending_reports_zero_then_restart_reconciles_audit(self):
        self.clip(codec="libx264")
        fixture_command = self.software_fixture_command
        remove = CompressionStorage._remove_owned

        def command(engine, source, target, probe):
            return fixture_command(source, target, probe)

        def blocked(path, expected_hash=None):
            if path.name.endswith(".original.mp4"):
                raise OSError("Injected cleanup failure")
            return remove(path, expected_hash)

        with (
            patch.object(RecordingCompression, "command", command),
            patch.object(CompressionStorage, "_remove_owned", staticmethod(blocked)),
        ):
            job = self.manager.create_job()
            failed = self.wait()
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["failed"], 1)
        self.assertEqual(failed["saved_bytes"], 0)
        self.assertTrue(self.manager.recovery_required)
        with self.assertRaises(CompressionConflict):
            self.manager.create_job()
        other = self.make_manager()
        try:
            asyncio.run(other.start())
            recovered = other.get_job(job["id"])
            self.assertFalse(other.recovery_required)
            self.assertEqual(recovered["status"], "interrupted")
            self.assertEqual(recovered["compressed"], 1)
            self.assertEqual(recovered["failed"], 0)
            self.assertGreater(recovered["saved_bytes"], 0)
            self.assertFalse(ArchiveTransfer.select().exists())
        finally:
            asyncio.run(other.close())

    def test_file_limit_and_permanent_completion_survive_policy_change(self):
        rows = [self.clip(codec="libx264") for _ in range(2)]

        def converted(engine, row):
            journal = engine.backup / f"{row.id}.json"
            engine.backup.mkdir(parents=True, exist_ok=True)
            engine.last_journal = journal
            journal.write_text(
                json.dumps(
                    {
                        "kind": "compression",
                        "state": "committed",
                        "row": {"id": row.id},
                    }
                )
            )
            return "compressed", 123

        with patch.object(RecordingCompression, "compress", converted):
            self.manager.create_job()
            first = self.wait()
            self.assertEqual(first["processed"], 1)
            self.assertEqual(first["compressed"], 1)
            self.config.storage.compression.target_mbps = 2
            self.manager.create_job()
            second = self.wait()
            self.assertEqual(second["compressed"], 1)
            self.manager.create_job()
            third = self.wait()
            self.assertEqual(third["compressed"], 0)
        self.assertTrue(all(self.manager._completed(row.id) for row in rows))

    def test_pause_resume_stop_preserve_completed_work(self):
        self.config.storage.compression.max_files = 10
        for _ in range(3):
            self.clip(codec="libx264")
        entered, release = threading.Event(), threading.Event()
        calls = []

        def blocked(engine, row):
            calls.append(row.id)
            entered.set()
            release.wait(timeout=10)
            return "not-smaller", 0

        with patch.object(RecordingCompression, "compress", blocked):
            job = self.manager.create_job()
            self.assertTrue(entered.wait(10))
            self.manager.control(job["id"], "pause")
            release.set()
            self.assertEqual(self.wait()["status"], "paused")
            self.assertEqual(len(calls), 1)
            self.manager.control(job["id"], "resume")
            self.wait()
            self.assertEqual(len(calls), 3)
        self.assertEqual(self.manager.get_job(job["id"])["processed"], 3)

    def test_weekly_slot_is_durable_and_only_one_batch_is_started(self):
        window = ("2026-10-18T02:00:00+00:00", time.time() + 3600)
        self.manager.create_job(window=window)
        self.wait()
        with self.assertRaises(CompressionConflict):
            self.manager.create_job(window=window)

    def test_restart_does_not_resume_paused_job(self):
        with self.manager._state() as db:
            db.execute(
                "INSERT INTO jobs(id,status,created_at,updated_at,policy,scheduled) VALUES('old','paused',?,?,?,0)",
                (
                    time.time(),
                    time.time(),
                    self.config.storage.compression.model_dump_json(),
                ),
            )
        other = self.make_manager()
        try:
            asyncio.run(other.start())
            self.assertEqual(other.get_job("old")["status"], "interrupted")
            self.assertIsNone(other._future)
        finally:
            asyncio.run(other.close())

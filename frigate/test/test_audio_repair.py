"""Generated media only: offline repair safety, packet identity, and recovery."""

import json
import os
import shutil
import subprocess
import sys
import unittest
import uuid
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from peewee import OperationalError, SqliteDatabase

from frigate.models import Event, Recordings
from frigate.record.audio_repair import AudioRepair, RepairError, _digest, main


class AudioRepairFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = os.environ.get("AUDIO_REPAIR_FFMPEG") or shutil.which("ffmpeg")
        cls.ffprobe = os.environ.get("AUDIO_REPAIR_FFPROBE") or shutil.which("ffprobe")
        if not cls.ffmpeg or not cls.ffprobe:
            raise unittest.SkipTest(
                "FFmpeg and ffprobe required for generated-media tests"
            )

    def setUp(self):
        # Keep all generated fixtures inside the checkout, never in system temp.
        self.root = Path.cwd() / f".audio-repair-test-{uuid.uuid4().hex}"
        self.root.mkdir()
        self.addCleanup(lambda: shutil.rmtree(self.root))
        self.recordings = self.root / "recordings"
        self.recordings.mkdir()
        self.cache = self.root / "cache"
        self.backup = self.root / "backups"
        self.db = SqliteDatabase(str(self.root / "index.db"))
        self.enterContext(self.db.bind_ctx([Recordings, Event]))
        self.db.connect()
        self.addCleanup(self.db.close)
        self.db.create_tables([Recordings, Event])
        self.enterContext(patch("frigate.record.audio_repair.assert_offline"))
        self.repair = AudioRepair(
            self.db,
            self.recordings,
            self.cache,
            self.backup,
            self.ffmpeg,
            self.ffprobe,
            timeout=30,
        )

    def clip(self, codec="libx265", audio=True, rate=16000, channels=1, offset=0):
        path = self.recordings / f"{uuid.uuid4().hex}.mp4"
        command = [
            self.ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x96:rate=10:duration=1.2",
        ]
        if audio:
            command += [
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=500:sample_rate={rate}:duration=1.2",
            ]
        command += ["-c:v", codec, "-preset", "ultrafast", "-threads", "1"]
        if codec == "libx265":
            command += ["-x265-params", "log-level=error:pools=none:frame-threads=1"]
        if audio:
            command += ["-c:a", "aac", "-ar", str(rate), "-ac", str(channels)]
        if offset:
            command += ["-output_ts_offset", str(offset)]
        command += [str(path)]
        result = subprocess.run(command, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        row = Recordings.create(
            id=path.stem,
            camera="front",
            path=str(path),
            start_time=100.0,
            end_time=101.2,
            duration=1.2,
            keyframes=[0],
            has_audio=audio,
            audio_rate=rate if audio else None,
            audio_codec="aac" if audio else None,
            video_codec="hevc" if codec == "libx265" else "h264",
        )
        return Recordings.get_by_id(row.id)

    def journal(self):
        return next(self.backup.glob("*.json"))


class TestAudioRepair(AudioRepairFixture):
    def test_hevc_and_h264_audio_only_preserve_video_packets_and_metadata(self):
        for codec in ("libx265", "libx264"):
            with self.subTest(codec=codec):
                row = self.clip(codec)
                before_row = dict(row.__data__)
                before = self.repair.probe(Path(row.path))
                original_hash = _digest(Path(row.path))
                self.assertEqual(self.repair.repair(row, apply=True), "repaired")
                current = Recordings.get_by_id(row.id)
                after = self.repair.probe(Path(row.path))
                # Also runs a full decode with fatal errors enabled.
                metadata = self.repair.validate(before, after, Path(row.path))
                self.assertEqual(current.audio_rate, 48000)
                self.assertEqual(after["audio"]["channels"], 2)
                self.assertEqual(after["audio"]["profile"], "LC")
                for key, value in before_row.items():
                    if key not in metadata:
                        self.assertEqual(current.__data__[key], value)
                self.assertEqual(current.keyframes, metadata["keyframes"])
                self.assertEqual(
                    current.segment_size, Path(row.path).stat().st_size / 2**20
                )
                journal = next(
                    entry
                    for entry in self.backup.glob("*.json")
                    if json.loads(entry.read_text())["row"]["id"] == row.id
                )
                data = json.loads(journal.read_text())
                self.assertEqual(data["state"], "committed")
                self.assertEqual(_digest(Path(data["backup"])), original_hash)

    def test_dry_run_never_changes_files_or_database(self):
        row = self.clip()
        original = _digest(Path(row.path))
        snapshot = dict(row.__data__)
        self.assertEqual(self.repair.repair(row), "would-repair")
        self.assertEqual(_digest(Path(row.path)), original)
        self.assertEqual(Recordings.get_by_id(row.id).__data__, snapshot)
        self.assertFalse(self.backup.exists())

    def test_nonzero_original_timestamps_are_preserved(self):
        row = self.clip(offset=2)
        before = self.repair.probe(Path(row.path))
        self.assertGreater(float(before["video"]["start_time"]), 1)
        self.assertEqual(self.repair.repair(row, apply=True), "repaired")
        self.repair.validate(before, self.repair.probe(Path(row.path)), Path(row.path))

    def test_video_hash_timestamp_and_audio_alignment_changes_are_rejected(self):
        row = self.clip()
        before = self.repair.probe(Path(row.path))
        self.repair.repair(row, apply=True)
        after = self.repair.probe(Path(row.path))
        for field, expected in (
            ("data_hash", "bitstream"),
            ("pts_time", "timing"),
        ):
            changed = deepcopy(after)
            packet = self.repair._packets(changed, changed["video"])[0]
            packet[field] = (
                "invalid" if field == "data_hash" else str(float(packet[field]) + 0.1)
            )
            with self.assertRaisesRegex(RepairError, expected):
                self.repair.validate(before, changed, Path(row.path))
        changed = deepcopy(after)
        changed["audio"]["start_time"] = "1"
        with self.assertRaisesRegex(RepairError, "alignment"):
            self.repair.validate(before, changed, Path(row.path))
        changed = deepcopy(after)
        packet = self.repair._packets(changed, changed["audio"])[2]
        packet["pts_time"] = str(float(packet["pts_time"]) + 0.1)
        with self.assertRaisesRegex(RepairError, "Discontinuous"):
            self.repair.validate(before, changed, Path(row.path))

    def test_no_audio_is_reported_never_synthesized(self):
        row = self.clip(audio=False)
        self.assertEqual(self.repair.repair(row, apply=True), "skip:no-audio")
        self.assertFalse(self.backup.exists())
        self.assertIsNone(self.repair.probe(Path(row.path))["audio"])

    def test_compatible_audio_is_skipped(self):
        row = self.clip(rate=48000, channels=2)
        self.assertEqual(self.repair.repair(row, apply=True), "skip:already-compatible")

    def test_archive_deletion_completed_and_outside_paths_are_skipped(self):
        row = self.clip()
        for field, value, reason in (
            ("storage_tier", "archive", "archived"),
            ("delete_requested", True, "deletion-pending"),
            ("end_time", 9999999999.0, "not-completed"),
            ("path", str(self.root / "export.mp4"), "outside-recordings"),
        ):
            with self.subTest(reason=reason):
                candidate = Recordings(**row.__data__)
                setattr(candidate, field, value)
                self.assertEqual(
                    self.repair.repair(candidate, apply=True), f"skip:{reason}"
                )

    def test_saved_event_padding_is_respected(self):
        row = self.clip()
        # A saved event beginning after the segment still protects its pre-roll.
        self.db.execute_sql(
            """INSERT INTO event
            (id, camera, label, start_time, end_time, top_score, score,
             false_positive, zones, thumbnail, has_clip, has_snapshot, region,
             box, area, retain_indefinitely, ratio, plus_id, model_hash,
             detector_type, model_type, data)
            VALUES ('saved', 'front', 'person', 120, 125, 1, 1, 0, '[]', '',
                    0, 0, '[]', '[]', 0, 1, 1, '', '', '', '', '{}')"""
        )
        self.assertEqual(self.repair.repair(row, apply=True), "skip:protected")
        self.assertFalse(self.backup.exists())

    def test_symlink_and_hardlink_are_not_repaired(self):
        row = self.clip()
        link = self.recordings / "linked.mp4"
        try:
            link.symlink_to(row.path)
        except OSError:
            pass  # Windows often requires administrator rights for symlinks.
        else:
            candidate = Recordings(**row.__data__)
            candidate.path = str(link)
            self.assertEqual(self.repair.repair(candidate, apply=True), "skip:symlink")
            link.unlink()
        os.link(row.path, link)
        with self.assertRaisesRegex(RepairError, "non-hardlinked"):
            self.repair.repair(row, apply=True)

    def test_source_change_aborts_before_replacement(self):
        row = self.clip()
        original_probe = self.repair.probe

        def changed(path):
            result = original_probe(path)
            with Path(row.path).open("ab") as target:
                target.write(b"changed")
            return result

        with patch.object(self.repair, "probe", side_effect=changed):
            with self.assertRaisesRegex(RepairError, "Source recording changed"):
                self.repair.repair(row, apply=True)
        self.assertFalse(any(self.backup.glob("*.json")))
        self.assertEqual(Recordings.get_by_id(row.id).audio_rate, 16000)

    def test_row_change_aborts_before_replacement(self):
        row = self.clip()
        original_probe = self.repair.probe

        def changed(path):
            result = original_probe(path)
            Recordings.update(delete_requested=True).where(
                Recordings.id == row.id
            ).execute()
            return result

        with patch.object(self.repair, "probe", side_effect=changed):
            with self.assertRaisesRegex(RepairError, "Indexed recording changed"):
                self.repair.repair(row, apply=True)

    def test_failed_decode_rolls_back_without_touching_original(self):
        row = self.clip()
        original = _digest(Path(row.path))
        with patch.object(
            self.repair, "validate", side_effect=RepairError("decode failed")
        ):
            with self.assertRaisesRegex(RepairError, "decode failed"):
                self.repair.repair(row, apply=True)
        self.assertEqual(_digest(Path(row.path)), original)
        self.assertEqual(json.loads(self.journal().read_text())["state"], "rolled-back")
        self.assertFalse(list(self.recordings.glob(".audio-repair-*")))

    def test_failure_after_publication_restores_file_and_database(self):
        row = self.clip()
        original = _digest(Path(row.path))
        save_journal = self.repair._journal

        def fail_commit(path, data):
            if data["state"] == "committed":
                raise OSError("journal fsync failed")
            save_journal(path, data)

        with patch.object(self.repair, "_journal", side_effect=fail_commit):
            with self.assertRaisesRegex(OSError, "journal fsync failed"):
                self.repair.repair(row, apply=True)
        self.assertEqual(_digest(Path(row.path)), original)
        self.assertEqual(Recordings.get_by_id(row.id).__data__, row.__data__)
        self.assertEqual(json.loads(self.journal().read_text())["state"], "rolled-back")

    def test_metadata_commit_failure_restores_original(self):
        row = self.clip()
        original = _digest(Path(row.path))
        update = self.repair._update
        calls = 0

        def fail_first(recording_id, metadata):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OperationalError("simulated write failure")
            return update(recording_id, metadata)

        with patch.object(self.repair, "_update", side_effect=fail_first):
            with self.assertRaises(OperationalError):
                self.repair.repair(row, apply=True)
        self.assertEqual(_digest(Path(row.path)), original)
        self.assertEqual(Recordings.get_by_id(row.id).__data__, row.__data__)
        self.assertEqual(json.loads(self.journal().read_text())["state"], "rolled-back")

    def test_crash_between_replacement_and_db_is_recoverable(self):
        row = self.clip()
        original = _digest(Path(row.path))
        # BaseException simulates a hard interruption, bypassing normal rollback.
        with patch.object(Recordings, "update", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.repair.repair(row, apply=True)
        self.assertNotEqual(_digest(Path(row.path)), original)
        self.assertEqual(Recordings.get_by_id(row.id).__data__, row.__data__)
        self.assertEqual(json.loads(self.journal().read_text())["state"], "ready")
        self.assertEqual(self.repair.recover(self.journal()), "rolled-back")
        self.assertEqual(_digest(Path(row.path)), original)
        self.assertEqual(Recordings.get_by_id(row.id).__data__, row.__data__)
        self.assertEqual(self.repair.recover(self.journal()), "rolled-back")

    def test_damaged_backup_and_external_change_block_recovery(self):
        row = self.clip()
        with patch.object(Recordings, "update", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.repair.repair(row, apply=True)
        data = json.loads(self.journal().read_text())
        saved = Path(data["backup"])
        original = saved.read_bytes()
        saved.write_bytes(b"damaged")
        with self.assertRaisesRegex(RepairError, "damaged"):
            self.repair.recover(self.journal())
        saved.write_bytes(original)
        with Path(row.path).open("ab") as target:
            target.write(b"external change")
        with self.assertRaisesRegex(RepairError, "manual recovery"):
            self.repair.recover(self.journal())

    def test_space_timeout_tool_errors_and_backup_boundaries(self):
        row = self.clip()
        with patch("frigate.record.audio_repair.shutil.disk_usage") as disk:
            disk.return_value.free = 0
            with self.assertRaisesRegex(RepairError, "disk space"):
                self.repair.repair(row, apply=True)
        self.repair.timeout = 0.01
        with self.assertRaisesRegex(RepairError, "timed out"):
            self.repair._run([sys.executable, "-c", "import time; time.sleep(10)"])
        self.repair.timeout = 30
        with self.assertRaisesRegex(RepairError, "exit code 7"):
            self.repair._run([sys.executable, "-c", "raise SystemExit(7)"])
        with patch.object(self.repair, "_run", side_effect=RepairError("timed out")):
            with self.assertRaisesRegex(RepairError, "timed out"):
                self.repair.probe(Path(row.path))
        with self.assertRaisesRegex(RepairError, "separate"):
            AudioRepair(
                self.db, self.recordings, self.cache, self.recordings / "backup"
            )

    def test_cli_selectors_dry_run_and_explicit_acknowledgment(self):
        self.clip()
        base = [
            "--database",
            str(self.root / "index.db"),
            "--recordings-dir",
            str(self.recordings),
            "--cache-dir",
            str(self.cache),
            "--backup-dir",
            str(self.backup),
            "--ffmpeg",
            self.ffmpeg,
            "--ffprobe",
            self.ffprobe,
        ]
        with patch("builtins.print") as output:
            self.assertEqual(
                main(
                    base
                    + [
                        "--camera",
                        "front",
                        "--start",
                        "99",
                        "--end",
                        "102",
                        "--limit",
                        "1",
                    ]
                ),
                0,
            )
            self.assertIn("would-repair", output.call_args.args[0])
        self.assertFalse(self.backup.exists())
        with self.assertRaises(SystemExit):
            main(base + ["--apply"])
        with patch("builtins.print"):
            self.assertEqual(main(base + ["--camera", "back"]), 0)


if __name__ == "__main__":
    unittest.main()

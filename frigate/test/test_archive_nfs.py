"""Opt-in lifecycle validation on a dedicated, identified NFS test archive."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from peewee import SqliteDatabase

from frigate.models import Event, RecordingLease, Recordings
from frigate.record.archive import copy_verified, pin_archive, validate_mount
from frigate.record.archive_store import expire_recording, move_recording


@unittest.skipUnless(
    os.environ.get("FRIGATE_TEST_ARCHIVE_PATH"),
    "Requires an explicitly provisioned NFS test archive",
)
class TestArchiveNfs(unittest.TestCase):
    def setUp(self):
        base = Path(os.environ["FRIGATE_TEST_ARCHIVE_PATH"])
        identity = os.environ["FRIGATE_TEST_ARCHIVE_ID"]
        validate_mount(base, identity)
        self.identity = identity
        remote = tempfile.TemporaryDirectory(prefix="validation-", dir=base)
        self.addCleanup(remote.cleanup)
        self.archive = Path(remote.name)
        marker = self.archive / ".frigate-archive"
        with marker.open("w") as destination:
            destination.write(identity + "\n")
            destination.flush()
            os.fsync(destination.fileno())
        local = tempfile.TemporaryDirectory(prefix="frigate-archive-local-")
        self.addCleanup(local.cleanup)
        self.local = Path(local.name)
        self.source = self.local / "2026-01-01" / "00" / "front" / "00.00.mp4"
        self.source.parent.mkdir(parents=True)
        self.payload = b"frigate-nfs-verification" * 65536
        self.source.write_bytes(self.payload)
        self.db = SqliteDatabase(":memory:")
        self.db.bind([Recordings, Event, RecordingLease])
        self.db.create_tables([Recordings, Event, RecordingLease])
        self.addCleanup(self.db.close)
        for target, value in (
            ("frigate.record.archive.RECORD_DIR", str(self.local)),
            ("frigate.record.archive_store.RECORD_DIR", str(self.local)),
            ("frigate.record.archive.STORAGE_LOCK", str(self.local / "storage.lock")),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_verified_transfer_playback_and_expiration_on_nfs(self):
        end = time.time() - 2 * 86400
        recording = Recordings.create(
            id="nfs-recording",
            camera="front",
            path=str(self.source),
            start_time=end - 10,
            end_time=end,
            duration=10,
        )
        with pin_archive(self.archive, self.identity) as pinned:
            self.assertTrue(
                move_recording(recording, self.archive, pinned, 0, lambda: False)
            )
        self.assertTrue(self.source.is_symlink())
        self.assertEqual(self.source.read_bytes(), self.payload)
        saved = Recordings.get_by_id(recording.id)
        self.assertEqual(saved.path, recording.path)
        self.assertEqual(saved.storage_tier, "archive")
        target = self.source.readlink()
        with pin_archive(self.archive, self.identity) as pinned:
            self.assertTrue(
                expire_recording(saved, self.archive, pinned, 1, time.time())
            )
        self.assertFalse(target.exists())
        self.assertFalse(os.path.lexists(self.source))
        self.assertEqual(Recordings.select().count(), 0)

    def test_interrupted_nfs_copy_preserves_source(self):
        target = self.archive / "interrupted.mp4"
        with self.assertRaises(InterruptedError):
            copy_verified(self.source, target, 0, lambda: True)
        self.assertEqual(self.source.read_bytes(), self.payload)
        self.assertFalse(target.exists())
        self.assertFalse(target.with_suffix(".mp4.partial").exists())

    def test_incorrect_nfs_identity_is_rejected(self):
        with self.assertRaises(OSError):
            validate_mount(self.archive, self.identity + "-incorrect")
        self.assertEqual(self.source.read_bytes(), self.payload)


if __name__ == "__main__":
    unittest.main()

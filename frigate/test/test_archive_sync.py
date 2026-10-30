"""Recording synchronization must not dereference archive links during outages."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from peewee import SqliteDatabase

from frigate.models import Recordings, RecordingsToDelete
from frigate.util.media import _local_recording_files, sync_recordings


class TestArchiveSync(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.db = SqliteDatabase(":memory:")
        self.db.bind([Recordings, RecordingsToDelete])
        self.db.create_tables([Recordings])
        self.addCleanup(self.db.close)
        patcher = patch("frigate.util.media.RECORD_DIR", str(self.root))
        patcher.start()
        self.addCleanup(patcher.stop)

    def recording(self, name, tier="local", *, create=True):
        path = self.root / name
        if create:
            path.write_bytes(b"recording-content")
        Recordings.create(
            id=name,
            camera="front",
            path=str(path),
            start_time=time.time() - 20,
            end_time=time.time() - 10,
            duration=10,
            storage_tier=tier,
        )
        return path

    def test_offline_archive_metadata_survives_missing_local_anchor(self):
        path = self.recording("archive.mp4", "archive", create=False)
        original_lexists = os.path.lexists

        def check_local_only(candidate):
            self.assertNotEqual(candidate, str(path))
            return original_lexists(candidate)

        with patch("frigate.util.media.os.path.lexists", side_effect=check_local_only):
            result = sync_recordings(force=True)
        self.assertIsNone(result.error)
        self.assertFalse(result.aborted)
        self.assertEqual(result.orphans_deleted, 0)
        self.assertTrue(
            Recordings.select().where(Recordings.id == "archive.mp4").exists()
        )

    def test_local_missing_files_are_still_reconciled(self):
        self.recording("local.mp4", create=False)
        result = sync_recordings(force=True)
        self.assertIsNone(result.error)
        self.assertEqual(result.orphans_deleted, 1)
        self.assertEqual(Recordings.select().count(), 0)

    def test_orphan_local_files_are_still_removed(self):
        path = self.root / "orphan.mp4"
        path.write_bytes(b"orphan-content")
        result = sync_recordings(force=True)
        self.assertIsNone(result.error)
        self.assertEqual(result.orphans_deleted, 1)
        self.assertFalse(path.exists())

    def test_copy_and_link_temporary_files_are_not_orphans(self):
        self.recording("kept.mp4")
        partial = self.root / "kept.mp4.partial"
        pending_link = self.root / "kept.mp4.archive-link"
        partial.write_bytes(b"incomplete")
        pending_link.write_bytes(b"pending-link")
        result = sync_recordings(force=True)
        self.assertIsNone(result.error)
        self.assertEqual(result.orphans_found, 0)
        self.assertTrue(partial.exists())
        self.assertTrue(pending_link.exists())

    def test_directory_enumeration_does_not_follow_symlinks(self):
        entry = MagicMock()
        entry.path = str(self.root / "archive.mp4")
        entry.name = "archive.mp4"
        entry.is_dir.return_value = False
        entries = MagicMock()
        entries.__enter__.return_value = iter([entry])
        with patch("frigate.util.media.os.scandir", return_value=entries):
            files = _local_recording_files(str(self.root), None)
        self.assertEqual(files, {entry.path})
        entry.is_dir.assert_called_once_with(follow_symlinks=False)

    def test_orphan_archive_anchors_are_preserved_without_remote_size_probe(self):
        path = self.root / "orphan.mp4"
        path.write_bytes(b"archive-anchor")
        with (
            patch("frigate.util.media.os.path.islink", return_value=True),
            patch(
                "frigate.util.media.os.lstat",
                side_effect=AssertionError("remote size probe"),
            ),
            self.assertLogs("frigate.util.media", level="WARNING") as logs,
        ):
            result = sync_recordings(force=True)
        self.assertIsNone(result.error)
        self.assertEqual(result.orphans_deleted, 0)
        self.assertIn("Preserving orphan archive link", logs.output[0])
        self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()

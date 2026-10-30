"""Tests for validated atomic configuration replacement."""

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from frigate.util.builtin import update_yaml_text_bulk
from frigate.util.config import write_config_atomic


class TestAtomicConfig(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "config.yml"
        self.path.write_text("# camera settings\nvalue: 1\n")

    def test_rendering_does_not_mutate_the_persisted_config(self):
        original = self.path.read_text()
        updated = update_yaml_text_bulk(original, {"value": 2})
        self.assertEqual(self.path.read_text(), original)
        self.assertIn("# camera settings", updated)
        self.assertIn("value: 2", updated)

    def test_replacement_preserves_mode_and_ownership(self):
        self.path.chmod(0o600)
        before = self.path.stat()
        write_config_atomic(str(self.path), "value: 2\n")
        after = self.path.stat()
        self.assertEqual(self.path.read_text(), "value: 2\n")
        self.assertEqual(stat.S_IMODE(after.st_mode), stat.S_IMODE(before.st_mode))
        self.assertEqual(after.st_uid, before.st_uid)
        self.assertEqual(after.st_gid, before.st_gid)
        self.assertEqual(list(self.path.parent.glob(".frigate-config-*")), [])

    def test_failed_replace_does_not_modify_the_original(self):
        original = self.path.read_bytes()
        with patch("frigate.util.config.os.replace", side_effect=OSError("fixture")):
            with self.assertRaises(OSError):
                write_config_atomic(str(self.path), "value: 2\n")
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob(".frigate-config-*")), [])

    @unittest.skipUnless(os.name == "posix", "POSIX symlink behavior")
    def test_updates_the_target_without_replacing_a_config_symlink(self):
        link = self.path.parent / "active.yml"
        link.symlink_to(self.path.name)
        write_config_atomic(str(link), "value: 2\n")
        self.assertTrue(link.is_symlink())
        self.assertEqual(self.path.read_text(), "value: 2\n")


if __name__ == "__main__":
    unittest.main()

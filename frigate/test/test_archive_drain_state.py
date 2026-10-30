"""Archive restart intent, identity changes and durable transition tests."""

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from frigate.record.archive import needs_transport
from frigate.record.archive_drain_state import ArchiveDrainState


class TestArchiveDrainState(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "archive-drain-state.json"
        self.settings = {
            "archive_path": "/synthetic/archive",
            "mount_id": "synthetic-only",
            "local_budget_gib": 2048,
            "drain_percent": 70,
            "enabled": True,
            "parallel_transfers": 8,
        }
        self.state = ArchiveDrainState(self.path)
        if os.name == "nt":
            self.enterContext(
                patch("frigate.record.archive_drain_state.fsync_directory")
            )

    def save(self, active):
        self.state.save(self.settings, active)

    def test_missing_state_does_not_start_an_unadmitted_drain(self):
        self.assertFalse(self.state.load(self.settings))
        self.assertFalse(self.path.exists())
        self.assertFalse(needs_transport(2007, 2048, 70, False, 99999, 1))

    def test_restart_resumes_only_the_previously_admitted_drain(self):
        self.save(True)
        restored = ArchiveDrainState(self.path)
        self.assertTrue(restored.load(self.settings))
        self.assertTrue(
            needs_transport(2007, 2048, 70, restored.load(self.settings), 99999, 1)
        )
        self.assertFalse(needs_transport(1433.6, 2048, 70, True, 99999, 1))

    def test_start_threshold_changes_invalidate_old_drain_intent(self):
        settings = {**self.settings, "start_percent": 80, "drain_percent": 40}
        self.state.save(settings, True)
        restored = ArchiveDrainState(self.path)
        self.assertTrue(restored.load(settings))
        self.assertFalse(restored.load({**settings, "start_percent": 60}))

    def test_legacy_intent_migrates_only_for_the_legacy_start_threshold(self):
        self.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "active": True,
                    "identity": {
                        key: self.settings[key]
                        for key in (
                            "archive_path",
                            "mount_id",
                            "local_budget_gib",
                            "drain_percent",
                        )
                    },
                }
            )
        )
        self.assertTrue(self.state.load(self.settings))
        self.state.save(self.settings, True)
        self.assertEqual(
            json.loads(self.path.read_text())["identity"]["start_percent"], 100
        )
        self.assertFalse(
            ArchiveDrainState(self.path).load({**self.settings, "start_percent": 80})
        )

    def test_reaching_low_water_clears_intent_for_next_restart(self):
        self.save(True)
        self.save(False)
        self.assertFalse(ArchiveDrainState(self.path).load(self.settings))

    def test_worker_count_and_enabled_changes_do_not_erase_admitted_intent(self):
        self.save(True)
        for change in ({"parallel_transfers": 50}, {"enabled": False}):
            self.assertTrue(
                ArchiveDrainState(self.path).load({**self.settings, **change})
            )

    def test_changed_identity_or_watermarks_do_not_resume_stale_work(self):
        self.save(True)
        for key, value in (
            ("archive_path", "/synthetic/other"),
            ("mount_id", "different"),
            ("local_budget_gib", 4096),
            ("drain_percent", 80),
        ):
            settings = {**self.settings, key: value}
            self.assertFalse(ArchiveDrainState(self.path).load(settings))

    def test_malformed_state_is_not_a_silent_fallback(self):
        for value in ("not-json", '{"version":1,"active":"true","identity":{}}'):
            self.path.write_text(value)
            with self.assertRaises(ValueError):
                ArchiveDrainState(self.path).load(self.settings)

    def test_no_repeated_writes_during_a_steady_drain(self):
        self.save(True)
        before = self.path.stat().st_mtime_ns
        self.state.save(self.settings, True)
        self.assertEqual(self.path.stat().st_mtime_ns, before)

    def test_identity_is_not_mutated(self):
        before = copy.deepcopy(self.settings)
        self.state.load(self.settings)
        self.assertEqual(self.settings, before)

    def test_identity_change_is_persisted_before_old_settings_can_return(self):
        self.save(True)
        changed = {**self.settings, "local_budget_gib": 4096}
        self.assertFalse(self.state.load(changed))
        self.state.save(changed, False)
        self.assertFalse(ArchiveDrainState(self.path).load(self.settings))

    def test_oversized_state_is_rejected(self):
        self.path.write_text(" " * 4097)
        with self.assertRaisesRegex(ValueError, "size limit"):
            self.state.load(self.settings)

    def test_failed_file_flush_does_not_report_a_committed_transition(self):
        with patch(
            "frigate.record.archive_drain_state.os.fsync",
            side_effect=OSError("synthetic state flush failure"),
        ):
            with self.assertRaisesRegex(OSError, "state flush failure"):
                self.state.save(self.settings, True)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.state.load(self.settings))

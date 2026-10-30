"""User-controlled online maintenance, persistence, cancellation and API contracts."""

import asyncio
import json
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from filelock import FileLock
from pydantic import ValidationError

from frigate.api.defs.request.audio_repair_body import (
    AudioRepairScanBody,
    AudioRepairStartBody,
)
from frigate.models import Recordings
from frigate.record.audio_repair import AudioRepair, _digest
from frigate.record.audio_repair_manager import AudioRepairManager, RepairJobConflict
from frigate.test.test_audio_repair import AudioRepairFixture as _AudioRepairFixture


class TestAudioRepairManager(_AudioRepairFixture):
    def setUp(self):
        super().setUp()
        self.cache.mkdir()
        self.enterContext(
            patch(
                "frigate.record.audio_repair.storage_lock",
                lambda: FileLock(str(self.cache / "recording-storage.lock"), timeout=1),
            )
        )
        capture = SimpleNamespace(pre_capture=0, post_capture=0)
        record = SimpleNamespace(alerts=capture, detections=capture)
        self.config = SimpleNamespace(
            database=SimpleNamespace(path=self.db.database),
            record=record,
            cameras={"front": SimpleNamespace(record=record)},
            storage=SimpleNamespace(archive_path=str(self.root / "archive")),
        )
        self.manager = self.new_manager()
        asyncio.run(self.manager.start())
        self.addCleanup(lambda: asyncio.run(self.manager.close()))

    def new_manager(self):
        return AudioRepairManager(
            lambda: self.config,
            backup_root=self.backup,
            recordings_root=self.recordings,
            cache_root=self.cache,
            ffmpeg=self.ffmpeg,
            ffprobe=self.ffprobe,
            timeout=30,
        )

    def wait(self):
        self.manager._future.result(timeout=60)
        return self.manager.list_jobs()["jobs"][0]

    def scan(self, **kwargs):
        job = self.manager.create_scan(AudioRepairScanBody(**kwargs))
        self.wait()
        return job["id"]

    def test_scan_never_writes_and_all_eligible_requires_explicit_start(self):
        row = self.clip()
        no_audio = self.clip(audio=False)
        before = _digest(Path(row.path))
        job_id = self.scan(limit=2)
        summary = self.manager.get_job(job_id)
        self.assertEqual(summary["phase"], "scan")
        self.assertEqual(summary["status"], "ready")
        self.assertEqual(summary["scanned"], 2)
        self.assertEqual(summary["eligible"], 1)
        self.assertEqual(summary["processed"], 0)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(_digest(Path(row.path)), before)
        self.assertFalse(list(self.backup.glob("*.original.mp4")))
        items = self.manager.list_items(job_id, limit=1)
        self.assertEqual(items["total"], 2)
        self.assertEqual(len(items["items"]), 1)
        skipped = self.manager.list_items(job_id, state="skipped")["items"][0]
        self.assertEqual(skipped["id"], no_audio.id)
        self.assertEqual(skipped["reason"], "no-audio")
        self.assertFalse(skipped["has_audio"])
        self.manager.start_job(job_id, AudioRepairStartBody(all_eligible=True))
        summary = self.wait()
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["phase"], "repair")
        self.assertEqual(summary["selected"], 1)
        self.assertEqual(summary["processed"], 1)
        self.assertEqual(summary["repaired"], 1)
        self.assertEqual(Recordings.get_by_id(row.id).audio_rate, 48000)
        self.assertNotIn("path", json.dumps(self.manager.list_items(job_id)))

    def test_pause_and_resume_scan_at_safe_file_boundary(self):
        self.clip()
        self.clip()
        entered, release = threading.Event(), threading.Event()
        original_probe = AudioRepair.probe

        def blocked(engine, path):
            entered.set()
            self.assertTrue(release.wait(10))
            return original_probe(engine, path)

        with patch.object(AudioRepair, "probe", blocked):
            job = self.manager.create_scan(AudioRepairScanBody())
            self.assertTrue(entered.wait(10))
            paused = self.manager.pause(job["id"])
            self.assertEqual(paused["status"], "pause_requested")
            release.set()
            summary = self.wait()
        self.assertEqual(summary["status"], "paused")
        self.assertEqual(summary["scanned"], 1)
        self.assertEqual(summary["phase"], "scan")
        self.manager.resume(job["id"])
        summary = self.wait()
        self.assertEqual(summary["status"], "ready")
        self.assertEqual(summary["scanned"], 2)

    def test_pause_repair_keeps_completed_file_and_resume_is_explicit(self):
        first, second = self.clip(), self.clip()
        job_id = self.scan()
        entered, release = threading.Event(), threading.Event()
        original = AudioRepair.repair

        def blocked(engine, row, apply=False):
            entered.set()
            self.assertTrue(release.wait(10))
            return original(engine, row, apply)

        with patch.object(AudioRepair, "repair", blocked):
            self.manager.start_job(job_id, AudioRepairStartBody(all_eligible=True))
            self.assertTrue(entered.wait(10))
            self.assertEqual(self.manager.pause(job_id)["status"], "pause_requested")
            release.set()
            summary = self.wait()
        self.assertEqual(summary["status"], "paused")
        self.assertEqual(summary["processed"], 1)
        self.assertEqual(summary["repaired"], 1)
        self.assertEqual(summary["phase"], "repair")
        rates = [Recordings.get_by_id(row.id).audio_rate for row in (first, second)]
        self.assertEqual(sorted(rates), [16000, 48000])
        self.manager.resume(job_id)
        self.assertEqual(self.wait()["repaired"], 2)

    def test_stop_during_conversion_preserves_current_original(self):
        row = self.clip()
        before = _digest(Path(row.path))
        job_id = self.scan()
        entered = threading.Event()
        original_run = AudioRepair._run

        def slow_conversion(engine, command):
            if "-c:v" in command:
                entered.set()
                # Exercise real Popen cancellation with a bounded child.
                import sys

                return original_run(
                    engine, [sys.executable, "-c", "import time; time.sleep(30)"]
                )
            return original_run(engine, command)

        with patch.object(AudioRepair, "_run", slow_conversion):
            self.manager.start_job(job_id, AudioRepairStartBody(item_ids=[row.id]))
            self.assertTrue(entered.wait(10))
            self.assertEqual(self.manager.stop(job_id)["status"], "stop_requested")
            summary = self.wait()
        self.assertEqual(summary["status"], "stopped")
        self.assertEqual(summary["processed"], 0)
        self.assertEqual(_digest(Path(row.path)), before)
        self.assertEqual(Recordings.get_by_id(row.id).audio_rate, 16000)
        self.assertEqual(
            self.manager.list_items(job_id)["items"][0]["state"], "candidate"
        )
        self.assertEqual(json.loads(self.journal().read_text())["state"], "rolled-back")

    def test_scan_stop_and_single_worker_conflict(self):
        self.clip()
        entered, release = threading.Event(), threading.Event()
        original = AudioRepair.probe

        def blocked(engine, path):
            entered.set()
            self.assertTrue(release.wait(10))
            return original(engine, path)

        with patch.object(AudioRepair, "probe", blocked):
            job = self.manager.create_scan(AudioRepairScanBody())
            self.assertTrue(entered.wait(10))
            with self.assertRaises(RepairJobConflict):
                self.manager.create_scan(AudioRepairScanBody())
            self.manager.stop(job["id"])
            release.set()
            summary = self.wait()
        self.assertEqual(summary["status"], "stopped")
        with self.assertRaises(RepairJobConflict):
            self.manager.start_job(job["id"], AudioRepairStartBody(all_eligible=True))

    def test_delete_claim_after_scan_is_skipped_and_counts_as_processed(self):
        row = self.clip()
        job_id = self.scan()
        Recordings.update(delete_requested=True).where(
            Recordings.id == row.id
        ).execute()
        self.manager.start_job(job_id, AudioRepairStartBody(item_ids=[row.id]))
        summary = self.wait()
        self.assertEqual(summary["repaired"], 0)
        self.assertEqual(summary["processed"], 1)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(
            self.manager.list_items(job_id)["items"][0]["reason"], "deletion-pending"
        )

    def test_storage_lock_is_not_held_during_conversion_or_probe(self):
        self.clip()
        job_id = self.scan()
        locked = []

        @contextmanager
        def guard():
            locked.append(True)
            try:
                yield
            finally:
                locked.pop()

        run = AudioRepair._run

        def verified(engine, command):
            self.assertFalse(locked)
            return run(engine, command)

        with (
            patch("frigate.record.audio_repair.storage_lock", guard),
            patch.object(AudioRepair, "_run", verified),
        ):
            self.manager.start_job(job_id, AudioRepairStartBody(all_eligible=True))
            self.assertEqual(self.wait()["repaired"], 1)

    def test_restart_never_automatically_resumes_and_retains_history(self):
        self.clip()
        job_id = self.scan()
        self.manager._set_job(job_id, status="running", phase="repair")
        other = self.new_manager()
        try:
            asyncio.run(other.start())
            summary = other.get_job(job_id)
            self.assertEqual(summary["status"], "interrupted")
            self.assertEqual(summary["error"], "restart-requires-new-scan")
            self.assertIsNone(other._future)
            self.assertEqual(other.list_jobs(limit=1)["total"], 1)
        finally:
            asyncio.run(other.close())

    def test_pending_journal_blocks_repairs_and_inputs_are_bounded(self):
        row = self.clip()
        job_id = self.scan()
        (self.backup / "unfinished.json").write_text('{"state":"ready"}')
        with self.assertRaisesRegex(RepairJobConflict, "offline recovery"):
            self.manager.start_job(job_id, AudioRepairStartBody(item_ids=[row.id]))
        for body in (
            {},
            {"item_ids": ["*"]},
            {"item_ids": [row.id], "all_eligible": True},
        ):
            with self.assertRaises(ValidationError):
                AudioRepairStartBody(**body)
        with self.assertRaises(ValidationError):
            AudioRepairScanBody(limit=10001)
        with self.assertRaises(ValidationError):
            AudioRepairScanBody(backup_dir=str(self.root))

    def test_api_routes_schemas_pagination_and_admin_dependency(self):
        try:
            from frigate.api.audio_repair import router
        except ModuleNotFoundError as error:
            if error.name == "resource":
                self.skipTest(
                    "API auth imports Linux resource module; validate API on Linux"
                )
            raise
        self.clip(audio=False)
        job_id = self.scan()
        app = FastAPI()
        app.frigate_config = SimpleNamespace(
            proxy=SimpleNamespace(separator=","),
            auth=SimpleNamespace(roles={"admin": [], "viewer": []}),
        )
        app.state.audio_repair_manager = self.manager
        app.include_router(router)
        with TestClient(app) as client:
            base = "/recordings/audio-repair/jobs"
            self.assertEqual(client.get(base).status_code, 403)
            self.assertEqual(
                client.get(base, headers={"remote-role": "viewer"}).status_code, 403
            )
            client.headers["remote-role"] = "admin"
            response = client.get(base)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["jobs"][0]["id"], job_id)
            self.assertEqual(client.get(f"{base}/{job_id}").json()["phase"], "scan")
            page = client.get(
                f"{base}/{job_id}/items", params={"limit": 1, "state": "skipped"}
            )
            self.assertEqual(page.json()["total"], 1)
            self.assertEqual(page.json()["items"][0]["reason"], "no-audio")
            self.assertNotIn(str(self.root), page.text)
            self.assertEqual(client.get(base, params={"limit": 501}).status_code, 422)
            self.assertEqual(client.get(f"{base}/missing").status_code, 404)
            self.assertEqual(
                client.post(f"{base}/{job_id}/start", json={}).status_code, 422
            )
            self.assertEqual(client.post(f"{base}/{job_id}/pause").status_code, 409)
            self.assertEqual(
                client.post(base, json={"path": "/etc/passwd"}).status_code, 422
            )

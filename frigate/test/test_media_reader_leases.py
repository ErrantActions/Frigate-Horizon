"""Original reader protection spans planning, blocking work, and cancellation."""

import asyncio
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from peewee import SqliteDatabase
from starlette.requests import ClientDisconnect

from frigate.api.media import (
    get_snapshot_from_recording,
    recording_clip,
    submit_recording_snapshot_to_plus,
)
from frigate.models import RecordingLease, Recordings
from frigate.record.export import PlaybackSourceEnum, RecordingExporter
from frigate.record.lease_async import AsyncRecordingLease
from frigate.util.ffmpeg import run_ffmpeg_with_progress
from frigate.util.image import run_ffmpeg_snapshot
from frigate.util.services import get_video_properties


class TestSnapshotOwnedProcess(unittest.TestCase):
    def run_snapshot(self, code, callback, timeout=None):
        spawn = subprocess.Popen

        def synthetic(_command, **kwargs):
            return spawn([sys.executable, "-c", code], **kwargs)

        with patch("frigate.util.image.sp.Popen", synthetic):
            return run_ffmpeg_snapshot(
                SimpleNamespace(ffmpeg_path="unused"),
                "unused",
                "png",
                timeout=timeout,
                process_started=callback,
            )

    def test_callback_owns_child_and_all_pipes_close_after_success(self):
        processes = []
        image, error = self.run_snapshot(
            "import sys; sys.stdout.buffer.write(b'synthetic-image')", processes.append
        )
        self.assertEqual((image, error), (b"synthetic-image", ""))
        self.assertEqual(len(processes), 1)
        self.assertEqual(processes[0].poll(), 0)
        self.assertTrue(processes[0].stdout.closed)
        self.assertTrue(processes[0].stderr.closed)

    def test_timeout_reaps_child_before_returning(self):
        processes = []
        image, error = self.run_snapshot(
            "import time; time.sleep(30)", processes.append, timeout=0.02
        )
        self.assertEqual((image, error), (None, "timeout"))
        self.assertIsNotNone(processes[0].poll())
        self.assertTrue(processes[0].stdout.closed)

    def test_registration_failure_is_not_a_success_and_reaps_the_child(self):
        processes = []

        def failed(process):
            processes.append(process)
            raise RuntimeError("synthetic lease failure")

        with self.assertRaisesRegex(RuntimeError, "synthetic lease failure"):
            self.run_snapshot("import time; time.sleep(30)", failed)
        self.assertIsNotNone(processes[0].poll())
        self.assertTrue(processes[0].stdout.closed)

    def test_progress_registration_failure_stops_and_reaps_its_actual_child(self):
        processes = []

        def failed(process):
            processes.append(process)
            raise RuntimeError("synthetic export protection failure")

        with self.assertRaisesRegex(RuntimeError, "synthetic export protection"):
            run_ffmpeg_with_progress(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                expected_duration_seconds=10,
                process_started=failed,
                use_low_priority=False,
            )
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())
        self.assertTrue(processes[0].stderr.closed)


class TestSnapshotReaderLease(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = SqliteDatabase(str(Path(self.directory.name) / "metadata.sqlite"))
        self.enterContext(self.db.bind_ctx([Recordings, RecordingLease]))
        self.db.create_tables([Recordings, RecordingLease])
        self.addCleanup(self.db.close)
        self.enterContext(patch("frigate.record.leases.storage_lock", nullcontext))
        Recordings.create(
            id="source",
            camera="front",
            path="never-opened-by-test.mp4",
            start_time=0,
            end_time=10,
            duration=10,
        )
        self.request = SimpleNamespace(
            app=SimpleNamespace(
                frigate_config=SimpleNamespace(
                    cameras={"front": SimpleNamespace()},
                    ffmpeg=SimpleNamespace(ffmpeg_path="unused"),
                ),
            )
        )

    async def test_lease_exists_while_the_original_reader_is_running(self):
        entered, release = threading.Event(), threading.Event()

        def reader(*args, process_started=None):
            self.assertIsNotNone(process_started)
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Synthetic reader was not released")
            return b"synthetic-image"

        with patch("frigate.api.media.get_image_from_recording", reader):
            task = asyncio.create_task(
                get_snapshot_from_recording(
                    self.request,
                    "front",
                    5,
                    "png",
                    None,
                )
            )
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            self.assertEqual(RecordingLease.select().count(), 1)
            release.set()
            response = await task
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, b"synthetic-image")
        self.assertEqual(RecordingLease.select().count(), 0)

    async def test_cancelled_snapshot_waits_for_its_actual_reader_thread(self):
        entered, release = threading.Event(), threading.Event()

        def reader(*args, process_started=None):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Synthetic reader was not released")
            return b"synthetic-image"

        with patch("frigate.api.media.get_image_from_recording", reader):
            task = asyncio.create_task(
                get_snapshot_from_recording(
                    self.request,
                    "front",
                    5,
                    "png",
                    None,
                )
            )
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertEqual(RecordingLease.select().count(), 1)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(RecordingLease.select().count(), 0)

    async def test_unknown_camera_does_not_acquire_a_lease(self):
        with patch("frigate.api.media.AsyncRecordingLease") as lease:
            response = await get_snapshot_from_recording(
                self.request,
                "unknown",
                5,
                "png",
                None,
            )
        self.assertEqual(response.status_code, 404)
        lease.assert_not_called()

    async def test_plus_extraction_is_leased_but_upload_does_not_hold_originals(self):
        calls = []

        def reader(*args, process_started=None):
            self.assertIsNotNone(process_started)
            with self.db.connection_context():
                self.assertEqual(RecordingLease.select().count(), 1)
            calls.append("read")
            return b"synthetic-image"

        def upload(image, camera):
            with self.db.connection_context():
                self.assertEqual(RecordingLease.select().count(), 0)
            calls.append("upload")
            self.assertEqual(camera, "front")

        self.request.app.frigate_config.plus_api = SimpleNamespace(upload_image=upload)
        entered, release = threading.Event(), threading.Event()

        def blocked_reader(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Synthetic Plus reader was not released")
            return reader(*args, **kwargs)

        with (
            patch("frigate.api.media.get_image_from_recording", blocked_reader),
            patch("frigate.api.media.cv2.imdecode", return_value=object()),
        ):
            task = asyncio.create_task(
                submit_recording_snapshot_to_plus(self.request, "front", "5")
            )
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            self.assertEqual(RecordingLease.select().count(), 1)
            release.set()
            response = await task
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, ["read", "upload"])
        self.assertEqual(RecordingLease.select().count(), 0)

    async def test_cancelled_plus_snapshot_joins_reader_and_does_not_upload(self):
        entered, release = threading.Event(), threading.Event()

        def reader(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Synthetic Plus reader was not released")
            return b"synthetic-image"

        with patch("frigate.api.media.get_image_from_recording", reader):
            task = asyncio.create_task(
                submit_recording_snapshot_to_plus(self.request, "front", "5")
            )
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertEqual(RecordingLease.select().count(), 1)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(RecordingLease.select().count(), 0)

    async def test_plus_rejects_invalid_frame_times_without_reader_or_upload(self):
        with patch("frigate.api.media.AsyncRecordingLease") as lease:
            for frame_time in ("invalid", "nan", "inf", "-inf"):
                with self.subTest(frame_time=frame_time):
                    response = await submit_recording_snapshot_to_plus(
                        self.request, "front", frame_time
                    )
                    self.assertEqual(response.status_code, 422)
        lease.assert_not_called()

    async def test_export_window_is_reserved_before_planning_and_released_after_work(
        self,
    ):
        exporter = RecordingExporter.__new__(RecordingExporter)
        exporter.camera, exporter.start_time, exporter.end_time = "front", 0, 10
        calls = []

        def work():
            with self.db.connection_context():
                self.assertEqual(RecordingLease.select().count(), 1)
            calls.append("planned-and-read")

        with patch.object(exporter, "_run", work):
            await exporter._run_leased()
        self.assertEqual(calls, ["planned-and-read"])
        self.assertEqual(RecordingLease.select().count(), 0)
        self.assertIsNone(exporter._source_lease)

    async def test_cancelled_export_reaps_child_before_releasing_originals(self):
        exporter = RecordingExporter.__new__(RecordingExporter)
        exporter.camera, exporter.start_time, exporter.end_time = "front", 0, 10
        entered = threading.Event()
        processes = []

        def work():
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"]
            )
            processes.append(process)
            exporter._original_process_started(process)
            entered.set()
            process.wait()

        with patch.object(exporter, "_run", work):
            task = asyncio.create_task(exporter._run_leased())
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            self.assertEqual(RecordingLease.select().count(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNotNone(processes[0].poll())
        self.assertEqual(RecordingLease.select().count(), 0)
        self.assertIsNone(exporter._source_lease)

    async def test_preview_export_does_not_reserve_original_recordings(self):
        exporter = RecordingExporter.__new__(RecordingExporter)
        exporter.playback_source = PlaybackSourceEnum.preview
        with (
            patch.object(exporter, "_run") as work,
            patch("frigate.record.export.AsyncRecordingLease") as lease,
        ):
            exporter.run()
        work.assert_called_once_with()
        lease.assert_not_called()

    async def test_guarded_original_probe_owns_child_and_never_uses_cv2_fallback(self):
        spawn = asyncio.create_subprocess_exec
        processes = []

        async def synthetic(*args, **kwargs):
            process = await spawn(
                sys.executable,
                "-c",
                "print('{}')",
                **kwargs,
            )
            processes.append(process)
            return process

        async with AsyncRecordingLease("front", 0, 10) as lease:
            with (
                patch("asyncio.create_subprocess_exec", synthetic),
                patch("frigate.util.services.cv2.VideoCapture") as cv2_probe,
            ):
                properties = await lease.guard(
                    get_video_properties(
                        SimpleNamespace(ffprobe_path="unused"),
                        "never-opened-by-test.mp4",
                        process_started=lease.process_started,
                    )
                )
            cv2_probe.assert_not_called()
            self.assertEqual(properties, {"has_valid_video": False})
        self.assertIsNotNone(processes[0].returncode)
        self.assertEqual(RecordingLease.select().count(), 0)

    async def test_clip_lease_is_deferred_and_disconnect_reaps_its_actual_child(self):
        cache = Path(self.directory.name)
        processes = []
        spawn = subprocess.Popen

        def synthetic(_command, **kwargs):
            process = spawn(
                [
                    sys.executable,
                    "-c",
                    "import sys,time;sys.stdout.buffer.write(b'x'*8192);sys.stdout.flush();time.sleep(30)",
                ],
                **kwargs,
            )
            processes.append(process)
            return process

        async def receive():
            await asyncio.Event().wait()

        async def disconnected(message):
            if message["type"] == "http.response.body" and message.get("body"):
                self.assertEqual(RecordingLease.select().count(), 1)
                raise OSError("synthetic disconnect")

        with (
            patch("frigate.api.media.CACHE_DIR", str(cache)),
            patch("frigate.api.media.sp.Popen", synthetic),
        ):
            response = await recording_clip(self.request, "front", 0, 10)
            self.assertEqual(RecordingLease.select().count(), 0)
            with self.assertRaises(ClientDisconnect):
                await response(
                    {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}},
                    receive,
                    disconnected,
                )
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())
        self.assertEqual(RecordingLease.select().count(), 0)
        self.assertFalse(list(cache.glob("playlist_*.txt")))

    async def test_clip_failure_before_iteration_cleans_plan_and_never_spawns(self):
        cache = Path(self.directory.name)

        async def receive():
            await asyncio.Event().wait()

        async def disconnected(message):
            if message["type"] == "http.response.start":
                self.assertEqual(RecordingLease.select().count(), 1)
                raise OSError("synthetic early disconnect")

        with (
            patch("frigate.api.media.CACHE_DIR", str(cache)),
            patch("frigate.api.media.sp.Popen") as spawn,
        ):
            response = await recording_clip(self.request, "front", 0, 10)
            with self.assertRaises(ClientDisconnect):
                await response(
                    {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}},
                    receive,
                    disconnected,
                )
            spawn.assert_not_called()
        self.assertEqual(RecordingLease.select().count(), 0)
        self.assertFalse(list(cache.glob("playlist_*.txt")))

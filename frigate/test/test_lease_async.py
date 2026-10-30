"""Async source leases retain real SQLite rows until owned readers have stopped."""

import asyncio
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from peewee import OperationalError, SqliteDatabase

from frigate.models import RecordingLease, Recordings
from frigate.record.lease_async import AsyncRecordingLease, AsyncRecordingLeaseLost
from frigate.record.leases import lease_recordings, release_recordings


class AsyncProcess:
    def __init__(self, events, *, ignore_terminate=False):
        self.returncode = None
        self.events = events
        self.ignore_terminate = ignore_terminate
        self.done = asyncio.Event()

    def terminate(self):
        self.events.append("terminate")
        if not self.ignore_terminate:
            self.returncode = -15
            self.done.set()

    def kill(self):
        self.events.append("kill")
        self.returncode = -9
        self.done.set()

    async def wait(self):
        await self.done.wait()
        self.events.append("waited")
        return self.returncode


class TestAsyncRecordingLease(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=".")
        self.addCleanup(self.directory.cleanup)
        self.db = SqliteDatabase(str(Path(self.directory.name) / "leases.db"))
        self.enterContext(self.db.bind_ctx([Recordings, RecordingLease]))
        self.db.create_tables([Recordings, RecordingLease])
        self.addCleanup(self.db.close)
        self.enterContext(patch("frigate.record.leases.storage_lock", nullcontext))

    def lease(self, **kwargs):
        return AsyncRecordingLease(
            "front",
            10,
            20,
            lifetime=10,
            interval=0.02,
            terminate_timeout=0.01,
            **kwargs,
        )

    async def asyncTearDown(self):
        tasks = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and task.get_name().startswith("source-lease:")
        ]
        self.assertEqual(tasks, [], "Source lease renewal task leaked")

    async def test_real_database_window_owner_and_renewal_then_release(self):
        lease = self.lease(owner="authenticated-user")
        renewed = threading.Event()
        calls = 0
        initial = []

        def acquire(*args, **kwargs):
            nonlocal calls
            calls += 1
            lease_recordings(*args, **kwargs)
            if calls == 1:
                initial.append(RecordingLease.get_by_id(lease.token).expires)
            else:
                renewed.set()

        with patch("frigate.record.lease_async.lease_recordings", side_effect=acquire):
            async with lease:
                self.assertTrue(await asyncio.to_thread(renewed.wait, 5))
                row = RecordingLease.get_by_id(lease.token)
                self.assertEqual(
                    (row.camera, row.start_time, row.end_time), ("front", 10, 20)
                )
                self.assertEqual(row.owner, "authenticated-user")
                self.assertGreater(row.expires, initial[0])
                self.assertFalse(
                    self.db.is_closed(), "A worker closed the caller's connection"
                )
        self.assertEqual(RecordingLease.select().count(), 0)

    async def test_renewal_failure_cancels_work_and_stops_child_before_release(self):
        events = []
        process = AsyncProcess(events, ignore_terminate=True)
        calls = 0

        def acquire(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise OperationalError("database unavailable")
            lease_recordings(*args, **kwargs)

        def release(token):
            self.assertIsNotNone(process.returncode)
            events.append("release")
            release_recordings(token)

        lease = self.lease()

        async def work():
            await lease.process_started(process)
            await asyncio.Event().wait()

        with (
            patch("frigate.record.lease_async.lease_recordings", side_effect=acquire),
            patch("frigate.record.lease_async.release_recordings", side_effect=release),
            self.assertRaises(AsyncRecordingLeaseLost),
        ):
            async with lease:
                await asyncio.wait_for(lease.guard(work()), 2)
        self.assertEqual(events, ["terminate", "kill", "waited", "release"])
        self.assertTrue(lease.failure_event.is_set())
        self.assertEqual(RecordingLease.select().count(), 0)

    async def test_failure_waiter_raises_and_a_late_child_is_stopped(self):
        calls = 0
        process = AsyncProcess([])

        def acquire(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise OperationalError("database unavailable")
            lease_recordings(*args, **kwargs)

        lease = self.lease()
        with (
            patch("frigate.record.lease_async.lease_recordings", side_effect=acquire),
            self.assertRaises(AsyncRecordingLeaseLost),
        ):
            async with lease:
                await asyncio.wait_for(lease.failure_event.wait(), 2)
                with self.assertRaises(AsyncRecordingLeaseLost):
                    await lease.wait_failed()
                await lease.process_started(process)
        self.assertIsNotNone(process.returncode)
        self.assertEqual(RecordingLease.select().count(), 0)

    async def test_repeated_cancellation_joins_work_and_child_before_releasing(self):
        events = []
        ready = asyncio.Event()
        process = AsyncProcess(events)
        lease = self.lease()

        async def work():
            await lease.process_started(process)
            ready.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.03)
                events.append("work-ended")

        async def run():
            async with lease:
                await lease.guard(work())

        task = asyncio.create_task(run())
        await asyncio.wait_for(ready.wait(), 2)
        task.cancel()
        await asyncio.sleep(0.005)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        self.assertEqual(events, ["work-ended", "terminate", "waited"])
        self.assertEqual(RecordingLease.select().count(), 0)

    async def test_cancelled_acquisition_waits_for_thread_then_releases(self):
        entered = threading.Event()
        finish = threading.Event()

        def acquire(*args, **kwargs):
            lease_recordings(*args, **kwargs)
            entered.set()
            finish.wait(2)

        lease = self.lease()
        with patch("frigate.record.lease_async.lease_recordings", side_effect=acquire):
            task = asyncio.create_task(lease.__aenter__())
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            task.cancel()
            await asyncio.sleep(0.02)
            self.assertFalse(task.done())
            self.assertEqual(RecordingLease.select().count(), 1)
            finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        self.assertEqual(RecordingLease.select().count(), 0)

    async def test_exit_joins_an_inflight_renewal_before_releasing(self):
        entered = threading.Event()
        finish = threading.Event()
        calls = 0

        def acquire(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls > 1:
                entered.set()
                finish.wait(2)
            lease_recordings(*args, **kwargs)

        lease = self.lease()
        with patch("frigate.record.lease_async.lease_recordings", side_effect=acquire):
            await lease.__aenter__()
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            task = asyncio.create_task(lease.__aexit__(None, None, None))
            await asyncio.sleep(0.02)
            self.assertFalse(task.done())
            finish.set()
            await asyncio.wait_for(task, 2)
        await asyncio.sleep(0.04)
        self.assertEqual(
            RecordingLease.select().count(), 0, "Late renewal recreated a released row"
        )

    async def test_acquisition_failure_does_not_start_work_or_a_renewer(self):
        lease = self.lease()
        with (
            patch(
                "frigate.record.lease_async.lease_recordings",
                side_effect=OperationalError("offline"),
            ),
            self.assertRaises(AsyncRecordingLeaseLost),
        ):
            async with lease:
                self.fail("An unprotected reader was started")
        self.assertEqual(RecordingLease.select().count(), 0)

    async def test_release_failure_is_not_silent_and_cannot_keep_renewing(self):
        lease = self.lease()
        with (
            patch(
                "frigate.record.lease_async.release_recordings",
                side_effect=OperationalError("offline"),
            ),
            self.assertRaises(AsyncRecordingLeaseLost),
        ):
            async with lease:
                pass
        expires = RecordingLease.get_by_id(lease.token).expires
        await asyncio.sleep(0.05)
        self.assertEqual(RecordingLease.get_by_id(lease.token).expires, expires)
        self.assertLessEqual(expires, time.time() + 10)

    async def test_body_error_is_preserved_and_releases_the_window(self):
        lease = self.lease()

        async def fail():
            raise ValueError("invalid source")

        with self.assertRaisesRegex(ValueError, "invalid source"):
            async with lease:
                await lease.guard(fail())
        self.assertEqual(RecordingLease.select().count(), 0)

    async def test_real_asyncio_subprocess_is_reaped_before_lease_release(self):
        lease = self.lease()
        async with lease:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                "import time; time.sleep(30)",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await lease.process_started(process)
            self.assertEqual(RecordingLease.select().count(), 1)
        self.assertIsNotNone(process.returncode)
        self.assertEqual(RecordingLease.select().count(), 0)

    async def test_cancelled_blocking_work_is_joined_before_release(self):
        entered, release = threading.Event(), threading.Event()
        events = []

        def reader():
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Test reader was not released")
            events.append("reader-exited")

        lease = self.lease()
        async with lease:
            work = asyncio.create_task(lease.run_blocking(reader))
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            work.cancel()
            await asyncio.sleep(0)
            self.assertFalse(work.done())
            self.assertEqual(RecordingLease.select().count(), 1)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await work
            self.assertEqual(events, ["reader-exited"])
            self.assertEqual(RecordingLease.select().count(), 1)
        self.assertEqual(RecordingLease.select().count(), 0)

    async def test_real_blocking_subprocess_is_stopped_before_release(self):
        lease = self.lease()
        async with lease:
            process = await asyncio.to_thread(
                subprocess.Popen,
                [sys.executable, "-c", "import time; time.sleep(30)"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            await asyncio.to_thread(lease.process_started_blocking, process)
            self.assertIsNone(process.poll())
            self.assertEqual(RecordingLease.select().count(), 1)
        self.assertIsNotNone(process.poll())
        self.assertEqual(RecordingLease.select().count(), 0)

    def test_invalid_renewal_windows_are_rejected(self):
        for values in (
            {"start": 20, "end": 10},
            {"start": 10, "end": 20, "lifetime": 100, "interval": 50},
            {"start": float("nan"), "end": 20},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                AsyncRecordingLease("front", **values)

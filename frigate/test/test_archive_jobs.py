"""Archive scheduling tests without Linux process setup or production storage."""

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from frigate.record.archive import copy_verified
from frigate.record.archive_jobs import run_archive_jobs
from frigate.record.archive_rate import ArchiveRateLimiter


class TestArchiveJobs(unittest.TestCase):
    def test_rolling_queue_uses_idle_slots_without_waiting_for_a_slow_peer(self):
        cancelled = threading.Event()
        third_started = threading.Event()
        lock = threading.Lock()
        coordinator = threading.get_ident()
        active, peak = 0, 0
        values = []

        def transfer(job):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                if job == 0 and not third_started.wait(3):
                    raise RuntimeError("Queue waited for the slow first job")
                if job == 2:
                    third_started.set()
                return job * 2
            finally:
                with lock:
                    active -= 1

        def supervise():
            self.assertEqual(threading.get_ident(), coordinator)
            return False

        def completed(job, future):
            self.assertEqual(threading.get_ident(), coordinator)
            values.append((job, future.result()))

        def publish():
            self.assertEqual(threading.get_ident(), coordinator)

        stopped = run_archive_jobs(
            list(range(8)), 2, transfer, supervise, completed, publish, cancelled
        )
        self.assertEqual(sorted(values), [(job, job * 2) for job in range(8)])
        self.assertEqual(peak, 2)
        self.assertEqual(active, 0)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(stopped)

    def test_live_cancellation_reaches_active_and_queued_jobs(self):
        cancelled = threading.Event()
        entered = threading.Event()
        values = []

        def transfer(job):
            entered.set()
            if not cancelled.wait(3):
                raise RuntimeError("Coordinator did not cancel active work")
            return False

        stopped = run_archive_jobs(
            list(range(8)),
            2,
            transfer,
            entered.is_set,
            lambda job, future: values.append(future.result()),
            lambda: None,
            cancelled,
        )
        self.assertEqual(values, [False] * 8)
        self.assertTrue(stopped)

    def test_configured_worker_bounds_include_large_scaling_trials(self):
        for workers in (1, 2, 4, 8, 25, 50):
            with self.subTest(workers=workers):
                cancelled = threading.Event()
                barrier = threading.Barrier(workers)
                lock = threading.Lock()
                active, peak = 0, 0
                results = []

                def transfer(job):
                    nonlocal active, peak
                    with lock:
                        active += 1
                        peak = max(peak, active)
                    try:
                        barrier.wait(timeout=3)
                        return job
                    finally:
                        with lock:
                            active -= 1

                run_archive_jobs(
                    list(range(workers * 4)),
                    workers,
                    transfer,
                    lambda: False,
                    lambda job, future: results.append(future.result()),
                    lambda: None,
                    cancelled,
                )
                self.assertEqual(peak, workers)
                self.assertEqual(sorted(results), list(range(workers * 4)))
                self.assertEqual(active, 0)

    def test_callback_errors_cancel_and_join_active_work_before_propagating(self):
        for failing_callback in ("completed", "publish", "supervise"):
            with self.subTest(callback=failing_callback):
                cancelled = threading.Event()
                peer_started, peer_exited = threading.Event(), threading.Event()

                def transfer(job):
                    if job == 0:
                        if not peer_started.wait(3):
                            raise RuntimeError("Synthetic peer did not start")
                        return True
                    peer_started.set()
                    try:
                        if not cancelled.wait(3):
                            raise RuntimeError("Callback failure did not stop its peer")
                        return False
                    finally:
                        peer_exited.set()

                def supervise():
                    if failing_callback == "supervise":
                        if not peer_started.wait(3):
                            raise RuntimeError("Synthetic peer did not start")
                        raise ValueError("synthetic callback failure")
                    return False

                def completed(job, future):
                    future.result()
                    if failing_callback == "completed":
                        raise ValueError("synthetic callback failure")

                def publish():
                    if failing_callback == "publish":
                        raise ValueError("synthetic callback failure")

                with self.assertRaisesRegex(ValueError, "synthetic callback failure"):
                    run_archive_jobs(
                        [0, 1], 2, transfer, supervise, completed, publish, cancelled
                    )
                self.assertTrue(cancelled.is_set())
                self.assertTrue(peer_exited.is_set())

    def test_invalid_worker_counts_and_unbounded_queues_are_rejected(self):
        transfer = Mock()
        for workers, jobs in (
            (0, []),
            (51, []),
            (True, []),
            (1.5, []),
            (1, list(range(5))),
        ):
            with self.subTest(workers=workers, size=len(jobs)):
                with self.assertRaises(ValueError):
                    run_archive_jobs(
                        jobs,
                        workers,
                        transfer,
                        Mock(),
                        Mock(),
                        Mock(),
                        threading.Event(),
                    )
        transfer.assert_not_called()

    def test_empty_batch_does_not_spawn_or_publish(self):
        callbacks = [Mock() for _ in range(4)]
        run_archive_jobs([], 2, *callbacks, threading.Event())
        for callback in callbacks:
            callback.assert_not_called()

    def test_real_verified_copies_share_one_budget_and_preserve_sources(self):
        cancelled = threading.Event()
        limiter = ArchiveRateLimiter(1)
        payload = b"synthetic-recording" * 4096
        committed = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for job in range(2):
                (root / f"source-{job}.mp4").write_bytes(payload)

            def transfer(job):
                copy_verified(
                    root / f"source-{job}.mp4",
                    root / f"archive-{job}.mp4",
                    1,
                    cancelled.is_set,
                    limiter=limiter,
                )
                return len(payload)

            with patch("frigate.record.archive.fsync_directory"):
                started = time.monotonic()
                run_archive_jobs(
                    [0, 1],
                    2,
                    transfer,
                    lambda: False,
                    lambda job, future: committed.append(future.result()),
                    lambda: None,
                    cancelled,
                )
            self.assertGreaterEqual(
                time.monotonic() - started, len(payload) * 4 / 2**20 * 0.95
            )
            self.assertEqual(sum(committed), len(payload) * 2)
            for job in range(2):
                self.assertEqual((root / f"source-{job}.mp4").read_bytes(), payload)
                self.assertEqual((root / f"archive-{job}.mp4").read_bytes(), payload)

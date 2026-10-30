"""Aggregate bandwidth and cancellation checks without network or media files."""

import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from frigate.record.archive import copy_verified
from frigate.record.archive_rate import ArchiveRateLimiter


class TestArchiveRateLimiter(unittest.TestCase):
    def test_verified_parallel_copies_charge_both_phases_to_one_budget(self):
        limiter = ArchiveRateLimiter(1)
        payload = b"synthetic-recording" * 4096
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = [root / f"source-{index}.mp4" for index in range(2)]
            targets = [root / f"archive-{index}.mp4" for index in range(2)]
            for source in sources:
                source.write_bytes(payload)
            with (
                patch("frigate.record.archive.fsync_directory"),
                patch.object(limiter, "consume", wraps=limiter.consume) as consume,
                ThreadPoolExecutor(max_workers=2) as executor,
            ):
                started = time.monotonic()
                futures = [
                    executor.submit(
                        copy_verified,
                        source,
                        target,
                        1,
                        lambda: False,
                        limiter=limiter,
                    )
                    for source, target in zip(sources, targets)
                ]
                for future in futures:
                    future.result(timeout=3)
                self.assertGreaterEqual(
                    time.monotonic() - started, len(payload) * 4 / 2**20 * 0.95
                )
            self.assertEqual(
                sum(call.args[0] for call in consume.call_args_list), len(payload) * 4
            )
            for source, target in zip(sources, targets):
                self.assertEqual(source.read_bytes(), payload)
                self.assertEqual(target.read_bytes(), payload)

    def test_parallel_workers_share_one_budget(self):
        limiter = ArchiveRateLimiter(1)
        barrier = threading.Barrier(2)

        def transfer():
            barrier.wait(timeout=2)
            for _ in range(4):
                limiter.consume(32 * 1024, lambda: False)

        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(transfer) for _ in range(2)]
            for future in futures:
                future.result(timeout=3)
        self.assertGreaterEqual(time.monotonic() - started, 0.24)

    def test_waiting_for_bandwidth_is_cancellable(self):
        limiter = ArchiveRateLimiter(0.01)
        stopped = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(limiter.consume, 2**20, stopped.is_set)
            stopped.set()
            with self.assertRaises(InterruptedError):
                result.result(timeout=1)

    def test_invalid_limits_and_negative_accounting_are_errors(self):
        for value in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                ArchiveRateLimiter(value)
        with self.assertRaises(ValueError):
            ArchiveRateLimiter(1).consume(-1, lambda: False)

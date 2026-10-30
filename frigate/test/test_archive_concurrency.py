"""Adaptive copy concurrency responds only to sustained verified completions."""

import unittest
from unittest.mock import patch

from frigate.record.archive_concurrency import ArchiveConcurrency


class TestArchiveConcurrency(unittest.TestCase):
    def setUp(self):
        self.clock = self.enterContext(
            patch("frigate.record.archive_concurrency.time.monotonic", return_value=100)
        )
        self.controller = ArchiveConcurrency()

    def sample(self, mib_per_second, **values):
        self.controller.observe(
            int(mib_per_second * 2**20 * 10),
            10,
            4,
            values.get("maximum", 8),
            values.get("rate_limit", 0),
        )

    def test_short_empty_single_file_and_nonfinite_samples_do_not_ramp(self):
        for size, elapsed, files in (
            (0, 10, 4),
            (2**20, 1, 4),
            (2**20, 10, 1),
            (2**20, float("nan"), 4),
        ):
            self.controller.observe(size, elapsed, files, 8, 0)
            self.assertEqual(self.controller.current, 1)

    def test_improved_verified_throughput_probes_one_extra_worker_at_a_time(self):
        self.sample(10)
        self.assertEqual(self.controller.current, 2)
        self.sample(12)
        self.assertEqual(self.controller.current, 3)
        self.assertEqual(self.controller.reason, "probing")

    def test_no_gain_reverts_and_observes_cooldown(self):
        self.sample(10)
        self.sample(10.2)
        self.assertEqual(self.controller.current, 1)
        self.assertEqual(self.controller.reason, "no_gain")
        self.sample(10)
        self.assertEqual(self.controller.current, 1)
        self.clock.return_value = 161
        self.sample(10)
        self.assertEqual(self.controller.current, 2)

    def test_failures_back_off_and_do_not_immediately_reopen_more_workers(self):
        self.controller.current = 8
        self.controller.failed()
        self.assertEqual(self.controller.current, 4)
        self.sample(10)
        self.assertEqual(self.controller.current, 4)
        self.assertEqual(self.controller.reason, "cooldown")

    def test_runtime_cap_decrease_is_immediate(self):
        self.controller.current = 8
        self.assertEqual(self.controller.workers(2), 2)

    def test_configured_bandwidth_limit_and_worker_cap_are_respected(self):
        self.sample(9.5, rate_limit=10)
        self.assertEqual(self.controller.current, 1)
        self.assertEqual(self.controller.reason, "rate_limited")
        self.sample(10, maximum=1)
        self.assertEqual(self.controller.current, 1)
        self.assertEqual(self.controller.reason, "maximum")


if __name__ == "__main__":
    unittest.main()

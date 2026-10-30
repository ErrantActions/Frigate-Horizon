"""Bounded, opt-in concurrency search using completed verified archive batches."""

import math
import time


class ArchiveConcurrency:
    """Probe one extra worker only when sustained verified throughput justifies it."""

    def __init__(self) -> None:
        self.current = 1
        self.baseline_workers = 1
        self.baseline_rate: float | None = None
        self.probing = False
        self.cooldown_until = 0.0
        self.reason = "starting"
        self.last_rate: float | None = None

    def workers(self, maximum: int) -> int:
        """Respect a reduced configured cap immediately."""
        self.current = min(self.current, maximum)
        self.baseline_workers = min(self.baseline_workers, maximum)
        return self.current

    def failed(self) -> None:
        """Back off after errors, preserving sources and giving the destination time."""
        self.current = max(1, self.current // 2)
        self.baseline_rate = None
        self.probing = False
        self.cooldown_until = time.monotonic() + 60
        self.reason = "backoff"

    def observe(
        self, size: int, elapsed: float, files: int, maximum: int, rate_limit: float
    ) -> None:
        """Compare complete copy/readback/publication batches, never buffered writes."""
        self.workers(maximum)
        if files < 2 or size < 2**20 or not math.isfinite(elapsed) or elapsed < 5:
            self.reason = "sampling"
            return
        rate = size / elapsed
        self.last_rate = rate
        now = time.monotonic()
        if self.probing:
            if self.baseline_rate is not None and rate < self.baseline_rate * 1.05:
                self.current = self.baseline_workers
                self.probing = False
                self.cooldown_until = now + 60
                self.reason = "no_gain"
                return
            self.probing = False
        elif self.baseline_rate is not None and rate < self.baseline_rate * 0.8:
            self.failed()
            return
        self.baseline_rate = rate
        self.baseline_workers = self.current
        if rate_limit > 0 and rate >= rate_limit * 2**20 * 0.9:
            self.reason = "rate_limited"
        elif now < self.cooldown_until:
            self.reason = "cooldown"
        elif self.current < maximum:
            self.current += 1
            self.probing = True
            self.reason = "probing"
        else:
            self.reason = "maximum"

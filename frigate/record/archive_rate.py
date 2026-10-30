"""Thread-safe aggregate copy and verification bandwidth accounting."""

import math
import threading
import time
from collections.abc import Callable


class ArchiveRateLimiter:
    """Share one transfer budget across concurrent files and both I/O phases."""

    def __init__(self, mib_per_second: float) -> None:
        if not math.isfinite(mib_per_second) or mib_per_second <= 0:
            raise ValueError("A shared archive rate must be positive")
        self.bytes_per_second = mib_per_second * 2**20
        self._lock = threading.Lock()
        self._deadline = 0.0

    def consume(self, amount: int, cancelled: Callable[[], bool]) -> None:
        """Charge completed I/O once, waiting interruptibly for its global slot."""
        if amount < 0:
            raise ValueError("Transferred byte count cannot be negative")
        if cancelled():
            raise InterruptedError("Archive transfer cancelled")
        with self._lock:
            self._deadline = (
                max(self._deadline, time.monotonic()) + amount / self.bytes_per_second
            )
            deadline = self._deadline
        while (delay := deadline - time.monotonic()) > 0:
            if cancelled():
                raise InterruptedError("Archive transfer cancelled")
            time.sleep(min(delay, 0.05))

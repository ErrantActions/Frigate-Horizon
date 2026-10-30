"""Bounded archive scheduling with coordinator-owned controls and reporting."""

import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import TypeVar

Job = TypeVar("Job")
Result = TypeVar("Result")
ARCHIVE_QUEUE_MULTIPLIER = 4


def run_archive_jobs(
    jobs: Sequence[Job],
    workers: int,
    transfer: Callable[[Job], Result],
    supervise: Callable[[], bool],
    completed: Callable[[Job, Future[Result]], None],
    publish: Callable[[], None],
    cancelled: threading.Event,
) -> bool:
    """Join every admitted job, keeping control and completion callbacks on the caller.

    Transfers must observe ``cancelled``. Completion callbacks consume each
    future, account for committed work and propagate or explicitly handle errors.
    Return whether supervision cancelled the batch before its jobs completed.
    """
    if (
        type(workers) is not int
        or not 1 <= workers <= 50
        or len(jobs) > workers * ARCHIVE_QUEUE_MULTIPLIER
    ):
        raise ValueError("Invalid archive worker count or bounded queue size")
    if not jobs:
        return False
    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="archive-copy"
    ) as executor:
        try:
            pending = {executor.submit(transfer, job): job for job in jobs}
            last_report = 0.0
            while pending:
                if supervise():
                    cancelled.set()
                done, _ = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
                for future in done:
                    completed(pending.pop(future), future)
                if done or time.monotonic() - last_report >= 1:
                    publish()
                    last_report = time.monotonic()
            return cancelled.is_set()
        finally:
            cancelled.set()

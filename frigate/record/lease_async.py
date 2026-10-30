"""Renew original-media leases around asynchronous readers and owned subprocesses."""

import asyncio
import math
import subprocess
import threading
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar
from uuid import uuid4

from frigate.models import Recordings
from frigate.record.leases import lease_recordings, release_recordings

T = TypeVar("T")


class AsyncRecordingLeaseLost(RuntimeError):
    """Original-media work must stop because its source protection was lost."""


def _database_call(operation: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    database = Recordings._meta.database
    if database is None:
        raise AsyncRecordingLeaseLost("Recording database is unavailable")
    opened_here = database.is_closed()
    try:
        return operation(*args, **kwargs)
    finally:
        if opened_here:
            database.close()


async def _settle(task: asyncio.Task[T]) -> tuple[T, bool]:
    """Join a shielded operation even if its caller is repeatedly cancelled."""
    cancelled = False
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True


class AsyncRecordingLease:
    """Hold an immutable window until all supervised original readers have exited."""

    def __init__(
        self,
        camera: str,
        start: float,
        end: float,
        *,
        token: str | None = None,
        owner: str | None = None,
        lifetime: float = 300,
        interval: float = 60,
        terminate_timeout: float = 3,
    ) -> None:
        if (
            not all(
                math.isfinite(value)
                for value in (start, end, lifetime, interval, terminate_timeout)
            )
            or end < start
            or not 0 < interval < lifetime / 2
            or terminate_timeout <= 0
        ):
            raise ValueError(
                "Invalid original recording lease window or renewal interval"
            )
        self.camera, self.start, self.end = camera, start, end
        self.token = token or f"async-reader:{uuid4().hex}"
        self.owner = owner
        self.lifetime, self.interval = lifetime, interval
        self.terminate_timeout = terminate_timeout
        self.failure_event = asyncio.Event()
        self.failure: Exception | None = None
        self._stop = asyncio.Event()
        self._renewal: asyncio.Task | None = None
        self._processes: set[asyncio.subprocess.Process] = set()
        self._blocking_processes: set[subprocess.Popen] = set()
        self._registry_lock = threading.Lock()
        self._process_lock = asyncio.Lock()
        self._entered = False
        self._closing = False

    def _operation(
        self, operation: Callable[..., T], *args: Any, **kwargs: Any
    ) -> asyncio.Task[T]:
        return asyncio.create_task(
            asyncio.to_thread(_database_call, operation, *args, **kwargs)
        )

    async def _acquire(self) -> bool:
        _, cancelled = await _settle(
            self._operation(
                lease_recordings,
                self.token,
                self.camera,
                self.start,
                self.end,
                self.lifetime,
                owner=self.owner,
            )
        )
        return cancelled

    async def __aenter__(self) -> "AsyncRecordingLease":
        if self._entered or self._closing:
            raise RuntimeError("Original recording lease contexts cannot be reused")
        try:
            cancelled = await self._acquire()
        except Exception as error:
            raise AsyncRecordingLeaseLost(
                "Original recording lease acquisition failed"
            ) from error
        self._entered = True
        if cancelled:
            await _settle(self._operation(release_recordings, self.token))
            self._closing = True
            raise asyncio.CancelledError
        self._renewal = asyncio.create_task(
            self._renew(), name=f"source-lease:{self.token}"
        )
        return self

    async def _renew(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), self.interval)
                    return
                except TimeoutError:
                    pass
                if await self._acquire():
                    return
        except asyncio.CancelledError:
            return
        except Exception as error:
            self.failure = error
            self.failure_event.set()
            await self._stop_processes()

    def check(self) -> None:
        """Refuse new reads after failure or outside the owned context lifetime."""
        if self.failure is not None:
            raise AsyncRecordingLeaseLost(
                "Original recording lease renewal failed"
            ) from self.failure
        if not self._entered or self._closing:
            raise AsyncRecordingLeaseLost("Original recording lease is not active")

    async def wait_failed(self) -> None:
        """Await renewal failure; callers must cancel this waiter on normal completion."""
        await self.failure_event.wait()
        self.check()

    async def process_started(self, process: asyncio.subprocess.Process) -> None:
        """Register immediately after spawning, stopping a late child if renewal failed."""
        with self._registry_lock:
            self._processes.add(process)
        if self.failure_event.is_set() or self._closing or not self._entered:
            await self._stop_processes()
        self.check()

    def process_started_blocking(self, process: subprocess.Popen) -> None:
        """Register a child immediately from a supervised blocking-reader thread."""
        with self._registry_lock:
            self._blocking_processes.add(process)
        if self.failure_event.is_set() or self._closing or not self._entered:
            self._stop_blocking_process(process)
        self.check()

    def _stop_blocking_process(self, process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=self.terminate_timeout)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            process.wait()

    async def _stop_process(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), self.terminate_timeout)
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()

    async def _stop_processes(self) -> None:
        async with self._process_lock:
            while True:
                with self._registry_lock:
                    processes = tuple(self._processes)
                    blocking = tuple(self._blocking_processes)
                if not processes and not blocking:
                    break
                await asyncio.gather(
                    *(self._stop_process(process) for process in processes),
                    *(
                        asyncio.to_thread(self._stop_blocking_process, process)
                        for process in blocking
                    ),
                )
                with self._registry_lock:
                    self._processes.difference_update(processes)
                    self._blocking_processes.difference_update(blocking)

    async def run_blocking(
        self, operation: Callable[..., T], *args: Any, **kwargs: Any
    ) -> T:
        """Join the actual reader thread before releasing protection on cancellation."""
        self.check()
        task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            self._closing = True
            await _settle(asyncio.create_task(self._stop_processes()))
            await _settle(asyncio.create_task(self._join(task)))
            raise

    async def cleanup_blocking(self, operation: Callable[..., T], *args: Any) -> T:
        """Join cleanup even when a disconnected caller repeatedly cancels it."""
        result, cancelled = await _settle(
            asyncio.create_task(asyncio.to_thread(operation, *args))
        )
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def guard(self, work: Awaitable[T]) -> T:
        """Cancel and join original-reading work if renewal fails or the caller exits."""
        task = asyncio.ensure_future(work)
        failed = asyncio.create_task(self.failure_event.wait())
        try:
            self.check()
            await asyncio.wait((task, failed), return_when=asyncio.FIRST_COMPLETED)
            self.check()
            return await task
        finally:
            failed.cancel()
            if not task.done():
                task.cancel()
            await _settle(asyncio.create_task(self._join(task, failed)))

    async def _join(self, *tasks: asyncio.Future) -> None:
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _cleanup(self) -> None:
        self._closing = True
        stopped = False
        try:
            # Keep renewing during slow normal child termination.
            await self._stop_processes()
            stopped = True
        finally:
            self._stop.set()
            if self._renewal is not None:
                self._renewal.cancel()
                await asyncio.gather(self._renewal, return_exceptions=True)
        if stopped:
            try:
                await _settle(self._operation(release_recordings, self.token))
            except Exception as error:
                raise AsyncRecordingLeaseLost(
                    "Original recording lease release failed"
                ) from error

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        _, cancelled = await _settle(asyncio.create_task(self._cleanup()))
        if cancelled:
            raise asyncio.CancelledError
        if exc_type is None and self.failure is not None:
            raise AsyncRecordingLeaseLost(
                "Original recording lease renewal failed"
            ) from self.failure

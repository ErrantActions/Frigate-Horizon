"""Renew original-media leases for blocking readers and their child processes."""

import logging
import subprocess
import threading
from uuid import uuid4

from frigate.models import Recordings
from frigate.record.leases import lease_recordings, release_recordings

logger = logging.getLogger(__name__)


class RecordingLeaseLost(RuntimeError):
    """An original-media reader must stop because its protection was lost."""


class RenewingRecordingLease:
    """Hold one window until every owned child has exited and reading has ended."""

    def __init__(
        self, camera: str, start: float, end: float,
        *, token: str | None = None, lifetime: float = 300, interval: float = 60,
    ):
        if not 0 < interval < lifetime / 2:
            raise ValueError("Recording lease renewal interval is unsafe")
        self.token = token or f"reader:{uuid4().hex}"
        self.camera, self.start, self.end = camera, start, end
        self.lifetime, self.interval = lifetime, interval
        self.stopped = threading.Event()
        self.failed = threading.Event()
        self.thread = None
        self.processes = []
        self.lock = threading.Lock()

    def __enter__(self):
        lease_recordings(self.token, self.camera, self.start, self.end, self.lifetime)
        self.thread = threading.Thread(target=self._renew, daemon=True)
        try:
            self.thread.start()
        except BaseException:
            release_recordings(self.token)
            raise
        return self

    def _renew(self):
        try:
            with Recordings._meta.database.connection_context():
                while not self.stopped.wait(self.interval):
                    lease_recordings(
                        self.token, self.camera, self.start, self.end, self.lifetime
                    )
        except Exception:
            logger.exception("Original recording lease renewal failed; stopping reader")
            self.failed.set()
            self._stop_processes()

    def check(self):
        """Refuse further source work after a renewal failure."""
        if self.failed.is_set():
            raise RecordingLeaseLost("Original recording lease renewal failed")

    def process_started(self, process):
        """Own a media subprocess so renewal failures terminate blocked readers."""
        with self.lock:
            self.processes.append(process)
        if self.failed.is_set() or self.stopped.is_set():
            self._stop_processes()
            self.check()

    def _stop_processes(self):
        with self.lock:
            processes = list(self.processes)
        for process in processes:
            if process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                except ProcessLookupError:
                    pass

    def __exit__(self, *exc):
        self.stopped.set()
        self._stop_processes()
        if self.thread is not None:
            self.thread.join()
        try:
            release_recordings(self.token)
        except Exception:
            logger.exception("Original recording lease release failed; awaiting expiry")

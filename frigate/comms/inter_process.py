"""Facilitates communication between processes."""

import logging
import multiprocessing as mp
import threading
import time
from collections.abc import Callable
from multiprocessing.synchronize import Event as MpEvent
from typing import Any

import zmq
from peewee import OperationalError

from frigate.comms.base_communicator import Communicator
from frigate.const import INSERT_MANY_RECORDINGS

logger = logging.getLogger(__name__)

SOCKET_REP_REQ = "ipc:///tmp/cache/comms"
SOCKET_CAMERA_ACTIVITY = "ipc:///tmp/cache/camera-activity"


class InterProcessCommunicator(Communicator):
    def __init__(
        self,
        endpoint: str = SOCKET_REP_REQ,
        allowed_topics: frozenset[str] | None = None,
    ) -> None:
        # bound eagerly so subprocesses starting before start_communicators()
        # can still connect; their requests queue in zmq until the reader runs
        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.REP)
        self.socket.bind(endpoint)
        self.allowed_topics = allowed_topics
        self.stop_event: MpEvent = mp.Event()
        self.reader_thread: threading.Thread | None = None

    def publish(self, topic: str, payload: Any, retain: bool = False) -> None:
        """There is no communication back to the processes."""
        pass

    def subscribe(self, receiver: Callable) -> None:
        self._dispatcher = receiver

    def start(self) -> None:
        self.reader_thread = threading.Thread(target=self.read)
        self.reader_thread.start()

    def read(self) -> None:
        while not self.stop_event.is_set():
            while True:  # load all messages that are queued
                has_message, _, _ = zmq.select([self.socket], [], [], 1)

                if not has_message:
                    break

                try:
                    raw = self.socket.recv_json(flags=zmq.NOBLOCK)

                    if isinstance(raw, list):
                        (topic, value) = raw
                        if (
                            self.allowed_topics is not None
                            and topic not in self.allowed_topics
                        ):
                            logger.error(
                                "Rejected topic on restricted inter-process channel"
                            )
                            response = None
                        else:
                            response = self._dispatch_with_recording_retry(topic, value)
                    else:
                        logging.warning(
                            f"Received unexpected data type in ZMQ recv_json: {type(raw)}"
                        )
                        response = None

                    if response is not None:
                        self.socket.send_json(response)
                    else:
                        self.socket.send_json([])
                except zmq.ZMQError:
                    break
                except InterruptedError:
                    return

    def _dispatch_with_recording_retry(self, topic: str, value: Any) -> Any:
        """Retain a single recording insert batch across transient SQLite contention."""
        last_warning = float("-inf")
        while not self.stop_event.is_set():
            try:
                return self._dispatcher(topic, value)
            except OperationalError as error:
                if topic != INSERT_MANY_RECORDINGS or str(error).lower() not in {
                    "database is locked",
                    "database is busy",
                }:
                    raise
                now = time.monotonic()
                if now - last_warning >= 30:
                    logger.warning(
                        "Recording metadata database is busy; retaining the batch and retrying"
                    )
                    last_warning = now
                if self.stop_event.wait(1):
                    break
        raise InterruptedError("Stopped while a recording metadata batch was pending")

    def stop(self) -> None:
        self.stop_event.set()
        if self.reader_thread is not None:
            self.reader_thread.join()
        self.socket.close(linger=0)
        self.context.destroy(linger=0)


class InterProcessRequestor:
    """Simplifies sending data to InterProcessCommunicator and getting a reply."""

    def __init__(self, endpoint: str = SOCKET_REP_REQ) -> None:
        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.REQ)
        self.socket.connect(endpoint)
        self.lock = threading.Lock()

    def send_data(self, topic: str, data: Any) -> Any:
        """Sends data and then waits for reply."""
        with self.lock:
            try:
                self.socket.send_json((topic, data))
                return self.socket.recv_json()
            except zmq.ZMQError:
                return ""

    def stop(self) -> None:
        self.socket.close(linger=0)
        self.context.destroy(linger=0)

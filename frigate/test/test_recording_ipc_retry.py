"""A transient SQLite busy result must not kill recording metadata ingestion."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from peewee import OperationalError, SqliteDatabase

from frigate.comms.inter_process import InterProcessCommunicator
from frigate.const import INSERT_MANY_RECORDINGS


class TestRecordingIpcRetry(unittest.TestCase):
    def setUp(self):
        self.communicator = InterProcessCommunicator.__new__(InterProcessCommunicator)
        self.communicator.stop_event = Mock()
        self.communicator.stop_event.is_set.return_value = False
        self.communicator.stop_event.wait.return_value = False

    def test_busy_batch_is_retained_until_successful_acknowledgment(self):
        payload = [{"id": "synthetic"}]
        self.communicator._dispatcher = Mock(
            side_effect=[OperationalError("database is locked"), {"stored": True}]
        )
        with self.assertLogs("frigate.comms.inter_process", "WARNING"):
            result = self.communicator._dispatch_with_recording_retry(
                INSERT_MANY_RECORDINGS, payload
            )
        self.assertEqual(result, {"stored": True})
        self.assertEqual(self.communicator._dispatcher.call_count, 2)
        self.assertIs(self.communicator._dispatcher.call_args.args[1], payload)

    def test_shutdown_does_not_acknowledge_an_unstored_batch(self):
        self.communicator._dispatcher = Mock(
            side_effect=OperationalError("database is busy")
        )
        self.communicator.stop_event.wait.return_value = True
        with self.assertLogs("frigate.comms.inter_process", "WARNING"):
            with self.assertRaises(InterruptedError):
                self.communicator._dispatch_with_recording_retry(
                    INSERT_MANY_RECORDINGS, [{"id": "pending"}]
                )

    def test_other_errors_and_non_idempotent_topics_are_not_replayed(self):
        for topic, message in (
            ("different-topic", "database is locked"),
            (INSERT_MANY_RECORDINGS, "disk I/O error"),
        ):
            self.communicator._dispatcher = Mock(side_effect=OperationalError(message))
            with self.subTest(topic=topic, message=message):
                with self.assertRaises(OperationalError):
                    self.communicator._dispatch_with_recording_retry(topic, {})
                self.assertEqual(self.communicator._dispatcher.call_count, 1)

    def test_real_sqlite_contention_recovers_without_losing_or_duplicating_batch(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        filename = str(Path(directory) / "metadata.db")
        blocker = SqliteDatabase(filename)
        blocker.connect()
        self.addCleanup(blocker.close)
        blocker.execute_sql("CREATE TABLE metadata (id TEXT PRIMARY KEY)")
        writer = SqliteDatabase(filename, timeout=0.01)
        busy = threading.Event()
        self.communicator.stop_event = threading.Event()
        results = []

        def dispatch(_topic, payload):
            try:
                writer.execute_sql("INSERT INTO metadata VALUES (?)", (payload["id"],))
            except OperationalError:
                busy.set()
                raise
            finally:
                writer.close()
            return {"stored": True}

        self.communicator._dispatcher = dispatch
        thread = threading.Thread(
            target=lambda: results.append(
                self.communicator._dispatch_with_recording_retry(
                    INSERT_MANY_RECORDINGS, {"id": "retained"}
                )
            )
        )
        with self.assertLogs("frigate.comms.inter_process", "WARNING"):
            with blocker.atomic("IMMEDIATE"):
                thread.start()
                self.assertTrue(busy.wait(2))
            thread.join(4)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [{"stored": True}])
        self.assertEqual(
            blocker.execute_sql("SELECT COUNT(*) FROM metadata").fetchone()[0], 1
        )

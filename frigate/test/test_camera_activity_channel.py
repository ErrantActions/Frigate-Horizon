"""Real IPC evidence that camera activity does not wait behind durable writes."""

import ast
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

import zmq

from frigate.camera.activity_manager import CameraActivityManager
from frigate.comms.inter_process import InterProcessCommunicator
from frigate.const import INSERT_MANY_RECORDINGS, UPDATE_CAMERA_ACTIVITY


class TestCameraActivityChannel(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.context = zmq.Context()
        self.addCleanup(self.context.term)
        self.sockets = []

    def socket(self, endpoint):
        """Create bounded test-client sockets in their consuming thread."""
        socket = self.context.socket(zmq.REQ)
        socket.setsockopt(zmq.LINGER, 0)
        socket.setsockopt(zmq.RCVTIMEO, 3000)
        socket.setsockopt(zmq.SNDTIMEO, 3000)
        socket.connect(endpoint)
        self.addCleanup(socket.close)
        return socket

    def test_live_transitions_continue_while_durable_dispatch_is_blocked(self):
        durable_endpoint = "ipc://" + str(Path(self.directory) / "durable")
        live_endpoint = "ipc://" + str(Path(self.directory) / "activity")
        blocked = threading.Event()
        release = threading.Event()
        stored = []
        activity = []

        def write(topic, payload):
            blocked.set()
            if not release.wait(5):
                raise RuntimeError("Test writer was not released")
            stored.append((topic, payload))

        def update(topic, payload):
            activity.append((topic, json.loads(json.dumps(payload))))

        durable = InterProcessCommunicator(durable_endpoint)
        live = InterProcessCommunicator(
            live_endpoint, frozenset({UPDATE_CAMERA_ACTIVITY})
        )
        durable.subscribe(write)
        live.subscribe(update)
        durable.start()
        live.start()
        try:
            writer = self.socket(durable_endpoint)
            client = self.socket(live_endpoint)
            writer.send_json((INSERT_MANY_RECORDINGS, [{"id": "must-not-be-dropped"}]))
            self.assertTrue(blocked.wait(1))
            started = time.monotonic()
            for snapshot in (
                {"front": {"objects": [{"label": "person"}]}, "back": {"objects": []}},
                {"front": {"objects": []}, "back": {"objects": []}},
                {"back": {"objects": [{"label": "car"}]}},
                {},
            ):
                client.send_json((UPDATE_CAMERA_ACTIVITY, snapshot))
                self.assertEqual(client.recv_json(), [])
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(len(activity), 4)
            self.assertEqual(activity[0][1]["front"]["objects"][0]["label"], "person")
            self.assertEqual(activity[1][1]["front"]["objects"], [])
            self.assertEqual(activity[-1][1], {})
            self.assertEqual(stored, [])
            client.send_json((INSERT_MANY_RECORDINGS, [{"id": "invalid-channel"}]))
            self.assertEqual(client.recv_json(), [])
            self.assertEqual(len(activity), 4)
            release.set()
            self.assertEqual(writer.recv_json(), [])
            self.assertEqual(
                stored, [(INSERT_MANY_RECORDINGS, [{"id": "must-not-be-dropped"}])]
            )
        finally:
            release.set()
            durable.stop()
            live.stop()
        self.assertFalse(live.reader_thread.is_alive())
        self.assertFalse(durable.reader_thread.is_alive())

    def test_all_activity_send_sites_use_dedicated_requestor_but_durable_work_does_not(
        self,
    ):
        source = (Path(__file__).parents[1] / "track/object_processing.py").read_text()
        calls = [
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "send_data"
            and node.args
        ]
        activities = [
            node
            for node in calls
            if isinstance(node.args[0], ast.Name)
            and node.args[0].id == "UPDATE_CAMERA_ACTIVITY"
        ]
        self.assertEqual(len(activities), 2)
        self.assertTrue(
            all(node.func.value.attr == "activity_requestor" for node in activities)
        )
        durable = [
            node
            for node in calls
            if isinstance(node.args[0], ast.Name)
            and node.args[0].id == "UPSERT_REVIEW_SEGMENT"
        ]
        self.assertEqual(len(durable), 1)
        self.assertEqual(durable[0].func.value.attr, "requestor")

    def test_legacy_and_fast_lane_updates_cannot_interleave(self):
        manager = CameraActivityManager.__new__(CameraActivityManager)
        manager._update_lock = threading.Lock()
        entered = threading.Event()
        release = threading.Event()
        seen = []

        def update(snapshot):
            seen.append(snapshot)
            if snapshot == {"first": {}}:
                entered.set()
                release.wait(3)

        manager._update_activity = Mock(side_effect=update)
        first = threading.Thread(target=manager.update_activity, args=({"first": {}},))
        second = threading.Thread(
            target=manager.update_activity, args=({"second": {}},)
        )
        first.start()
        self.assertTrue(entered.wait(1))
        second.start()
        try:
            self.assertEqual(seen, [{"first": {}}])
        finally:
            release.set()
            first.join(3)
            second.join(3)
        self.assertEqual(seen, [{"first": {}}, {"second": {}}])
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())

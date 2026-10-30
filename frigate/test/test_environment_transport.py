"""Local-only ZMQ glare transport uses isolated endpoints and no camera data."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from frigate.comms.environment import GlareSignalPublisher, GlareSignalReceiver
from frigate.comms.zmq_proxy import ZmqProxy
from frigate.environment.messages import GlareSignal


class TestGlareTransport(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        pub = f"ipc://{Path(self.directory) / 'publish'}"
        sub = f"ipc://{Path(self.directory) / 'subscribe'}"
        self.enterContext(patch("frigate.comms.zmq_proxy.SOCKET_PUB", pub))
        self.enterContext(patch("frigate.comms.zmq_proxy.SOCKET_SUB", sub))
        self.enterContext(patch("frigate.comms.environment.SOCKET_SUB", sub))
        self.proxy = ZmqProxy()
        self.addCleanup(self.proxy.stop)

    async def test_roundtrip_metadata_keeps_camera_and_does_not_forward_image_pixels(
        self,
    ):
        receiver = GlareSignalReceiver()
        publisher = GlareSignalPublisher("front")
        self.addCleanup(publisher.stop)
        self.addCleanup(receiver.stop)
        message = GlareSignal("front", "a" * 64, "glare", time.time(), ("driveway",))
        received = None
        deadline = time.monotonic() + 3
        while received is None and time.monotonic() < deadline:
            publisher.publish(message)
            received = await receiver.receive()
        self.assertEqual(received, message)


if __name__ == "__main__":
    unittest.main()

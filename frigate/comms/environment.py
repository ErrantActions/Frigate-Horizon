"""Local ZMQ glare signals; these are not outbound WebSocket topics."""

import json
import logging
from dataclasses import asdict

import zmq
import zmq.asyncio

from frigate.comms.zmq_proxy import SOCKET_SUB, Publisher
from frigate.environment.messages import GLARE_TOPIC, GlareSignal

logger = logging.getLogger(__name__)


class GlareSignalPublisher:
    """Publish metadata only, without exporting image pixels."""

    def __init__(self, camera: str):
        self._publisher = Publisher[dict[str, object]](f"{GLARE_TOPIC}{camera}")

    def publish(self, signal: GlareSignal) -> None:
        """Send one camera's status or transient-glare signal."""
        self._publisher.publish(asdict(signal))

    def stop(self) -> None:
        """Close this worker's local publisher."""
        self._publisher.stop()


class GlareSignalReceiver:
    """Consume bounded local messages on the environment service's own event loop."""

    def __init__(self):
        self.context = zmq.asyncio.Context()
        self.socket = self.context.socket(zmq.SUB)
        self.socket.setsockopt(zmq.MAXMSGSIZE, 65536)
        self.socket.setsockopt_string(zmq.SUBSCRIBE, GLARE_TOPIC)
        try:
            self.socket.connect(SOCKET_SUB)
        except zmq.ZMQError:
            self.stop()
            raise

    async def receive(self) -> GlareSignal | None:
        """Wait briefly for a validated signal, logging malformed metadata explicitly."""
        if not await self.socket.poll(timeout=500):
            return None
        try:
            raw = await self.socket.recv_string()
            topic, payload = raw.split(" ", 1)
            signal = GlareSignal.parse(json.loads(payload))
            if topic != f"{GLARE_TOPIC}{signal.camera}":
                raise ValueError("Glare topic does not match camera identity")
        except (ValueError, UnicodeDecodeError):
            logger.warning("Ignoring malformed local glare signal")
            return None
        return signal

    def stop(self) -> None:
        """Close the local subscriber on its owning loop."""
        self.socket.close(linger=0)
        self.context.destroy(linger=0)

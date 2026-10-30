"""Adapt detect-stream luma and configured zones to lightweight glare analysis."""

import logging
import time
from typing import TYPE_CHECKING

import cv2
import numpy as np
import zmq

from frigate.comms.environment import GlareSignalPublisher
from frigate.const import REPLAY_CAMERA_PREFIX
from frigate.environment.glare import GlareDetector
from frigate.environment.messages import GlareSignal, glare_configuration_hash

if TYPE_CHECKING:
    from frigate.config import CameraConfig

logger = logging.getLogger(__name__)
GLARE_ANALYSIS_WIDTH = 320
GLARE_HEARTBEAT_SECONDS = 5


class CameraGlareMonitor:
    """Analyze a small luma image without changing motion or object-detection inputs."""

    def __init__(self, camera: "CameraConfig", frame_shape: tuple[int, int]):
        if camera.name is None:
            raise ValueError("Glare monitoring requires a camera identity")
        self.name = camera.name
        self.frame_shape = frame_shape
        self.camera = camera
        self.publisher: GlareSignalPublisher | None = None
        self.detector: GlareDetector | None = None
        self.configuration_hash: str | None = None
        self._last_heartbeat = float("-inf")
        self._sample_shape = frame_shape
        self.reconfigure(camera)

    def reconfigure(self, camera: "CameraConfig") -> None:
        """Reset only when relevant analysis settings or zone geometry change."""
        self.camera = camera
        enabled = (
            not self.name.startswith(REPLAY_CAMERA_PREFIX)
            and camera.enabled
            and camera.environment.enabled
            and camera.environment.glare.enabled
        )
        if not enabled:
            self.detector = None
            return
        digest = glare_configuration_hash(camera, self.frame_shape)
        if digest == self.configuration_hash and self.detector is not None and enabled:
            return
        self.configuration_hash = digest
        self.detector = None
        height, width = self.frame_shape
        scale = min(1.0, GLARE_ANALYSIS_WIDTH / width)
        sample_width = max(1, round(width * scale))
        sample_height = max(1, round(height * scale))
        self._sample_shape = (sample_height, sample_width)
        masks = {}
        try:
            if self.publisher is None:
                self.publisher = GlareSignalPublisher(self.name)
            for name in camera.environment.glare.zones:
                zone = camera.zones.get(name)
                if zone is None or zone.contour.size == 0:
                    raise ValueError("Glare zone geometry is unavailable")
                contour = zone.contour.astype(np.float32).copy()
                contour[:, 0] *= sample_width / width
                contour[:, 1] *= sample_height / height
                mask = np.zeros(self._sample_shape, np.uint8)
                cv2.fillPoly(mask, [contour.astype(np.int32)], 1)
                masks[name] = mask.astype(np.bool_)
            self.detector = GlareDetector(camera.environment.glare, masks)
        except (ValueError, cv2.error, zmq.ZMQError):
            logger.exception("Glare analysis unavailable for camera %s", self.name)
            self._send("unavailable", time.time(), error_code="invalid_glare_zones")
        self._last_heartbeat = float("-inf")

    def _send(
        self,
        kind: str,
        frame_time: float,
        zones: tuple[str, ...] = (),
        error_code: str | None = None,
    ) -> None:
        if self.publisher is None or self.configuration_hash is None:
            return
        try:
            self.publisher.publish(
                GlareSignal(
                    self.name,
                    self.configuration_hash,
                    kind,
                    frame_time,
                    zones,
                    error_code,
                )
            )
        except zmq.ZMQError:
            logger.exception(
                "Unable to publish glare analysis for camera %s", self.name
            )

    def update(
        self, frame: np.ndarray, frame_time: float, camera_moving: bool = False
    ) -> None:
        """Inspect copied/downscaled luma, leaving shared frame memory untouched."""
        if self.detector is None:
            return
        now = time.monotonic()
        height, width = self.frame_shape
        try:
            if frame.ndim != 2 or frame.shape[0] < height or frame.shape[1] != width:
                raise ValueError(
                    "Glare frame geometry does not match the detect stream"
                )
            luma = cv2.resize(
                frame[:height, :width],
                (self._sample_shape[1], self._sample_shape[0]),
                interpolation=cv2.INTER_AREA,
            )
            zones = self.detector.update(luma, now, lighting_transition=camera_moving)
        except (ValueError, cv2.error):
            logger.exception("Glare analysis rejected a frame for camera %s", self.name)
            self._send("unavailable", frame_time, error_code="invalid_glare_frame")
            self.detector = None
            return
        if zones:
            self._send("glare", frame_time, zones)
        if now - self._last_heartbeat >= GLARE_HEARTBEAT_SECONDS:
            self._send("ready", frame_time)
            self._last_heartbeat = now

    def stop(self) -> None:
        """Release this camera worker's optional local signal publisher."""
        if self.publisher is not None:
            self.publisher.stop()

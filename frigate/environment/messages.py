"""Small local-only glare signals bound to the camera's current analysis settings."""

import hashlib
import json
from dataclasses import dataclass
from math import isfinite
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from frigate.config import CameraConfig

GLARE_TOPIC = "environment/glare/"


def glare_configuration_hash(
    camera: "CameraConfig", frame_shape: tuple[int, int] | None = None
) -> str:
    """Fingerprint configuration, not credentials or an authentication boundary."""
    settings = camera.environment.glare
    payload = {
        "camera": camera.name,
        "glare": settings.model_dump(mode="json"),
        "shape": frame_shape if frame_shape is not None else camera.frame_shape,
        "zones": {
            name: camera.zones[name].model_dump(mode="json", include={"coordinates"})
            if name in camera.zones
            else None
            for name in settings.zones
        },
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class GlareSignal:
    camera: str
    configuration_hash: str
    kind: str
    frame_time: float
    zones: tuple[str, ...] = ()
    error_code: str | None = None
    version: int = 1

    @classmethod
    def parse(cls, payload: object) -> "GlareSignal":
        """Reject malformed local signals rather than inventing camera identity or time."""
        if not isinstance(payload, dict):
            raise ValueError("Glare signal must be an object")
        if set(payload) != {
            "version",
            "camera",
            "configuration_hash",
            "kind",
            "frame_time",
            "zones",
            "error_code",
        }:
            raise ValueError("Glare signal fields do not match its version")
        version = payload.get("version")
        camera = payload.get("camera")
        digest = payload.get("configuration_hash")
        kind = payload.get("kind")
        timestamp = payload.get("frame_time")
        zones = payload.get("zones")
        error = payload.get("error_code")
        if (
            type(version) is not int
            or version != 1
            or not isinstance(camera, str)
            or not camera
            or len(camera) > 128
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(letter not in "0123456789abcdef" for letter in digest)
            or kind not in ("ready", "glare", "unavailable")
            or not isinstance(kind, str)
            or isinstance(timestamp, bool)
            or not isinstance(timestamp, (int, float))
            or not isfinite(timestamp)
            or timestamp < 0
            or not isinstance(zones, list)
            or len(zones) > 64
            or any(not isinstance(name, str) or not name for name in zones)
        ):
            raise ValueError("Invalid glare signal fields")
        if len(set(zones)) != len(zones) or (kind == "glare" and not zones):
            raise ValueError("Invalid glare event zones")
        if error is not None and (
            not isinstance(error, str) or not error or len(error) > 64
        ):
            raise ValueError("Invalid glare error code")
        if kind == "unavailable" and error is None:
            raise ValueError("Unavailable glare analysis requires an error code")
        return cls(camera, digest, kind, float(timestamp), tuple(zones), error)

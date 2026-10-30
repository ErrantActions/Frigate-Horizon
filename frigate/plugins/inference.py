"""Short camera-scoped leases for event-triggered object detection."""

import json
import logging
import math
import threading
import time
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from frigate.plugins.packages import write_private_json

logger = logging.getLogger(__name__)
LEASE_PATH = Path("/dev/shm/horizon-plugin-inference.json")


class EventInferenceConfig(BaseModel):
    """Opt into bounded AI only after the selected camera reports motion."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    window_seconds: Literal[15, 30, 60] = 30
    triggers: list[Literal["motion", "person"]] = Field(
        default=["motion"], min_length=1, max_length=1
    )


@lru_cache(maxsize=4)
def _read_leases(path: str, modified: int, size: int) -> dict:
    try:
        data = json.loads(Path(path).read_bytes())
        if not isinstance(data, dict):
            raise ValueError("Lease map must be an object")
        for name, lease in data.items():
            if not isinstance(name, str) or not isinstance(lease, dict):
                raise ValueError("Invalid lease entry")
            start, end = lease["started"], lease["expires"]
            if (
                type(start) not in (int, float)
                or type(end) not in (int, float)
                or not math.isfinite(start)
                or not math.isfinite(end)
                or not 0 < end - start <= 60
            ):
                raise ValueError("Invalid inference lease interval")
        return data
    except (OSError, ValueError, KeyError, TypeError):
        logger.error("Invalid plugin inference lease; event inference remains off")
        return {}


def lease_status(
    camera: str | None, now: float | None = None, *, path: Path | None = None
) -> dict:
    """Never interpret wall-clock changes or an expired lease as permission."""
    path = LEASE_PATH if path is None else path
    try:
        metadata = path.stat()
    except FileNotFoundError:
        return {"active": False, "remaining_seconds": 0}
    except OSError:
        logger.error(
            "Unable to read plugin inference lease; event inference remains off"
        )
        return {"active": False, "remaining_seconds": 0}
    lease = _read_leases(str(path), metadata.st_mtime_ns, metadata.st_size).get(camera)
    current = time.monotonic() if now is None else now
    active = lease is not None and lease["started"] <= current < lease["expires"]
    return {
        "active": active,
        "remaining_seconds": max(0, lease["expires"] - current)
        if active and lease is not None
        else 0,
    }


class EventInferenceController:
    """Persist preferences separately from backward-compatible plugin manifests."""

    def __init__(
        self,
        root: Path,
        clock: Callable[[], float] = time.monotonic,
        *,
        lease_path: Path | None = None,
    ) -> None:
        self.path = root / "inference-settings.json"
        self.clock = clock
        self.lease_path = LEASE_PATH if lease_path is None else lease_path
        self.preferences: dict[str, EventInferenceConfig] = {}
        self.leases: dict[str, dict[str, float]] = {}
        self.lock = threading.RLock()

    def initialize(self) -> None:
        """Restore preferences but require a new event after every service start."""
        with self.lock:
            if self.path.exists():
                self.preferences = TypeAdapter(
                    dict[str, EventInferenceConfig]
                ).validate_json(self.path.read_bytes())
            self.leases = {}
            write_private_json(self.lease_path, {})

    def configure(self, camera: str, settings: EventInferenceConfig) -> None:
        """Changing settings never fabricates a motion trigger."""
        with self.lock:
            updated = {**self.preferences, camera: settings}
            write_private_json(
                self.path,
                {name: value.model_dump() for name, value in updated.items()},
            )
            self.preferences = updated
            if not settings.enabled:
                self.revoke(camera)

    def trigger(self, camera: str, kind: str) -> bool:
        """New accepted motion refreshes the deadline; doorbell presses do not."""
        with self.lock:
            settings = self.preferences.get(camera)
            if (
                settings is None
                or not settings.enabled
                or kind not in settings.triggers
            ):
                return False
            now = self.clock()
            leases = {
                name: lease
                for name, lease in self.leases.items()
                if lease["expires"] > now
            }
            leases[camera] = {
                "started": now,
                "expires": now + settings.window_seconds,
            }
            write_private_json(self.lease_path, leases)
            self.leases = leases
            return True

    def revoke(self, camera: str) -> None:
        """Revoke decoder permission without touching any recorder."""
        with self.lock:
            leases = {
                name: lease for name, lease in self.leases.items() if name != camera
            }
            write_private_json(self.lease_path, leases)
            self.leases = leases

    def status(self, camera: str) -> dict:
        """Report an allowed AI window, not a claim of successful detections."""
        with self.lock:
            settings = self.preferences.get(camera, EventInferenceConfig())
            return {
                **settings.model_dump(),
                **lease_status(camera, self.clock(), path=self.lease_path),
            }

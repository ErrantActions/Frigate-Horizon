"""Person-specific, bounded Coral windows using the existing inference lease engine."""

from __future__ import annotations

import json
import logging
import math
import re
import time
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

from frigate.const import CONFIG_DIR
from frigate.plugins.inference import (
    EventInferenceConfig,
    EventInferenceController,
    lease_status,
)
from frigate.plugins.packages import write_private_json

if TYPE_CHECKING:
    from frigate.config import CameraConfig, FrigateConfig
    from frigate.track.tracked_object import TrackedObject

logger = logging.getLogger(__name__)
LEASE_PATH = Path("/dev/shm/horizon-native-person-inference.json")
WINDOWS_PATH = Path("/dev/shm/horizon-native-person-windows.json")
WINDOW_SECONDS = 30
FINISH_GRACE_SECONDS = 120


class PersonWindow(TypedDict):
    """A verified native episode's monotonic permission and matching frame interval."""

    started: float
    expires: float
    wall_start: float
    wall_end: float


@lru_cache(maxsize=4)
def _read_windows(path: str, modified: int, size: int) -> dict[str, list[PersonWindow]]:
    try:
        if size > 128 * 1024:
            raise ValueError("Native person window file exceeds its limit")
        value = json.loads(Path(path).read_bytes())
        if not isinstance(value, dict) or len(value) > 64:
            raise ValueError("Invalid native person window map")
        result: dict[str, list[PersonWindow]] = {}
        for camera, windows in value.items():
            if (
                not isinstance(camera, str)
                or not isinstance(windows, list)
                or len(windows) > 4
            ):
                raise ValueError("Invalid native person window list")
            result[camera] = []
            for window in windows:
                for key in ("started", "expires", "wall_start", "wall_end"):
                    if type(window[key]) not in (int, float) or not math.isfinite(
                        window[key]
                    ):
                        raise ValueError("Invalid native person window time")
                if (
                    window["expires"] != window["started"] + WINDOW_SECONDS
                    or window["wall_end"] != window["wall_start"] + WINDOW_SECONDS
                ):
                    raise ValueError("Native person window exceeds its hard limit")
                result[camera].append(
                    PersonWindow(
                        started=float(window["started"]),
                        expires=float(window["expires"]),
                        wall_start=float(window["wall_start"]),
                        wall_end=float(window["wall_end"]),
                    )
                )
        return result
    except (OSError, ValueError, TypeError, KeyError):
        logger.error(
            "Invalid native person windows; analysis and best-shot permission remain off"
        )
        return {}


def windows_for(camera: str | None) -> list[PersonWindow]:
    """Read only validated grants, never infer permission from a native motion event."""
    if camera is None:
        return []
    try:
        metadata = WINDOWS_PATH.stat()
    except FileNotFoundError:
        return []
    except OSError:
        logger.error("Unable to read native person windows")
        return []
    return _read_windows(str(WINDOWS_PATH), metadata.st_mtime_ns, metadata.st_size).get(
        camera, []
    )


def native_person_active(camera: str | None, now: float | None = None) -> bool:
    """Both the private lease and its verified, hard-30-second grant must be active."""
    current = time.monotonic() if now is None else now
    return lease_status(camera, current, path=LEASE_PATH)["active"] and any(
        window["started"] <= current < window["expires"]
        for window in windows_for(camera)
    )


def person_window_matches(
    camera: str | None, frame_time: float, now: float | None = None
) -> bool:
    """Keep enough bounded history to finish a real tracked event after decoder shutdown."""
    current = time.monotonic() if now is None else now
    return any(
        window["started"] <= current <= window["expires"] + FINISH_GRACE_SECONDS
        and window["wall_start"] <= frame_time <= window["wall_end"]
        for window in windows_for(camera)
    )


def native_person_snapshot_allowed(camera: CameraConfig, obj: TrackedObject) -> bool:
    """Only confirmed person tracks captured during a granted window may opt into snapshots."""
    if (
        not getattr(camera.snapshots, "native_person", False)
        or obj.false_positive
        or obj.obj_data.get("label") != "person"
        or not obj.thumbnail_data
    ):
        return False
    return person_window_matches(camera.name, obj.thumbnail_data["frame_time"])


def person_review_window_matches(camera: str | None, started_at: float) -> bool:
    """A person can join an existing vehicle/manual review after its start time."""
    now = time.monotonic()
    return any(
        window["started"] <= now <= window["expires"] + FINISH_GRACE_SECONDS
        and started_at <= window["wall_end"]
        for window in windows_for(camera)
    )


def person_window_finishing(camera: str, started_at: float) -> bool:
    """Give the real track-end/database callbacks up to five seconds to finish."""
    now = time.monotonic()
    return any(
        started_at <= window["wall_end"]
        and window["expires"] <= now < window["expires"] + 5
        for window in windows_for(camera)
    )


def defer_person_alert(camera: CameraConfig, payload: dict) -> bool:
    """Choose the completed best shot before consuming the existing shared cooldown."""
    after = payload.get("after", {})
    labels = after.get("data", {}).get("objects", [])
    started = after.get("start_time")
    return (
        getattr(camera.snapshots, "native_person", False)
        and payload.get("type") not in ("end", "genai")
        and bool({"person", "person-verified"}.intersection(labels))
        and type(started) in (int, float)
        and person_review_window_matches(camera.name, started)
    )


class NativePersonController:
    """Accept only native person starts, never polling refreshes or generic motion."""

    def __init__(
        self,
        config_provider: Callable[[], FrigateConfig],
        *,
        root: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.config_provider = config_provider
        self.clock = clock
        self.wall_clock = wall_clock
        self.inference = EventInferenceController(
            root or Path(CONFIG_DIR) / "native-person-snapshots",
            clock=clock,
            lease_path=LEASE_PATH,
        )
        self.history: dict[str, list[PersonWindow]] = {}
        self.consumed: dict[str, str] = {}
        self.seen: dict[str, float] = {}
        self.statuses: dict[str, str] = {}
        self.initialized = False

    def initialize(self) -> None:
        """A service restart never inherits a permission to decode."""
        LEASE_PATH.unlink(missing_ok=True)
        WINDOWS_PATH.unlink(missing_ok=True)
        self.history = {}
        self.consumed = {}
        self.seen = {}
        self.initialized = False
        self.sync()

    def sync(self) -> None:
        """Revoke disabled/unsupported cameras and prune bounded completion grants."""
        from frigate.comms.reolink_api import native_api_selected
        from frigate.motion.record_only import recording_only

        config = self.config_provider()
        requested = {
            name
            for name, camera in config.cameras.items()
            if camera.enabled and getattr(camera.snapshots, "native_person", False)
        }
        if requested and not self.initialized:
            self.inference.initialize()
            self.initialized = True
        changed = False
        for name in set(self.inference.preferences) | set(config.cameras):
            reason = "disabled"
            if name in requested:
                model = config.model_for_camera(name)
                if not native_api_selected(name):
                    reason = "native_person_signal_unavailable"
                elif not recording_only(name):
                    reason = "recording_only_mode_required"
                elif (
                    not model.devices
                    or any(
                        device.split(":", 1)[0] != "edgetpu" for device in model.devices
                    )
                    or "person" not in model.merged_labelmap.values()
                ):
                    reason = "coral_person_model_required"
                else:
                    reason = "ready"
            if (
                reason not in ("ready", "disabled")
                and self.statuses.get(name) != reason
            ):
                logger.warning(
                    "Native person analysis unavailable for %s (%s)", name, reason
                )
            self.statuses[name] = reason
            settings = EventInferenceConfig(
                enabled=reason == "ready", window_seconds=30, triggers=["person"]
            )
            if self.initialized and self.inference.preferences.get(name) != settings:
                self.inference.configure(name, settings)
            if reason != "ready" and name in self.history:
                del self.history[name]
                changed = True
        now = self.clock()
        for name, windows in tuple(self.history.items()):
            retained = [
                window
                for window in windows
                if now <= window["expires"] + FINISH_GRACE_SECONDS
            ]
            if retained != windows:
                self.history[name] = retained
                changed = True
        self.seen = {key: expiry for key, expiry in self.seen.items() if expiry > now}
        if changed:
            write_private_json(WINDOWS_PATH, self.history)

    def consume(self, payload: dict) -> bool:
        """Return True only for a person episode owned by this opt-in analysis path."""
        camera, identity, action = (
            payload.get("camera"),
            payload.get("event_id"),
            payload.get("action"),
        )
        if not isinstance(camera, str) or not isinstance(identity, str):
            raise ValueError("Invalid native person episode identity")
        if identity in self.consumed:
            if self.consumed[identity] != camera:
                raise ValueError("Native person episode changed its camera owner")
            if action == "end":
                del self.consumed[identity]
            return True
        if (
            action != "start"
            or payload.get("label") != "person"
            or payload.get("source") != "reolink"
        ):
            return False
        self.sync()
        if self.statuses.get(camera) != "ready":
            return False
        timestamp = payload.get("timestamp")
        if (
            not isinstance(identity, str)
            or re.fullmatch(r"\d+\.\d{6}-[a-f0-9]{6}", identity) is None
            or not isinstance(timestamp, (int, float))
            or isinstance(timestamp, bool)
            or not math.isfinite(timestamp)
            or abs(timestamp - self.wall_clock()) > 30
        ):
            raise ValueError("Invalid native person trigger")
        if identity in self.seen:
            return True
        if len(self.seen) >= 512 or len(self.consumed) >= 64:
            logger.warning("Native person episode capacity reached; trigger discarded")
            return True
        now = self.clock()
        self.seen[identity] = now + 600
        self.consumed[identity] = camera
        # A long-lived person signal and its refreshes cannot extend the window.
        if native_person_active(camera, now):
            return True
        if not self.inference.trigger(camera, "person"):
            raise RuntimeError("Configured native person lease was rejected")
        wall = self.wall_clock()
        window: PersonWindow = {
            "started": now,
            "expires": now + 30,
            "wall_start": wall,
            "wall_end": wall + 30,
        }
        self.history[camera] = [*self.history.get(camera, [])[-3:], window]
        write_private_json(WINDOWS_PATH, self.history)
        return True

    def status(self, camera: str) -> dict:
        """Report permission, not an unverified claim that Coral found or saved a person."""
        return {
            "state": self.statuses.get(camera, "disabled"),
            "active": native_person_active(camera, self.clock()),
            "window_seconds": WINDOW_SECONDS,
            "trigger": "camera_native_person",
            "label": "person",
        }

    def close(self) -> None:
        """Revoke only this controller's leases; existing Ring motion leases are untouched."""
        if self.initialized:
            write_private_json(LEASE_PATH, {})
            write_private_json(WINDOWS_PATH, {})

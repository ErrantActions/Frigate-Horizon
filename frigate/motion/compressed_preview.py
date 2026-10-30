"""Explicit bounded compressed-vector inspection without pixel decoding or configuration writes."""

import logging
import secrets
import subprocess
import threading
import time
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from frigate.comms.reolink_api import native_api_selected
from frigate.config import CameraConfig
from frigate.motion.compressed_live import BINARY, native_media_input
from frigate.motion.compressed_protocol import CompressedEvidence, read_extractor_events
from frigate.motion.compressed_tuner import CompressedNoiseEstimator
from frigate.motion.record_only import recording_only

logger = logging.getLogger(__name__)


class VectorPreviewOptions(BaseModel):
    """Temporary preview settings, never changes to the camera or saved detector settings."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    min_distance: float = Field(default=0.5, ge=0, le=4096)
    max_distance: float | None = Field(default=None, gt=0, le=16384)
    min_activity: float = Field(default=0, ge=0, le=1)
    auto_tune: bool = False

    @model_validator(mode="after")
    def ordered_distance(self) -> Self:
        """Reject reversed ranges rather than silently showing no movement."""
        if self.max_distance is not None and self.max_distance <= self.min_distance:
            raise ValueError("Maximum expected distance must exceed the minimum")
        return self


class VectorPreview:
    """One leased source with a hard lifetime and no decoded image output."""

    def __init__(
        self,
        camera: CameraConfig,
        options: VectorPreviewOptions,
        *,
        background: bool = False,
    ) -> None:
        self.camera = camera.name
        self.id = secrets.token_hex(16)
        self.options = options
        self.background = background
        self.lifetime = 180 if options.auto_tune else 120
        self.started = time.monotonic()
        self.last_poll = self.started
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.stop_lock = threading.Lock()
        self.latest: dict[str, object] | None = None
        self.error: str | None = None
        self.noise = CompressedNoiseEstimator() if options.auto_tune else None
        source = native_media_input(camera)
        self.source = source
        command = [
            BINARY,
            "--live-input",
            "--framed",
            "--min-motion",
            str(options.min_distance),
        ]
        if not background and not options.auto_tune:
            command += ["--seconds", "120"]
        if options.max_distance is not None:
            command += ["--max-motion", str(options.max_distance)]
        if source.wallclock:
            command.append("--wallclock")
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            start_new_session=True,
        )
        assert self.process.stdin is not None
        try:
            self.process.stdin.write((source.source.get_secret_value() + "\n").encode())
            self.process.stdin.close()
            self.process.stdin = None
        except OSError:
            self.process.terminate()
            self.process.wait(timeout=5)
            raise
        self.timer = (
            None if background else threading.Timer(self.lifetime + 5, self.stop)
        )
        if self.timer is not None:
            self.timer.daemon = True
        self.thread = threading.Thread(
            target=self._read, name="compressed-vector-preview", daemon=True
        )
        if self.timer is not None:
            self.timer.start()
        self.thread.start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        next_snapshot = 0.0
        try:
            for event in read_extractor_events(self.process.stdout):
                if self.stopped.is_set() or (
                    not self.background
                    and (
                        time.monotonic() - self.last_poll > 20
                        or time.monotonic() - self.started > self.lifetime
                    )
                ):
                    break
                if not isinstance(event, CompressedEvidence):
                    continue
                tuning = self.noise.observe(event) if self.noise is not None else None
                if time.monotonic() < next_snapshot:
                    continue
                if event.vector_x is None or event.vector_y is None:
                    raise ValueError("Extractor direction data is unavailable")
                data: dict[str, object] = {
                    "updated_at": time.time(),
                    "source_pts": event.pts,
                    "width": event.width,
                    "height": event.height,
                    "rows": 18,
                    "columns": 32,
                    "picture_type": event.picture_type,
                    "activity": event.activity,
                    "coded_activity": event.coded_activity,
                    "vector_x": event.vector_x,
                    "vector_y": event.vector_y,
                    "uncertainty": event.uncertainty,
                    "minimum_activity": self.options.min_activity,
                    "autotune": tuning,
                }
                with self.lock:
                    self.latest = data
                next_snapshot = time.monotonic() + 0.5
        except (ValueError, OSError, EOFError) as error:
            if not self.stopped.is_set():
                self.error = type(error).__name__
                logger.error(
                    "Compressed vector preview failed for %s (%s)",
                    self.camera,
                    self.error,
                )
        finally:
            self.stop()

    def snapshot(self) -> dict[str, object]:
        """Read cached data; polling never starts a new source or renderer."""
        self.last_poll = time.monotonic()
        with self.lock:
            data = self.latest
        updated = data.get("updated_at") if data else None
        age = time.time() - updated if isinstance(updated, (int, float)) else None
        status = (
            "error"
            if self.error
            else "stopped"
            if self.stopped.is_set()
            else "warming_up"
            if data is None
            else "stale"
            if age is None or age > 3
            else "running"
        )
        return {
            "camera": self.camera,
            "session": self.id,
            "status": status,
            "server_time": time.time(),
            "error": self.error,
            "preview_only": True,
            "pixels_decoded": False,
            "settings_saved": False,
            "expires_in": None
            if self.background
            else max(0, self.lifetime - (time.monotonic() - self.started)),
            "background_learning": self.background,
            "data": data if status == "running" else None,
        }

    def stop(self) -> None:
        """Stop and reap only this source; recording and detector workers are untouched."""
        with self.stop_lock:
            if not self.stopped.is_set():
                self.stopped.set()
                if self.timer is not None:
                    self.timer.cancel()
                if self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=5)
                if self.process.stdout is not None:
                    self.process.stdout.close()
        if threading.current_thread() is not self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)


class BackgroundPreviewLease:
    """A viewer lease never owns or stops the camera's continuously learning worker."""

    def __init__(self, manager: "VectorPreviewManager", camera: str) -> None:
        self.manager = manager
        self.camera = camera
        self.id = secrets.token_hex(16)
        self.started = time.monotonic()
        self.stopped = threading.Event()
        self.last_poll = self.started

    def snapshot(self) -> dict[str, object]:
        """Read this camera's own model, including across a same-camera worker restart."""
        self.last_poll = time.monotonic()
        if time.monotonic() - self.started > 120:
            self.stopped.set()
        worker = self.manager.background.get(self.camera)
        if self.stopped.is_set() or worker is None:
            return {
                "camera": self.camera,
                "session": self.id,
                "status": "stopped",
                "data": None,
            }
        value = worker.snapshot()
        value["session"] = self.id
        value["expires_in"] = max(0, 120 - (time.monotonic() - self.started))
        return value

    def stop(self) -> None:
        """Close only the viewer, not another camera's or this camera's learning."""
        self.stopped.set()


class VectorPreviewManager:
    """Allow one explicit preview and release it on stop, lease expiry or application shutdown."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.preview: VectorPreview | BackgroundPreviewLease | None = None
        self.background: dict[str, VectorPreview] = {}
        self.background_cameras: dict[str, CameraConfig] = {}
        self.stop_background = threading.Event()
        self.supervisor: threading.Thread | None = None

    def start_background(self, cameras: dict[str, CameraConfig]) -> None:
        """Start an independent compressed-only model for each explicitly selected camera."""
        selected = {
            name: camera
            for name, camera in cameras.items()
            if camera.enabled and recording_only(name) and not native_api_selected(name)
        }
        if not selected:
            return
        if not Path(BINARY).is_file():
            raise FileNotFoundError(
                "Compressed-only learning requires the native extractor"
            )
        with self.lock:
            self.background_cameras = selected
            for name, camera in selected.items():
                self.background[name] = VectorPreview(
                    camera, VectorPreviewOptions(auto_tune=True), background=True
                )
        self.supervisor = threading.Thread(
            target=self._supervise, name="per-camera-compressed-learning", daemon=True
        )
        self.supervisor.start()

    def update_cameras(self, cameras: dict[str, CameraConfig]) -> None:
        """Rebind saved inputs without resetting unchanged cameras' noise models."""
        with self.lock:
            self.background_cameras = {
                name: camera
                for name, camera in cameras.items()
                if camera.enabled
                and recording_only(name)
                and not native_api_selected(name)
            }
            if self.background_cameras and self.supervisor is None:
                self.supervisor = threading.Thread(
                    target=self._supervise,
                    name="per-camera-compressed-learning",
                    daemon=True,
                )
                self.supervisor.start()

    def _supervise(self) -> None:
        while not self.stop_background.wait(5):
            with self.lock:
                if isinstance(self.preview, BackgroundPreviewLease) and (
                    time.monotonic() - self.preview.last_poll > 20
                    or time.monotonic() - self.preview.started > 120
                ):
                    self.preview.stop()
                for name in self.background.keys() - self.background_cameras.keys():
                    self.background.pop(name).stop()
                for name, camera in self.background_cameras.items():
                    if not camera.enabled:
                        worker = self.background.pop(name, None)
                        if worker is not None:
                            worker.stop()
                        continue
                    worker = self.background.get(name)
                    try:
                        if (
                            worker is not None
                            and not worker.stopped.is_set()
                            and worker.source == native_media_input(camera)
                        ):
                            continue
                        if worker is not None:
                            worker.stop()
                        logger.warning(
                            "Restarting compressed-only learner for %s; no pixel fallback",
                            name,
                        )
                        self.background[name] = VectorPreview(
                            camera,
                            VectorPreviewOptions(auto_tune=True),
                            background=True,
                        )
                    except (OSError, ValueError):
                        failed = self.background.pop(name, None)
                        if failed is not None:
                            failed.stop()
                        logger.exception(
                            "Unable to restart compressed learner for %s", name
                        )

    def status(self, camera: str, session: str | None) -> dict[str, object]:
        """Report availability without starting processing."""
        if session is None:
            worker = self.background.get(camera)
            state = worker.snapshot() if worker is not None else None
            data = state.get("data") if state else None
            tuning = data.get("autotune") if isinstance(data, dict) else None
            return {
                "camera": camera,
                "available": Path(BINARY).is_file(),
                "status": "stopped",
                "data": None,
                "background_learning": worker is not None
                and not worker.stopped.is_set(),
                "background_status": state.get("status") if state else None,
                "background_samples": tuning.get("samples")
                if isinstance(tuning, dict)
                else 0,
                "background_updated_at": data.get("updated_at")
                if isinstance(data, dict)
                else None,
            }
        with self.lock:
            current = self.preview
            if current is None or current.id != session or current.camera != camera:
                raise KeyError("Preview session is unavailable")
            return current.snapshot()

    def start(
        self, camera: CameraConfig, options: VectorPreviewOptions
    ) -> dict[str, object]:
        """Start a source only after an explicit authorized request."""
        if not Path(BINARY).is_file():
            raise FileNotFoundError("Native vector extractor is not installed")
        with self.lock:
            if self.preview is not None and not self.preview.stopped.is_set():
                raise RuntimeError("A vector preview is already running")
            if (
                camera.name in self.background
                and options.auto_tune
                and options.min_distance == 0.5
                and options.max_distance is None
                and options.min_activity == 0
            ):
                assert camera.name is not None
                self.preview = BackgroundPreviewLease(self, camera.name)
            else:
                self.preview = VectorPreview(camera, options)
            return self.preview.snapshot()

    def stop(self, camera: str, session: str) -> None:
        """Refuse to stop a different camera or viewer's session."""
        with self.lock:
            if (
                self.preview is None
                or self.preview.id != session
                or self.preview.camera != camera
            ):
                raise KeyError("Preview session is unavailable")
            self.preview.stop()
            self.preview = None

    def close(self) -> None:
        """Release the owned source during application shutdown."""
        self.stop_background.set()
        with self.lock:
            if self.preview is not None:
                self.preview.stop()
                self.preview = None
            for worker in self.background.values():
                worker.stop()
            self.background.clear()
        if self.supervisor is not None:
            self.supervisor.join(timeout=10)

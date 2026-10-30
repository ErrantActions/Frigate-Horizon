"""Bounded off-frame-thread persistence and diagnostics for adaptive motion."""

import hashlib
import json
import logging
import math
import os
import queue
import shutil
import threading
import time
import zipfile
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from frigate.config.camera.motion import AdaptiveMotionConfig
from frigate.const import CACHE_DIR, CONFIG_DIR
from frigate.motion.adaptive import (
    AdaptiveMotionProfile,
    MotionProfileKey,
    gust_band_bounds,
)
from frigate.motion.alignment import CameraViewMonitor

logger = logging.getLogger(__name__)


def camera_token(camera: str) -> str:
    """Use fixed safe filenames rather than request-controlled path components."""
    return hashlib.sha256(camera.encode()).hexdigest()[:24]


def cache_path(camera: str, suffix: str) -> Path:
    """Locate only the local adaptive-motion diagnostic cache."""
    return Path(CACHE_DIR) / "adaptive-motion" / f"{camera_token(camera)}.{suffix}"


def atomic_json(path: Path, value: dict, *, durable: bool = False) -> None:
    """Publish complete local snapshots, optionally with persistent durability."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        with temporary.open("w") as file:
            json.dump(value, file, allow_nan=False)
            file.flush()
            if durable:
                os.fsync(file.fileno())
        temporary.replace(path)
        if durable:
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass
class MotionSample:
    timestamp: float
    delta: np.ndarray
    boxes: list[tuple[int, int, int, int]]
    paused: bool
    reset: bool
    reference: np.ndarray | None = None


class AdaptiveMotionRuntime:
    """Keep disk work outside detection and preserve independent condition state."""

    def __init__(
        self,
        camera: str,
        settings: AdaptiveMotionConfig,
        frame_shape: tuple[int, int],
        baseline: int,
        view_identity: str,
    ) -> None:
        self.camera = camera
        self.settings = settings.model_copy(deep=True)
        self.frame_shape = frame_shape
        self.baseline = baseline
        identity = hashlib.sha256(
            (
                f"{view_identity}:{frame_shape}:{baseline}:{settings.timezone}:{settings.wind_band_mph}:{settings.direction_sectors}"
                + (":low-wind-bands-v2" if settings.wind_band_mph == 10 else "")
            ).encode()
        ).hexdigest()[:24]
        self.state_directory = (
            Path(CONFIG_DIR) / "adaptive-motion" / camera_token(camera)
        )
        self.directory = self.state_directory / identity
        self.base_directory = self.directory
        self.initialized = False
        self.profiles: OrderedDict[MotionProfileKey, AdaptiveMotionProfile] = (
            OrderedDict()
        )
        self.key: MotionProfileKey | None = None
        self.adjustment: np.ndarray | None = None
        self.error: str | None = None
        self.last_error_log = 0.0
        self.updated_at = 0.0
        self.last_submit = 0.0
        self.last_save = 0.0
        self.reset_pending = False
        self.alignment = CameraViewMonitor()
        self.alignment_loaded = False
        self.alignment_warning: str | None = None
        self.samples: queue.Queue[MotionSample] = queue.Queue(maxsize=1)
        self.stopped = threading.Event()
        self.thread = threading.Thread(
            target=self._run, name=f"adaptive-{camera}", daemon=True
        )
        self.thread.start()

    def submit(
        self,
        delta: np.ndarray,
        boxes: list[tuple[int, int, int, int]],
        *,
        paused: bool,
        reset: bool = False,
        reference: np.ndarray | None = None,
    ) -> None:
        """Coalesce evidence at one Hz; frame processing never waits on storage."""
        now = time.time()
        self.reset_pending |= reset
        if now - self.last_submit < 1 or self.stopped.is_set():
            return
        sample = MotionSample(
            now,
            delta.copy(),
            list(boxes),
            paused,
            self.reset_pending,
            None if reference is None else reference.copy(),
        )
        try:
            self.samples.put_nowait(sample)
        except queue.Full:
            return
        self.reset_pending = False
        self.last_submit = now

    def threshold(
        self, delta: np.ndarray, boxes: list[tuple[int, int, int, int]]
    ) -> np.ndarray:
        """Apply a fresh local map only in active mode, with baseline retests."""
        now = time.time()
        adjustment = self.adjustment
        if (
            self.settings.mode != "active"
            or adjustment is None
            or self.error
            or now - self.updated_at > 5
            or now % self.settings.probe_interval_seconds < self.settings.probe_seconds
        ):
            return np.where(delta > self.baseline, 255, 0).astype(np.uint8)
        local = cv2.resize(
            adjustment,
            (delta.shape[1], delta.shape[0]),
            interpolation=cv2.INTER_NEAREST,
        )
        height, width = self.frame_shape
        for x1, y1, x2, y2 in boxes:
            left = max(0, int(x1 * delta.shape[1] / width))
            right = min(delta.shape[1], int(np.ceil(x2 * delta.shape[1] / width)))
            top = max(0, int(y1 * delta.shape[0] / height))
            bottom = min(delta.shape[0], int(np.ceil(y2 * delta.shape[0] / height)))
            local[top:bottom, left:right] = 0
        return np.where(delta > np.minimum(self.baseline + local, 255), 255, 0).astype(
            np.uint8
        )

    def _load(self, key: MotionProfileKey, now: float) -> AdaptiveMotionProfile:
        if not self.initialized:
            self.base_directory.mkdir(parents=True, exist_ok=True)
            epoch = self.base_directory / "view.json"
            if epoch.exists():
                value = json.loads(epoch.read_text())["epoch"]
                if not isinstance(value, int) or value <= 0:
                    raise ValueError("adaptive_view_identity_invalid")
                self.directory = self.base_directory / f"view-{value}"
            self.initialized = True
        if key in self.profiles:
            profile = self.profiles.pop(key)
            self.profiles[key] = profile
            return profile

        self.state_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_directory.chmod(0o700)
        self.directory.chmod(0o700)
        path = self.directory / key.filename
        if (
            not path.exists()
            and len(list(self.directory.glob("window-*.npz")))
            >= self.settings.max_profiles
        ):
            raise ValueError("adaptive_profile_limit")
        profile = AdaptiveMotionProfile(self.settings)
        if path.exists():
            if path.stat().st_size > 64 * 1024 * 1024:
                raise ValueError("adaptive_profile_too_large")
            with np.load(path, allow_pickle=False) as saved:
                old_shape = saved["adjustment"].shape
                if (
                    len(old_shape) != 2
                    or max(old_shape) > 8192
                    or min(old_shape) < 4
                    or old_shape[0] * old_shape[1] > 2_000_000
                ):
                    raise ValueError("adaptive_profile_shape_invalid")
                if old_shape != profile.adjustment.shape:
                    archived = path.with_name(
                        f"{path.stem}-{old_shape[1]}x{old_shape[0]}-{time.time_ns()}.previous"
                    )
                    # Preserve the original; do not duplicate its sample counts.
                    owned = sum(
                        item.stat().st_size
                        for item in self.state_directory.rglob("*")
                        if item.is_file()
                    )
                    if (
                        owned + path.stat().st_size
                        > self.settings.max_state_mib * 2**20
                    ):
                        raise OSError(
                            "Adaptive grid migration exceeds its learned-state budget"
                        )
                    shutil.copyfile(path, archived)
                    archived.chmod(0o600)
                    for name in ("adjustment", "activity"):
                        values = saved[name]
                        if not np.isfinite(values).all():
                            raise ValueError("adaptive_profile_values_invalid")
                        setattr(
                            profile,
                            name,
                            cv2.resize(
                                values,
                                (self.settings.columns, self.settings.rows),
                                interpolation=cv2.INTER_LINEAR,
                            ).astype(np.float32)
                            * 0.5,
                        )
                    profile.inherited = True
                else:
                    for name in profile.ARRAY_NAMES:
                        values = saved[name]
                        if values.shape != old_shape or not np.isfinite(values).all():
                            raise ValueError("adaptive_profile_values_invalid")
                        setattr(profile, name, values.copy())
                profile.updated_at = float(saved["updated_at"].item())
                if old_shape == profile.adjustment.shape and "inherited" in saved:
                    profile.inherited = bool(saved["inherited"].item())
                if not np.isfinite(profile.updated_at) or profile.updated_at > now + 60:
                    raise ValueError("adaptive_profile_time_invalid")
            np.clip(
                profile.adjustment,
                0,
                self.settings.max_adjustment,
                out=profile.adjustment,
            )
        profile.activate(now)
        self.profiles[key] = profile
        while len(self.profiles) > 4:
            previous, value = self.profiles.popitem(last=False)
            self._save(previous, value)
        return profile

    def _interpolate(
        self, key: MotionProfileKey, weather: dict, now: float
    ) -> np.ndarray:
        """Blend outputs, not evidence or PID history, with baseline for unknown neighbors."""
        speed = weather.get("gust_mps")
        direction = weather.get("direction_degrees")
        speeds = [(key.gust_band_mph, 1.0)]
        directions = [(key.direction_sector, 1.0)]
        if speed is not None:
            mph = speed * 2.2369362921
            lower, upper = gust_band_bounds(mph, self.settings.wind_band_mph)
            fraction = (mph - lower) / (upper - lower)
            speeds = [
                (lower, 1 - fraction),
                (upper, fraction),
            ]
        if direction is not None:
            coordinate = (direction % 360) / (360 / self.settings.direction_sectors)
            lower = int(coordinate)
            fraction = coordinate - lower
            directions = [
                (lower, 1 - fraction),
                ((lower + 1) % self.settings.direction_sectors, fraction),
            ]
        result = np.zeros((self.settings.rows, self.settings.columns), np.float32)
        for band, speed_weight in speeds:
            for sector, direction_weight in directions:
                weight = speed_weight * direction_weight
                neighbor = MotionProfileKey(key.window, band, sector, key.source)
                if weight <= 0:
                    continue
                if (
                    neighbor not in self.profiles
                    and not (self.directory / neighbor.filename).exists()
                ):
                    continue
                profile = self._load(neighbor, now)
                supported = (
                    profile.observed >= self.settings.minimum_observation_seconds
                ) | profile.inherited
                age_decay = np.exp(
                    -max(0, now - profile.updated_at) / self.settings.decay_seconds
                )
                result += (
                    weight
                    * np.where(supported, profile.effective_adjustment(now), 0)
                    * age_decay
                )
        return result

    def _save(self, key: MotionProfileKey, profile: AdaptiveMotionProfile) -> None:
        path = self.directory / key.filename
        if (
            not path.exists()
            and len(list(self.directory.glob("window-*.npz")))
            >= self.settings.max_profiles
        ):
            raise OSError("Adaptive motion condition-profile limit reached")
        owned = sum(
            value.stat().st_size
            for value in self.state_directory.rglob("*")
            if value.is_file()
        )
        if owned >= self.settings.max_state_mib * 2**20:
            raise OSError("Adaptive motion learned-state budget reached")
        temporary = path.with_suffix(".tmp")
        try:
            with temporary.open("wb") as file:
                os.fchmod(file.fileno(), 0o600)
                np.savez_compressed(
                    file,
                    **{name: getattr(profile, name) for name in profile.ARRAY_NAMES},
                    updated_at=np.array(profile.updated_at),
                    inherited=np.array(profile.inherited),
                )
                file.flush()
                os.fsync(file.fileno())
            previous_size = path.stat().st_size if path.exists() else 0
            if (
                owned - previous_size + temporary.stat().st_size
                > self.settings.max_state_mib * 2**20
            ):
                raise OSError("Adaptive motion learned-state budget reached")
            temporary.replace(path)
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)

    def _weather(self, now: float) -> dict:
        path = Path(CACHE_DIR) / "adaptive-motion" / "weather.json"
        try:
            with path.open() as file:
                weather = json.load(file)
        except FileNotFoundError:
            return {
                "state": "unknown",
                "gust_mps": None,
                "direction_degrees": None,
                "observed_at": None,
            }
        if not isinstance(weather, dict):
            raise ValueError("adaptive_weather_shape_invalid")
        observed = weather.get("observed_at")
        if weather.get("state") == "unknown":
            return weather
        if (
            not isinstance(observed, (int, float))
            or not 0 <= now - observed
            or now > weather.get("valid_until", observed + 1800)
        ):
            return {
                "state": "stale",
                "gust_mps": None,
                "direction_degrees": None,
                "observed_at": observed,
            }
        for name in ("gust_mps", "direction_degrees"):
            value = weather.get(name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
                or (name == "direction_degrees" and value > 360)
            ):
                raise ValueError("adaptive_weather_values_invalid")
        return weather

    def _publish(
        self, sample: MotionSample, profile: AdaptiveMotionProfile, weather: dict
    ) -> None:
        try:
            request = json.loads(cache_path(self.camera, "request").read_text())
        except FileNotFoundError:
            return
        if not 0 <= sample.timestamp - request.get("timestamp", 0) < 15:
            return
        field = request.get("field", "adjustment")
        if field not in {"adjustment", "activity", "effective", "p", "i", "d"}:
            raise ValueError("adaptive_diagnostic_field_invalid")
        effective = (
            self.adjustment
            if self.adjustment is not None
            else np.zeros_like(profile.adjustment)
        )
        applying = self.settings.mode == "active" and not sample.paused
        values = (
            np.minimum(
                self.baseline + (effective if applying else np.zeros_like(effective)),
                255,
            )
            if field == "effective"
            else getattr(profile, field)
        )
        resolution = max(4, min(100, int(request.get("resolution", 64))))
        columns, rows = (
            min(resolution, self.settings.columns),
            min(resolution, self.settings.rows),
        )
        display = cv2.resize(values, (columns, rows), interpolation=cv2.INTER_AREA)
        key = self.key
        if key is None:
            raise RuntimeError("Adaptive profile not selected")
        probing = (
            sample.timestamp % self.settings.probe_interval_seconds
            < self.settings.probe_seconds
        )
        output = {
            "camera": self.camera,
            "mode": self.settings.mode,
            "status": "paused" if sample.paused else "learning",
            "updated_at": sample.timestamp,
            "baseline_threshold": self.baseline,
            "max_adjustment": self.settings.max_adjustment,
            "applied": self.settings.mode == "active"
            and not probing
            and not sample.paused,
            "positive_holds": int(
                np.count_nonzero(profile.hold_until > sample.timestamp)
            ),
            "warning": (
                self.alignment_warning
                if self.alignment_warning
                else "persistent_local_motion"
                if np.count_nonzero(
                    (profile.episodes >= self.settings.minimum_episodes * 2)
                    & (profile.activity > 0.5)
                    & (profile.hold_until <= sample.timestamp)
                )
                > 0
                else "inherited_grid_estimates"
                if profile.inherited
                else "grid_limited_to_native_resolution"
                if (
                    self.settings.grid_height > self.frame_shape[0]
                    if "grid_height" in self.settings.model_fields_set
                    else self.settings.short_edge > min(self.frame_shape)
                )
                else "grid_finer_than_motion_analysis"
                if self.settings.rows > sample.delta.shape[0]
                or self.settings.columns > sample.delta.shape[1]
                else "baseline_retest"
                if probing
                else None
            ),
            "error": self.error,
            "profile": {
                "window_index": key.window,
                "start_minute": key.window * 30,
                "end_minute": (key.window + 1) * 30,
                "timezone": self.settings.timezone,
                "gust_band_mph": key.gust_band_mph,
                "wind_direction_sector": key.direction_sector,
                "weather_state": weather["state"],
                "weather_observed_at": weather.get("observed_at"),
            },
            "grid": {
                "columns": self.settings.columns,
                "rows": self.settings.rows,
                "analysis_width": sample.delta.shape[1],
                "analysis_height": sample.delta.shape[0],
                "display_columns": columns,
                "display_rows": rows,
                "field": field,
                "values": display.round(4).ravel().tolist(),
                "minimum": float(np.min(values)),
                "maximum": float(np.max(values)),
            },
        }
        if request.get("column") is not None and request.get("row") is not None:
            column, row = int(request["column"]), int(request["row"])
            if (
                not 0 <= column < self.settings.columns
                or not 0 <= row < self.settings.rows
            ):
                raise ValueError("adaptive_diagnostic_cell_invalid")
            output["cell"] = {
                "column": column,
                "row": row,
                **{
                    name: float(getattr(profile, name)[row, column])
                    for name in ("adjustment", "activity", "p", "i", "d")
                },
                "applied_adjustment": float(effective[row, column])
                if output["applied"]
                else 0,
                "effective_threshold": min(
                    255,
                    self.baseline
                    + (float(effective[row, column]) if output["applied"] else 0),
                ),
                "episode_count": int(profile.episodes[row, column]),
                "hold_remaining_seconds": max(
                    0, float(profile.hold_until[row, column]) - sample.timestamp
                ),
                "quiet_seconds": float(profile.quiet[row, column]),
                "decay_rate": self.settings.quiet_decay_multiplier
                if profile.quiet[row, column] >= self.settings.quiet_seconds
                else 1,
            }
        atomic_json(cache_path(self.camera, "snapshot"), output)

    def _invalidate_view(self) -> None:
        for key, value in self.profiles.items():
            self._save(key, value)
        epoch = time.time_ns()
        atomic_json(self.base_directory / "view.json", {"epoch": epoch}, durable=True)
        self.directory = self.base_directory / f"view-{epoch}"
        self.directory.mkdir(parents=True)
        self.initialized = True
        self.profiles.clear()
        self.key = None
        self.adjustment = None
        logger.warning(
            "Camera view changed for %s; adaptive motion history restarted", self.camera
        )

    def _check_alignment(self, sample: MotionSample) -> bool:
        if sample.reference is None:
            self.alignment_warning = "camera_alignment_unavailable"
            return False
        scale = min(1.0, 320 / sample.reference.shape[1])
        image_width = max(1, round(sample.reference.shape[1] * scale))
        image_height = max(1, round(sample.reference.shape[0] * scale))
        image = cv2.resize(
            sample.reference,
            (image_width, image_height),
            interpolation=cv2.INTER_AREA,
        )
        if not self.alignment_loaded:
            path = self.directory / "reference.npy"
            if path.exists():
                if path.stat().st_size > 2 * 1024 * 1024:
                    raise ValueError("adaptive_view_reference_too_large")
                reference = np.load(path, allow_pickle=False)
                if reference.ndim != 2 or reference.dtype != np.uint8:
                    raise ValueError("adaptive_view_reference_invalid")
                self.alignment.reference = reference
            self.alignment_loaded = True
        if sample.paused:
            return False
        excluded = np.zeros((image_height, image_width), dtype=bool)
        height, width = self.frame_shape
        for x1, y1, x2, y2 in sample.boxes:
            left = max(0, int(x1 * excluded.shape[1] / width))
            right = min(excluded.shape[1], int(np.ceil(x2 * excluded.shape[1] / width)))
            top = max(0, int(y1 * excluded.shape[0] / height))
            bottom = min(
                excluded.shape[0], int(np.ceil(y2 * excluded.shape[0] / height))
            )
            excluded[top:bottom, left:right] = True
        result = self.alignment.check(image, excluded)
        self.alignment_warning = (
            None
            if result.state == "stable"
            else "camera_view_changed"
            if result.state == "moved"
            else "camera_alignment_unavailable"
        )
        if result.state == "moved":
            self._invalidate_view()
        return result.state == "stable"

    def _save_reference(self) -> None:
        if self.alignment.reference is None:
            return
        path = self.directory / "reference.npy"
        temporary = path.with_suffix(".tmp")
        try:
            with temporary.open("wb") as file:
                os.fchmod(file.fileno(), 0o600)
                np.save(file, self.alignment.reference, allow_pickle=False)
                file.flush()
                os.fsync(file.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _run(self) -> None:
        while not self.stopped.is_set():
            try:
                sample = self.samples.get(timeout=1)
            except queue.Empty:
                continue
            try:
                if sample.reset:
                    self._invalidate_view()
                    self.alignment = CameraViewMonitor()
                    self.alignment_loaded = True
                weather = self._weather(sample.timestamp)
                sample.paused = sample.paused or bool(
                    weather.get("learning_paused", False)
                )
                key = MotionProfileKey.at(
                    sample.timestamp,
                    self.settings.timezone,
                    weather.get("gust_mps"),
                    weather.get("direction_degrees"),
                    self.settings.direction_sectors,
                    self.settings.wind_band_mph,
                    source=weather.get("source") or "unknown",
                )
                profile = self._load(key, sample.timestamp)
                if not self._check_alignment(sample):
                    sample.paused = True
                    profile = self._load(key, sample.timestamp)
                if self.key != key:
                    profile.activate(sample.timestamp)
                    self.key = key
                profile.hold(sample.boxes, self.frame_shape, sample.timestamp)
                raw = (sample.delta > self.baseline).astype(np.float32)
                motion = cv2.resize(
                    raw,
                    (self.settings.columns, self.settings.rows),
                    interpolation=cv2.INTER_AREA,
                )
                profile.update(
                    motion,
                    sample.timestamp,
                    paused=sample.paused,
                )
                desired = self._interpolate(key, weather, sample.timestamp)
                desired = np.minimum(desired, max(0, 255 - self.baseline))
                if self.adjustment is not None:
                    step = (
                        self.settings.max_change_per_minute
                        * min(2, max(0, sample.timestamp - self.updated_at))
                        / 60
                    )
                    desired = np.minimum(desired, self.adjustment + step)
                desired[profile.hold_until > sample.timestamp] = 0
                self.adjustment = None if sample.paused else desired
                self.updated_at = sample.timestamp
                self.error = None
                if sample.timestamp - self.last_save >= 60:
                    self._save(key, profile)
                    self._save_reference()
                    self.last_save = sample.timestamp
                self._publish(sample, profile, weather)
            except (
                OSError,
                ValueError,
                KeyError,
                TypeError,
                zipfile.BadZipFile,
                cv2.error,
            ) as error:
                self.adjustment = None
                self.error = "adaptive_profile_unavailable"
                if sample.timestamp - self.last_error_log >= 30:
                    logger.exception(
                        "Adaptive motion learning unavailable for %s (%s)",
                        self.camera,
                        type(error).__name__,
                    )
                    self.last_error_log = sample.timestamp
                try:
                    self._publish_failure(sample)
                except (OSError, ValueError):
                    logger.exception(
                        "Unable to publish adaptive motion failure for %s", self.camera
                    )
        for key, profile in self.profiles.items():
            try:
                self._save(key, profile)
            except OSError:
                logger.exception(
                    "Unable to save adaptive motion profile for %s", self.camera
                )

    def _publish_failure(self, sample: MotionSample) -> None:
        try:
            request = json.loads(cache_path(self.camera, "request").read_text())
        except FileNotFoundError:
            return
        if not 0 <= sample.timestamp - request.get("timestamp", 0) < 15:
            return
        atomic_json(
            cache_path(self.camera, "snapshot"),
            {
                "camera": self.camera,
                "mode": self.settings.mode,
                "status": "error",
                "updated_at": sample.timestamp,
                "baseline_threshold": self.baseline,
                "max_adjustment": self.settings.max_adjustment,
                "applied": False,
                "positive_holds": 0,
                "warning": None,
                "error": self.error,
                "profile": None,
                "grid": {
                    "columns": self.settings.columns,
                    "rows": self.settings.rows,
                    "analysis_width": sample.delta.shape[1],
                    "analysis_height": sample.delta.shape[0],
                    "display_columns": 0,
                    "display_rows": 0,
                    "values": [],
                    "field": request.get("field", "adjustment"),
                },
            },
        )

    def stop(self) -> None:
        """Persist learned state on shutdown without indefinite frame-thread waits."""
        self.stopped.set()
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            logger.error("Adaptive motion persistence did not stop for %s", self.camera)

"""Independent, bounded per-cell PID profiles for motion sensitivity only."""

import hashlib
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import cv2
import numpy as np

from frigate.config.camera.motion import AdaptiveMotionConfig


def gust_band_bounds(mph: float, band_mph: int) -> tuple[int, int]:
    """Keep calm and light gusts distinct before using the selected band width."""
    if not math.isfinite(mph) or mph < 0 or band_mph not in (5, 10):
        raise ValueError("Invalid adaptive wind band input")
    width = 5 if mph < 10 else band_mph
    lower = int(mph // width) * width
    return lower, lower + width


@dataclass(frozen=True)
class MotionProfileKey:
    window: int
    gust_band_mph: int | None
    direction_sector: int | None
    source: str = "unknown"

    @classmethod
    def at(
        cls,
        timestamp: float,
        timezone: str,
        gust_mps: float | None,
        direction_degrees: float | None,
        sectors: int,
        band_mph: int = 10,
        source: str = "unknown",
    ) -> "MotionProfileKey":
        """Select independent recurring half-hour and measured wind conditions."""
        local = datetime.fromtimestamp(
            timestamp, UTC if timezone in ("UTC", "Etc/UTC") else ZoneInfo(timezone)
        )
        gust = (
            gust_band_bounds(gust_mps * 2.2369362921, band_mph)[0]
            if gust_mps is not None and math.isfinite(gust_mps) and gust_mps >= 0
            else None
        )
        direction = (
            int((direction_degrees % 360 + 180 / sectors) // (360 / sectors)) % sectors
            if direction_degrees is not None and math.isfinite(direction_degrees)
            else None
        )
        return cls(local.hour * 2 + local.minute // 30, gust, direction, source)

    @property
    def filename(self) -> str:
        source = hashlib.sha256(self.source.encode()).hexdigest()[:12]
        return f"window-{self.window}-gust-{self.gust_band_mph}-direction-{self.direction_sector}-source-{source}.npz"


class AdaptiveMotionProfile:
    """Own one condition's controller state; no AI scores or global thresholds."""

    ARRAY_NAMES = (
        "activity",
        "integral",
        "previous_error",
        "derivative",
        "adjustment",
        "episodes",
        "quiet",
        "since_episode",
        "hold_until",
        "observed",
        "previous_motion",
        "p",
        "i",
        "d",
    )

    def __init__(self, settings: AdaptiveMotionConfig) -> None:
        self.settings = settings
        shape = (settings.rows, settings.columns)
        for name in self.ARRAY_NAMES:
            setattr(self, name, np.zeros(shape, dtype=np.float32))
        self.since_episode.fill(settings.episode_gap_seconds)
        self.updated_at = 0.0
        self.hold_until = np.zeros(shape, dtype=np.float64)
        self.active = False
        self.inherited = False
        self.activity: np.ndarray
        self.integral: np.ndarray
        self.previous_error: np.ndarray
        self.derivative: np.ndarray
        self.adjustment: np.ndarray
        self.episodes: np.ndarray
        self.quiet: np.ndarray
        self.since_episode: np.ndarray
        self.observed: np.ndarray
        self.previous_motion: np.ndarray
        self.p: np.ndarray
        self.i: np.ndarray
        self.d: np.ndarray

    def activate(self, now: float) -> None:
        """Apply inactive elapsed-time decay without inventing quiet observations."""
        elapsed = max(0.0, now - self.updated_at) if self.updated_at else 0.0
        factor = math.exp(-elapsed / self.settings.decay_seconds)
        self.adjustment *= factor
        self.integral *= factor
        self.activity *= factor
        self.derivative.fill(0)
        self.d.fill(0)
        self.i[:] = self.settings.ki * self.integral
        self.p[:] = self.settings.kp * (self.activity - self.settings.target_activity)
        self.previous_error[:] = self.activity - self.settings.target_activity
        self.previous_motion.fill(0)
        self.quiet.fill(0)
        self.since_episode += min(elapsed, self.settings.episode_gap_seconds)
        self.updated_at = now
        self.active = True

    def hold(
        self,
        boxes: list[tuple[int, int, int, int]],
        frame_shape: tuple[int, int],
        now: float,
    ) -> None:
        """Protect every cell overlapping a reliable track, including boundaries."""
        height, width = frame_shape
        for x1, y1, x2, y2 in boxes:
            left = max(
                0,
                min(
                    self.settings.columns,
                    math.floor(x1 * self.settings.columns / width),
                ),
            )
            right = max(
                0,
                min(
                    self.settings.columns, math.ceil(x2 * self.settings.columns / width)
                ),
            )
            top = max(
                0, min(self.settings.rows, math.floor(y1 * self.settings.rows / height))
            )
            bottom = max(
                0, min(self.settings.rows, math.ceil(y2 * self.settings.rows / height))
            )
            if right > left and bottom > top:
                self.hold_until[top:bottom, left:right] = (
                    now + self.settings.object_hold_seconds
                )

    def update(self, motion: np.ndarray, now: float, *, paused: bool = False) -> None:
        """Learn from raw baseline evidence at a bounded observation cadence."""
        if motion.shape != self.adjustment.shape or not np.isfinite(motion).all():
            raise ValueError("Adaptive motion evidence has invalid shape or values")
        elapsed = now - self.updated_at
        if not self.active:
            self.activate(now)
            return
        if elapsed > 10:
            self.activate(now)
            self.previous_motion[:] = motion > 0.05
            return
        self.updated_at = now
        if elapsed <= 0 or paused:
            if elapsed > 0:
                factor = math.exp(-elapsed / self.settings.decay_seconds)
                self.adjustment *= factor
                self.integral *= factor
            self.derivative.fill(0)
            self.previous_motion[:] = motion > 0.05
            return
        dt = min(elapsed, 2.0)
        held = self.hold_until > now
        moving = motion > 0.05
        learning = ~held
        self.observed[learning] += dt
        self.quiet[:] = np.where(moving | held, 0, self.quiet + dt)
        self.since_episode += dt
        starts = (
            moving
            & (self.previous_motion == 0)
            & (self.since_episode >= self.settings.episode_gap_seconds)
            & learning
        )
        self.episodes[starts] = np.minimum(self.episodes[starts] + 1, 1000000)
        self.since_episode[starts] = 0
        self.previous_motion[:] = moving
        smoothing = 1 - math.exp(-dt / self.settings.learning_seconds)
        self.activity[learning] += smoothing * (
            motion[learning] - self.activity[learning]
        )
        multiplier = np.where(
            self.quiet >= self.settings.quiet_seconds,
            self.settings.quiet_decay_multiplier,
            1.0,
        )
        decay = np.exp(-dt * multiplier / self.settings.decay_seconds)
        self.integral *= decay
        self.adjustment *= decay
        error = self.activity - self.settings.target_activity
        derivative = (error - self.previous_error) / dt
        self.derivative[:] = 0.95 * self.derivative + 0.05 * derivative
        self.previous_error[:] = error
        eligible = (
            learning
            & (self.observed >= self.settings.minimum_observation_seconds)
            & (self.episodes >= self.settings.minimum_episodes)
        )
        # Saturation must not accumulate a positive integral that outlives a tree.
        integrate = eligible & (
            (self.adjustment < self.settings.max_adjustment) | (error < 0)
        )
        self.integral[integrate] += error[integrate] * dt
        limit = self.settings.max_adjustment / max(self.settings.ki, 1e-9)
        np.clip(self.integral, 0, limit, out=self.integral)
        recurrence = 1 + np.minimum(
            cv2.dilate(self.episodes, np.ones((3, 3), np.uint8))
            / max(self.settings.minimum_episodes * 4, 1),
            1,
        )
        self.p[:] = self.settings.kp * error * recurrence
        self.i[:] = self.settings.ki * self.integral
        self.d[:] = self.settings.kd * self.derivative
        desired = np.clip(self.p + self.i + self.d, 0, self.settings.max_adjustment)
        step = self.settings.max_change_per_minute * dt / 60
        self.adjustment[eligible] += np.clip(
            desired[eligible] - self.adjustment[eligible], -step, step
        )
        np.clip(self.adjustment, 0, self.settings.max_adjustment, out=self.adjustment)

    def effective_adjustment(self, now: float) -> np.ndarray:
        """Restore baseline for periodic probes and useful-object occupied cells."""
        if now % self.settings.probe_interval_seconds < self.settings.probe_seconds:
            return np.zeros_like(self.adjustment)
        return np.where(self.hold_until > now, 0, self.adjustment).astype(np.float32)

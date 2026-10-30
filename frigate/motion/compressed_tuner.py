"""Compressed-data-only background recurrence learning; no pixel input or reused pixel history."""

import math
from collections import deque
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from frigate.motion.compressed_protocol import CompressedEvidence

ROWS, COLUMNS = 18, 32
MAX_OFFSET = 4 / 255


@dataclass(frozen=True)
class CompressedThresholds:
    vector: NDArray[np.float32]
    coded: NDArray[np.float32]


class CompressedMotionTuner:
    """Learn bounded spatial persistence and variability from current coded stream evidence."""

    def __init__(
        self,
        *,
        minimum_observation_seconds: float = 300,
        learning_seconds: float = 60,
        maximum_offset: float = MAX_OFFSET,
    ) -> None:
        if (
            not all(
                math.isfinite(value)
                for value in (
                    minimum_observation_seconds,
                    learning_seconds,
                    maximum_offset,
                )
            )
            or minimum_observation_seconds <= 0
            or learning_seconds <= 0
            or not 0 <= maximum_offset <= MAX_OFFSET
        ):
            raise ValueError("Invalid compressed-data background tuner settings")
        self.minimum_observation_seconds = minimum_observation_seconds
        self.learning_seconds = learning_seconds
        self.maximum_offset = maximum_offset
        self.mean = np.zeros((2, ROWS, COLUMNS), np.float32)
        self.variance = np.zeros_like(self.mean)
        self.observed = np.zeros_like(self.mean)
        self.persistent = np.zeros_like(self.mean)
        self.adjustment = np.zeros_like(self.mean)
        self.recurrence = np.zeros_like(self.mean)
        self.duty = np.zeros_like(self.mean)
        self.integral = np.zeros_like(self.mean)
        self.previous_error = np.zeros_like(self.mean)
        self.last_timestamp: float | None = None
        self.last_observation: float | None = None
        self.shape: tuple[int, int] | None = None
        self.samples = 0
        self.resets = 0
        self.reason = "warming_up"
        self.patterns: deque[NDArray[np.float32]] = deque(maxlen=300)
        self.pattern_timestamp: float | None = None

    def reset(self) -> None:
        """Discard incompatible source observations, never carry them into a changed stream."""
        for value in (
            self.mean,
            self.variance,
            self.observed,
            self.persistent,
            self.adjustment,
            self.integral,
            self.previous_error,
            self.recurrence,
            self.duty,
        ):
            value.fill(0)
        self.last_observation = None
        self.samples = 0
        self.resets += 1
        self.patterns.clear()
        self.pattern_timestamp = None

    def observe(
        self,
        evidence: CompressedEvidence,
        *,
        protected_activity: bool = False,
        suppression_allowed: bool = False,
    ) -> CompressedThresholds:
        """Learn from coded data only; a separate safety policy authorizes any use of offsets."""
        now = evidence.timestamp
        shape = (evidence.width, evidence.height)
        if (
            self.last_timestamp is not None
            and (now <= self.last_timestamp or now - self.last_timestamp > 1)
        ) or (self.shape is not None and self.shape != shape):
            self.reset()
        self.last_timestamp, self.shape = now, shape
        reliable = (
            evidence.picture_type == "P"
            and evidence.uncertainty is None
            and evidence.coverage >= 0.5
            and evidence.profile_idc == 100
            and evidence.cabac == 1
        )
        activity = np.asarray(
            (evidence.activity, evidence.coded_activity), dtype=np.float32
        ).reshape(2, ROWS, COLUMNS)
        global_change = reliable and bool(
            np.any(np.mean(activity > 0.1, axis=(1, 2)) >= 0.75)
        )
        if global_change:
            self.reset()
            reliable = False
            self.reason = "global_coded_change"
        if protected_activity:
            self.adjustment.fill(0)
            self.integral.fill(0)
            self.last_observation = None
            self.reason = "protected_activity"
        elif reliable:
            dt = 0 if self.last_observation is None else now - self.last_observation
            self.last_observation = now
            if dt > 1:
                dt = 0
            if dt > 0:
                alpha = -math.expm1(-dt / self.learning_seconds)
                delta = activity - self.mean
                self.mean += np.float32(alpha) * delta
                self.variance += np.float32(alpha) * (delta * delta - self.variance)
                self.observed += dt
                self.duty += np.float32(alpha) * (
                    (activity > 0).astype(np.float32) - self.duty
                )
                # Continuous activity earns background duration without stop/start episodes.
                self.persistent[:] = np.where(activity > 0, self.persistent + dt, 0)
                matches = np.zeros_like(activity, dtype=bool)
                if (
                    self.pattern_timestamp is None
                    or now - self.pattern_timestamp >= 0.195
                ):
                    self.patterns.append(activity.copy())
                    self.pattern_timestamp = now
                    if len(self.patterns) >= 8:
                        history = np.stack(tuple(self.patterns))
                        recent = history[-4:]
                        for lag in (4, 8, 12, 16):
                            if len(history) >= lag + 4:
                                previous = history[-lag - 4 : -lag]
                                distance = np.sqrt(
                                    np.mean((recent - previous) ** 2, axis=0)
                                )
                                matches |= distance <= self.mean * 0.2 + 0.002
                        # Longer windows cover multi-second gust envelopes without
                        # treating activity in a new place as the same background.
                        if len(history) >= 32:
                            gust = history[-16:]
                            for lag in (16, 32, 64, 128, 256):
                                if len(history) >= lag + 16:
                                    previous = history[-lag - 16 : -lag]
                                    distance = np.sqrt(
                                        np.mean((gust - previous) ** 2, axis=0)
                                    )
                                    matches |= distance <= self.mean * 0.25 + 0.002
                        self.recurrence[:] = np.where(
                            matches, self.recurrence + min(dt * 5, 0.25), 0
                        )
                eligible = (
                    (self.observed >= self.minimum_observation_seconds)
                    & (self.duty >= 0.5)
                    & (self.recurrence >= 6)
                )
                error = np.maximum(self.mean - np.sqrt(np.maximum(self.variance, 0)), 0)
                self.integral[:] = np.clip(
                    self.integral * math.exp(-dt / 600) + error * dt,
                    0,
                    self.maximum_offset / 0.001 if self.maximum_offset else 0,
                )
                derivative = (error - self.previous_error) / dt
                desired = np.clip(
                    2 * error + 0.001 * self.integral + 0.1 * derivative,
                    0,
                    self.maximum_offset,
                )
                step = self.maximum_offset * dt / 300
                self.adjustment[:] = np.where(
                    eligible,
                    self.adjustment + np.clip(desired - self.adjustment, -step, step),
                    0,
                )
                self.previous_error[:] = error
                self.samples += 1
            self.reason = "learning_compressed_background"
        elif evidence.picture_type != "I":
            self.last_observation = None
            self.reason = "compressed_evidence_unavailable"
        offset = self.adjustment.copy()
        # New coded patterns must wake, even in a cell with established background.
        novel = np.abs(activity - self.mean) > (
            3 * np.sqrt(np.maximum(self.variance, 0)) + 0.002
        )
        offset[novel | (self.recurrence < 6)] = 0
        if (
            not reliable
            or protected_activity
            or not suppression_allowed
            or now % 30 < 2
        ):
            offset.fill(0)
        return CompressedThresholds(offset[0], offset[1])

    def snapshot(self) -> dict[str, object]:
        """Expose separate learning and its conservative limits, not semantic object identity."""
        return {
            "enabled": True,
            "input": "motion_vectors_and_coded_block_activity",
            "pixel_data_used_for_learning": False,
            "storage": "ram_only",
            "pixel_history_reused": False,
            "samples": self.samples,
            "resets": self.resets,
            "reason": self.reason,
            "minimum_observation_seconds": self.minimum_observation_seconds,
            "continuous_background_cells": int(
                np.any(
                    self.persistent >= self.minimum_observation_seconds, axis=0
                ).sum()
            ),
            "repeating_pattern_cells": int(np.any(self.recurrence >= 6, axis=0).sum()),
            "pattern_input_hz": 5,
            "pattern_history_samples": len(self.patterns),
            "pattern_history_seconds": 60,
            "pattern_comparison": "short oscillations and sixteen-sample gust windows at multiple lags",
            "maximum_offset": self.maximum_offset,
            "vector_max_offset": float(self.adjustment[0].max()),
            "coded_max_offset": float(self.adjustment[1].max()),
            "semantic_motion_recall_proven": False,
        }


class CompressedNoiseEstimator:
    """Per-frame noise cancellation with quiet calibration and protected background learning."""

    def __init__(
        self,
        minimum_samples: int = 60,
        capacity: int = 240,
        *,
        quiet_seconds: float = 120,
    ) -> None:
        if (
            not 10 <= minimum_samples <= capacity <= 240
            or not math.isfinite(quiet_seconds)
            or quiet_seconds <= 0
        ):
            raise ValueError("Invalid compressed noise observation bounds")
        self.minimum_samples = minimum_samples
        self.capacity = capacity
        self.quiet_seconds = quiet_seconds
        self.samples: deque[NDArray[np.float32]] = deque(maxlen=capacity)
        self.last_timestamp: float | None = None
        self.last_sample: float | None = None
        self.last_reliable = False
        self.shape: tuple[int, int] | None = None
        self.thresholds = np.zeros((2, ROWS, COLUMNS), dtype=np.float32)
        self.hold_until = np.zeros_like(self.thresholds, dtype=np.float64)
        self.quiet_duration = np.zeros_like(self.hold_until)
        self.novelty_floor = np.zeros_like(self.thresholds)
        self.ready = np.zeros((2, ROWS, COLUMNS), dtype=bool)
        self.candidate_ready = np.zeros_like(self.ready)
        self.observed_seconds = 0.0

    def observe(self, evidence: CompressedEvidence) -> dict[str, object]:
        """Pass changes immediately; never smooth foreground into the noise estimate."""
        now = evidence.timestamp
        shape = (evidence.width, evidence.height)
        if self.last_timestamp is not None and (
            now <= self.last_timestamp
            or now - self.last_timestamp > 2
            or shape != self.shape
        ):
            self.samples.clear()
            self.ready.fill(False)
            self.thresholds.fill(0)
            self.novelty_floor.fill(0)
            self.candidate_ready.fill(False)
            self.hold_until.fill(0)
            self.quiet_duration.fill(0)
            self.observed_seconds = 0
            self.last_timestamp = None
            self.last_sample = None
            self.last_reliable = False
        elapsed = (
            0 if self.last_timestamp is None else min(1.0, now - self.last_timestamp)
        )
        self.last_timestamp, self.shape = now, shape
        value = np.asarray(
            (evidence.activity, evidence.coded_activity), dtype=np.float32
        ).reshape(2, ROWS, COLUMNS)
        reliable = (
            evidence.picture_type == "P"
            and evidence.uncertainty is None
            and evidence.coverage >= 0.5
        )
        novel = np.zeros_like(self.ready)
        if reliable:
            # High cell occupancy cannot bootstrap itself into "quiet" at startup.
            novel = (value >= 0.25) | (
                self.candidate_ready
                & (value > self.novelty_floor + np.where(self.ready, 0.005, 0.05))
            )
            expanded = novel.copy()
            for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                shifted = np.roll(novel, (dy, dx), axis=(1, 2))
                if dy < 0:
                    shifted[:, -1] = False
                if dy > 0:
                    shifted[:, 0] = False
                if dx < 0:
                    shifted[:, :, -1] = False
                if dx > 0:
                    shifted[:, :, 0] = False
                expanded |= shifted
            self.hold_until[expanded] = now + 0.5
            held = self.hold_until > now
            if self.last_reliable:
                self.quiet_duration[~held] += elapsed
                self.observed_seconds += elapsed
            # Robust statistics run at 2 Hz, but novelty and cancellation run on every frame.
            if self.last_sample is None or now - self.last_sample >= 0.5:
                self.last_sample = now
                sample = value.copy()
                sample[held] = np.nan
                self.samples.append(sample)
                history = np.stack(tuple(self.samples))
                valid = np.sum(np.isfinite(history), axis=0)
                eligible = valid >= self.minimum_samples
                usable = np.where(np.isfinite(history), history, np.nan)
                usable[:, ~eligible] = 0
                median = np.nanmedian(usable, axis=0)
                deviation = np.nanmedian(np.abs(usable - median), axis=0)
                estimated = median + 3 * 1.4826 * deviation + 0.002
                update = eligible & ~held
                self.novelty_floor[update] = estimated[update]
                self.candidate_ready |= eligible
                self.thresholds[update] = np.minimum(0.25, estimated[update])
            self.ready |= self.candidate_ready & (
                self.quiet_duration >= self.quiet_seconds
            )
        self.last_reliable = reliable
        held = self.hold_until > now
        floor = np.where(self.ready, self.thresholds, 0)
        if not reliable:
            floor.fill(0)
        # A learning hold never removes the baseline from the output or creates a long tail.
        signal = np.maximum(value - floor, 0)
        signal[novel] = value[novel]
        return {
            "status": "ready" if bool(np.any(self.ready)) else "learning",
            "algorithm": "quiet_baseline_fast_residual",
            "samples": len(self.samples),
            "minimum_samples": self.minimum_samples,
            "observed_seconds": self.observed_seconds,
            "baseline_seconds": self.quiet_seconds,
            "quiet_seconds": min(self.quiet_seconds, float(self.quiet_duration.max())),
            "calibrated_cells": int(np.any(self.ready, axis=0).sum()),
            "reliable": reliable,
            "wake_required": not reliable or bool(np.any(signal > 0.005)),
            "attack_frames": 1,
            "release_frames": 1,
            "input": "compressed_activity_only",
            "pixels_used": False,
            "applies_to_live_detector": False,
            "held_cells": int(np.any(held, axis=0).sum()),
            "vector_floor": floor[0].ravel().tolist(),
            "coded_floor": floor[1].ravel().tolist(),
            "vector_signal": signal[0].ravel().tolist(),
            "coded_signal": signal[1].ravel().tolist(),
            "maximum_floor": 0.25,
            "limited_cells": int(
                np.any(self.ready & (self.thresholds >= 0.25), axis=0).sum()
            ),
            "storage": "ram_only",
        }

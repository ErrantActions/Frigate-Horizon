"""Shadow-only compressed-feature motion analysis with independent PID and fail-open policy."""

import math
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from frigate.config.camera.motion import AdaptiveMotionConfig
from frigate.motion.adaptive import AdaptiveMotionProfile

BACKEND_ID = "h264-vector-shadow-v1"


@dataclass(frozen=True)
class VectorEvidence:
    """Measurements from an explicitly identified extractor, not a physical-motion guarantee."""

    timestamp: float
    width: int
    height: int
    picture_type: str
    activity: NDArray[np.float32]
    coverage: float
    vector_count: int
    uncertainty: str | None


def vector_evidence(
    vectors: np.ndarray | None,
    *,
    timestamp: float,
    width: int,
    height: int,
    picture_type: str,
    rows: int = 18,
    columns: int = 32,
    minimum_displacement: float = 0.5,
) -> VectorEvidence:
    """Aggregate backward-reference vectors; preserve absent/invalid evidence as unknown."""
    if not math.isfinite(timestamp) or width < 1 or height < 1:
        raise ValueError("Invalid motion evidence timestamp or image dimensions")
    if not 4 <= rows <= 128 or not 4 <= columns <= 128:
        raise ValueError(
            "Shadow grids must be bounded between 4 and 128 cells per axis"
        )
    if not math.isfinite(minimum_displacement) or minimum_displacement < 0:
        raise ValueError("Motion-vector displacement threshold must be nonnegative")
    grid = np.zeros((rows, columns), dtype=np.float32)
    empty = VectorEvidence(timestamp, width, height, picture_type, grid, 0, 0, None)
    if picture_type not in ("P", "B"):
        return VectorEvidence(
            **{**empty.__dict__, "uncertainty": "intra_or_unknown_frame"}
        )
    required = {
        "source",
        "w",
        "h",
        "dst_x",
        "dst_y",
        "motion_x",
        "motion_y",
        "motion_scale",
    }
    if (
        vectors is None
        or not required <= set(vectors.dtype.names or ())
        or vectors.ndim != 1
    ):
        return VectorEvidence(
            **{**empty.__dict__, "uncertainty": "vectors_unavailable"}
        )
    if len(vectors) > 2_000_000:
        raise ValueError("Motion-vector count exceeds the bounded frame limit")
    if not len(vectors):
        return VectorEvidence(
            **{**empty.__dict__, "uncertainty": "vectors_unavailable"}
        )
    backwards = vectors[vectors["source"] < 0]
    if not len(backwards):
        return VectorEvidence(
            **{**empty.__dict__, "uncertainty": "reference_vectors_unavailable"}
        )
    scale = backwards["motion_scale"].astype(np.float64)
    valid = (
        (scale > 0)
        & np.isfinite(scale)
        & (backwards["w"] > 0)
        & (backwards["h"] > 0)
        & (backwards["dst_x"] >= 0)
        & (backwards["dst_x"] < width)
        & (backwards["dst_y"] >= 0)
        & (backwards["dst_y"] < height)
    )
    if not valid.all():
        return VectorEvidence(**{**empty.__dict__, "uncertainty": "invalid_vectors"})
    displacement = np.hypot(
        backwards["motion_x"].astype(np.float64) / scale,
        backwards["motion_y"].astype(np.float64) / scale,
    )
    if not np.isfinite(displacement).all():
        return VectorEvidence(**{**empty.__dict__, "uncertainty": "invalid_vectors"})
    area = backwards["w"].astype(np.float64) * backwards["h"].astype(np.float64)
    y = np.minimum(rows - 1, backwards["dst_y"].astype(np.int64) * rows // height)
    x = np.minimum(columns - 1, backwards["dst_x"].astype(np.int64) * columns // width)
    moving = displacement >= minimum_displacement
    np.add.at(
        grid,
        (y[moving], x[moving]),
        (area[moving] * rows * columns / (width * height)).astype(np.float32),
    )
    np.clip(grid, 0, 1, out=grid)
    coverage = min(1.0, float(area.sum()) / (width * height))
    uncertainty = (
        "sparse_vector_coverage"
        if coverage < 0.5
        else "global_motion_or_illumination"
        if float(np.mean(grid > 0.1)) >= 0.75
        else None
    )
    return VectorEvidence(
        timestamp,
        width,
        height,
        picture_type,
        grid,
        coverage,
        len(vectors),
        uncertainty,
    )


class ShadowMotionPID:
    """Reuse bounded PID math with separate RAM-only state, never pixel-motion history."""

    def __init__(self, rows: int = 18, columns: int = 32) -> None:
        self.settings = AdaptiveMotionConfig(
            rows=rows,
            columns=columns,
            grid_height=min(rows, columns),
            kp=2,
            ki=0.001,
            kd=0.1,
            max_adjustment=4,
            minimum_observation_seconds=300,
            minimum_episodes=6,
        )
        self.profile = AdaptiveMotionProfile(self.settings)
        self.last_timestamp: float | None = None
        self.last_shape: tuple[int, int] | None = None

    def observe(
        self,
        evidence: VectorEvidence,
        *,
        pixel_verified_quiet: bool = False,
        view_verified: bool = False,
        tracked_boxes: list[tuple[int, int, int, int]] | None = None,
    ) -> NDArray[np.float32]:
        """Learn only from independently verified quiet background, never unobserved gaps."""
        if evidence.activity.shape != (self.settings.rows, self.settings.columns):
            raise ValueError("Motion evidence and independent PID grid differ")
        if not np.isfinite(evidence.activity).all() or np.any(
            (evidence.activity < 0) | (evidence.activity > 1)
        ):
            raise ValueError("Motion activity must be finite and normalized")
        shape = (evidence.height, evidence.width)
        gap = self.last_timestamp is not None and (
            evidence.timestamp <= self.last_timestamp
            or evidence.timestamp - self.last_timestamp > 1
        )
        if gap or (self.last_shape is not None and shape != self.last_shape):
            self.profile = AdaptiveMotionProfile(self.settings)
        self.last_timestamp = evidence.timestamp
        self.last_shape = shape
        self.profile.hold(tracked_boxes or [], shape, evidence.timestamp)
        self.profile.update(
            evidence.activity,
            evidence.timestamp,
            paused=bool(evidence.uncertainty)
            or not pixel_verified_quiet
            or not view_verified,
        )
        # Offsets are normalized shadow-score units, not pixel threshold values.
        return (
            self.profile.effective_adjustment(evidence.timestamp) / np.float32(255)
        ).astype(np.float32)


@dataclass(frozen=True)
class Qualification:
    """Explicit acceptance evidence; unavailable fields cannot qualify a live gate."""

    extractor_avoids_reconstruction: bool = False
    matched_labeled_events: int = 0
    labeled_events: int = 0
    tested_day_and_night: bool = False
    tested_small_slow_objects: bool = False
    measured_resource_ratio: float | None = None
    safe_preroll_verified: bool = False

    def blockers(self) -> tuple[str, ...]:
        """Reject unsupported efficiency/coverage claims rather than enabling by preference."""
        failures = []
        if not self.extractor_avoids_reconstruction:
            failures.append("extractor_still_reconstructs_frames")
        if (
            self.labeled_events < 50
            or self.matched_labeled_events != self.labeled_events
        ):
            failures.append("insufficient_or_missed_labeled_events")
        if not self.tested_day_and_night:
            failures.append("day_night_validation_missing")
        if not self.tested_small_slow_objects:
            failures.append("small_slow_object_validation_missing")
        ratio = self.measured_resource_ratio
        if ratio is None or not math.isfinite(ratio) or not 0 <= ratio <= 0.5:
            failures.append("resource_savings_not_demonstrated")
        if not self.safe_preroll_verified:
            failures.append("compressed_preroll_not_verified")
        return tuple(failures)


@dataclass(frozen=True)
class GateDecision:
    decode: bool
    reason: str


class ShadowDecodeGate:
    """Simulate wake decisions only; this class has no capture/process control methods."""

    def __init__(self, qualification: Qualification | None = None) -> None:
        self.qualification = qualification or Qualification()
        self.hold_until = float("-inf")
        self.last_probe: float | None = None
        self.last_timestamp: float | None = None

    def decide(
        self,
        evidence: VectorEvidence,
        *,
        tracked_object_present: bool = False,
        view_verified: bool = False,
        extraction_failed: bool = False,
        adaptive_offset: NDArray[np.float32] | None = None,
    ) -> GateDecision:
        """Fail open for unqualified extraction, unknown state, object holds and probes."""
        if self.qualification.blockers():
            return GateDecision(True, "unqualified_shadow_backend")
        if self.last_timestamp is not None and (
            evidence.timestamp <= self.last_timestamp
            or evidence.timestamp - self.last_timestamp > 1
        ):
            self.last_timestamp = evidence.timestamp
            self.last_probe = None
            self.hold_until = evidence.timestamp + 10
            return GateDecision(True, "timestamp_gap_or_reset")
        self.last_timestamp = evidence.timestamp
        if extraction_failed or evidence.uncertainty or not view_verified:
            self.hold_until = evidence.timestamp + 10
            return GateDecision(True, "uncertain_or_unavailable")
        if tracked_object_present:
            self.hold_until = evidence.timestamp + 10
            return GateDecision(True, "tracked_object_hold")
        if self.last_probe is None or evidence.timestamp - self.last_probe >= 30:
            self.last_probe = evidence.timestamp
            self.hold_until = evidence.timestamp + 2
            return GateDecision(True, "periodic_full_decode_probe")
        offset = (
            np.zeros_like(evidence.activity)
            if adaptive_offset is None
            else adaptive_offset
        )
        if (
            offset.shape != evidence.activity.shape
            or not np.isfinite(offset).all()
            or np.any((offset < 0) | (offset > 4 / 255))
        ):
            raise ValueError("Invalid independent shadow PID offset")
        if np.any(evidence.activity > 0.02 + offset):
            self.hold_until = evidence.timestamp + 10
            return GateDecision(True, "compressed_motion")
        if evidence.timestamp < self.hold_until:
            return GateDecision(True, "quiet_hold")
        return GateDecision(False, "qualified_quiet_simulation_only")

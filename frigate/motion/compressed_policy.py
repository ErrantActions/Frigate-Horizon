"""Experimental native decode decisions; no production qualification or process control is implied."""

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from frigate.motion.compressed_protocol import CompressedEvidence

if TYPE_CHECKING:
    from frigate.motion.compressed_tuner import CompressedThresholds


@dataclass(frozen=True)
class PixelFeedback:
    """Only real decoded-frame observations may verify a view or hold tracked activity."""

    timestamp: float
    view_verified: bool
    motion_present: bool
    active_objects: bool


@dataclass(frozen=True)
class NativeDecodeDecision:
    """Differentiate an independent keyframe probe from a full reference-chain wake."""

    action: Literal["full", "idr_probe", "quiet"]
    reason: str


class NativeDecodePolicy:
    """Fail-open trial policy using both vectors and conservative coded-block evidence."""

    def __init__(
        self,
        *,
        startup_seconds: float = 30,
        hold_seconds: float = 10,
        feedback_max_age: float = 5,
        activity_threshold: float = 0,
        use_pixel_motion: bool = True,
    ) -> None:
        if (
            not all(
                math.isfinite(value)
                for value in (
                    startup_seconds,
                    hold_seconds,
                    feedback_max_age,
                    activity_threshold,
                )
            )
            or startup_seconds < 0
            or hold_seconds < 0
            or feedback_max_age <= 0
            or not 0 <= activity_threshold <= 1
        ):
            raise ValueError("Invalid experimental native decode policy")
        self.startup_seconds = startup_seconds
        self.hold_seconds = hold_seconds
        self.feedback_max_age = feedback_max_age
        self.threshold = activity_threshold
        self.use_pixel_motion = use_pixel_motion
        self.first_timestamp: float | None = None
        self.previous_timestamp: float | None = None
        self.geometry: tuple[int, int] | None = None
        self.hold_until = float("-inf")

    def decide(
        self,
        evidence: CompressedEvidence,
        *,
        idr: bool,
        feedback: PixelFeedback | None,
        buffer_pressure: bool = False,
        thresholds: "CompressedThresholds | None" = None,
    ) -> NativeDecodeDecision:
        """Request native pixels for uncertainty; an IDR alone need not create a ten-second wake."""
        now = evidence.timestamp
        geometry = (evidence.width, evidence.height)
        reset = self.previous_timestamp is not None and (
            now <= self.previous_timestamp
            or now - self.previous_timestamp > 1
            or geometry != self.geometry
        )
        self.previous_timestamp = now
        self.geometry = geometry
        if self.first_timestamp is None or reset:
            self.first_timestamp = now
        if reset:
            self.hold_until = now + self.hold_seconds
            return NativeDecodeDecision("full", "source_clock_or_geometry_reset")
        if now - self.first_timestamp < self.startup_seconds:
            return NativeDecodeDecision("full", "startup_pixel_reference")
        if buffer_pressure:
            self.hold_until = now + self.hold_seconds
            return NativeDecodeDecision("full", "compressed_buffer_pressure")
        if (
            evidence.profile_idc != 100
            or evidence.cabac != 1
            or evidence.picture_type == "B"
        ):
            return NativeDecodeDecision("full", "unverified_source_coding")
        if (
            feedback is None
            or not math.isfinite(feedback.timestamp)
            or feedback.timestamp > now + 0.25
            or now - feedback.timestamp > self.feedback_max_age
            or not feedback.view_verified
        ):
            return NativeDecodeDecision("full", "pixel_view_feedback_unverified")
        if (
            self.use_pixel_motion and feedback.motion_present
        ) or feedback.active_objects:
            self.hold_until = now + self.hold_seconds
            return NativeDecodeDecision("full", "pixel_motion_or_object_hold")
        if now < self.hold_until:
            return NativeDecodeDecision("full", "motion_hold")
        if evidence.picture_type == "I" and idr:
            return NativeDecodeDecision("idr_probe", "native_idr_safety_probe")
        if evidence.picture_type != "P" or evidence.uncertainty is not None:
            self.hold_until = now + self.hold_seconds
            return NativeDecodeDecision("full", "uncertain_compressed_evidence")
        if thresholds is None:
            activity = (
                max(evidence.activity) > self.threshold
                or max(evidence.coded_activity) > self.threshold
            )
        else:
            import numpy as np

            from frigate.motion.compressed_tuner import COLUMNS, MAX_OFFSET, ROWS

            for offset in (thresholds.vector, thresholds.coded):
                if (
                    offset.shape != (ROWS, COLUMNS)
                    or not np.isfinite(offset).all()
                    or np.any(offset < 0)
                    or np.any(offset > MAX_OFFSET + 1e-6)
                ):
                    raise ValueError("Unsafe compressed tuning thresholds")
            activity = bool(
                np.any(
                    np.asarray(evidence.activity).reshape(ROWS, COLUMNS)
                    > self.threshold + thresholds.vector
                )
                or np.any(
                    np.asarray(evidence.coded_activity).reshape(ROWS, COLUMNS)
                    > self.threshold + thresholds.coded
                )
            )
        if activity:
            self.hold_until = now + self.hold_seconds
            return NativeDecodeDecision("full", "vector_or_coded_block_activity")
        return NativeDecodeDecision("quiet", "experimental_quiet_evidence")

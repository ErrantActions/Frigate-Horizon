"""Exact per-camera experimental-pipeline checks, not a substitute for event-coverage testing."""

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass


def number(value: object) -> float | None:
    """Keep unavailable, nonfinite and boolean telemetry distinct from an actual zero."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def mapping(value: object) -> Mapping[str, object]:
    """Read schema-shaped telemetry without treating arbitrary values as dictionaries."""
    if isinstance(value, Mapping):
        return value
    return {}


@dataclass(frozen=True)
class CameraPipelineHealth:
    camera: str
    healthy: bool
    reasons: tuple[str, ...]
    capture_fps: float | None
    process_fps: float | None
    skipped_fps: float | None
    skipped_limit: float
    recording_age_seconds: float | None
    backend: str | None
    native_state: str | None
    source_fps: float | None
    decoded_fps: float | None
    native_sample_age_seconds: float | None
    frame_age_seconds: float | None
    snapshot_age_seconds: float | None

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe, credential-free readiness record with every failed criterion."""
        return asdict(self)


def camera_pipeline_health(
    camera: str,
    stats: object,
    *,
    now: float,
    latest_recording_end: object,
    baseline_skipped_fps: float = 0,
    require_native: bool = True,
    snapshot_at: float | None = None,
) -> CameraPipelineHealth:
    """Evaluate the actual cutover thresholds and retain separate source/frame/consumer failures."""
    if (
        not math.isfinite(now)
        or not math.isfinite(baseline_skipped_fps)
        or baseline_skipped_fps < 0
    ):
        raise ValueError("Invalid pipeline-health clock or baseline")
    if snapshot_at is not None and not math.isfinite(snapshot_at):
        raise ValueError("Invalid camera snapshot timestamp")
    observed_at = now if snapshot_at is None else snapshot_at
    snapshot_age = None if snapshot_at is None else now - snapshot_at
    data = mapping(stats)
    native = mapping(data.get("native_capture"))
    capture, process, skipped = (
        number(data.get(key)) for key in ("camera_fps", "process_fps", "skipped_fps")
    )
    skipped_limit = (
        max(baseline_skipped_fps + 0.5, baseline_skipped_fps * 1.5)
        if baseline_skipped_fps > 0
        else 0.0
    )
    recording_end = number(latest_recording_end)
    recording_age = None if recording_end is None else now - recording_end
    reasons = []
    if recording_age is None or not 0 <= recording_age < 60:
        reasons.append("recording_not_fresh")
    if capture is None or capture <= 0:
        reasons.append("capture_not_flowing")
    if process is None or process <= 0:
        reasons.append("processing_not_flowing")
    if skipped is None:
        reasons.append("capture_skip_counter_unavailable")
    elif skipped > skipped_limit:
        reasons.append("capture_skips_exceed_baseline")
    backend_value, state_value = native.get("backend"), native.get("state")
    backend = backend_value if isinstance(backend_value, str) else None
    state = state_value if isinstance(state_value, str) else None
    source_fps, decoded_fps = (
        number(native.get("source_fps")),
        number(native.get("decoded_fps")),
    )
    sample_time, frame_time = (
        number(native.get("sampled_at")),
        number(native.get("last_frame_at")),
    )
    sample_age = None if sample_time is None else observed_at - sample_time
    frame_age = None if frame_time is None else observed_at - frame_time
    if require_native:
        if snapshot_age is not None and not 0 <= snapshot_age < 45:
            reasons.append("camera_snapshot_stale")
        if native.get("source_width") != 3840 or native.get("source_height") != 2160:
            reasons.append("native_geometry_unverified")
        if backend == "experimental_native4k_gated":
            if source_fps is None or source_fps < 10:
                reasons.append("entropy_source_rate_low")
            rendered = number(native.get("rendered_frames"))
            if rendered is None or rendered <= 0:
                reasons.append("native_frames_never_rendered")
        elif backend == "native4k_full_decode_fallback":
            if decoded_fps is None or decoded_fps < 4:
                reasons.append("full_decode_fallback_rate_low")
        else:
            reasons.append("native_backend_unavailable")
        if sample_age is None or not 0 <= sample_age < 10:
            reasons.append("native_status_stale")
        if frame_age is None or not 0 <= frame_age < 10:
            reasons.append("native_frames_stale")
    return CameraPipelineHealth(
        camera,
        not reasons,
        tuple(reasons),
        capture,
        process,
        skipped,
        skipped_limit,
        recording_age,
        backend,
        state,
        source_fps,
        decoded_fps,
        sample_age,
        frame_age,
        snapshot_age,
    )

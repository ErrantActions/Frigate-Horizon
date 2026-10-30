"""Read-only, allowlisted interpretation of measured camera and hardware health."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from frigate.config import FrigateConfig

AI_DEFINITIONS = {
    "decoding_disabled_by_operator": "Legacy decoded-frame capture is intentionally disabled. No object inference or pixel motion frames are expected. Native stream-copy recording and independent compressed-only learning are separate workers.",
    "capture_offline": "An enabled camera reports near-zero decoded capture FPS. The cause is not established.",
    "capture_queue_full": "Capture-to-tracker queue.Full discards a detection frame. This does not measure lost recorded video.",
    "output_queue_full": "The tracker completed processing but the downstream tracked-object queue was full, so its result was discarded.",
    "throughput_gap": "Published processing FPS is below capture FPS. Without a current downstream counter, worker versus consumer bottlenecks remain unproven.",
    "analysis_floor": "The separate AI analysis copy is at its configured minimum. Capture decoding and the model's fixed tensor dimensions do not change.",
    "decoder_saturated": "GPU video-decoder utilization is at least 95 percent. This is not GPU compute utilization or proof of recording loss.",
    "gpu_hot": "A temperature measurement is at least 85C. Actual thermal throttling and device-specific temperature limits are not established by this snapshot.",
    "emergency_deletion": "The existing storage policy permits deleting eligible RECORDINGS under critical free-space pressure. This does not mean temporary buffers or logs are being cleared, or prove any deletion occurred.",
    "inference_slow": "A detector's measured inference time is at least 30ms. Camera frames may request multiple region inferences.",
}


class DiagnosticFinding(BaseModel):
    code: str
    severity: Literal["info", "warning", "error"]
    confidence: Literal["observed", "possible"]
    camera: str | None = None
    evidence: dict[str, float] = Field(default_factory=dict)


class CameraDiagnostic(BaseModel):
    camera: str
    capture_fps: float | None
    process_fps: float | None
    skipped_fps: float | None
    skipped_percent: float | None
    detection_fps: float | None
    tracker_fps: float | None = None
    output_dropped_fps: float | None = None
    source_height: float | None = None
    analysis_height: float | None = None
    detection_enabled: bool


class GpuDiagnostic(BaseModel):
    gpu: str
    decoder_percent: float | None
    compute_percent: float | None
    temperature: float | None


class DiagnosticsReport(BaseModel):
    sampled_at: float
    stats_time: float | None
    age_seconds: float | None
    stale: bool
    findings: list[DiagnosticFinding]
    cameras: list[CameraDiagnostic]
    gpus: list[GpuDiagnostic]
    detector_inference_ms: list[float]
    total_cameras: int
    truncated: bool
    ai_available: bool


def metric(value: object) -> float | None:
    """Keep missing/invalid measurements unknown, not zero or healthy."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        result = float(value.rstrip("%") if isinstance(value, str) else value)
    except ValueError:
        return None
    return result if math.isfinite(result) and result >= 0 else None


def mapping(value: object) -> dict[str, Any]:
    """Read only mappings from partially available stats snapshots."""
    return value if isinstance(value, dict) else {}


def build_diagnostics(
    stats: dict[str, Any], config: FrigateConfig, now: float
) -> DiagnosticsReport:
    """Explain measured pressure without treating tracking skips as lost recordings."""
    service = mapping(stats.get("service"))
    stats_time = metric(service.get("last_updated"))
    age = max(0, now - stats_time) if stats_time is not None else None
    stale = age is None or age > 30 or stats_time > now + 5
    findings: list[DiagnosticFinding] = []

    def finding(
        code, severity="warning", confidence="observed", camera=None, **evidence
    ):
        findings.append(
            DiagnosticFinding(
                code=code,
                severity=severity,
                confidence=confidence,
                camera=camera,
                evidence={
                    key: value for key, value in evidence.items() if value is not None
                },
            )
        )

    if stale:
        finding("stale", age_seconds=age)
    elif (metric(service.get("uptime")) or 0) < 120:
        finding("warming_up", severity="info")
    cameras = []
    active = [
        (name, camera) for name, camera in config.cameras.items() if camera.enabled
    ]
    for name, camera in active[:100]:
        data = mapping(mapping(stats.get("cameras")).get(name))
        record_only = data.get("capture_mode") == "recording_only"
        capture = metric(data.get("camera_fps"))
        process = metric(data.get("process_fps"))
        skipped = metric(data.get("skipped_fps"))
        skipped_percent = (
            min(100, 100 * skipped / capture)
            if capture is not None and capture > 0 and skipped is not None
            else None
        )
        pipeline = mapping(data.get("pipeline"))
        pipeline_time = metric(pipeline.get("sampled_at"))
        pipeline_fresh = (
            pipeline_time is not None
            and stats_time is not None
            and -5 <= stats_time - pipeline_time <= 15
        )
        tracker = metric(pipeline.get("tracker_fps")) if pipeline_fresh else None
        dropped = metric(pipeline.get("output_dropped_fps")) if pipeline_fresh else None
        dynamic = mapping(data.get("dynamic_resolution"))
        cameras.append(
            CameraDiagnostic(
                camera=name,
                capture_fps=capture,
                process_fps=process,
                skipped_fps=skipped,
                skipped_percent=skipped_percent,
                detection_fps=metric(data.get("detection_fps")),
                tracker_fps=tracker,
                output_dropped_fps=dropped,
                source_height=metric(dynamic.get("source_height")),
                analysis_height=metric(dynamic.get("height")),
                detection_enabled=camera.detect.enabled and not record_only,
            )
        )
        if stale:
            continue
        if record_only:
            finding("decoding_disabled_by_operator", severity="info", camera=name)
            continue
        if capture is None:
            finding("camera_unknown", camera=name)
        elif capture < 0.1:
            finding(
                "capture_offline", severity="error", camera=name, capture_fps=capture
            )
        elif camera.detect.enabled:
            if process is not None and process <= 0:
                finding(
                    "processor_stalled",
                    severity="error",
                    camera=name,
                    capture_fps=capture,
                    process_fps=process,
                )
            if skipped_percent is not None and skipped_percent >= 5:
                finding(
                    "capture_queue_full",
                    camera=name,
                    skipped_percent=skipped_percent,
                    skipped_fps=skipped,
                    capture_fps=capture,
                )
            if dropped is not None and dropped >= 0.1:
                finding(
                    "output_queue_full",
                    camera=name,
                    output_dropped_fps=dropped,
                    tracker_fps=tracker,
                    process_fps=process,
                )
            elif dropped is None and process is not None and process < capture * 0.7:
                finding(
                    "throughput_gap",
                    confidence="possible",
                    camera=name,
                    capture_fps=capture,
                    process_fps=process,
                )
            if (
                dynamic.get("enabled")
                and dynamic.get("reason") == "minimum"
                and skipped_percent is not None
                and skipped_percent >= 5
            ):
                finding(
                    "analysis_floor",
                    camera=name,
                    analysis_height=metric(dynamic.get("height")),
                    source_height=metric(dynamic.get("source_height")),
                )
    gpus = []
    for name, raw in list(mapping(stats.get("gpu_usages")).items())[:16]:
        data = mapping(raw)
        decoder, compute, temperature = (
            metric(data.get(key)) for key in ("dec", "gpu", "temp")
        )
        gpus.append(
            GpuDiagnostic(
                gpu=name,
                decoder_percent=decoder,
                compute_percent=compute,
                temperature=temperature,
            )
        )
        if not stale and decoder is not None and decoder >= 95:
            finding("decoder_saturated", decoder_percent=decoder)
        if not stale and temperature is not None and temperature >= 85:
            finding("gpu_hot", temperature=temperature, confidence="possible")
    inference = []
    for data in list(mapping(stats.get("detectors")).values())[:16]:
        value = metric(mapping(data).get("inference_speed"))
        if value is not None:
            inference.append(value)
            if not stale and value >= 30:
                finding("inference_slow", inference_ms=value)
    for category in ("recordings", "cache", "shm"):
        path = {
            "recordings": "/media/frigate/recordings",
            "cache": "/tmp/cache",
            "shm": "/dev/shm",
        }[category]
        data = mapping(mapping(service.get("storage")).get(path))
        free, total = metric(data.get("free")), metric(data.get("total"))
        if (
            not stale
            and free is not None
            and total is not None
            and total > 0
            and free / total < 0.05
        ):
            finding(
                category + "_space_low",
                severity="error",
                free_percent=100 * free / total,
            )
    if config.storage.emergency_delete:
        finding("emergency_deletion", confidence="observed")
    if not findings:
        finding("no_thresholds", severity="info")
    order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda item: order[item.severity])
    return DiagnosticsReport(
        sampled_at=now,
        stats_time=stats_time,
        age_seconds=age,
        stale=stale,
        findings=findings,
        cameras=cameras,
        gpus=gpus,
        detector_inference_ms=inference,
        total_cameras=len(active),
        truncated=len(active) > 100,
        ai_available=any(
            "chat" in provider.roles for provider in config.genai.values()
        ),
    )


def explanation_payload(report: DiagnosticsReport) -> dict[str, Any]:
    """Send numeric allowlisted evidence with aliases, never names/config/prompts/images."""
    aliases = {
        item.camera: f"Camera{index + 1}" for index, item in enumerate(report.cameras)
    }
    return {
        "stats_time": report.stats_time,
        "age_seconds": report.age_seconds,
        "stale": report.stale,
        "total_cameras": report.total_cameras,
        "truncated": report.truncated or len(report.cameras) > 20,
        "cameras": [
            {**item.model_dump(exclude={"camera"}), "camera": aliases[item.camera]}
            for item in report.cameras[:20]
        ],
        "gpus": [
            {**item.model_dump(exclude={"gpu"}), "gpu": f"GPU{index + 1}"}
            for index, item in enumerate(report.gpus)
        ],
        "detector_inference_ms": report.detector_inference_ms,
        "definitions": {
            item.code: AI_DEFINITIONS[item.code]
            for item in report.findings
            if item.code in AI_DEFINITIONS
        },
        "findings": [
            {**item.model_dump(exclude={"camera"}), "camera": aliases.get(item.camera)}
            for item in report.findings
            if item.camera is None
            or item.camera in {camera.camera for camera in report.cameras[:20]}
        ],
    }

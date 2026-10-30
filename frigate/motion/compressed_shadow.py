"""Bounded offline motion-vector/PID replay; never controls live decoding or recordings."""

import argparse
import hashlib
import json
import math
import platform
import statistics
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import av
import cv2
import numpy as np
from av.sidedata.motionvectors import MotionVectors

from frigate.motion.compressed import (
    BACKEND_ID,
    Qualification,
    ShadowDecodeGate,
    ShadowMotionPID,
    vector_evidence,
)

BACKENDS = {
    "normal_decode": {},
    "ffmpeg_vector_export": {"flags2": "+export_mvs"},
    "ffmpeg_vector_export_no_deblock": {
        "flags2": "+export_mvs",
        "skip_loop_filter": "all",
    },
}


def frame_time(frame: av.VideoFrame) -> float:
    """Require source presentation time; frame arrival is not a substitute."""
    if frame.pts is None or frame.time_base is None:
        raise ValueError("Source frame lacks a presentation timestamp")
    result = float(frame.pts * frame.time_base)
    if not math.isfinite(result):
        raise ValueError("Source timestamp is not finite")
    return result


def vectors_from(frame: av.VideoFrame) -> np.ndarray | None:
    """Read real FFmpeg motion side data, not estimates synthesized from packet sizes."""
    side = frame.side_data.get("MOTION_VECTORS")
    if isinstance(side, MotionVectors):
        result = side.to_ndarray()
        if not isinstance(result, np.ndarray):
            raise ValueError("The extractor returned an invalid vector array")
        return result
    return None


def benchmark(path: Path, backend: str, seconds: float) -> dict:
    """Measure identical bounded source spans without copying image planes to Python."""
    cpu, wall = time.process_time(), time.perf_counter()
    frames = vector_count = 0
    first = last = None
    with path.open("rb") as handle, av.open(handle, mode="r") as container:
        stream = container.streams.video[0]
        if stream.codec_context.name != "h264":
            raise ValueError("This shadow backend is evaluated only for H.264")
        stream.codec_context.thread_count = 2
        stream.codec_context.options = BACKENDS[backend]
        for frame in container.decode(stream):
            timestamp = frame_time(frame)
            first = timestamp if first is None else first
            if timestamp - first >= seconds:
                break
            last = timestamp
            frames += 1
            if backend != "normal_decode":
                vectors = vectors_from(frame)
                if vectors is not None:
                    vector_count += len(vectors)
    if not frames or first is None or last is None:
        raise ValueError("The input did not yield a bounded video sample")
    return {
        "backend": backend,
        "frames": frames,
        "vectors": vector_count,
        "cpu_seconds": time.process_time() - cpu,
        "wall_seconds": time.perf_counter() - wall,
        "source_first_pts_seconds": first,
        "source_last_pts_seconds": last,
        "full_pixel_reconstruction": True,
        "note": "Skipping deblocking or RGB export does not make this an entropy-only extractor",
    }


def replay(path: Path, seconds: float) -> dict:
    """Compare vector activity with a pixel-change proxy; do not call it event recall."""
    cpu, wall = time.process_time(), time.perf_counter()
    pid = ShadowMotionPID()
    gate = ShadowDecodeGate()
    first = last = previous_sample = None
    previous_gray = None
    observed = unknown = pixel_changes = disagreements = 0
    vector_candidates = wake = 0
    samples = []
    with path.open("rb") as handle, av.open(handle, mode="r") as container:
        stream = container.streams.video[0]
        stream.codec_context.thread_count = 2
        stream.codec_context.options = BACKENDS["ffmpeg_vector_export"]
        for frame in container.decode(stream):
            timestamp = frame_time(frame)
            first = timestamp if first is None else first
            if timestamp - first >= seconds:
                break
            if previous_sample is not None and timestamp - previous_sample < 0.195:
                continue
            previous_sample, last = timestamp, timestamp
            vectors = vectors_from(frame)
            picture_type = av.video.frame.PictureType(frame.pict_type).name
            evidence = vector_evidence(
                vectors,
                timestamp=timestamp,
                width=frame.width,
                height=frame.height,
                picture_type=picture_type,
            )
            gray_frame = frame.reformat(width=320, height=180, format="gray")
            gray = gray_frame.to_ndarray()
            changed: bool | None = (
                None
                if previous_gray is None
                else float(np.mean(cv2.absdiff(gray, previous_gray) > 12)) >= 0.001
            )
            previous_gray = gray
            # A quiet adjacent frame is not sufficient proof of stable camera alignment.
            offset = pid.observe(
                evidence,
                pixel_verified_quiet=changed is False,
                view_verified=False,
            )
            candidate = bool(np.any(evidence.activity > 0.02 + offset))
            decision = gate.decide(
                evidence, view_verified=False, adaptive_offset=offset
            )
            observed += 1
            unknown += int(evidence.uncertainty is not None)
            pixel_changes += int(changed is True)
            disagreements += int(changed is True and not candidate)
            vector_candidates += int(candidate)
            wake += int(decision.decode)
            samples.append(
                {
                    "pts_seconds": timestamp,
                    "picture_type": picture_type,
                    "vector_count": evidence.vector_count,
                    "coverage": evidence.coverage,
                    "active_cell_fraction": float(np.mean(evidence.activity > 0.02)),
                    "uncertainty": evidence.uncertainty,
                    "pixel_change_proxy": changed,
                    "vector_motion_candidate": candidate,
                    "would_decode": decision.decode,
                    "gate_reason": decision.reason,
                }
            )
    if not observed:
        raise ValueError("The shadow replay produced no observations")
    return {
        "sampled_frames": observed,
        "unknown_frames": unknown,
        "vector_motion_candidates": vector_candidates,
        "pixel_change_proxy_frames": pixel_changes,
        "pixel_proxy_disagreements": disagreements,
        "would_decode_frames": wake,
        "cpu_seconds": time.process_time() - cpu,
        "wall_seconds": time.perf_counter() - wall,
        "source_first_pts_seconds": first,
        "source_last_pts_seconds": last,
        "pid_backend_identity": BACKEND_ID,
        "pid_persisted": False,
        "pid_learning_paused": "Camera alignment is not independently verified by this replay",
        "reference_limit": "Adjacent-frame luminance-change proxy only; not labeled-event ground truth or the production adaptive detector",
        "samples": samples,
    }


def run(path: Path, seconds: float, repeats: int) -> dict:
    """Produce honest timing and qualification evidence without a live-activation option."""
    if not path.is_file() or not 1 <= seconds <= 120 or not 1 <= repeats <= 3:
        raise ValueError("Use an existing local file, 1-120 seconds and 1-3 repeats")
    with path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    timings: dict[str, list[dict[str, Any]]] = {backend: [] for backend in BACKENDS}
    for iteration in range(repeats):
        order = list(BACKENDS)
        if iteration % 2:
            order.reverse()
        for backend in order:
            timings[backend].append(benchmark(path, backend, seconds))
    counts = {item["frames"] for values in timings.values() for item in values}
    if len(counts) != 1:
        raise ValueError("Backend frame counts differ; do not compare mismatched work")
    medians = {
        backend: statistics.median(item["cpu_seconds"] for item in values)
        for backend, values in timings.items()
    }
    reference = medians["normal_decode"]
    ratio = medians["ffmpeg_vector_export"] / reference if reference > 0 else None
    qualification = Qualification(measured_resource_ratio=ratio)
    return {
        "mode": "OFFLINE_SHADOW_ONLY",
        "live_detection_changed": False,
        "live_gate_available": False,
        "input_name": path.name,
        "input_sha256": digest,
        "requested_seconds": seconds,
        "repeats": repeats,
        "software": {"python": platform.python_version(), "pyav": av.__version__},
        "backend_timings": timings,
        "median_cpu_seconds": medians,
        "vector_export_to_normal_cpu_ratio": ratio,
        "qualification": asdict(qualification),
        "live_gate_blockers": qualification.blockers(),
        "replay": replay(path, seconds),
        "interpretation": (
            "All currently benchmarked backends use FFmpeg frame decoding. "
            "No real entropy-only backend, residual features, labeled event recall, "
            "day/night coverage or compressed pre-roll handover has been validated. "
            "Do not enable suppression based on this report."
        ),
    }


def main() -> None:
    """Replay a local recording only; there are no stream credentials or apply flags."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = run(args.input, args.seconds, args.repeats)
    except (av.FFmpegError, OSError, ValueError) as error:
        parser.exit(
            1,
            "Shadow replay failed: "
            + type(error).__name__
            + "; live detection unchanged\n",
        )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, indent=2, allow_nan=False), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "mode": report["mode"],
                "input_sha256": report["input_sha256"],
                "median_cpu_seconds": report["median_cpu_seconds"],
                "sampled_frames": report["replay"]["sampled_frames"],
                "live_gate_available": False,
                "blockers": report["live_gate_blockers"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

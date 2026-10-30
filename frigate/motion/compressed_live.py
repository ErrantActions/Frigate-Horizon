"""Explicit experimental native4K capture integration; no activation in ordinary images."""

import io
import logging
import queue
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from functools import lru_cache
from multiprocessing import Value
from multiprocessing.synchronize import Event as MpEvent
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import av
import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from frigate.camera import CameraMetrics
from frigate.config import CameraConfig
from frigate.config.camera.ffmpeg import CameraRoleEnum
from frigate.motion.compressed_policy import NativeDecodePolicy, PixelFeedback
from frigate.motion.compressed_protocol import (
    CompressedEvidence,
    ExtractorEvent,
    PacketRecord,
    read_extractor_events,
)
from frigate.motion.compressed_renderer import NativeDecodedFrame
from frigate.motion.compressed_session import NativeCaptureSession
from frigate.motion.compressed_tuner import CompressedMotionTuner
from frigate.util.builtin import EventsPerSecond
from frigate.util.image import SharedMemoryFrameManager

logger = logging.getLogger(__name__)
ACTIVATION = Path("/opt/frigate/native-compressed-motion.json")
BINARY = "/opt/frigate/bin/native-h264-entropy"


class NativeActivation(BaseModel):
    """Image-scoped operator trial, not a claim of completed live event-recall qualification."""

    model_config = ConfigDict(extra="forbid")
    mode: Literal["experimental_native4k_gated"]
    cameras: set[str] = Field(min_length=1)
    startup_seconds: float = Field(default=30, ge=30, le=300)
    hold_seconds: float = Field(default=10, ge=1, le=60)
    compressed_tuner: bool = True


class NativeInput(BaseModel):
    """Private source fields never become process arguments, stats, or log messages."""

    source: SecretStr
    wallclock: bool


class NativeFeedbackRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    timestamp: float
    view_verified: bool
    motion_present: bool
    active_objects: bool


@lru_cache(maxsize=1)
def activation() -> NativeActivation | None:
    """Ordinary images have no activation; malformed trial configuration fails explicitly."""
    try:
        payload = ACTIVATION.read_bytes()
    except FileNotFoundError:
        return None
    return NativeActivation.model_validate_json(payload)


def native_enabled(camera: CameraConfig) -> bool:
    """Select only explicitly named cameras without changing their source or recording policy."""
    if camera.motion.backend == "pixel":
        return False
    settings = activation()
    if settings is None or camera.name not in settings.cameras:
        raise ValueError(
            "Compressed motion was selected without an enabled native trial build"
        )
    return True


def native_media_input(camera: CameraConfig) -> NativeInput:
    """Use the recording main stream, not a possibly lower-resolution detection substream."""
    commands = [
        entry["cmd"] for entry in camera.ffmpeg_cmds if "record" in entry["roles"]
    ]
    if len(commands) != 1:
        raise ValueError("Native capture requires one main recording input")
    command = commands[0]
    source = command[command.index("-i") + 1]
    if urlsplit(source).scheme != "rtsp" or any(char in source for char in "\r\n\x00"):
        raise ValueError("Native capture requires a qualified RTSP input")
    if (
        "-rtsp_transport" in command
        and command[command.index("-rtsp_transport") + 1] != "tcp"
    ):
        raise ValueError("Native capture requires the existing TCP transport")
    wallclock = (
        "-use_wallclock_as_timestamps" in command
        and command[command.index("-use_wallclock_as_timestamps") + 1] == "1"
    )
    return NativeInput(source=SecretStr(source), wallclock=wallclock)


def native_input(camera: CameraConfig) -> NativeInput:
    """Require native buffers for capture, not a separate compressed-only diagnostic reader."""
    settings = native_media_input(camera)
    if (camera.detect.width, camera.detect.height) != (3840, 2160):
        raise ValueError("Native trial requires active3840x2160 capture buffers")
    if camera.detect.dynamic_resolution.enabled:
        raise ValueError(
            "Native trial cannot use an intermediate dynamic analysis copy"
        )
    return settings


def recording_commands(camera: CameraConfig) -> list[dict[str, Any]]:
    """Give stream-copy recordings their own unchanged outputs, independent of decode sleep."""
    if not native_enabled(camera):
        return [entry for entry in camera.ffmpeg_cmds if "detect" not in entry["roles"]]
    commands = []
    for original in camera.ffmpeg.inputs:
        item = original.model_copy(deep=True)
        item.roles = [role for role in item.roles if role != CameraRoleEnum.detect]
        command = camera._get_ffmpeg_cmd(item)
        if command is not None:
            commands.append({"roles": item.roles, "cmd": command})
    return commands


def start_native_source(
    camera: CameraConfig, error_output: int
) -> subprocess.Popen[bytes]:
    """Read media only; credentials travel once through a private stdin pipe."""
    settings = native_input(camera)
    if not camera.name:
        raise ValueError("Native capture requires a configured camera identity")
    command = [BINARY, "--live-input", "--framed"]
    command.extend(["--min-motion", str(camera.motion.compressed_min_distance)])
    if camera.motion.compressed_max_distance is not None:
        command.extend(["--max-motion", str(camera.motion.compressed_max_distance)])
    if settings.wallclock:
        command.append("--wallclock")
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=error_output,
        start_new_session=True,
        bufsize=0,
    )
    assert process.stdin is not None
    try:
        process.stdin.write((settings.source.get_secret_value() + "\n").encode())
        process.stdin.close()
        process.stdin = None
    except OSError:
        process.terminate()
        process.wait(timeout=10)
        logger.error("Unable to initialize native entropy source")
        raise
    return process


def full_native_fallback_command(camera: CameraConfig) -> list[str]:
    """Preserve native main pixels using the normal decoder when entropy evidence fails."""
    native_input(camera)
    inputs = [item for item in camera.ffmpeg.inputs if "record" in item.roles]
    if len(inputs) != 1:
        raise ValueError("Native full-decode fallback requires one recording main")
    source = inputs[0].model_copy(deep=True)
    source.roles = [CameraRoleEnum.detect]
    command = camera._get_ffmpeg_cmd(source)
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(value, str) for value in command)
        or command[-1] != "pipe:"
    ):
        raise ValueError("Unable to construct native full-decode fallback")
    return [str(value) for value in command]


class NativeCameraCapture(threading.Thread):
    """Publish only real native GPU frames into the existing camera shared-memory ring."""

    def __init__(
        self,
        config: CameraConfig,
        source: subprocess.Popen[bytes],
        error_output: int,
        shm_frame_count: int,
        frame_index: int,
        camera_metrics: CameraMetrics,
        stop_event: MpEvent,
        on_fallback: Callable[[subprocess.Popen[bytes]], None] | None = None,
    ) -> None:
        if not config.name:
            raise ValueError("Native capture requires a configured camera identity")
        super().__init__(name=f"native-capture:{config.name}")
        self.camera_name = config.name
        self.config = config
        self.source_process = source
        self.error_output = error_output
        self.shm_frame_count = shm_frame_count
        self.frame_index = frame_index
        self.frame_queue = camera_metrics.frame_queue
        self.camera_fps = camera_metrics.camera_fps
        self.skipped_fps = camera_metrics.skipped_fps
        self.stop_event = stop_event
        self.pipeline = camera_metrics.pipeline
        self.on_fallback = on_fallback
        self.fallback_process: subprocess.Popen[bytes] | None = None
        self.fallback_frames = 0
        self.process_lock = threading.Lock()
        self.current_frame = Value("d", time.time())
        self.epoch: float | None = None
        self.frame_manager = SharedMemoryFrameManager()
        self.frame_times: NDArray[np.float64] | None = None
        self.frame_rate = EventsPerSecond()
        self.skips = EventsPerSecond()
        self.source_rate = EventsPerSecond()
        self.next_status = 0.0
        self.session: NativeCaptureSession | None = None
        self.last_evidence = time.monotonic()
        self.last_rendered = time.monotonic()
        self.last_rendered_at: float | None = None
        self.local_stop = threading.Event()
        self.last_failure: str | None = None

    def stop(self) -> None:
        """Interrupt only this camera's capture reader before watchdog reconfiguration."""
        self.local_stop.set()
        with self.process_lock:
            for process in (self.source_process, self.fallback_process):
                if process is not None and process.poll() is None:
                    process.terminate()

    def _stop_source(self) -> None:
        if self.source_process.poll() is None:
            self.source_process.terminate()
            try:
                self.source_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.source_process.kill()
                self.source_process.wait(timeout=5)
        if (
            self.source_process.stdout is not None
            and not self.source_process.stdout.closed
        ):
            self.source_process.stdout.close()

    def _run_full_fallback(self) -> None:
        """Stop uncertain entropy suppression and retain ordinary native pixel processing."""
        if self.session is not None:
            self.session.close()
        self._stop_source()
        with self.process_lock:
            if self.local_stop.is_set() or self.stop_event.is_set():
                return
            if self.on_fallback is None:
                raise RuntimeError("Native full-decode fallback has no watchdog owner")
            process = subprocess.Popen(
                full_native_fallback_command(self.config),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=self.error_output,
                bufsize=3840 * 2160 * 3 // 2,
                start_new_session=True,
            )
            self.fallback_process = process
            self.on_fallback(process)
        assert process.stdout is not None
        if not isinstance(process.stdout, io.BufferedReader):
            raise RuntimeError("Native fallback requires a buffered pixel pipe")
        frame_bytes = 3840 * 2160 * 3 // 2
        storage = bytearray(frame_bytes)
        try:
            while (
                not self.stop_event.is_set()
                and not self.local_stop.is_set()
                and self.config.enabled
            ):
                count = process.stdout.readinto(storage)
                if count != frame_bytes:
                    if self.stop_event.is_set() or self.local_stop.is_set():
                        return
                    raise EOFError(
                        "Native full-decode fallback stopped producing frames"
                    )
                now = time.time()
                self.current_frame.value = now
                if self.epoch is None:
                    self.epoch = now
                pixels = np.frombuffer(storage, dtype=np.uint8).reshape(3240, 3840)
                self.emit(NativeDecodedFrame(pixels, now - self.epoch))
                self.fallback_frames += 1
                self.pipeline["native_capture"] = {
                    "backend": "native4k_full_decode_fallback",
                    "state": "full_fallback",
                    "source_width": 3840,
                    "source_height": 2160,
                    "decoded_fps": self.frame_rate.eps(),
                    "rendered_frames": self.fallback_frames,
                    "parser_pid": None,
                    "renderer_pid": process.pid,
                    "sampled_at": now,
                    "last_frame_at": now,
                    "live_recall_qualified": False,
                    "fallback_enabled": self.config.motion.compressed_fallback,
                    "compressed_tuner": {
                        "enabled": False,
                        "reason": "full_decode_fallback",
                    },
                    "reason": "entropy_source_failed",
                }
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            process.stdout.close()

    def feedback(self) -> PixelFeedback | None:
        """Use actual tracker/pixel alignment observations; absent feedback forces full decode."""
        value = self.pipeline.get("native_feedback")
        if value is None or self.epoch is None:
            return None
        feedback = NativeFeedbackRecord.model_validate(value)
        return PixelFeedback(
            feedback.timestamp - self.epoch,
            feedback.view_verified,
            feedback.motion_present,
            feedback.active_objects,
        )

    def emit(self, frame: NativeDecodedFrame) -> None:
        """Copy native pixels without scaling or falsifying an unobserved frame timestamp."""
        if self.epoch is None or self.frame_times is None:
            raise RuntimeError(
                "Native source clock or shared-memory ledger is unavailable"
            )
        timestamp = self.epoch + frame.timestamp
        frame_name = f"{self.config.name}_frame{self.frame_index}"
        target = self.frame_manager.write(frame_name)
        if target is None:
            raise RuntimeError("Native camera shared-memory slot is unavailable")
        self.frame_times[self.frame_index] = 0
        pixels = np.ndarray(self.config.frame_shape_yuv, dtype=np.uint8, buffer=target)
        if pixels.shape != frame.yuv.shape:
            raise ValueError("Native camera shared-memory geometry changed")
        pixels[:] = frame.yuv
        self.frame_times[self.frame_index] = timestamp
        self.frame_rate.update()
        self.last_rendered = time.monotonic()
        self.last_rendered_at = time.time()
        self.camera_fps.value = self.frame_rate.eps()
        try:
            self.frame_queue.put((frame_name, timestamp), False)
        except queue.Full:
            self.skips.update()
            self.skipped_fps.value = self.skips.eps()
            return
        self.frame_index = (self.frame_index + 1) % self.shm_frame_count

    def events(self) -> Iterator[ExtractorEvent]:
        """Source packets provide liveness even while image decoding is intentionally asleep."""
        assert self.source_process.stdout is not None
        for event in read_extractor_events(self.source_process.stdout):
            if (
                self.stop_event.is_set()
                or self.local_stop.is_set()
                or not self.config.enabled
            ):
                return
            self.current_frame.value = time.time()
            if isinstance(event, PacketRecord):
                self.source_rate.update()
            elif isinstance(event, CompressedEvidence):
                self.last_evidence = time.monotonic()
                if self.epoch is None:
                    self.epoch = time.time() - event.timestamp
            now = time.monotonic()
            if now - self.last_evidence > 10:
                raise RuntimeError(
                    "Native source stopped producing verified motion evidence"
                )
            if (
                self.session is not None
                and self.session.renderer is not None
                and now - self.last_rendered > 10
            ):
                raise RuntimeError("Native decoder stopped delivering safety probes")
            if now >= self.next_status and self.session is not None:
                stats = self.session.stats
                self.pipeline["native_capture"] = {
                    "backend": "experimental_native4k_gated",
                    "source_fps": self.source_rate.eps(),
                    "decoded_fps": self.frame_rate.eps(),
                    "source_packets": stats.source_packets,
                    "gpu_packets": stats.gpu_packets,
                    "rendered_frames": stats.rendered_frames,
                    "quiet_requests": stats.quiet_requests,
                    "full_requests": stats.full_requests,
                    "idr_probes": stats.idr_probes,
                    "parser_pid": self.source_process.pid,
                    "renderer_pid": self.session.renderer.process.pid
                    if self.session.renderer
                    else None,
                    "source_width": 3840,
                    "source_height": 2160,
                    "sampled_at": time.time(),
                    "live_recall_qualified": False,
                    "state": self.session.last_decision.action
                    if self.session.last_decision
                    else "starting",
                    "last_frame_at": self.last_rendered_at,
                    "fallback_enabled": self.config.motion.compressed_fallback,
                    "compressed_tuner": (
                        self.session.tuner.snapshot()
                        if self.session.tuner is not None
                        else {"enabled": False}
                    ),
                }
                self.camera_fps.value = self.frame_rate.eps()
                self.skipped_fps.value = self.skips.eps()
                self.next_status = now + 2
            yield event

    def run(self) -> None:
        settings = activation()
        if settings is None:
            raise RuntimeError("Native capture has no explicit experiment activation")
        self.frame_times = self.frame_manager.frame_times(self.camera_name)
        self.frame_rate.start()
        self.skips.start()
        self.source_rate.start()
        self.session = NativeCaptureSession(
            self.config.ffmpeg.ffmpeg_path,
            self.error_output,
            self.emit,
            self.feedback,
            policy=NativeDecodePolicy(
                startup_seconds=settings.startup_seconds,
                hold_seconds=settings.hold_seconds,
                use_pixel_motion=not settings.compressed_tuner,
                activity_threshold=self.config.motion.compressed_min_activity,
            ),
            tuner=CompressedMotionTuner() if settings.compressed_tuner else None,
        )
        try:
            self.session.run(self.events())
        except (
            ValueError,
            OSError,
            EOFError,
            RuntimeError,
            TimeoutError,
            av.error.FFmpegError,
        ) as error:
            if (
                not self.stop_event.is_set()
                and not self.local_stop.is_set()
                and self.config.enabled
            ):
                self.last_failure = type(error).__name__
                if self.config.motion.compressed_fallback:
                    logger.exception(
                        "Native entropy source failed for %s; opted-in full native decoding will start",
                        self.config.name,
                    )
                    try:
                        self._run_full_fallback()
                    except (ValueError, OSError, EOFError, RuntimeError):
                        logger.exception(
                            "Native full-decode fallback failed for %s",
                            self.config.name,
                        )
                else:
                    logger.exception(
                        "Native entropy source failed for %s; full-decode fallback is disabled, the compressed worker will restart",
                        self.config.name,
                    )
        finally:
            self.session.close()
            self.pipeline["native_capture"] = {
                "backend": "experimental_native4k_gated",
                "state": "failed" if self.last_failure else "stopped",
                "sampled_at": time.time(),
                "live_recall_qualified": False,
                "failure": self.last_failure,
                "fallback_enabled": self.config.motion.compressed_fallback,
            }
            self._stop_source()
            self.frame_times = None
            self.frame_manager.close_camera(self.camera_name)

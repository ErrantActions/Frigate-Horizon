"""Experimental encoded-packet/decision/native-renderer coordination; not a qualified live gate."""

import queue
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import av

from frigate.motion.compressed_buffer import (
    EncodedGopBuffer,
    EncodedPacket,
    H264Stream,
    h264_is_idr,
)
from frigate.motion.compressed_policy import (
    NativeDecodeDecision,
    NativeDecodePolicy,
    PixelFeedback,
)
from frigate.motion.compressed_protocol import (
    CompressedEvidence,
    ExtractorEvent,
    ExtractorSummary,
    PacketRecord,
    StreamHeader,
)
from frigate.motion.compressed_renderer import NativeDecodedFrame, NativePacketRenderer
from frigate.motion.compressed_tuner import CompressedMotionTuner


@dataclass
class NativeSessionStats:
    """Count actual supplied/rendered work rather than claiming a utilization saving."""

    source_packets: int = 0
    gpu_packets: int = 0
    rendered_frames: int = 0
    full_requests: int = 0
    idr_probes: int = 0
    quiet_requests: int = 0


class NativeCaptureSession:
    """Keep packet ownership and source timestamps intact through a bounded decoder wake."""

    def __init__(
        self,
        ffmpeg: str,
        error_output: int,
        emit: Callable[[NativeDecodedFrame], None],
        feedback: Callable[[], PixelFeedback | None],
        *,
        policy: NativeDecodePolicy | None = None,
        expected_geometry: tuple[int, int] = (3840, 2160),
        cuda: bool = True,
        tuner: CompressedMotionTuner | None = None,
    ) -> None:
        self.ffmpeg = ffmpeg
        self.error_output = error_output
        self.emit = emit
        self.feedback = feedback
        self.policy = policy or NativeDecodePolicy()
        self.expected_geometry = expected_geometry
        self.cuda = cuda
        self.tuner = tuner
        self.buffer = EncodedGopBuffer()
        self.stats = NativeSessionStats()
        self.header: StreamHeader | None = None
        self.source: H264Stream | None = None
        self.pending: dict[int, PacketRecord] = {}
        self.pending_bytes = 0
        self.renderer: NativePacketRenderer | None = None
        self.consumer: threading.Thread | None = None
        self.reader_errors: queue.Queue[Exception] = queue.Queue()
        self.last_sent: int | float | None = None
        self.last_emitted = float("-inf")
        self.stopping = False
        self.verified_summary: ExtractorSummary | None = None
        self.last_decision: NativeDecodeDecision | None = None

    def _consume(self) -> None:
        assert self.renderer is not None
        try:
            for frame in self.renderer.frames():
                if frame.timestamp <= self.last_emitted:
                    continue
                self.last_emitted = frame.timestamp
                self.emit(frame)
                self.stats.rendered_frames += 1
        except (
            ValueError,
            OSError,
            EOFError,
            RuntimeError,
            av.error.FFmpegError,
        ) as error:
            if not self.stopping:
                self.reader_errors.put(error)

    def _check_reader(self) -> None:
        try:
            error = self.reader_errors.get_nowait()
        except queue.Empty:
            return
        raise RuntimeError("Native frame consumer failed") from error

    def _renderer(self) -> NativePacketRenderer:
        if self.renderer is None:
            if self.source is None:
                raise RuntimeError("Native source geometry is not verified")
            self.renderer = NativePacketRenderer(
                self.ffmpeg, self.source, self.error_output, cuda=self.cuda
            )
            self.consumer = threading.Thread(target=self._consume, daemon=True)
            self.consumer.start()
        return self.renderer

    def _send(self, packet: EncodedPacket) -> None:
        if self.last_sent is not None and packet.dts <= self.last_sent:
            return
        self._renderer().write(packet)
        self.last_sent = packet.dts
        self.stats.gpu_packets += 1

    def observe(self, event: ExtractorEvent) -> None:
        """Process real source evidence; raw packets remain pending until their frame validates."""
        self._check_reader()
        if isinstance(event, StreamHeader):
            if self.header is not None:
                raise ValueError(
                    "Native source configuration restarted without a new session"
                )
            self.header = event
            return
        if isinstance(event, PacketRecord):
            if event.pts in self.pending:
                raise ValueError(
                    "Source access units have ambiguous presentation timestamps"
                )
            self.pending[event.pts] = event
            self.pending_bytes += len(event.data)
            self.stats.source_packets += 1
            if self.pending_bytes > self.buffer.max_bytes:
                raise ValueError(
                    "Unvalidated encoded packets exceeded their memory budget"
                )
            return
        if isinstance(event, ExtractorSummary):
            self.verified_summary = event
            return
        if not isinstance(event, CompressedEvidence) or self.header is None:
            raise ValueError("Native evidence has no source configuration")
        if (event.width, event.height) != self.expected_geometry:
            raise ValueError("Native source geometry does not match capture buffers")
        if self.source is None:
            self.source = self.header.native_stream(event.width, event.height)
        encoded = self.pending.pop(event.pts, None)
        if encoded is None:
            raise ValueError("Compressed evidence did not match a source access unit")
        self.pending_bytes -= len(encoded.data)
        packet = encoded.for_stream(self.source)
        pressure = self.buffer.bytes_used + len(
            packet.data
        ) >= self.buffer.max_bytes * 0.8 or (
            self.buffer.gops
            and packet.decode_time - self.buffer.gops[0][0].decode_time
            >= self.buffer.max_seconds - 1
        )
        update = self.buffer.append(packet)
        if not update.stored:
            raise ValueError("Native decoder reference-chain pre-roll is unavailable")
        idr = h264_is_idr(packet.data, self.source.length_size)
        feedback = self.feedback()
        thresholds = (
            self.tuner.observe(
                event,
                protected_activity=feedback is not None and feedback.active_objects,
                suppression_allowed=feedback is not None and feedback.view_verified,
            )
            if self.tuner is not None
            else None
        )
        decision = self.policy.decide(
            event,
            idr=idr,
            feedback=feedback,
            buffer_pressure=bool(pressure),
            thresholds=thresholds,
        )
        self.last_decision = decision
        if decision.action == "full":
            self.stats.full_requests += 1
            for cached in self.buffer.snapshot(0).packets:
                self._send(cached)
        elif decision.action == "idr_probe":
            self.stats.idr_probes += 1
            self._send(packet)
        else:
            self.stats.quiet_requests += 1

    def run(self, events: Iterable[ExtractorEvent]) -> NativeSessionStats:
        """Run a bounded source replay and require verified extractor and renderer completion."""
        try:
            for event in events:
                self.observe(event)
            if self.verified_summary is None:
                raise ValueError(
                    "Native source did not finish with an entropy-only summary"
                )
            if self.renderer is not None:
                self.renderer.finish_input()
                assert self.consumer is not None
                self.consumer.join(timeout=10)
                if self.consumer.is_alive():
                    raise TimeoutError("Native renderer did not drain source frames")
                self._check_reader()
                self.renderer.wait()
            return self.stats
        finally:
            self.close()

    def close(self) -> None:
        """Stop only this session's renderer and release its encoded working set."""
        self.stopping = True
        if self.renderer is not None:
            self.renderer.stop()
        if self.consumer is not None:
            self.consumer.join(timeout=5)
        self.pending.clear()
        self.pending_bytes = 0
        self.buffer.clear()

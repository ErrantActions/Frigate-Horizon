"""On-demand native-resolution rendering of buffered H264 packets, never a 1080p transcode."""

import io
import logging
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from frigate.motion.compressed_buffer import EncodedPacket, H264Stream
from frigate.motion.compressed_mux import EncodedNutMuxer

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

logger = logging.getLogger(__name__)


def native_decoder_command(
    ffmpeg: str, fps: int = 5, *, cuda: bool = True
) -> list[str]:
    """Keep native pixels, select without delayed FPS duplication, and retain IDR probes."""
    if not ffmpeg or not 1 <= fps <= 60:
        raise ValueError("Invalid native decoder executable or FPS")
    select = (
        "select=isnan(prev_selected_t)"
        f"+gt(floor(t*{fps}+0.000001)\\,floor(prev_selected_t*{fps}+0.000001))"
    )
    filters = select + (",hwdownload,format=nv12" if cuda else "")
    return [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-copyts",
        "-threads",
        "1",
        *(["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"] if cuda else []),
        "-analyzeduration",
        "1",
        "-fpsprobesize",
        "0",
        "-probesize",
        "32",
        "-f",
        "nut",
        "-i",
        "pipe:0",
        "-map",
        "0:v:0",
        "-an",
        "-vf",
        filters,
        "-pix_fmt",
        "yuv420p",
        "-fps_mode",
        "passthrough",
        "-enc_time_base",
        "demux",
        "-c:v",
        "rawvideo",
        "-threads",
        "1",
        "-avoid_negative_ts",
        "disabled",
        "-flush_packets",
        "1",
        "-f",
        "nut",
        "pipe:1",
    ]


@dataclass(frozen=True)
class NativeDecodedFrame:
    """A selected native I420 frame with its real presentation time."""

    yuv: "NDArray[np.uint8]"
    timestamp: float


class NativePacketRenderer:
    """Feed only requested encoded packets; CUDA is required unless reference mode is explicit."""

    def __init__(
        self,
        ffmpeg: str,
        stream: H264Stream,
        error_output: int,
        *,
        fps: int = 5,
        cuda: bool = True,
    ) -> None:
        self.source = stream
        self.input_finished = False
        self.stopped = False
        self.process = subprocess.Popen(
            native_decoder_command(ffmpeg, fps, cuda=cuda),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=error_output,
            bufsize=0,
            start_new_session=True,
        )
        assert self.process.stdin is not None
        try:
            if not isinstance(self.process.stdin, io.RawIOBase):
                raise RuntimeError("Native decoder requires an unbuffered input pipe")
            self.process.stdin = io.BufferedWriter(
                self.process.stdin, buffer_size=65536
            )
            self.muxer = EncodedNutMuxer(self.process.stdin, stream)
        except (ValueError, OSError, RuntimeError):
            self.process.terminate()
            self.process.wait(timeout=10)
            raise

    def write(self, packet: EncodedPacket) -> None:
        """Prime from encoded pre-roll or forward a current native access unit."""
        if self.input_finished or self.stopped:
            raise RuntimeError("Native decoder input is closed")
        self.muxer.write(packet)

    def frames(self) -> Iterator[NativeDecodedFrame]:
        """Read already-rendered raw pixels; fail explicitly on geometry or timestamp drift."""
        import av
        import numpy as np

        assert self.process.stdout is not None
        with av.open(
            self.process.stdout,
            mode="r",
            format="nut",
            options={"analyzeduration": "1", "fpsprobesize": "0", "probesize": "32"},
        ) as decoded:
            if len(decoded.streams.video) != 1:
                raise ValueError("Native renderer output requires one video stream")
            video = decoded.streams.video[0]
            if video.codec_context.name != "rawvideo":
                raise ValueError("Native renderer output is not raw pixels")
            video.codec_context.thread_count = 1
            for frame in decoded.decode(video):
                if not isinstance(frame, av.VideoFrame):
                    raise ValueError("Native renderer returned a non-video frame")
                if (frame.width, frame.height) != (
                    self.source.width,
                    self.source.height,
                ):
                    raise ValueError("Native decoder changed the source geometry")
                if frame.pts is None or frame.time_base is None:
                    raise ValueError("Native decoder output lost presentation time")
                pixels = frame.to_ndarray(format="yuv420p")
                if pixels.dtype != np.uint8 or pixels.shape != (
                    self.source.height * 3 // 2,
                    self.source.width,
                ):
                    raise ValueError("Native decoder returned invalid I420 pixels")
                yield NativeDecodedFrame(
                    np.asarray(pixels, dtype=np.uint8),
                    float(frame.pts * frame.time_base),
                )

    def finish_input(self) -> None:
        """Signal source EOF while allowing the consumer to drain rendered frames."""
        if not self.input_finished:
            self.muxer.close()
            if self.process.stdin is not None:
                self.process.stdin.close()
                self.process.stdin = None
            self.input_finished = True

    def wait(self, timeout: float = 10) -> None:
        """Require a clean renderer exit after normal input/consumer completion."""
        if self.process.wait(timeout=timeout):
            raise RuntimeError("Native decoder exited unsuccessfully")

    def stop(self) -> None:
        """Abort an owned renderer without stranding a blocked packet writer."""
        import av

        if self.stopped:
            return
        self.stopped = True
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        try:
            self.muxer.close()
        except (BrokenPipeError, OSError, av.error.FFmpegError):
            logger.debug("Native encoded input closed during renderer shutdown")
        if self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except BrokenPipeError:
                logger.debug("Native decoder pipe closed during renderer shutdown")
            self.process.stdin = None
        if self.process.stdout is not None:
            self.process.stdout.close()

"""Native decoder selection and real packet-to-pixel handoff without a spatial resize."""

import io
import tempfile
import threading
import unittest

import av
import numpy as np

from frigate.config import FrigateConfig
from frigate.motion.compressed_buffer import EncodedPacket, H264Stream
from frigate.motion.compressed_mux import avc_parameter_sets
from frigate.motion.compressed_renderer import (
    NativePacketRenderer,
    native_decoder_command,
)


class TestNativeRenderer(unittest.TestCase):
    def test_sparse_idr_probe_arrives_without_a_following_frame(self):
        if "libx264" not in av.codecs_available:
            self.skipTest("Fixture encoding requires existing libx264")
        fixture = io.BytesIO()
        with av.open(fixture, mode="w", format="mp4") as container:
            stream = container.add_stream("libx264", rate=10)
            stream.width = stream.height = 128
            stream.pix_fmt = "yuv420p"
            stream.options = {"preset": "ultrafast", "g": "10"}
            for packet in stream.encode(
                av.VideoFrame.from_ndarray(
                    np.zeros((128, 128, 3), dtype=np.uint8), format="rgb24"
                )
            ):
                container.mux(packet)
            for packet in stream.encode():
                container.mux(packet)
        with av.open(io.BytesIO(fixture.getvalue())) as reference:
            video = reference.streams.video[0]
            extra = video.codec_context.extradata
            _, length = avc_parameter_sets(extra)
            source = H264Stream(128, 128, extra, length, video.time_base)
            first = next(packet for packet in reference.demux(video) if packet.size)
            coded = EncodedPacket(
                source, bytes(first), first.dts, first.pts, first.duration
            )
        ffmpeg = FrigateConfig(mqtt={"enabled": False}, cameras={}).ffmpeg.ffmpeg_path
        arrived = threading.Event()
        errors = []
        with tempfile.TemporaryFile() as stderr:
            renderer = NativePacketRenderer(ffmpeg, source, stderr.fileno(), cuda=False)

            def collect():
                try:
                    for _ in renderer.frames():
                        arrived.set()
                except (ValueError, OSError, EOFError, av.error.FFmpegError) as error:
                    errors.append(error)

            consumer = threading.Thread(target=collect, daemon=True)
            consumer.start()
            try:
                renderer.write(coded)
                self.assertTrue(
                    arrived.wait(timeout=3), "IDR probe waited for a later frame or EOF"
                )
                self.assertFalse(errors)
            finally:
                renderer.finish_input()
                consumer.join(timeout=5)
                renderer.stop()

    def test_cuda_is_default_and_no_resize_or_video_encoder_is_requested(self):
        command = native_decoder_command("ffmpeg")
        self.assertEqual(command[command.index("-hwaccel") + 1], "cuda")
        self.assertEqual(command[command.index("-c:v") + 1], "rawvideo")
        self.assertNotIn("-s", command)
        self.assertNotIn("1080", " ".join(command))
        filters = command[command.index("-vf") + 1]
        self.assertNotIn("scale", filters)
        self.assertIn("select=", filters)
        self.assertIn("floor(t*5", filters)
        self.assertIn("hwdownload", filters)

    def test_software_reference_requires_explicit_option(self):
        command = native_decoder_command("ffmpeg", cuda=False)
        self.assertNotIn("-hwaccel", command)
        self.assertNotIn("hwdownload", " ".join(command))

    def test_invalid_capture_rate_is_not_accepted(self):
        with self.assertRaises(ValueError):
            native_decoder_command("ffmpeg", fps=0)

    def test_actual_decoder_keeps_geometry_and_cadence_with_off_grid_keyframes(self):
        if "libx264" not in av.codecs_available:
            self.skipTest("Fixture encoding requires existing libx264")
        fixture = io.BytesIO()
        with av.open(fixture, mode="w", format="mp4") as container:
            stream = container.add_stream("libx264", rate=10)
            stream.width = stream.height = 128
            stream.pix_fmt = "yuv420p"
            stream.options = {"preset": "ultrafast", "g": "3"}
            for index in range(20):
                pixels = np.zeros((128, 128, 3), dtype=np.uint8)
                pixels[20:50, index : index + 20] = 200
                for packet in stream.encode(
                    av.VideoFrame.from_ndarray(pixels, format="rgb24")
                ):
                    container.mux(packet)
            for packet in stream.encode():
                container.mux(packet)
        with av.open(io.BytesIO(fixture.getvalue())) as reference:
            video = reference.streams.video[0]
            extra = video.codec_context.extradata
            _, length = avc_parameter_sets(extra)
            source = H264Stream(128, 128, extra, length, video.time_base)
            packets = [
                EncodedPacket(source, bytes(packet), packet.dts, packet.pts)
                for packet in reference.demux(video)
                if packet.size
            ]
        ffmpeg = FrigateConfig(mqtt={"enabled": False}, cameras={}).ffmpeg.ffmpeg_path
        frames, errors = [], []
        with tempfile.TemporaryFile() as stderr:
            renderer = NativePacketRenderer(ffmpeg, source, stderr.fileno(), cuda=False)

            def collect():
                try:
                    frames.extend(renderer.frames())
                except (ValueError, OSError, EOFError, av.error.FFmpegError) as error:
                    errors.append(error)

            consumer = threading.Thread(target=collect, daemon=True)
            deadline = threading.Timer(20, renderer.stop)
            deadline.daemon = True
            deadline.start()
            consumer.start()
            try:
                for packet in packets:
                    renderer.write(packet)
                renderer.finish_input()
                consumer.join(timeout=20)
                stderr.seek(0)
                diagnostic = stderr.read(4000).decode(errors="replace")
                self.assertFalse(consumer.is_alive(), diagnostic)
                self.assertFalse(errors, diagnostic)
                renderer.wait(timeout=5)
                self.assertEqual(len(frames), 10, diagnostic)
                self.assertTrue(all(frame.yuv.shape == (192, 128) for frame in frames))
                self.assertEqual(
                    [round(frame.timestamp, 3) for frame in frames],
                    [i / 5 for i in range(10)],
                )
                self.assertGreater(frames[0].yuv[:128].std(), 1)
            finally:
                deadline.cancel()
                renderer.stop()


if __name__ == "__main__":
    unittest.main()

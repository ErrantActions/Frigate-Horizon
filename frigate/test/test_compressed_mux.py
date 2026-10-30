"""Encoded-only transport keeps native H264 slices, timestamps and codec-context isolation."""

import io
import unittest
from fractions import Fraction

import av
import numpy as np

from frigate.motion.compressed_buffer import EncodedPacket, H264Stream
from frigate.motion.compressed_mux import (
    START_CODE,
    EncodedNutMuxer,
    annexb_packet,
    avc_packet_nals,
    avc_parameter_sets,
)

SPS = b"\x67\x64\x00\x1f"
PPS = b"\x68\xee\x11"
AVCC = (
    b"\x01\x64\x00\x1f\xff\xe1"
    + len(SPS).to_bytes(2, "big")
    + SPS
    + b"\x01"
    + len(PPS).to_bytes(2, "big")
    + PPS
)
IDR_NAL = b"\x65\x88"
P_NAL = b"\x41\x99"


def framed(nal: bytes):
    return len(nal).to_bytes(4, "big") + nal


class TestCompressedMux(unittest.TestCase):
    def test_parameter_sets_are_preserved(self):
        parameters, length = avc_parameter_sets(AVCC)
        self.assertEqual(parameters, (SPS, PPS))
        self.assertEqual(length, 4)
        self.assertEqual(
            avc_parameter_sets(AVCC + b"\xfd\xf8\xf8\x00"),
            ((SPS, PPS), 4),
        )

    def test_bad_codec_records_and_extensions_are_explicit_errors(self):
        for extra in (
            b"",
            AVCC[:7],
            AVCC[:-1],
            AVCC + b"\xfd\xf8",
            AVCC + b"\xfd\xfa\xf8\x00",
            AVCC + b"\xfd\xf8\xf8\x01",
        ):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                avc_parameter_sets(extra)

    def test_reframing_preserves_coded_slice_bytes_and_native_geometry(self):
        source = H264Stream(3840, 2160, AVCC, 4, Fraction(1, 90000))
        packet = EncodedPacket(source, framed(IDR_NAL), 90000, 90000)
        before = packet.data
        data = annexb_packet(packet, include_headers=True)
        self.assertEqual(
            data, START_CODE + SPS + START_CODE + PPS + START_CODE + IDR_NAL
        )
        self.assertEqual(packet.data, before)
        self.assertEqual((packet.stream.width, packet.stream.height), (3840, 2160))

    def test_bad_packet_length_is_not_a_valid_remux(self):
        with self.assertRaises(ValueError):
            avc_packet_nals(b"\x00\x00\x00\x20\x65", 4)
        with self.assertRaises(ValueError):
            avc_packet_nals(b"", 4)

    def test_no_codec_context_and_bad_start_do_not_create_video(self):
        target = io.BytesIO()
        source = H264Stream(3840, 2160, AVCC, 4)
        muxer = EncodedNutMuxer(target, source)
        self.assertIsNone(muxer.stream.codec_context)
        with self.assertRaises(ValueError):
            muxer.write(EncodedPacket(source, framed(P_NAL), 0, 0))
        muxer.close()
        with self.assertRaises(RuntimeError):
            muxer.write(EncodedPacket(source, framed(IDR_NAL), 0, 0))
        self.assertEqual(target.getvalue(), b"")

    def test_annexb_start_requires_parameter_sets(self):
        source = H264Stream(3840, 2160, b"")
        muxer = EncodedNutMuxer(io.BytesIO(), source)
        with self.assertRaises(ValueError):
            muxer.write(EncodedPacket(source, START_CODE + IDR_NAL, 0, 0))
        muxer.close()

    def test_real_packet_remux_never_constructs_a_codec_context(self):
        if "libx264" not in av.codecs_available:
            self.skipTest("Fixture encoding requires existing libx264")
        fixture = io.BytesIO()
        with av.open(fixture, mode="w", format="mp4") as container:
            stream = container.add_stream("libx264", rate=10)
            stream.width = stream.height = 128
            stream.pix_fmt = "yuv420p"
            stream.options = {"preset": "ultrafast", "g": "10", "crf": "23"}
            for index in range(20):
                pixels = np.zeros((128, 128, 3), dtype=np.uint8)
                pixels[20:50, index : index + 20] = 210
                for packet in stream.encode(
                    av.VideoFrame.from_ndarray(pixels, format="rgb24")
                ):
                    container.mux(packet)
            for packet in stream.encode():
                container.mux(packet)
        target = io.BytesIO()
        expected = []
        times = []
        with av.open(io.BytesIO(fixture.getvalue())) as reference:
            video = reference.streams.video[0]
            extra = video.codec_context.extradata
            _, length = avc_parameter_sets(extra)
            source = H264Stream(128, 128, extra, length, video.time_base)
            muxer = EncodedNutMuxer(target, source)
            for packet in reference.demux(video):
                if not packet.size:
                    continue
                coded = EncodedPacket(source, bytes(packet), packet.dts, packet.pts)
                expected.extend(
                    nal
                    for nal in avc_packet_nals(coded.data, length)
                    if nal[0] & 31 in (1, 5)
                )
                times.append(float(packet.pts * packet.time_base))
                muxer.write(coded)
                self.assertIsNone(muxer.stream.codec_context)
            muxer.close()
        observed = []
        observed_times = []
        with av.open(io.BytesIO(target.getvalue()), format="nut") as verification:
            video = verification.streams.video[0]
            self.assertEqual(video.codec_context.width, 128)
            self.assertEqual(video.codec_context.height, 128)
            for packet in verification.demux(video):
                if not packet.size:
                    continue
                nals = (
                    bytes(packet)
                    .replace(START_CODE, b"\x00\x00\x01")
                    .split(b"\x00\x00\x01")
                )
                observed.extend(nal for nal in nals if nal and nal[0] & 31 in (1, 5))
                observed_times.append(float(packet.pts * packet.time_base))
            tolerance = float(video.time_base)
        self.assertEqual(observed, expected)
        self.assertEqual(len(observed_times), len(times))
        self.assertTrue(
            all(
                abs(left - right) <= tolerance
                for left, right in zip(times, observed_times)
            )
        )


if __name__ == "__main__":
    unittest.main()

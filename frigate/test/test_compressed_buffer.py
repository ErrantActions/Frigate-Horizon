"""Native encoded pre-roll preserves bytes, IDR references, limits and timestamp safety."""

import unittest
from fractions import Fraction

from frigate.motion.compressed_buffer import (
    EncodedGopBuffer,
    EncodedPacket,
    H264Stream,
    PrerollUnavailable,
    h264_is_idr,
)

IDR = b"\x00\x00\x00\x01\x65\x88"
P_FRAME = b"\x00\x00\x01\x41\x99"
STREAM = H264Stream(3840, 2160, b"native-sps-pps")


def packet(time: float, data: bytes = P_FRAME, pts: float | None = None):
    return EncodedPacket(STREAM, data, time, time if pts is None else pts)


class TestEncodedPreroll(unittest.TestCase):
    def test_original_integer_ticks_and_stream_timebase_are_preserved(self):
        stream = H264Stream(3840, 2160, STREAM.extradata, time_base=Fraction(1, 90000))
        buffer = EncodedGopBuffer()
        originals = [
            EncodedPacket(stream, IDR, 9000000, 9000000),
            EncodedPacket(stream, P_FRAME, 9090000, 9090000),
            EncodedPacket(stream, P_FRAME, 9180000, 9180000),
        ]
        for item in originals:
            buffer.append(item)
        window = buffer.snapshot(2)
        self.assertEqual(window.packets, tuple(originals))
        self.assertEqual(window.packets[0].dts, 9000000)
        self.assertEqual(window.lookback_seconds, 2)

    def test_idr_not_intra_or_keyframe_flag_is_required(self):
        self.assertTrue(h264_is_idr(IDR))
        self.assertFalse(h264_is_idr(P_FRAME))
        self.assertFalse(h264_is_idr(b"\x00\x00\x01\x61\x88"))
        self.assertTrue(h264_is_idr(b"\x00\x00\x01\x67\x99" + IDR))

    def test_avc_length_prefixes_and_bad_packets(self):
        self.assertTrue(h264_is_idr(b"\x00\x00\x00\x02\x65\x88", 4))
        for data, length in (
            (b"", None),
            (b"\x00\x00\x01", None),
            (b"\x00\x00\x01\xe5", None),
            (b"invalid", None),
            (b"\x00\x00\x00\x20\x65", 4),
            (b"\x00", 4),
            (b"\x00\x00\x00\x00", 4),
        ):
            with self.subTest(data=data, length=length), self.assertRaises(ValueError):
                h264_is_idr(data, length)

    def test_native_geometry_and_bytes_are_preserved(self):
        buffer = EncodedGopBuffer()
        originals = [packet(0, IDR), packet(1), packet(2), packet(3, IDR), packet(4)]
        for item in originals:
            buffer.append(item)
        window = buffer.snapshot(2)
        self.assertEqual(window.stream, STREAM)
        self.assertEqual((window.stream.width, window.stream.height), (3840, 2160))
        self.assertEqual(window.packets, tuple(originals))
        self.assertEqual(
            b"".join(item.data for item in window.packets),
            b"".join(item.data for item in originals),
        )
        self.assertEqual(
            window.bytes_used,
            len(STREAM.extradata) + sum(len(item.data) for item in originals),
        )

    def test_reordered_presentation_timestamps_are_not_decode_order_resets(self):
        buffer = EncodedGopBuffer()
        values = [
            packet(-0.08, IDR, 0),
            packet(-0.04, pts=0.12),
            packet(0, pts=0.04),
            packet(0.04, pts=0.08),
        ]
        for item in values:
            buffer.append(item)
        self.assertEqual(buffer.snapshot(0.1).packets, tuple(values))

    def test_initial_partial_gop_and_short_history_are_unavailable(self):
        buffer = EncodedGopBuffer()
        update = buffer.append(packet(0))
        self.assertEqual(update.reason, "awaiting_idr")
        self.assertFalse(update.has_random_access)
        with self.assertRaises(PrerollUnavailable):
            buffer.snapshot()
        buffer.append(packet(1, IDR))
        with self.assertRaises(PrerollUnavailable):
            buffer.snapshot()

    def test_decode_timestamp_reset_invalidates_reference_chain(self):
        buffer = EncodedGopBuffer()
        buffer.append(packet(10, IDR))
        update = buffer.append(packet(0))
        self.assertFalse(update.has_random_access)
        self.assertEqual(update.reason, "decode_timestamp_reset")
        with self.assertRaises(PrerollUnavailable):
            buffer.snapshot()

    def test_geometry_or_codec_change_requires_new_idr(self):
        buffer = EncodedGopBuffer()
        buffer.append(packet(0, IDR))
        changed = H264Stream(2560, 1440, b"changed-sps-pps")
        update = buffer.append(EncodedPacket(changed, P_FRAME, 1, 1))
        self.assertFalse(update.has_random_access)
        self.assertEqual(update.reason, "stream_configuration_changed")
        with self.assertRaises(PrerollUnavailable):
            buffer.snapshot()
        buffer.append(EncodedPacket(changed, IDR, 2, 2))
        self.assertEqual(buffer.snapshot(0).stream, changed)

    def test_evicts_whole_gops_under_byte_limit(self):
        limit = len(STREAM.extradata) + len(IDR) + len(P_FRAME)
        buffer = EncodedGopBuffer(max_bytes=limit)
        for item in (packet(0, IDR), packet(1), packet(2, IDR), packet(3)):
            buffer.append(item)
        window = buffer.snapshot(1)
        self.assertEqual([item.dts for item in window.packets], [2, 3])
        self.assertLessEqual(buffer.bytes_used, limit)

    def test_long_idr_interval_invalidates_instead_of_growing_unbounded(self):
        buffer = EncodedGopBuffer(max_seconds=2)
        buffer.append(packet(0, IDR))
        update = buffer.append(packet(3))
        self.assertFalse(update.has_random_access)
        self.assertEqual(update.reason, "idr_interval_exceeds_buffer_limit")
        self.assertEqual(buffer.bytes_used, 0)

    def test_oversized_or_invalid_packet_does_not_leave_stale_preroll(self):
        buffer = EncodedGopBuffer(max_bytes=64)
        buffer.append(packet(0, IDR))
        update = buffer.append(packet(1, IDR + b"x" * 100))
        self.assertFalse(update.has_random_access)
        self.assertEqual(update.reason, "oversized_encoded_packet")
        buffer.append(packet(2, IDR))
        with self.assertRaises(ValueError):
            buffer.append(packet(3, b"bad"))
        with self.assertRaises(PrerollUnavailable):
            buffer.snapshot()
        buffer.append(packet(4, IDR))
        with self.assertRaises(ValueError):
            buffer.append(packet(float("nan")))
        self.assertEqual(buffer.bytes_used, 0)

    def test_snapshot_chooses_latest_idr_that_covers_required_lookback(self):
        buffer = EncodedGopBuffer()
        for item in (
            packet(0, IDR),
            packet(1),
            packet(2, IDR),
            packet(3),
            packet(4, IDR),
            packet(5),
        ):
            buffer.append(item)
        window = buffer.snapshot(2)
        self.assertEqual(window.packets[0].dts, 2)
        self.assertGreaterEqual(window.lookback_seconds, 2)

    def test_time_eviction_keeps_decoder_order_and_never_returns_short_preroll(self):
        buffer = EncodedGopBuffer(max_seconds=3)
        for item in (
            packet(0, IDR),
            packet(1),
            packet(2, IDR),
            packet(3),
            packet(4, IDR),
            packet(5),
            packet(6),
        ):
            buffer.append(item)
        self.assertEqual(buffer.snapshot(2).packets[0].dts, 4)
        with self.assertRaises(PrerollUnavailable):
            buffer.snapshot(3)


if __name__ == "__main__":
    unittest.main()

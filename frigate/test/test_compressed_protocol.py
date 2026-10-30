"""Entropy transport rejects reconstruction, missing evidence, malformed clocks and broken packets."""

import io
import json
import unittest

from frigate.motion.compressed_protocol import (
    GRID_CELLS,
    HEADER,
    MAX_RECORD_BYTES,
    PACKET,
    CompressedEvidence,
    ExtractorSummary,
    PacketRecord,
    StreamHeader,
    read_extractor_events,
)


def record(kind: bytes, value: dict | bytes) -> bytes:
    payload = value if isinstance(value, bytes) else json.dumps(value).encode()
    return HEADER.pack(b"FCM1", kind, len(payload)) + payload


def config():
    return {
        "codec": "h264",
        "mode": "entropy_only",
        "width": 3840,
        "height": 2160,
        "time_base": [1, 90000],
        "extradata_hex": "",
    }


def evidence():
    return {
        "type": "frame",
        "index": 0,
        "pts": 90000,
        "relative_seconds": 0,
        "width": 3840,
        "height": 2160,
        "picture_type": "P",
        "vector_count": 32400,
        "moving_vectors": 0,
        "coverage": 1.0,
        "vectors_sha256": None,
        "uncertainty": None,
        "time_base": [1, 90000],
        "profile_idc": 100,
        "cabac": 1,
        "activity": [0.0] * GRID_CELLS,
        "coded_activity": [0.0] * GRID_CELLS,
    }


def summary():
    return {
        "type": "summary",
        "mode": "entropy_only",
        "frames": 1,
        "vector_frames": 1,
        "width": 3840,
        "height": 2160,
        "profile_idc": 100,
        "cabac": 1,
        "parsed_macroblocks": 32400,
        "reconstructed_macroblocks": 0,
        "deblocked_rows": 0,
        "pixel_reconstruction_performed": False,
        "frame_buffers_allocated": True,
        "vectors_hashed": False,
        "cpu_seconds": 0.01,
        "wall_seconds": 0.02,
    }


class Fragmented(io.BytesIO):
    def read(self, size=-1):
        return super().read(min(size, 7) if size >= 0 else 7)


class TestCompressedProtocol(unittest.TestCase):
    def test_complete_stream_keeps_native_bytes_and_original_ticks(self):
        payload = PACKET.pack(90000, 90000, 3600, 1, 0) + b"\x00\x00\x01\x65\x88"
        blob = (
            record(b"C", config())
            + record(b"P", payload)
            + record(b"M", evidence())
            + record(b"S", summary())
        )
        events = list(read_extractor_events(Fragmented(blob)))
        self.assertIsInstance(events[0], StreamHeader)
        self.assertIsInstance(events[1], PacketRecord)
        self.assertIsInstance(events[2], CompressedEvidence)
        self.assertIsInstance(events[3], ExtractorSummary)
        stream = events[0].native_stream(3840, 2160)
        packet = events[1].for_stream(stream)
        self.assertEqual(packet.data, payload[PACKET.size :])
        self.assertEqual(packet.pts, 90000)
        self.assertEqual(packet.duration, 3600)
        self.assertEqual(events[2].timestamp, 1)

    def test_full_decode_mode_is_not_an_entropy_success(self):
        header = config()
        header["mode"] = "reference"
        with self.assertRaises(ValueError):
            list(read_extractor_events(io.BytesIO(record(b"C", header))))
        for field, value in (
            ("reconstructed_macroblocks", 1),
            ("pixel_reconstruction_performed", True),
            ("deblocked_rows", 1),
        ):
            final = summary()
            final[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                list(
                    read_extractor_events(
                        io.BytesIO(
                            record(b"C", config())
                            + record(b"M", evidence())
                            + record(b"S", final)
                        )
                    )
                )

    def test_truncated_or_oversized_records_are_not_quiet_motion(self):
        for blob in (
            b"",
            b"FC",
            HEADER.pack(b"FCM1", b"P", 100) + b"short",
        ):
            with self.subTest(blob=blob), self.assertRaises(EOFError):
                list(read_extractor_events(io.BytesIO(blob)))
        for blob in (
            HEADER.pack(b"BAD!", b"C", 1) + b"x",
            HEADER.pack(b"FCM1", b"C", MAX_RECORD_BYTES + 1),
            HEADER.pack(b"FCM1", b"X", 1) + b"x",
        ):
            with self.subTest(blob=blob), self.assertRaises(ValueError):
                list(read_extractor_events(io.BytesIO(blob)))

    def test_missing_config_and_broken_packet_sequence_are_errors(self):
        payload = PACKET.pack(0, 0, 3600, 1, 1) + b"\x00\x00\x01\x65\x88"
        with self.assertRaises(ValueError):
            list(read_extractor_events(io.BytesIO(record(b"P", payload))))
        with self.assertRaises(ValueError):
            list(
                read_extractor_events(
                    io.BytesIO(record(b"C", config()) + record(b"P", payload))
                )
            )

    def test_missing_timestamp_and_corruption_are_explicit_errors(self):
        for pts, flags in ((-(1 << 63), 0), (0, 2)):
            payload = PACKET.pack(pts, 0, 3600, flags, 0) + b"\x00\x00\x01\x65\x88"
            with self.subTest(pts=pts, flags=flags), self.assertRaises(ValueError):
                list(
                    read_extractor_events(
                        io.BytesIO(record(b"C", config()) + record(b"P", payload))
                    )
                )

    def test_bad_evidence_shape_and_nonfinite_values_are_rejected(self):
        for field, value in (
            ("activity", [0.0]),
            ("coded_activity", [float("nan")] * GRID_CELLS),
            ("coverage", -1),
            ("pts", -(1 << 63)),
            ("time_base", [0, 90000]),
        ):
            observed = evidence()
            observed[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                list(
                    read_extractor_events(
                        io.BytesIO(record(b"C", config()) + record(b"M", observed))
                    )
                )

    def test_summary_cannot_cover_missing_or_additional_data(self):
        with self.assertRaises(ValueError):
            list(
                read_extractor_events(
                    io.BytesIO(record(b"C", config()) + record(b"S", summary()))
                )
            )
        complete = (
            record(b"C", config()) + record(b"M", evidence()) + record(b"S", summary())
        )
        with self.assertRaises(ValueError):
            list(read_extractor_events(io.BytesIO(complete + record(b"M", evidence()))))

    def test_source_clock_and_geometry_cannot_change_midstream(self):
        for field, value in (("time_base", [1, 1000]), ("width", 1920)):
            observed = evidence()
            observed[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                list(
                    read_extractor_events(
                        io.BytesIO(record(b"C", config()) + record(b"M", observed))
                    )
                )


if __name__ == "__main__":
    unittest.main()

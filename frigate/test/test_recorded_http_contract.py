"""Portable immutable HTTP range and request-contract regressions."""

import unittest
from email.utils import formatdate

from pydantic import ValidationError

from frigate.api.defs.request.recorded_http_body import (
    RecordedHTTPCreate,
    RecordedHTTPResolve,
)
from frigate.api.defs.response.recorded_http_response import RecordedHTTPDiscovery
from frigate.record.http_range import UnsatisfiableRange, media_range
from frigate.record.http_timeline import (
    PacketTimelineError,
    effective_window_start,
    first_source_frame,
    measure_packet_spans,
    measure_packet_stream,
)


class TestRecordedHTTPContract(unittest.TestCase):
    def test_forward_start_skips_measured_gap_without_extending_window(self):
        spans = [
            {"source_start": 100, "source_end": 101.999},
            {"source_start": 105.005, "source_end": 108},
        ]
        self.assertIsNone(effective_window_start(spans, 102.0001, 100, 110))
        self.assertEqual(
            effective_window_start(spans, 102.0001, 100, 110, forward=True), 105.005
        )
        self.assertIsNone(
            effective_window_start(spans, 102.0001, 100, 104, forward=True)
        )
        self.assertIsNone(effective_window_start(spans, 108, 100, 110, forward=True))

    def test_real_frame_target_does_not_infer_fractional_catalog_tail(self):
        packets = [
            {"pts_time": 0, "duration_time": 0.04},
            {"pts_time": 1.96, "duration_time": 0.04},
        ]
        self.assertIsNone(first_source_frame(packets, 100, 0, 101.9991, 102.003))
        self.assertEqual(first_source_frame(packets, 105.005, 0, 102.003, 108), 105.005)
        self.assertIsNone(first_source_frame(packets, 105.005, 0, 102.003, 105.005))
        packets = [{"pts_time": 5.92, "duration_time": 0.04}]
        wall_start = 1793555994.103237
        self.assertIsNone(
            first_source_frame(packets, wall_start, 0, 1793555999.999237, 1793556000)
        )
        self.assertEqual(
            first_source_frame(packets, wall_start, 0, 1793556000, 1793556006),
            wall_start + 5.92,
        )

    def test_resolver_bounds_are_separate_from_thirty_second_creation(self):
        body = RecordedHTTPResolve(after=100, before=3700, start_timestamp=101)
        self.assertEqual(body.before, 3700)
        for update in (
            {"before": 3700.001},
            {"start_timestamp": 3700},
            {"after": float("nan")},
            {"before": 100},
        ):
            with self.assertRaises(ValidationError):
                RecordedHTTPResolve(**{**body.model_dump(), **update})
        with self.assertRaises(ValidationError):
            RecordedHTTPCreate(after=100, before=3700, start_timestamp=101)

    def test_audio_and_video_have_independent_measured_presentation_endpoints(self):
        video = [{"data_hash": "video", "pts_time": 0.021, "duration_time": 4.0}]
        audio = [{"data_hash": "audio", "pts_time": 0, "duration_time": 4.042}]
        self.assertEqual(measure_packet_stream([video], video), (0.021, 4.021))
        self.assertEqual(measure_packet_stream([audio], audio), (0, 4.042))
        with self.assertRaises(PacketTimelineError):
            measure_packet_stream([audio], [])

    def test_muxer_extended_last_sample_is_measured_without_extending_catalog(self):
        spans = [
            {"source_start": 100, "source_end": 101, "media_start": 0, "media_end": 1},
            {"source_start": 110, "source_end": 111, "media_start": 1, "media_end": 2},
        ]
        source = [
            [{"data_hash": "a"}, {"data_hash": "b"}],
            [{"data_hash": "c"}, {"data_hash": "d"}],
        ]
        final = [
            {"data_hash": "a", "pts_time": 0, "duration_time": 0.5},
            {"data_hash": "b", "pts_time": 0.5, "duration_time": 0.522},
            {"data_hash": "c", "pts_time": 1.022, "duration_time": 0.5},
            {"data_hash": "d", "pts_time": 1.522, "duration_time": 0.5},
        ]
        mapped = measure_packet_spans(spans, source, final)
        self.assertAlmostEqual(mapped[0]["media_end"], 1.022)
        self.assertEqual(mapped[0]["source_end"], 101)
        self.assertAlmostEqual(mapped[1]["media_start"], 1.022)
        self.assertAlmostEqual(mapped[1]["media_end"], 2.022)
        self.assertEqual(mapped[1]["source_start"], 110)
        for corrupt in (
            final[:-1],
            [{**packet, "data_hash": "wrong"} for packet in final],
            [{**packet, "duration_time": 0} for packet in final],
            [{**packet, "pts_time": float("nan")} for packet in final],
        ):
            with self.assertRaises(PacketTimelineError):
                measure_packet_spans(spans, source, corrupt)

    def test_window_matches_the_shared_thirty_second_planner(self):
        body = RecordedHTTPCreate(after=100, before=130, start_timestamp=105)
        self.assertEqual(body.quality, "auto")
        self.assertEqual(
            RecordedHTTPDiscovery(enabled=True, available=True).max_window_seconds, 30
        )
        for changes in (
            {"before": 131},
            {"before": 100.01},
            {"start_timestamp": 130},
            {"after": float("nan")},
            {"before": float("inf")},
            {"quality": "hls"},
            {"path": "private.mp4"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                RecordedHTTPCreate(**{**body.model_dump(), **changes})

    def test_full_explicit_open_and_suffix_ranges(self):
        for value, expected in (
            (None, (200, 0, 99)),
            ("bytes=10-19", (206, 10, 19)),
            ("bytes=10-", (206, 10, 99)),
            ("bytes=-10", (206, 90, 99)),
            ("bytes=-1000", (206, 0, 99)),
            ("bytes=0-1000", (206, 0, 99)),
            ("bytes=99-99", (206, 99, 99)),
        ):
            with self.subTest(value=value):
                self.assertEqual(media_range(100, value, None, '"abc"', 1000), expected)

    def test_invalid_and_multiple_ranges_are_explicit_416(self):
        for value in (
            "bytes=100-",
            "bytes=9-1",
            "bytes=-0",
            "bytes=-",
            "bytes=0-1,4-5",
            "items=0-1",
            "bytes=a-2",
            "bytes=+1-3",
            "bytes=" + "9" * 5000 + "-",
        ):
            with self.subTest(value=value), self.assertRaises(UnsatisfiableRange):
                media_range(100, value, None, '"abc"', 1000)

    def test_if_range_strong_etag_and_date(self):
        for validator in (
            '"abc"',
            formatdate(1000, usegmt=True),
            formatdate(1001, usegmt=True),
        ):
            self.assertEqual(
                media_range(100, "bytes=1-2", validator, '"abc"', 1000), (206, 1, 2)
            )
        for validator in (
            '"different"',
            'W/"abc"',
            "invalid",
            formatdate(999, usegmt=True),
        ):
            self.assertEqual(
                media_range(100, "bytes=invalid", validator, '"abc"', 1000),
                (200, 0, 99),
            )

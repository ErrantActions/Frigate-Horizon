"""Attribute immutable remuxed video packets to their measured source spans."""

import math


class PacketTimelineError(ValueError):
    """A predefined packet-attribution failure safe for the public status."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def effective_window_start(spans, requested, after, before, *, forward=False):
    """Resolve only measured coverage inside the original half-open MP4 window."""
    for span in spans:
        start = max(after, span["source_start"])
        end = min(before, span["source_end"])
        if start <= requested < end:
            return requested
        if forward and requested < start < end:
            return start
    return None


def first_source_frame(packets, wall_start, stream_start, after, before):
    """Return a real video frame at or after a target, never an inferred tail."""
    candidates = []
    for packet in packets:
        pts = float(packet["pts_time"])
        duration = float(packet["duration_time"])
        if not math.isfinite(pts) or not math.isfinite(duration) or duration <= 0:
            raise PacketTimelineError("recorded_http_invalid_packet_timing")
        timestamp = wall_start + (pts - stream_start)
        if after <= timestamp < before:
            candidates.append(timestamp)
    return min(candidates) if candidates else None


def measure_packet_stream(source_packets, final_packets):
    """Validate complete payload identity and return this stream's own PTS extent."""
    flattened = [packet for group in source_packets for packet in group]
    if not flattened or len(flattened) != len(final_packets):
        raise PacketTimelineError("recorded_http_packet_count_mismatch")
    for source, final in zip(flattened, final_packets, strict=True):
        if not source.get("data_hash") or source["data_hash"] != final.get("data_hash"):
            raise PacketTimelineError("recorded_http_packet_identity_mismatch")
    timings = [
        (float(packet["pts_time"]), float(packet["duration_time"]))
        for packet in final_packets
    ]
    if any(
        not math.isfinite(pts) or not math.isfinite(duration) or duration <= 0
        for pts, duration in timings
    ):
        raise PacketTimelineError("recorded_http_invalid_packet_timing")
    return min(pts for pts, _ in timings), max(
        pts + duration for pts, duration in timings
    )


def measure_packet_spans(spans, source_packets, final_packets):
    """Use preserved packet identity, not nominal container offsets, for boundaries."""
    if len(spans) != len(source_packets):
        raise PacketTimelineError("recorded_http_packet_count_mismatch")
    measure_packet_stream(source_packets, final_packets)
    result = []
    cursor = 0
    for span, original in zip(spans, source_packets, strict=True):
        if not original:
            raise PacketTimelineError("recorded_http_empty_measured_span")
        selected = final_packets[cursor : cursor + len(original)]
        for source, final in zip(original, selected, strict=True):
            if not source.get("data_hash") or source["data_hash"] != final.get(
                "data_hash"
            ):
                raise PacketTimelineError("recorded_http_packet_identity_mismatch")
        timings = [
            (float(packet["pts_time"]), float(packet["duration_time"]))
            for packet in selected
        ]
        if any(
            not math.isfinite(pts) or not math.isfinite(duration) or duration <= 0
            for pts, duration in timings
        ):
            raise PacketTimelineError("recorded_http_invalid_packet_timing")
        first = min(pts for pts, _ in timings)
        last = max(pts + duration for pts, duration in timings)
        if (
            first < 0
            or last <= first
            or (result and first < result[-1]["media_end"] - 0.002)
        ):
            raise PacketTimelineError("recorded_http_overlapping_measured_spans")
        # A muxer may extend the last sample to the next segment's first sample.
        # That hold changes media duration, not the recorded catalog source end.
        result.append({**span, "media_start": first, "media_end": last})
        cursor += len(original)
    return result

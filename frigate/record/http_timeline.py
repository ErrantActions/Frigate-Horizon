"""Attribute immutable remuxed video packets to their measured source spans."""

import math
from fractions import Fraction


class PacketTimelineError(ValueError):
    """A predefined packet-attribution failure safe for the public status."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _packet_clock(packet, time_base, field="pts"):
    if field in packet:
        return int(packet[field]) * Fraction(time_base)
    return Fraction(str(packet[field + "_time"]))


def _microsecond_time(value):
    sign = "-" if value < 0 else ""
    value = abs(value)
    return f"{sign}{value // 1_000_000}.{value % 1_000_000:06d}"


def original_seek_plan(packets, time_base, stream_start, wall_start, start, end):
    """Seek a real keyframe using FFmpeg's microsecond clock, not a rounded index."""
    tick = Fraction(time_base)
    origin = round(Fraction(str(stream_start)) / tick) * tick
    target = origin + Fraction(str(start)) - Fraction(str(wall_start))
    cutoff = origin + Fraction(str(end)) - Fraction(str(wall_start))
    keys = [
        (index, _packet_clock(packet, time_base))
        for index, packet in enumerate(packets)
        if "K" in packet.get("flags", "") and _packet_clock(packet, time_base) <= target
    ]
    if not keys:
        raise PacketTimelineError("recorded_http_original_keyframe_unavailable")
    index, timestamp = max(keys, key=lambda item: item[1])
    # AV_TIME_BASE is an integer microsecond clock. Ceiling is the first
    # representable seek instant not before this exact packet timestamp.
    seek_us = math.ceil(timestamp * 1_000_000)
    end_us = math.floor(cutoff * 1_000_000)
    if end_us <= seek_us:
        raise PacketTimelineError("recorded_http_original_window_unplayable")
    return {
        "index": index,
        "origin": origin,
        "seek": _microsecond_time(seek_us),
        "duration": _microsecond_time(end_us - seek_us),
    }


def original_packet_span(
    source, copied, plan, source_base, copied_base, wall_start, end
):
    """Prove Original packet identity and clock translation before mapping footage."""
    selected = source[plan["index"] : plan["index"] + len(copied)]
    if not copied or len(selected) != len(copied):
        raise PacketTimelineError("recorded_http_original_packet_count_mismatch")
    shifts = []
    timestamps = []
    for original, output in zip(selected, copied, strict=True):
        if not original.get("data_hash") or original["data_hash"] != output.get(
            "data_hash"
        ):
            raise PacketTimelineError("recorded_http_original_packet_identity_mismatch")
        timestamp = _packet_clock(original, source_base)
        duration = (
            int(original["duration"]) * Fraction(source_base)
            if "duration" in original
            else Fraction(str(original["duration_time"]))
        )
        if duration <= 0:
            raise PacketTimelineError("recorded_http_invalid_packet_timing")
        shifts.append(timestamp - _packet_clock(output, copied_base))
        timestamps.append((timestamp, duration))
    if max(shifts) - min(shifts) > max(Fraction(source_base), Fraction(copied_base)):
        raise PacketTimelineError("recorded_http_original_clock_translation_mismatch")
    first = wall_start + float(min(pts for pts, _ in timestamps) - plan["origin"])
    last = wall_start + float(
        max(pts + duration for pts, duration in timestamps) - plan["origin"]
    )
    if first >= end or any(
        wall_start + float(pts - plan["origin"]) >= end for pts, _ in timestamps
    ):
        raise PacketTimelineError("recorded_http_original_packet_outside_window")
    return first, min(end, last)


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

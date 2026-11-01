"""Bounded shared catalog planning and leased source-frame target discovery."""

from pathlib import Path

from frigate.api.defs.request.playback_body import PlaybackCreateBody
from frigate.comms.live_webrtc import LiveWebRTCError
from frigate.const import STREAM_TYPE_MAIN, STREAM_TYPE_SUB
from frigate.models import Recordings
from frigate.record.http_timeline import first_source_frame
from frigate.record.playback import PlaybackError, plan_sources
from frigate.util.recording_coverage import (
    _rows_query,
    build_spans,
    coverage_from_rows,
    plan_clip,
)

MAX_TARGET_ROWS = 64
MAX_TARGET_PROBES = 8


def load_http_sources(camera, window):
    """Defer start validation to actual HTTP media without changing legacy callers."""
    database = Recordings._meta.database
    if database is None:
        raise LiveWebRTCError(503, "recorded_http_catalog_unavailable")
    opened_here = database.is_closed()
    try:
        return plan_sources(
            camera,
            PlaybackCreateBody(
                **window.model_dump(), client_id="recorded-http", generation=0
            ),
            require_start=False,
        )
    except PlaybackError as error:
        raise LiveWebRTCError(error.status, error.code) from None
    finally:
        if opened_here:
            database.close()


def resolve_source_target(camera, body, lease, ffprobe, probe, packet_probe):
    """Use indexed, capped rows and at most eight actual source video probes."""
    database = Recordings._meta.database
    if database is None:
        raise LiveWebRTCError(503, "recorded_http_catalog_unavailable")
    opened_here = database.is_closed()
    try:
        query_start = body.start_timestamp
        main = list(
            _rows_query(camera, query_start, body.before, STREAM_TYPE_MAIN).limit(
                MAX_TARGET_ROWS + 1
            )
        )
        sub = list(
            _rows_query(camera, query_start, body.before, STREAM_TYPE_SUB).limit(
                MAX_TARGET_ROWS + 1
            )
        )
        search_before = body.before
        for rows in (main, sub):
            if len(rows) > MAX_TARGET_ROWS:
                search_before = min(search_before, rows[MAX_TARGET_ROWS - 1].end_time)
        intervals = coverage_from_rows(
            main[:MAX_TARGET_ROWS], sub[:MAX_TARGET_ROWS], query_start, search_before
        )
        spans = build_spans(intervals, None)
        cached = {}
        inspected_through = query_start
        for row, start, end, _ in spans:
            lease.check()
            plan = plan_clip(row, start, end)
            if plan.skipped:
                inspected_through = end
                continue
            if row.path not in cached:
                if len(cached) >= MAX_TARGET_PROBES:
                    return {
                        "effective_start_timestamp": None,
                        "exhausted": False,
                        "searched_before": inspected_through,
                        "reason": "search_limit",
                    }
                path = Path(row.path)
                if not path.is_file() or path.is_symlink():
                    raise LiveWebRTCError(422, "recorded_http_source_not_regular")
                info = probe(lease, ffprobe, path)
                packets = packet_probe(lease, ffprobe, path)
                cached[row.path] = info, packets
            info, packets = cached[row.path]
            offset = (plan.clip_from_ms or 0) / 1000
            effective = first_source_frame(
                packets,
                row.start_time,
                info["start"],
                max(query_start, start, row.start_time + offset),
                min(
                    end, body.before, row.start_time + offset + plan.duration_ms / 1000
                ),
            )
            if effective is not None:
                return {
                    "effective_start_timestamp": effective,
                    "exhausted": False,
                    "searched_before": search_before,
                    "reason": "frame_found",
                }
            inspected_through = end
        limited = search_before < body.before
        return {
            "effective_start_timestamp": None,
            "exhausted": not limited,
            "searched_before": search_before,
            "reason": "search_limit" if limited else "no_frame_in_range",
        }
    finally:
        if opened_here:
            database.close()

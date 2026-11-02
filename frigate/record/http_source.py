"""Indexed, immutable recording files for on-demand authenticated range viewing."""

import math
import stat
import struct
from pathlib import Path

from frigate.comms.live_webrtc import LiveWebRTCError
from frigate.const import STREAM_TYPE_MAIN, STREAM_TYPE_SUB
from frigate.models import Recordings
from frigate.util.recording_coverage import _rows_query


def source_recording(camera, target, before):
    """Find at most one indexed main file, or a historical sub-stream replacement."""
    database = Recordings._meta.database
    if database is None:
        raise LiveWebRTCError(503, "recorded_http_catalog_unavailable")
    opened_here = database.is_closed()
    try:
        candidates = []
        for stream in (STREAM_TYPE_MAIN, STREAM_TYPE_SUB):
            row = _rows_query(camera, target, before, stream).first()
            if row is not None:
                if row.start_time <= target:
                    return row
                candidates.append(row)
        if candidates:
            return min(candidates, key=lambda row: row.start_time)
        raise LiveWebRTCError(404, "recordings_unavailable")
    finally:
        if opened_here:
            database.close()


def indexed_mp4(path: Path) -> bool:
    """Accept a complete normal MP4 index at either end, not an empty fragment index."""
    size = path.stat().st_size
    moov = mdat = False
    with path.open("rb") as source:
        position = 0
        while position < size:
            header = source.read(8)
            if len(header) != 8:
                return False
            length, kind = struct.unpack(">I4s", header)
            minimum = 8
            if length == 1:
                extended = source.read(8)
                if len(extended) != 8:
                    return False
                length = struct.unpack(">Q", extended)[0]
                minimum = 16
            if length < minimum or position + length > size or kind == b"moof":
                return False
            moov |= kind == b"moov"
            mdat |= kind == b"mdat"
            position += length
            source.seek(position)
    return moov and mdat


def file_identity(path: Path) -> tuple[int, int, int, int]:
    """Keep range validators tied to the exact closed file protected by a lease."""
    details = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(details.st_mode) or details.st_size <= 0:
        raise LiveWebRTCError(422, "recorded_http_source_not_regular")
    return details.st_dev, details.st_ino, details.st_size, details.st_mtime_ns


def source_timeline(row, info, after, before):
    """Map original media time directly, clipping only the visible catalog interval."""
    start = max(after, float(row.start_time))
    end = min(before, float(row.end_time), float(row.start_time) + info["duration"])
    if not all(math.isfinite(value) for value in (start, end)) or start >= end:
        raise LiveWebRTCError(422, "playback_start_in_gap")
    return [
        {
            "source_start": start,
            "source_end": end,
            "media_start": info["start"] + start - float(row.start_time),
            "media_end": info["start"] + end - float(row.start_time),
        }
    ]

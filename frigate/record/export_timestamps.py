"""Capture-time subtitle cues shared by export timestamp rendering modes."""

import datetime
import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import Enum
from zoneinfo import ZoneInfo


class ExportTimestampMode(str, Enum):
    """Optional export-only labels, never a mutation of recording media."""

    none = "none"
    subtitle = "subtitle"
    burn_in = "burn_in"
    both = "both"


def timestamp_mux_command(
    ffmpeg: str,
    source: str,
    subtitles: str,
    destination: str,
    mode: ExportTimestampMode,
) -> list[str]:
    """Build a shell-free pass preserving original codecs for subtitle-only output."""
    if mode == ExportTimestampMode.none:
        raise ValueError("Timestamp mux requires an enabled mode")
    # Generated paths are internal, but filter syntax still requires escaping.
    escaped = subtitles.replace("\\", "/")
    for token in ("\\", "'", ":", ",", "[", "]", ";"):
        escaped = escaped.replace(token, "\\" + token)
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-threads",
        "2",
        "-filter_threads",
        "2",
        "-i",
        source,
    ]
    subtitle = mode in (ExportTimestampMode.subtitle, ExportTimestampMode.both)
    if subtitle:
        command.extend(["-i", subtitles])
    command.extend(
        ["-map", "0:v:0", "-map", "0:a?", "-map_metadata", "0", "-map_chapters", "0"]
    )
    if mode in (ExportTimestampMode.burn_in, ExportTimestampMode.both):
        command.extend(
            [
                "-vf",
                f"subtitles=filename='{escaped}':force_style='Alignment=7,FontSize=18,Outline=1,MarginL=12,MarginV=12'",
                "-c:v",
                "libx264",
                "-threads",
                "2",
                "-preset",
                "fast",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
            ]
        )
    else:
        command.extend(["-c:v", "copy"])
    command.extend(["-c:a", "copy"])
    if subtitle:
        command.extend(
            [
                "-map",
                "1:s:0",
                "-c:s",
                "mov_text",
                "-metadata:s:s:0",
                "title=Frigate capture timeline (inferred)",
                "-metadata:s:s:0",
                "language=und",
                "-disposition:s:0",
                "0",
            ]
        )
    command.extend(["-movflags", "+faststart", destination])
    return command


@dataclass(frozen=True)
class TimestampSpan:
    """One realized output span, including any encoded keyframe lead-in."""

    capture_start: float
    capture_end: float
    output_duration: float
    lead_in: float = 0


@dataclass(frozen=True)
class TimestampCue:
    """A timestamp label covering a half-open output-media interval."""

    start: float
    end: float
    text: str


def timestamp_cues(
    spans: Sequence[TimestampSpan],
    timezone: str,
    speed: float = 1,
) -> Iterator[TimestampCue]:
    """Map realized media spans to capture-time labels without filling gaps.

    Speed is output playback speed relative to capture. Lead-in and trailing
    padding retain the nearest known capture time, matching playback mapping.
    """
    # Some container layouts bind local time over the system UTC zone file.
    zone = datetime.UTC if timezone in ("UTC", "Etc/UTC") else ZoneInfo(timezone)
    if not math.isfinite(speed) or speed <= 0:
        raise ValueError("Export timestamp speed must be positive and finite")
    cursor = 0.0
    previous_end: float | None = None
    for span in spans:
        if (
            not all(
                math.isfinite(value)
                for value in (
                    span.capture_start,
                    span.capture_end,
                    span.output_duration,
                    span.lead_in,
                )
            )
            or span.capture_end <= span.capture_start
            or span.output_duration <= 0
            or not 0 <= span.lead_in < span.output_duration
            or (previous_end is not None and span.capture_start < previous_end)
        ):
            raise ValueError("Invalid realized export timestamp span")
        previous_end = span.capture_end
        local = 0.0
        while local < span.output_duration:
            capture = min(
                span.capture_end,
                span.capture_start + max(0.0, local - span.lead_in),
            )
            if local < span.lead_in:
                boundary = span.lead_in
            elif capture < span.capture_end:
                boundary = local + min(
                    math.floor(capture) + 1 - capture,
                    span.capture_end - capture,
                )
            else:
                boundary = span.output_duration
            end = min(span.output_duration, boundary)
            if end <= local:
                raise ValueError("Export timestamp timeline did not advance")
            label = datetime.datetime.fromtimestamp(capture, tz=zone).strftime(
                "%Y-%m-%d %H:%M:%S %Z %z"
            )
            yield TimestampCue((cursor + local) / speed, (cursor + end) / speed, label)
            local = end
        cursor += span.output_duration


def subtitle_timestamp(seconds: float) -> str:
    """Format an output timestamp for a generated SRT subtitle resource."""
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("Subtitle time must be nonnegative and finite")
    milliseconds = round(seconds * 1000)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


def srt_timestamp_lines(cues: Iterator[TimestampCue]) -> Iterator[str]:
    """Stream generated subtitles without materializing a long export in memory."""
    number = 0
    for cue in cues:
        start, end = subtitle_timestamp(cue.start), subtitle_timestamp(cue.end)
        if start == end:
            continue
        number += 1
        yield f"{number}\n{start} --> {end}\n{cue.text}\n\n"

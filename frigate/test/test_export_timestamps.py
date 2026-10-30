"""Capture-clock export labels across gaps, speed changes and daylight saving."""

import datetime
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfoNotFoundError

from frigate.record.export_metadata import metadata_mux_command, metadata_text
from frigate.record.export_timestamps import (
    ExportTimestampMode,
    TimestampSpan,
    srt_timestamp_lines,
    subtitle_timestamp,
    timestamp_cues,
    timestamp_mux_command,
)


class TestExportTimestamps(unittest.TestCase):
    def test_subtitles_preserve_video_audio_and_remain_optional(self):
        command = timestamp_mux_command(
            "ffmpeg",
            "/input.mp4",
            "/time.srt",
            "/output.mp4",
            ExportTimestampMode.subtitle,
        )
        self.assertEqual(command[command.index("-c:v") + 1], "copy")
        self.assertEqual(command[command.index("-c:a") + 1], "copy")
        self.assertEqual(command[command.index("-c:s") + 1], "mov_text")
        self.assertEqual(command[command.index("-disposition:s:0") + 1], "0")
        self.assertNotIn("-vf", command)

    def test_burn_in_is_export_only_h264_and_both_can_mux_subtitles(self):
        for mode in (ExportTimestampMode.burn_in, ExportTimestampMode.both):
            command = timestamp_mux_command(
                "ffmpeg", "/input.mp4", "/time.srt", "/output.mp4", mode
            )
            self.assertEqual(command[command.index("-c:v") + 1], "libx264")
            self.assertIn("subtitles=filename=", command[command.index("-vf") + 1])
            self.assertEqual("-c:s" in command, mode == ExportTimestampMode.both)
            self.assertEqual(command[-1], "/output.mp4")

    def test_capture_gap_does_not_become_elapsed_wall_time(self):
        cues = list(
            timestamp_cues(
                [
                    TimestampSpan(100, 102, 2),
                    TimestampSpan(200, 202, 2),
                ],
                "UTC",
            )
        )
        self.assertEqual([cue.start for cue in cues], [0, 1, 2, 3])
        self.assertIn("00:03:20 UTC +0000", cues[2].text)
        self.assertEqual(cues[-1].end, 4)

    def test_lead_in_and_padding_hold_nearest_known_capture_time(self):
        cues = list(timestamp_cues([TimestampSpan(100, 102, 3, 0.5)], "UTC"))
        self.assertEqual(cues[0].start, 0)
        self.assertEqual(cues[0].end, 0.5)
        self.assertEqual(cues[0].text, cues[1].text)
        self.assertIn("00:01:42", cues[-1].text)
        self.assertEqual(cues[-1].end, 3)

    def test_timelapse_scales_output_not_capture_labels(self):
        cues = list(timestamp_cues([TimestampSpan(100, 110, 10)], "UTC", speed=25))
        self.assertAlmostEqual(cues[-1].end, 0.4)
        self.assertIn("00:01:49", cues[-1].text)

    def test_dst_transition_labels_include_offsets(self):
        start = datetime.datetime(
            2026, 11, 1, 6, 59, 59, tzinfo=datetime.UTC
        ).timestamp()
        cues = list(
            timestamp_cues([TimestampSpan(start, start + 2, 2)], "America/Chicago")
        )
        self.assertIn("01:59:59 CDT -0500", cues[0].text)
        self.assertIn("01:00:00 CST -0600", cues[1].text)

    def test_srt_format_and_cue_numbering(self):
        lines = list(
            srt_timestamp_lines(timestamp_cues([TimestampSpan(100, 102, 2)], "UTC"))
        )
        self.assertTrue(lines[0].startswith("1\n00:00:00,000 --> 00:00:01,000\n"))
        self.assertTrue(lines[1].startswith("2\n"))
        self.assertEqual(subtitle_timestamp(3661.125), "01:01:01,125")

    def test_invalid_metadata_fails_explicitly(self):
        for span in (
            TimestampSpan(100, 90, 10),
            TimestampSpan(100, 110, 0),
            TimestampSpan(100, 110, 10, 10),
            TimestampSpan(float("nan"), 110, 10),
        ):
            with self.subTest(span=span), self.assertRaises(ValueError):
                list(timestamp_cues([span], "UTC"))
        with self.assertRaises(ValueError):
            list(timestamp_cues([], "UTC", speed=0))
        with self.assertRaises(ZoneInfoNotFoundError):
            list(timestamp_cues([], "Invalid/Timezone"))
        with self.assertRaises(ValueError):
            list(
                timestamp_cues(
                    [TimestampSpan(100, 110, 10), TimestampSpan(105, 115, 10)], "UTC"
                )
            )


class TestTimestampMedia(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("FFMPEG_BIN") or shutil.which("ffmpeg"),
        "FFmpeg required for real-media verification",
    )
    def test_real_subtitle_and_burn_in_outputs(self):
        ffmpeg = os.environ.get("FFMPEG_BIN") or shutil.which("ffmpeg")
        probe = str(Path(ffmpeg).with_name("ffprobe"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            subtitles = root / "timestamps.srt"
            subprocess.run(
                [
                    ffmpeg,
                    "-v",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=320x180:r=5:d=4",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=4",
                    "-c:v",
                    "libx264",
                    "-threads",
                    "1",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    str(source),
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            cues = timestamp_cues(
                [TimestampSpan(100, 102, 2), TimestampSpan(200, 202, 2)], "UTC"
            )
            subtitles.write_text("".join(srt_timestamp_lines(cues)), encoding="utf-8")
            for mode in (
                ExportTimestampMode.subtitle,
                ExportTimestampMode.burn_in,
                ExportTimestampMode.both,
            ):
                with self.subTest(mode=mode):
                    target = root / f"{mode.value}.mp4"
                    subprocess.run(
                        timestamp_mux_command(
                            ffmpeg, str(source), str(subtitles), str(target), mode
                        ),
                        check=True,
                        capture_output=True,
                        timeout=30,
                    )
                    result = subprocess.run(
                        [
                            probe,
                            "-v",
                            "error",
                            "-show_streams",
                            "-show_format",
                            "-of",
                            "json",
                            str(target),
                        ],
                        check=True,
                        capture_output=True,
                        timeout=10,
                    )
                    metadata = json.loads(result.stdout)
                    self.assertAlmostEqual(
                        float(metadata["format"]["duration"]), 4, delta=0.25
                    )
                    streams = metadata["streams"]
                    self.assertTrue(
                        any(stream["codec_type"] == "audio" for stream in streams)
                    )
                    tracks = [
                        stream
                        for stream in streams
                        if stream["codec_type"] == "subtitle"
                    ]
                    self.assertEqual(
                        len(tracks), 0 if mode == ExportTimestampMode.burn_in else 1
                    )
                    if tracks:
                        self.assertEqual(tracks[0]["codec_name"], "mov_text")
                        self.assertEqual(tracks[0]["disposition"]["forced"], 0)
                        text = subprocess.run(
                            [
                                ffmpeg,
                                "-v",
                                "error",
                                "-i",
                                str(target),
                                "-map",
                                "0:s:0",
                                "-f",
                                "srt",
                                "-",
                            ],
                            check=True,
                            capture_output=True,
                            timeout=10,
                        ).stdout.decode()
                        self.assertIn("00:01:40 UTC +0000", text)
                        self.assertIn("00:03:20 UTC +0000", text)
                    # Decode a video frame to prove burn-in changed pixels, not just metadata.
                    frame = subprocess.run(
                        [
                            ffmpeg,
                            "-v",
                            "error",
                            "-ss",
                            "1",
                            "-i",
                            str(target),
                            "-frames:v",
                            "1",
                            "-pix_fmt",
                            "rgb24",
                            "-f",
                            "rawvideo",
                            "-",
                        ],
                        check=True,
                        capture_output=True,
                        timeout=10,
                    ).stdout
                    self.assertEqual(len(frame), 320 * 180 * 3)
                    if mode != ExportTimestampMode.subtitle:
                        self.assertGreater(max(frame), 100)
                    self.assertEqual(
                        hashlib.sha256(source.read_bytes()).hexdigest(), before
                    )
                    context = {
                        "camera": "synthetic",
                        "note": "one=two;#\\\nnext",
                        "timing": {"confidence": "inferred"},
                    }
                    resource = root / "context.txt"
                    resource.write_text(metadata_text(context), encoding="utf-8")
                    annotated = root / f"{mode.value}-metadata.mp4"
                    subprocess.run(
                        metadata_mux_command(
                            ffmpeg, str(target), str(resource), str(annotated)
                        ),
                        check=True,
                        capture_output=True,
                        timeout=20,
                    )
                    info = json.loads(
                        subprocess.run(
                            [
                                probe,
                                "-v",
                                "error",
                                "-show_format",
                                "-show_streams",
                                "-of",
                                "json",
                                str(annotated),
                            ],
                            check=True,
                            capture_output=True,
                            timeout=10,
                        ).stdout
                    )
                    self.assertEqual(
                        json.loads(info["format"]["tags"]["comment"]), context
                    )
                    self.assertEqual(len(info["streams"]), len(streams))

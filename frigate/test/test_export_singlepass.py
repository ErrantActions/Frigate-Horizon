"""Verify one-pass optional captions/context without changing source assembly."""

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


@unittest.skipUnless(os.name == "posix", "Export media runs on Linux")
class TestSinglePassExport(unittest.TestCase):
    def setUp(self):
        from frigate.record.export import (
            ExportStreamEnum,
            PlaybackSourceEnum,
            RecordingExporter,
        )
        from frigate.record.export_timestamps import ExportTimestampMode

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.ffmpeg = os.environ.get("FRIGATE_PLAYBACK_TEST_FFMPEG")
        self.ffprobe = os.environ.get("FRIGATE_PLAYBACK_TEST_FFPROBE")
        if not self.ffmpeg or not self.ffprobe:
            self.skipTest("Actual FFmpeg and FFprobe required")
        self.exporter = RecordingExporter.__new__(RecordingExporter)
        self.exporter.config = SimpleNamespace(
            ffmpeg=SimpleNamespace(ffmpeg_path=self.ffmpeg, ffprobe_path=self.ffprobe),
            ui=SimpleNamespace(timezone="UTC"),
            cameras={"front": SimpleNamespace(get_formatted_name=lambda: "Front")},
        )
        self.exporter.camera = "front"
        self.exporter.export_id = "synthetic-direct"
        self.exporter.start_time, self.exporter.end_time = 100, 113
        self.exporter.ffmpeg_input_args = self.exporter.ffmpeg_output_args = None
        self.exporter.stream = ExportStreamEnum.auto
        self.exporter.playback_source = PlaybackSourceEnum.recordings
        self.exporter.timestamp = ExportTimestampMode.none
        self.exporter.metadata_categories = ()
        self.exporter.chapters = None
        self.exporter.staged_runs = []
        self.exporter._timestamp_files = []
        self.exporter._source_lease = None
        self.exporter.on_progress = None
        self.rows = []
        for index, start in enumerate((100, 110)):
            path = self.root / f"source-{index}.mp4"
            subprocess.run(
                [
                    self.ffmpeg,
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=160x90:rate=10",
                    "-t",
                    "3",
                    "-c:v",
                    "libx264",
                    "-threads",
                    "1",
                    "-preset",
                    "ultrafast",
                    "-g",
                    "10",
                    "-bf",
                    "0",
                    str(path),
                ],
                check=True,
                timeout=15,
                capture_output=True,
            )
            self.rows.append(
                SimpleNamespace(
                    path=str(path),
                    start_time=start,
                    end_time=start + 3,
                    duration=3,
                    has_audio=False,
                    keyframes=[0, 1000, 2000],
                    video_codec="h264",
                )
            )
        self.hashes = {
            row.path: hashlib.sha256(Path(row.path).read_bytes()).hexdigest()
            for row in self.rows
        }
        from frigate.record.export_timestamps import TimestampSpan

        context = patch.object(
            self.exporter,
            "_timestamp_spans",
            return_value=[TimestampSpan(100, 103, 3), TimestampSpan(110, 113, 3)],
        )
        context.start()
        self.addCleanup(context.stop)
        context = patch.object(
            self.exporter,
            "_chapter_metadata_path",
            return_value=str(self.root / "chapters.txt"),
        )
        context.start()
        self.addCleanup(context.stop)
        context = patch(
            "frigate.record.export.EXPORT_STAGING_DIR", str(self.root / "staging")
        )
        context.start()
        self.addCleanup(context.stop)

    def probe(self, path):
        return json.loads(
            subprocess.check_output(
                [
                    self.ffprobe,
                    "-v",
                    "error",
                    "-show_streams",
                    "-show_format",
                    "-show_chapters",
                    "-of",
                    "json",
                    str(path),
                ],
                timeout=15,
            )
        )

    def assemble(self, expected_codec="copy"):
        output = self.root / "export.mp4"
        playlist = [f"file '{row.path}'" for row in self.rows]
        command, playlist = self.exporter._finish_record_export_command(
            str(output),
            "-y -protocol_whitelist pipe,file -f concat -safe 0 -i /dev/stdin",
            playlist,
            self.rows,
            True,
        )
        subprocess.run(
            command,
            input="\n".join(playlist).encode(),
            check=True,
            timeout=30,
            capture_output=True,
        )
        self.assertEqual(self.exporter.staged_runs, [])
        codec = "-c:v" if "-c:v" in command else "-c"
        self.assertEqual(command[command.index(codec) + 1], expected_codec)
        for path, digest in self.hashes.items():
            self.assertEqual(
                hashlib.sha256(Path(path).read_bytes()).hexdigest(), digest
            )
        return command, self.probe(output)

    def test_normal_export_copy_preserves_gaps_without_encoding(self):
        command, media = self.assemble()
        self.assertAlmostEqual(float(media["format"]["duration"]), 6, delta=0.1)
        self.assertEqual(media["streams"][0]["codec_name"], "h264")
        self.assertEqual(command.count("+faststart"), 1)

    def test_caption_and_metadata_are_muxed_in_the_same_copy_pass(self):
        from frigate.config import ChaptersEnum
        from frigate.record.export_metadata import ExportMetadataCategory
        from frigate.record.export_timestamps import ExportTimestampMode

        self.exporter.timestamp = ExportTimestampMode.subtitle
        self.exporter.metadata_categories = (ExportMetadataCategory.provenance,)
        self.exporter.chapters = ChaptersEnum.recording_segments
        command, media = self.assemble()
        self.assertIn("mov_text", command)
        self.assertTrue(
            any(stream["codec_name"] == "mov_text" for stream in media["streams"])
        )
        self.assertIn("frigate-investigation-v1", media["format"]["tags"]["comment"])
        self.assertEqual(command.count("+faststart"), 1)
        self.assertEqual(len(media["chapters"]), 2)
        self.assertAlmostEqual(float(media["chapters"][-1]["end_time"]), 6, delta=0.1)
        self.assertEqual(
            [span.capture_start for span in self.exporter._timestamp_spans()],
            [100, 110],
        )

    def test_subtitle_only_has_one_copy_pass(self):
        from frigate.record.export_timestamps import ExportTimestampMode

        self.exporter.timestamp = ExportTimestampMode.subtitle
        command, media = self.assemble()
        self.assertEqual(command.count("-i"), 2)
        self.assertEqual(
            [stream["codec_name"] for stream in media["streams"]], ["h264", "mov_text"]
        )

    def test_both_encodes_once_and_keeps_selectable_caption_track(self):
        from frigate.record.export_timestamps import ExportTimestampMode

        self.exporter.timestamp = ExportTimestampMode.both
        command, media = self.assemble("libx264")
        self.assertEqual(command.count("libx264"), 1)
        self.assertEqual(command.count("+faststart"), 1)
        self.assertAlmostEqual(float(media["format"]["duration"]), 6, delta=0.1)
        self.assertIn("mov_text", [stream["codec_name"] for stream in media["streams"]])

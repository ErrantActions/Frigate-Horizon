"""Opt-in real-FFmpeg coverage using generated sources, never camera media."""

import asyncio
import json
import os
import struct
import unittest

from peewee import SqliteDatabase

from frigate.api.defs.request.playback_body import PlaybackCreateBody
from frigate.models import Recordings
from frigate.record.playback import PlaybackManager
from frigate.test.test_playback import PlaybackDirectory

FFMPEG = os.environ.get("FRIGATE_PLAYBACK_TEST_FFMPEG")
FFPROBE = os.environ.get("FRIGATE_PLAYBACK_TEST_FFPROBE")


@unittest.skipUnless(FFMPEG and FFPROBE, "Explicit synthetic-media test tools required")
class TestRealPlayback(PlaybackDirectory, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        assert FFMPEG is not None and FFPROBE is not None
        self.ffmpeg = FFMPEG
        self.ffprobe = FFPROBE
        self.create_directory()
        self.database = SqliteDatabase(
            ":memory:", check_same_thread=False, thread_safe=False
        )
        self.binding = self.database.bind_ctx([Recordings])
        self.binding.__enter__()
        self.database.create_tables([Recordings])
        self.hwaccel = os.environ.get("FRIGATE_PLAYBACK_TEST_HWACCEL", "")
        self.manager = PlaybackManager(
            self.ffmpeg,
            str(self.directory),
            get_hwaccel_args=lambda _camera: self.hwaccel,
        )
        await self.manager.start()

    async def asyncTearDown(self):
        await self.manager.close()
        self.database.close()
        self.binding.__exit__(None, None, None)
        self.remove_directory()

    async def run_command(self, *command):
        """Run one owned, bounded synthetic-media process."""
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 90)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        self.assertEqual(process.returncode, 0, stderr.decode(errors="replace")[-4000:])
        return stdout

    async def generate_source(self, name, encoder, size, audio_rate, channels):
        """Generate known video and an audible tone without external inputs."""
        path = self.directory / f"{name}.mp4"
        command = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=blue:size={size}:rate=15",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate={audio_rate}",
            "-t",
            "2",
            "-c:v",
            encoder,
            "-threads",
            "2",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
        ]
        if encoder == "libx265":
            command += ["-x265-params", "pools=1:frame-threads=1:log-level=error"]
        command += [
            "-c:a",
            "aac",
            "-ar",
            str(audio_rate),
            "-ac",
            str(channels),
            "-b:a",
            "48k",
            str(path),
        ]
        await self.run_command(*command)
        return path

    async def probe(self, path):
        """Read actual encoded stream metadata from a generated file."""
        output = await self.run_command(
            self.ffprobe,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        )
        return json.loads(output)

    async def test_audio_tail_preserves_planned_playback_duration(self):
        source = self.directory / "audio-tail.mp4"
        await self.run_command(
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:size=320x180:rate=15:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=2.3",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-threads",
            "2",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source),
        )
        original = source.read_bytes()
        metadata = await self.probe(source)
        video = next(s for s in metadata["streams"] if s["codec_type"] == "video")
        duration = float(metadata["format"]["duration"])
        self.assertGreater(duration - float(video["duration"]), 0.2)
        Recordings.create(
            id="synthetic-audio-tail",
            camera="synthetic",
            path=str(source),
            stream_type="main",
            start_time=1000,
            end_time=1000 + duration,
            duration=duration,
            has_audio=True,
            keyframes=[0],
        )
        session = await self.manager.create(
            "synthetic",
            "synthetic-test-owner",
            PlaybackCreateBody(
                after=1000,
                before=1000 + duration,
                start_timestamp=1001,
                client_id="synthetic-audio-tail-test",
            ),
        )
        await asyncio.wait_for(asyncio.shield(session.task), 100)
        status = self.manager.status(session)
        self.assertEqual(status.state, "complete", status.error)
        if self.hwaccel.startswith("preset-nvidia"):
            command = self.manager.command(session, session.clips[0], 0)
            self.assertEqual(command[command.index("-c:v") + 1], "h264_nvenc")
        self.assertEqual(len(status.timeline), 1)
        span = status.timeline[0]
        self.assertTrue(span.measured)
        self.assertEqual((span.start_time, span.end_time), (1000, 1000 + duration))
        self.assertEqual(span.lead_in_ms, 0)
        self.assertAlmostEqual(span.duration, duration, delta=1 / 15 + 0.005)
        manifest = session.directory / "index.m3u8"
        text = manifest.read_text(encoding="utf-8")
        self.assertIn("#EXT-X-ENDLIST", text)
        measured = sum(
            float(line[8:].rstrip(","))
            for line in text.splitlines()
            if line.startswith("#EXTINF:")
        )
        self.assertAlmostEqual(measured, span.duration)
        frames = await self.run_command(
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            str(manifest),
            "-map",
            "0:v:0",
            "-vf",
            "scale=1:1,format=rgb24",
            "-f",
            "rawvideo",
            "-",
        )
        self.assertGreater(len(frames) // 3, 32)
        self.assertGreater(frames[-1], 200, frames[-3:])
        self.assertLess(frames[-3], 30, frames[-3:])
        self.assertEqual(source.read_bytes(), original)

    async def test_real_hevc_mono_to_browser_avc_aac_and_cross_stream_handoff(self):
        hevc = await self.generate_source("main", "libx265", "3840x2160", 16000, 1)
        avc = await self.generate_source("sub", "libx264", "320x240", 48000, 2)
        originals = {path: path.read_bytes() for path in (hevc, avc)}
        source = await self.probe(hevc)
        self.assertEqual(source["streams"][0]["codec_name"], "hevc")
        self.assertEqual(source["streams"][0]["width"], 3840)
        self.assertEqual(source["streams"][1]["sample_rate"], "16000")
        self.assertEqual(source["streams"][1]["channels"], 1)
        for index, (path, stream) in enumerate(((hevc, "main"), (avc, "sub"))):
            Recordings.create(
                id=f"synthetic-{index}",
                camera="synthetic",
                path=str(path),
                stream_type=stream,
                start_time=1000 + index * 2,
                end_time=1002 + index * 2,
                duration=2,
                has_audio=True,
                keyframes=[0],
            )
        session = await self.manager.create(
            "synthetic",
            "synthetic-test-owner",
            PlaybackCreateBody(
                after=1000,
                before=1004,
                start_timestamp=1002.5,
                client_id="synthetic-test",
            ),
        )
        await asyncio.wait_for(asyncio.shield(session.task), 100)
        status = self.manager.status(session)
        self.assertEqual(status.state, "complete", status.error)
        self.assertEqual(len(status.timeline), 2)
        self.assertTrue(all(span.measured for span in status.timeline))
        self.assertTrue(all(span.lead_in_ms == 0 for span in status.timeline))
        manifest = session.directory / "index.m3u8"
        text = manifest.read_text(encoding="utf-8")
        self.assertEqual(text.count("#EXT-X-DISCONTINUITY"), 1)
        self.assertIn("#EXT-X-ENDLIST", text)
        measured = sum(
            float(line[8:].rstrip(","))
            for line in text.splitlines()
            if line.startswith("#EXTINF:")
        )
        self.assertAlmostEqual(sum(span.duration for span in status.timeline), measured)
        self.assertLessEqual(measured, 4.2)
        for resource in sorted(session.resources):
            if not resource.endswith(".ts"):
                continue
            media = await self.probe(session.directory / resource)
            video = next(s for s in media["streams"] if s["codec_type"] == "video")
            audio = next(s for s in media["streams"] if s["codec_type"] == "audio")
            self.assertEqual(video["codec_name"], "h264")
            self.assertIn(video["profile"], ("Baseline", "Constrained Baseline"))
            self.assertEqual(video["pix_fmt"], "yuv420p")
            self.assertLessEqual(video["width"], 1280)
            self.assertLessEqual(video["height"], 720)
            self.assertEqual(
                (video["width"], video["height"]),
                (1280, 720) if resource.startswith("p0-") else (960, 720),
            )
            self.assertEqual(video["r_frame_rate"], "15/1")
            self.assertEqual(audio["codec_name"], "aac")
            self.assertEqual(audio["profile"], "LC")
            self.assertEqual(audio["sample_rate"], "48000")
            self.assertEqual(audio["channels"], 2)
            starts = [float(video["start_time"]), float(audio["start_time"])]
            self.assertLess(abs(min(starts)), 0.025, starts)
            self.assertLess(abs(starts[0] - starts[1]), 0.067, starts)
            pcm = await self.run_command(
                self.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-i",
                str(session.directory / resource),
                "-map",
                "0:a:0",
                "-c:a",
                "pcm_s16le",
                "-f",
                "s16le",
                "-",
            )
            self.assertTrue(any(pcm), "The stored tone must remain audible")
        await self.run_command(
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            str(manifest),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-f",
            "null",
            "-",
        )
        for path, original in originals.items():
            self.assertEqual(path.read_bytes(), original)

    async def test_nonzero_source_pts_trim_audio_sync_and_controller_lead_in(self):
        source = self.directory / "offset-and-transition.mp4"
        await self.run_command(
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=320x240:r=15:d=1[r];"
            "color=c=blue:s=320x240:r=15:d=1[b];[r][b]concat=n=2:v=1:a=0",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000:duration=1[a];"
            "sine=frequency=880:sample_rate=16000:duration=1[b];"
            "[a][b]concat=n=2:v=0:a=1",
            "-t",
            "2",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-threads",
            "2",
            "-g",
            "15",
            "-keyint_min",
            "15",
            "-sc_threshold",
            "0",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            "16000",
            "-ac",
            "1",
            "-output_ts_offset",
            "5",
            str(source),
        )
        original = source.read_bytes()
        metadata = await self.probe(source)
        self.assertGreater(float(metadata["format"]["start_time"]), 4.9)
        Recordings.create(
            id="synthetic-offset",
            camera="synthetic",
            path=str(source),
            stream_type="main",
            start_time=1000,
            end_time=1002,
            duration=2,
            has_audio=True,
            keyframes=[0, 1000],
        )
        session = await self.manager.create(
            "synthetic",
            "synthetic-test-owner",
            PlaybackCreateBody(
                after=1001.4,
                before=1001.9,
                start_timestamp=1001.6,
                client_id="synthetic-trim-test",
                generation=1,
            ),
        )
        await asyncio.wait_for(asyncio.shield(session.task), 100)
        status = self.manager.status(session)
        self.assertEqual(status.state, "complete", status.error)
        span = status.timeline[0]
        self.assertEqual((span.start_time, span.end_time), (1001.4, 1001.9))
        self.assertAlmostEqual(span.duration, 0.9, delta=1 / 15 + 0.005)
        self.assertAlmostEqual(span.lead_in_ms, 400)
        lead_in = span.lead_in_ms / 1000
        controller_seek = lead_in + (1001.6 - span.start_time)
        # Keyframe back-snap is 0.4s, not the input's five-second PTS origin.
        # CFR endpoint rounding is bounded to one 15-FPS frame.
        self.assertAlmostEqual(controller_seek, 0.6, places=6)
        segment = session.directory / "p0-000.ts"
        normalized = await self.probe(segment)
        video = next(s for s in normalized["streams"] if s["codec_type"] == "video")
        audio = next(s for s in normalized["streams"] if s["codec_type"] == "audio")
        starts = [float(video["start_time"]), float(audio["start_time"])]
        self.assertLess(abs(min(starts)), 0.025, starts)
        self.assertLess(abs(starts[0] - starts[1]), 0.067, starts)
        pixel = await self.run_command(
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            str(segment),
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-vf",
            "scale=1:1,format=rgb24",
            "-f",
            "rawvideo",
            "-",
        )
        self.assertEqual(len(pixel), 3)
        self.assertGreater(pixel[2], 200, pixel)
        self.assertLess(pixel[0], 30, pixel)
        pcm = await self.run_command(
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            str(segment),
            "-map",
            "0:a:0",
            "-t",
            "0.7",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-c:a",
            "pcm_s16le",
            "-f",
            "s16le",
            "-",
        )
        samples = [sample[0] for sample in struct.iter_unpack("<h", pcm)][4800:]
        crossings = sum((a < 0) != (b < 0) for a, b in zip(samples, samples[1:]))
        frequency = crossings * 48000 / (2 * (len(samples) - 1))
        # Both blue video and the 880-Hz tone must come from after the same
        # one-second transition, including AAC priming and the source PTS shift.
        self.assertAlmostEqual(frequency, 880, delta=30)
        self.assertEqual(source.read_bytes(), original)

"""Focused stored-recording playback tests with no original media writes."""

import asyncio
import shutil
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from peewee import SqliteDatabase
from pydantic import ValidationError

from frigate.api.defs.request.playback_body import PlaybackCreateBody
from frigate.models import Recordings
from frigate.record.playback import (
    PlaybackError,
    PlaybackManager,
    PlaybackSession,
    SourceClip,
    parse_playlist,
    plan_sources,
)


def body(**changes):
    """Make a valid, short compatibility window."""
    return PlaybackCreateBody(
        **{
            "after": 1000,
            "before": 1010,
            "start_timestamp": 1001,
            "client_id": "review-player",
            **changes,
        }
    )


def write_clip(directory, index, durations=(2.0,)):
    """Write only synthetic HLS output in an owned test namespace."""
    lines = ["#EXTM3U"]
    for segment, duration in enumerate(durations):
        name = f"p{index}-{segment:03}.ts"
        (directory / name).write_bytes(b"synthetic segment")
        lines.extend([f"#EXTINF:{duration},", name])
    lines.append("#EXT-X-ENDLIST")
    (directory / f"p{index}.m3u8").write_text("\n".join(lines), encoding="utf-8")


class TestPlaybackInput(unittest.TestCase):
    def test_bounds_and_nonfinite(self):
        for changes in (
            {"before": 1000},
            {"before": 1031},
            {"after": float("nan")},
            {"before": float("inf")},
            {"start_timestamp": 1010},
            {"stream": "http://example.invalid"},
            {"client_id": "../outside"},
            {"generation": -1},
            {"generation": True},
            {"generation": 1.5},
            {"generation": 9007199254740992},
            {"path": "recording.mp4"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                body(**changes)


class TestPlaybackCoverage(unittest.TestCase):
    def setUp(self):
        self.database = SqliteDatabase(
            ":memory:", check_same_thread=False, thread_safe=False
        )
        self.binding = self.database.bind_ctx([Recordings])
        self.binding.__enter__()
        self.database.create_tables([Recordings])

    def tearDown(self):
        self.database.close()
        self.binding.__exit__(None, None, None)

    def insert(self, name, start, end, stream="main", **extra):
        """Create isolated coverage metadata, never a media file."""
        Recordings.create(
            id=name,
            camera="front",
            path=str(Path.cwd() / f"{name}.mp4"),
            start_time=start,
            end_time=end,
            duration=end - start,
            stream_type=stream,
            keyframes=extra.pop("keyframes", [0, 2000, 4000, 6000, 8000]),
            **extra,
        )

    def test_heterogeneous_handoff_and_audio_glitch_not_dropped(self):
        self.insert("hevc", 1000, 1004, video_codec="hevc", has_audio=True)
        self.insert("avc", 1004, 1008, "sub", video_codec="h264", has_audio=False)
        clips = plan_sources("front", body(before=1008))
        self.assertEqual([clip.start for clip in clips], [1000, 1004])
        self.assertEqual([clip.has_audio for clip in clips], [True, False])
        self.assertEqual([clip.duration for clip in clips], [4, 4])

    def test_explicit_stream_and_gap(self):
        self.insert("main", 1000, 1004)
        self.insert("sub", 1006, 1010, "sub")
        clips = plan_sources("front", body(stream="main"))
        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0].start, 1000)
        with self.assertRaisesRegex(PlaybackError, "playback_start_in_gap"):
            plan_sources("front", body(start_timestamp=1005))
        with self.assertRaisesRegex(PlaybackError, "playback_start_in_gap"):
            plan_sources("front", body(stream="sub"))

    def test_keyframe_backsnap_matches_review(self):
        self.insert("source", 1000, 1010)
        clips = plan_sources(
            "front", body(after=1003, before=1008, start_timestamp=1004)
        )
        self.assertEqual(
            (clips[0].start, clips[0].offset, clips[0].duration), (1002, 2, 6)
        )
        self.assertEqual((clips[0].wall_start, clips[0].wall_end), (1003, 1008))

    def test_large_backsnap_rejected_instead_of_encoding_hour(self):
        self.insert("source", 980, 1010, keyframes=None)
        self.insert("next", 1010, 1020)
        with self.assertRaisesRegex(PlaybackError, "snapped_window_too_large"):
            plan_sources("front", body(before=1020))

    def test_missing_and_unplayable_spans_fail_explicitly(self):
        with self.assertRaisesRegex(PlaybackError, "recordings_unavailable"):
            plan_sources("front", body())
        self.insert("short", 1000, 1010)
        with (
            patch(
                "frigate.record.playback.plan_clip",
                return_value=SimpleNamespace(skipped=True),
            ),
            self.assertRaisesRegex(PlaybackError, "recording_span_unplayable"),
        ):
            plan_sources("front", body())


class PlaybackDirectory:
    """Tests must not use OS temporary directories or production cache paths."""

    def create_directory(self):
        self.directory = Path.cwd() / (".playback-test-" + uuid.uuid4().hex)
        self.directory.mkdir()

    def remove_directory(self):
        shutil.rmtree(self.directory)


class TestPlaylist(PlaybackDirectory, unittest.TestCase):
    def setUp(self):
        self.create_directory()

    def tearDown(self):
        self.remove_directory()

    def test_measured_durations(self):
        write_clip(self.directory, 0, (2.0, 1.6))
        _, duration, resources, target = parse_playlist(
            self.directory / "p0.m3u8", "p0"
        )
        self.assertAlmostEqual(duration, 3.6)
        self.assertEqual(target, 2)
        self.assertEqual(resources, {"p0-000.ts", "p0-001.ts"})

    def test_reject_external_incomplete_nonfinite_and_traversal(self):
        for content in (
            "#EXTM3U\n#EXTINF:2,\nhttps://example.invalid/video.ts\n#EXT-X-ENDLIST",
            "#EXTM3U\n#EXTINF:2,\n../other.ts\n#EXT-X-ENDLIST",
            "#EXTM3U\n#EXTINF:nan,\np0-000.ts\n#EXT-X-ENDLIST",
            "#EXTM3U\n#EXTINF:2,\np0-000.ts",
        ):
            (self.directory / "p0.m3u8").write_text(content, encoding="utf-8")
            with self.subTest(content=content), self.assertRaises(PlaybackError):
                parse_playlist(self.directory / "p0.m3u8", "p0")


class TestPlaybackManager(PlaybackDirectory, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.create_directory()
        self.manager = PlaybackManager("configured-ffmpeg", str(self.directory))
        await self.manager.start()
        self.clips = [SourceClip("unopened-source.mp4", 1000, 2, 0, True)]

    async def asyncTearDown(self):
        await self.manager.close()
        self.remove_directory()

    async def create(self, **changes):
        """Resolve only synthetic metadata and output in these manager tests."""
        with patch("frigate.record.playback._load_sources", return_value=self.clips):
            return await self.manager.create("front", "alice", body(**changes))

    async def test_progress_waits_for_requested_timestamp_and_measures(self):
        first_published = asyncio.Event()
        finish = asyncio.Event()
        self.clips = [
            SourceClip("hevc.mp4", 1000, 2, 0, True),
            SourceClip("avc.mp4", 1004, 2, 0, True),
        ]

        async def encode(session, clip, index):
            if index:
                first_published.set()
                await finish.wait()
            write_clip(session.directory, index, (2.0,))

        with patch.object(self.manager, "_encode", side_effect=encode):
            session = await self.create(start_timestamp=1005)
            await asyncio.wait_for(first_published.wait(), 2)
            self.assertEqual(session.state, "preparing")
            self.assertEqual(self.manager.status(session).ready_through, 1002)
            self.assertNotIn(
                "#EXT-X-ENDLIST", (session.directory / "index.m3u8").read_text()
            )
            finish.set()
            await session.task
        status = self.manager.status(session)
        self.assertEqual(status.state, "complete")
        self.assertEqual([span.duration for span in status.timeline], [2, 2])
        self.assertEqual([span.start_time for span in status.timeline], [1000, 1004])
        manifest = (session.directory / "index.m3u8").read_text()
        self.assertIn("#EXT-X-DISCONTINUITY", manifest)
        self.assertIn("#EXT-X-ENDLIST", manifest)
        self.assertNotIn("hevc.mp4", status.model_dump_json())
        self.assertFalse(status.manifest_path.startswith("/"))

    async def test_ready_while_later_span_encodes(self):
        later = asyncio.Event()
        finish = asyncio.Event()
        self.clips *= 2

        async def encode(session, clip, index):
            if index:
                later.set()
                await finish.wait()
            write_clip(session.directory, index)

        with patch.object(self.manager, "_encode", side_effect=encode):
            session = await self.create()
            await asyncio.wait_for(later.wait(), 2)
            self.assertEqual(session.state, "ready")
            finish.set()
            await session.task
        self.assertEqual(session.state, "complete")

    async def test_capacity_deduplication_ownership_and_reader_lease(self):
        never = asyncio.Event()

        async def encode(*args):
            await never.wait()

        with patch.object(self.manager, "_encode", side_effect=encode):
            session = await self.create()
            self.assertIs(session, await self.create())
            await self.create(client_id="other")
            with self.assertRaisesRegex(PlaybackError, "capacity_exhausted"):
                await self.create(client_id="third")
            for camera, owner in (("back", "alice"), ("front", "bob")):
                with self.assertRaisesRegex(PlaybackError, "playback_not_found"):
                    self.manager.get(session.id, camera, owner)
            session.readers += 1
            await self.manager.cancel(session)
            self.assertTrue(session.directory.exists())
            session.readers -= 1
            await self.manager.cancel(session)
            self.assertFalse(session.directory.exists())

    async def test_new_generation_reserves_before_old_source_preparation_finishes(self):
        preparing = asyncio.Event()
        finish_old = asyncio.Event()
        original_to_thread = asyncio.to_thread

        async def controlled_to_thread(function, *args, **kwargs):
            if function.__name__ == "_load_sources":
                if args[1].generation == 1:
                    preparing.set()
                    await finish_old.wait()
                return self.clips
            return await original_to_thread(function, *args, **kwargs)

        async def encode(session, clip, index):
            write_clip(session.directory, index)

        with (
            patch(
                "frigate.record.playback.asyncio.to_thread",
                side_effect=controlled_to_thread,
            ),
            patch.object(self.manager, "_encode", side_effect=encode),
        ):
            old = asyncio.create_task(
                self.manager.create("front", "alice", body(generation=1))
            )
            await asyncio.wait_for(preparing.wait(), 2)
            newer = asyncio.create_task(
                self.manager.create(
                    "front", "alice", body(generation=2, start_timestamp=1001.5)
                )
            )
            # The new reservation runs before acquiring the old create's lock.
            await asyncio.sleep(0)
            self.assertEqual(
                self.manager._generations[("alice", "front", "review-player")][
                    0
                ].generation,
                2,
            )
            finish_old.set()
            with self.assertRaisesRegex(PlaybackError, "generation_superseded"):
                await old
            session = await newer
            await session.task
        status = self.manager.status(session)
        self.assertEqual(status.generation, 2)
        self.assertEqual(status.client_id, "review-player")
        self.assertEqual(len(self.manager.sessions), 1)
        with self.assertRaisesRegex(PlaybackError, "generation_superseded"):
            await self.manager.create("front", "alice", body(generation=1))
        self.assertEqual(session.state, "complete")

    async def test_generation_conflicts_and_other_clients_do_not_supersede(self):
        async def encode(session, clip, index):
            write_clip(session.directory, index)

        with patch.object(self.manager, "_encode", side_effect=encode):
            session = await self.create(generation=10)
            await session.task
            with self.assertRaisesRegex(PlaybackError, "generation_conflict"):
                await self.create(generation=10, start_timestamp=1001.5)
            other = await self.create(client_id="second-player", generation=1)
            await other.task
            self.assertIs(session, await self.create(generation=10))
        self.assertIn(session.id, self.manager.sessions)
        self.assertIn(other.id, self.manager.sessions)

    async def test_generation_history_is_bounded_and_inactive_entries_expire(self):
        with patch.object(
            self.manager, "_create", side_effect=PlaybackError("synthetic_rejection")
        ):
            for index in range(128):
                with self.assertRaisesRegex(PlaybackError, "synthetic_rejection"):
                    await self.manager.create(
                        "front", "alice", body(client_id=f"client-{index}")
                    )
            with self.assertRaisesRegex(PlaybackError, "client_capacity_exhausted"):
                await self.manager.create(
                    "front", "alice", body(client_id="one-too-many")
                )
            self.assertEqual(len(self.manager._generations), 128)
            self.assertFalse(self.manager._creating)
            self.manager._generations = {
                key: (request, touched - 301)
                for key, (request, touched) in self.manager._generations.items()
            }
            with self.assertRaisesRegex(PlaybackError, "synthetic_rejection"):
                await self.manager.create("front", "alice", body(client_id="fresh"))
        self.assertEqual(len(self.manager._generations), 1)

    async def test_failure_safe_terminal_and_cleanup(self):
        with patch.object(
            self.manager, "_encode", side_effect=OSError("secret/source/password")
        ):
            session = await self.create()
            await session.task
        status = self.manager.status(session)
        self.assertEqual(status.state, "failed")
        self.assertNotIn("secret", status.model_dump_json())
        await self.manager.cancel(session)
        self.assertFalse(session.directory.exists())

    async def test_shortened_source_is_terminal_not_silently_dropped(self):
        async def encode(session, clip, index):
            write_clip(session.directory, index, (0.5,))

        with patch.object(self.manager, "_encode", side_effect=encode):
            session = await self.create()
            await session.task
        self.assertEqual(session.state, "failed")
        self.assertEqual(session.error, "playback_span_duration_mismatch")
        self.assertEqual(session.timeline, [])

    async def test_explicit_lead_in_excludes_cfr_tail_padding(self):
        async def encode(session, clip, index):
            write_clip(session.directory, index, (2.0,))

        with patch.object(self.manager, "_encode", side_effect=encode):
            for lead_in in (0.0, 0.4):
                self.clips = [
                    SourceClip(
                        "unopened-source.mp4",
                        1000,
                        1.98,
                        0,
                        True,
                        1000 + lead_in,
                        1001.98,
                    )
                ]
                session = await self.create(client_id=f"tail-{int(lead_in * 1000)}")
                await session.task
                span = self.manager.status(session).timeline[0]
                self.assertEqual(session.state, "complete", session.error)
                self.assertEqual(span.start_time, 1000 + lead_in)
                self.assertEqual(span.end_time, 1001.98)
                self.assertEqual(span.duration, 2)
                self.assertAlmostEqual(span.lead_in_ms, lead_in * 1000)
                inferred = 1000 * (span.duration - (span.end_time - span.start_time))
                self.assertAlmostEqual(inferred - span.lead_in_ms, 20)
                await self.manager.cancel(session)

    async def test_disk_full_explicit_and_no_reservation(self):
        with (
            patch(
                "frigate.record.playback.shutil.disk_usage",
                return_value=SimpleNamespace(free=0),
            ),
            self.assertRaisesRegex(PlaybackError, "playback_cache_full"),
        ):
            await self.create()
        self.assertFalse(self.manager.sessions)

    async def test_expiry_preserves_readers_and_renewed_paused_lease(self):
        never = asyncio.Event()

        async def encode(*args):
            await never.wait()

        with patch.object(self.manager, "_encode", side_effect=encode):
            session = await self.create()
            session.touched = time.monotonic() - 100
            session.readers = 1
            await self.manager._expire_sessions()
            self.assertIn(session.id, self.manager.sessions)
            session.readers = 0
            self.manager.get(session.id, "front", "alice")
            await self.manager._expire_sessions()
            self.assertIn(session.id, self.manager.sessions)
            session.touched = time.monotonic() - 100
            await self.manager._expire_sessions()
            self.assertNotIn(session.id, self.manager.sessions)

    async def test_stale_namespace_counts_toward_total_without_deleting_it(self):
        stale = self.directory / "playback-compatibility-prior-runtime"
        stale.mkdir()
        original = stale / "owned-by-prior-runtime.ts"
        original.write_bytes(b"old")
        with (
            patch("frigate.record.playback.TOTAL_BYTES", 32 * 1024 * 1024),
            self.assertRaisesRegex(PlaybackError, "playback_cache_capacity_exhausted"),
        ):
            await self.create()
        self.assertEqual(original.read_bytes(), b"old")

    async def test_cleanup_failure_retains_quota_and_can_retry(self):
        with patch.object(
            self.manager, "_encode", side_effect=PlaybackError("synthetic_failure")
        ):
            session = await self.create()
            await session.task
        with (
            patch("frigate.record.playback.shutil.rmtree", side_effect=PermissionError),
            self.assertRaisesRegex(PlaybackError, "playback_cleanup_failed"),
        ):
            await self.manager.cancel(session)
        self.assertIn(session.id, self.manager.sessions)
        self.assertTrue(session.deleting)
        await self.manager.cancel(session)
        self.assertNotIn(session.id, self.manager.sessions)

    async def test_session_deadline_becomes_terminal(self):
        never = asyncio.Event()

        async def encode(*args):
            await never.wait()

        with (
            patch.object(self.manager, "_encode", side_effect=encode),
            patch("frigate.record.playback.SESSION_TIMEOUT", 0.01),
        ):
            session = await self.create()
            await session.task
        self.assertEqual(session.state, "failed")
        self.assertEqual(session.error, "playback_session_timeout")

    async def test_missing_source_and_spawn_failure_are_terminal(self):
        session = await self.create()
        await session.task
        self.assertEqual(session.error, "recording_source_unavailable")
        await self.manager.cancel(session)
        source = self.directory / "synthetic-source"
        source.write_bytes(b"not media")
        self.clips = [SourceClip(str(source), 1000, 4, 0, True)]
        with patch(
            "asyncio.create_subprocess_exec", side_effect=OSError("private path")
        ):
            session = await self.create()
            await session.task
        self.assertEqual(session.state, "failed")
        self.assertNotIn("private", self.manager.status(session).model_dump_json())

    async def test_bounded_encoder_command_and_silence_for_absent_audio(self):
        session = PlaybackSession(
            "session", "alice", "front", body(), self.directory, self.clips
        )
        clip = SourceClip("source.mp4", 1000, 4, 2, False)
        command = self.manager.command(session, clip, 0)
        self.assertEqual(command[0], "configured-ffmpeg")
        self.assertIn("libx264", command)
        self.assertIn("aac_low", command)
        self.assertIn("48000", command)
        self.assertIn("yuv420p", command)
        self.assertIn("anullsrc=r=48000:cl=stereo", command)
        self.assertIn("force_divisible_by=2,fps=15,setsar=1", " ".join(command))
        self.assertNotIn("concat", command)
        self.assertNotIn("-c:v copy", " ".join(command))

    async def test_nvidia_playback_uses_nvenc_and_preserves_bounds(self):
        session = PlaybackSession(
            "session",
            "alice",
            "front",
            body(),
            self.directory,
            self.clips,
            hwaccel_args="preset-nvidia",
        )
        command = self.manager.command(
            session, SourceClip("source with spaces.mp4", 1000, 4, 2, True), 0
        )
        self.assertEqual(command[command.index("-c:v") + 1], "h264_nvenc")
        self.assertNotIn("libx264", command)
        self.assertEqual(command[command.index("-preset:v") + 1], "p4")
        self.assertEqual(command[command.index("-t") + 1], "4")
        self.assertEqual(command[command.index("-bf") + 1], "0")
        self.assertNotIn("-sc_threshold", command)
        self.assertEqual(
            command[command.index("-i") + 1],
            str(Path("source with spaces.mp4").resolve()),
        )
        self.assertIn("tpad=stop_mode=clone:stop=-1", " ".join(command))
        self.assertEqual(
            self.manager.status(session).encoder, "h264_nvenc (NVIDIA GPU)"
        )

    async def test_session_snapshots_configured_acceleration(self):
        settings = ["-hwaccel", "cuda"]
        self.manager.get_hwaccel_args = lambda _camera: settings
        with patch.object(self.manager, "_run", return_value=None):
            session = await self.create()
            await session.task
        settings.append("changed-after-create")
        self.assertEqual(session.hwaccel_args, ["-hwaccel", "cuda"])

    async def test_cancellation_terminates_only_owned_process(self):
        source = self.directory / "synthetic-source"
        source.write_bytes(b"not media")
        stopped = asyncio.Event()
        process = SimpleNamespace(returncode=None)

        async def wait():
            await stopped.wait()
            return process.returncode

        def terminate():
            process.returncode = -15
            stopped.set()

        process.wait = wait
        process.terminate = Mock(side_effect=terminate)
        process.kill = Mock()
        self.clips = [SourceClip(str(source), 1000, 4, 0, True)]
        spawned = asyncio.Event()

        async def spawn(*args, **kwargs):
            spawned.set()
            return process

        with patch("asyncio.create_subprocess_exec", side_effect=spawn):
            session = await self.create()
            await asyncio.wait_for(spawned.wait(), 2)
            await self.manager.cancel(session)
        process.terminate.assert_called_once()
        process.kill.assert_not_called()
        self.assertFalse(self.manager.sessions)

    async def test_span_deadline_and_cache_guard_reap_encoder(self):
        source = self.directory / "synthetic-source"
        source.write_bytes(b"not media")
        self.clips = [SourceClip(str(source), 1000, 4, 0, True)]
        for reason in (
            "playback_encoder_timeout",
            "playback_cache_limit",
            "playback_compatibility_disabled",
        ):
            with self.subTest(reason=reason):
                self.manager.compatibility_enabled = None
                stopped = asyncio.Event()
                process = SimpleNamespace(returncode=None)

                async def wait():
                    await stopped.wait()
                    return process.returncode

                def terminate():
                    process.returncode = -15
                    stopped.set()

                async def spawn(*args, **kwargs):
                    if reason == "playback_cache_limit":
                        output = Path(args[-1]).parent / "bounded-output.ts"
                        output.write_bytes(b"synthetic output")
                    if reason == "playback_compatibility_disabled":
                        self.manager.compatibility_enabled = lambda: False
                    return process

                process.wait = wait
                process.terminate = Mock(side_effect=terminate)
                process.kill = Mock()
                with (
                    patch("asyncio.create_subprocess_exec", side_effect=spawn),
                    patch(
                        "frigate.record.playback.SPAN_TIMEOUT",
                        0 if reason == "playback_encoder_timeout" else 45,
                    ),
                    patch(
                        "frigate.record.playback.SESSION_BYTES",
                        1024 * 1024 + 1
                        if reason == "playback_cache_limit"
                        else 32 * 1024 * 1024,
                    ),
                ):
                    session = await self.create()
                    await session.task
                self.assertEqual(session.state, "failed")
                self.assertEqual(session.error, reason)
                process.terminate.assert_called_once()
                process.kill.assert_not_called()
                await self.manager.cancel(session)

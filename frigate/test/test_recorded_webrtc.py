"""Recorded WebRTC source, admission, generation and cancellation regressions."""

import asyncio
import hashlib
import json
import shutil
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from pydantic import ValidationError

from frigate.api.defs.request.recorded_webrtc_body import (
    RecordedAnswer,
    RecordedCandidates,
    RecordedControl,
    RecordedCreate,
    RecordedQuality,
    RecordedWindow,
)
from frigate.api.defs.response.recorded_webrtc_response import RecordedWebRTCSession
from frigate.comms.live_webrtc import (
    LiveWebRTCError,
    LiveWebRTCManager,
    MediaGroup,
    ViewerBody,
)
from frigate.comms.recorded_webrtc import (
    GENERATION_BYTES,
    WARMUP_ITEM_MS,
    WARMUP_SECONDS,
    RecordedMedia,
    RecordedWebRTCManager,
    ScheduledItem,
    ScheduledTransport,
    load_sources,
)
from frigate.config.live_webrtc import LiveWebRTCConfig
from frigate.record.lease_async import AsyncRecordingLease
from frigate.record.playback import SourceClip
from frigate.test.test_live_webrtc import FakeTransport
from frigate.util.ovenmediaengine_bootstrap import generate_runtime, server_xml


class ReaderLease(AsyncRecordingLease):
    """Keep the real async reader joining logic, replacing only database writes."""

    instances = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.released = False
        self.__class__.instances.append(self)

    def _operation(self, operation, *args, **kwargs):
        async def completed():
            return None

        return asyncio.create_task(completed())

    async def __aexit__(self, *args):
        await super().__aexit__(*args)
        self.released = True


class FakeScheduledTransport:
    def __init__(self):
        self.created = []
        self.deleted = []
        self.create_error = False
        self.delete_error = False
        self.gate = None
        self.entered = asyncio.Event()
        self.current = {}
        self.activated = []
        self.activate_gate = None
        self.activate_error = False
        self.activate_entered = asyncio.Event()

    async def create(self, media):
        media.attempted = True
        self.created.append(media)
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.create_error:
            raise LiveWebRTCError(503, "recorded_webrtc_engine_unavailable")
        media.confirmed = True
        self.set_program(media, warming=True)

    def set_program(self, media, *, warming):
        selected = media.items[0]
        self.current[media.group.stream] = {
            "currentProgram": {
                "name": media.warmup_name if warming else media.playback_name,
                "currentItem": {
                    "url": selected.url,
                    "start": selected.offset_ms,
                    "duration": min(WARMUP_ITEM_MS, selected.duration_ms)
                    if warming
                    else selected.duration_ms,
                    "currentPosition": 100 if warming else 1000,
                },
            }
        }

    async def activate(self, media):
        media.attempted_at = time.monotonic()
        media.confirmed = False
        self.activate_entered.set()
        if self.activate_gate is not None:
            await self.activate_gate.wait()
        if self.activate_error:
            raise LiveWebRTCError(503, "recorded_webrtc_activation_not_ready")
        self.set_program(media, warming=False)
        media.confirmed = True
        self.activated.append(media)

    async def delete(self, media):
        if self.delete_error:
            raise LiveWebRTCError(503, "recorded_webrtc_cleanup_pending")
        self.deleted.append(media)
        self.current.pop(media.group.stream, None)

    async def state(self, group):
        return self.current.get(group.stream)


class TestRecordedBodies(unittest.TestCase):
    def test_bounds_and_untrusted_inputs(self):
        base = {"after": 100, "before": 120, "start_timestamp": 105}
        for update in (
            {"before": 131},
            {"before": 100},
            {"start_timestamp": 120},
            {"after": float("nan")},
            {"before": float("inf")},
            {"path": "/private/source.mp4"},
            {"url": "file://secret"},
            {"ffmpeg": "-i untrusted"},
            {"max_height": 9999},
        ):
            with self.subTest(update=update), self.assertRaises(ValidationError):
                RecordedCreate(**{**base, **update})
        for body in (
            {"generation": 1, "action": "seek"},
            {"generation": 1, "action": "pause", "window": base},
            {"generation": True, "action": "pause"},
            {"generation": 0, "action": "resume"},
            {"generation": 1, "action": "speed"},
            {"generation": 1, "action": "activate"},
            {"generation": 1, "action": "activate", "decoded_ready": False},
            {"generation": 1, "action": "activate", "decoded_ready": "true"},
            {"generation": 1, "action": "pause", "decoded_ready": True},
        ):
            with self.subTest(body=body), self.assertRaises(ValidationError):
                RecordedControl(**body)
        with self.assertRaises(ValidationError):
            RecordedAnswer(generation=1, sdp="x" * 128001)
        with self.assertRaises(ValidationError):
            RecordedCandidates(
                generation=1,
                candidates=[{"candidate": "x\nprivate", "sdpMLineIndex": 0}],
            )

    def test_every_app_has_a_separate_private_schedule_directory(self):
        settings = LiveWebRTCConfig(enabled=True, advertised_hosts=["10.1.1.4"])
        root = ET.fromstring(server_xml(settings, "synthetic-only"))
        directories = []
        for app in root.findall(".//Application"):
            schedule = app.find("Providers/Schedule")
            if app.findtext("Name").startswith("frigate_detail"):
                self.assertIsNotNone(schedule)
                directories.append(schedule.findtext("ScheduleFilesDir"))
                self.assertIn(app.findtext("Name"), schedule.findtext("MediaRootDir"))
        self.assertEqual(len(directories), len(set(directories)))
        self.assertNotIn("<ScheduledChannel>", ET.tostring(root, encoding="unicode"))

    def test_catalog_planner_preserves_main_archive_and_sub_selection(self):
        window = RecordedWindow(
            after=100, before=120, start_timestamp=105, stream="main"
        )
        row = SimpleNamespace(
            path="/archive/main.mp4",
            start_time=100,
            has_audio=False,
        )
        plan = SimpleNamespace(skipped=False, clip_from_ms=0, duration_ms=20000)
        database = SimpleNamespace(is_closed=lambda: True, close=lambda: None)
        with (
            patch("frigate.comms.recorded_webrtc.Recordings") as recordings,
            patch(
                "frigate.record.playback.resolve_coverage", return_value=["interval"]
            ) as coverage,
            patch(
                "frigate.record.playback.build_spans",
                return_value=[(row, 100, 120, None)],
            ) as spans,
            patch("frigate.record.playback.plan_clip", return_value=plan),
        ):
            recordings._meta.database = database
            result = load_sources("front", window)
        coverage.assert_called_once_with("front", 100, 120)
        spans.assert_called_once_with(["interval"], "main")
        self.assertEqual(result[0].path, "/archive/main.mp4")
        self.assertFalse(result[0].has_audio)


class TestRecordedWebRTCManager(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = Path.cwd() / (".recorded-webrtc-test-" + uuid4().hex)
        self.directory.mkdir(mode=0o700)
        self.addCleanup(shutil.rmtree, self.directory)
        self.now = 1000.0
        self.settings = LiveWebRTCConfig(
            enabled=True,
            advertised_hosts=["10.1.1.4"],
            max_detail_cameras=2,
            max_viewers=4,
        )
        self.config = SimpleNamespace(
            ui=SimpleNamespace(recorded_http=False),
            live_webrtc=self.settings,
            cameras={
                name: SimpleNamespace(
                    enabled=True,
                    live=SimpleNamespace(transcode=SimpleNamespace(source=name)),
                )
                for name in ("front", "garden")
            },
            go2rtc=SimpleNamespace(
                streams={"front": ["synthetic"], "garden": ["synthetic"]}
            ),
        )
        generate_runtime(self.settings, self.directory)
        self.live_transport = FakeTransport()
        self.live = LiveWebRTCManager(
            lambda: self.config,
            runtime=self.directory,
            clock=lambda: self.now,
            transport=self.live_transport,
        )
        await self.live.start()
        self.native = FakeScheduledTransport()
        self.original = self.directory / "original.mp4"
        self.original.write_bytes(b"synthetic immutable original")
        self.clips = [SourceClip(str(self.original), 100, 20, 0, True, 100, 120)]
        self.authorized = True
        self.manager = RecordedWebRTCManager(
            self.live,
            transport=self.native,
            planner=lambda *_: self.clips,
            lease_factory=ReaderLease,
            account_check=lambda *_: self.authorized,
        )
        await self.manager.start()
        self.addAsyncCleanup(self.live.close)
        self.addAsyncCleanup(self.manager.close)
        self.body = RecordedCreate(after=100, before=120, start_timestamp=105)

    async def create(self, **updates):
        return await self.manager.create(
            "front",
            "alice",
            self.body.model_copy(update=updates),
            2000,
        )

    async def activate(self, result):
        identifier = result["session_id"]
        generation = result["generation"]
        await self.manager.answer(
            "front",
            "alice",
            identifier,
            RecordedAnswer(generation=generation, sdp="v=0\r\nsynthetic-only\r\n"),
        )
        return await self.manager.control(
            "front",
            "alice",
            identifier,
            RecordedControl(
                generation=generation, action="activate", decoded_ready=True
            ),
        )

    async def test_offer_mapping_bounded_copy_and_original_hash_unchanged(self):
        before = hashlib.sha256(self.original.read_bytes()).digest()
        result = await self.create()
        RecordedWebRTCSession.model_validate(result)
        self.assertEqual(result["generation"], 1)
        self.assertTrue(result["renegotiate"])
        self.assertEqual(result["position_kind"], "input_read_cursor")
        self.assertIsNone(result["position_timestamp"])
        self.assertEqual(
            result["timeline"],
            [
                {
                    "source_start": 105,
                    "source_end": 120,
                    "media_start": 0,
                    "media_end": 15,
                }
            ],
        )
        media = self.native.created[0]
        self.assertEqual(media.items[0].offset_ms, 5000)
        self.assertEqual(media.items[0].duration_ms, 15000)
        self.assertNotIn(str(self.directory), json.dumps(result))
        self.assertNotIn("file://", json.dumps(result))
        self.assertTrue(ReaderLease.instances[-1].released)
        await self.manager.delete("front", "alice", result["session_id"])
        self.assertEqual(hashlib.sha256(self.original.read_bytes()).digest(), before)
        self.assertFalse(media.directory.exists())
        self.assertFalse(self.live.external_viewers)
        self.assertFalse(self.live.external_encoders)

    async def test_absent_or_heterogeneous_audio_does_not_remove_video(self):
        self.clips = [
            SourceClip(str(self.original), 100, 10, 0, True, 100, 110),
            SourceClip(str(self.original), 110, 10, 0, None, 110, 120),
        ]
        result = await self.create()
        self.assertFalse(result["audio"])
        self.assertEqual(
            result["audio_reason"], "source_audio_absent_heterogeneous_or_unknown"
        )
        self.assertTrue(self.native.created[0].group.app.endswith("_0"))
        self.assertEqual(len(result["timeline"]), 2)

    async def test_gaps_are_skipped_and_timeline_is_not_fabricated(self):
        self.clips = [
            SourceClip(str(self.original), 100, 9, 0, False, 100, 109),
            SourceClip(str(self.original), 112, 8, 0, False, 112, 120),
        ]
        result = await self.create()
        self.assertEqual(result["gap_policy"], "skip")
        self.assertEqual(result["timeline"][1]["media_start"], 4)
        self.assertEqual(result["timeline"][1]["source_start"], 112)

    async def test_start_in_gap_fails_without_native_channel_or_leaked_slots(self):
        self.clips = [SourceClip(str(self.original), 110, 10, 0, True, 110, 120)]
        with self.assertRaises(LiveWebRTCError) as raised:
            await self.create()
        self.assertEqual(raised.exception.code, "playback_start_in_gap")
        self.assertFalse(self.native.created)
        self.assertFalse(self.manager.sessions)
        self.assertFalse(self.manager.generations)
        self.assertFalse(self.live.external_viewers)
        self.assertFalse(self.live.external_encoders)

    async def test_copy_capacity_is_enforced_on_actual_file_size(self):
        with self.original.open("wb") as source:
            source.truncate(GENERATION_BYTES + 1)
        with self.assertRaises(LiveWebRTCError) as raised:
            await self.create()
        self.assertEqual(raised.exception.status, 507)
        self.assertFalse(self.native.created)
        self.assertFalse(self.manager.generations)

    async def test_shared_detail_encoder_limits_work_in_both_directions(self):
        self.settings.max_detail_cameras = 1
        # Match the configured snapshot, just as a fresh runtime would.
        self.live.settings = self.settings.model_copy(deep=True)
        live = await self.live.create("front", "alice", ViewerBody(), 2000)
        with self.assertRaises(LiveWebRTCError) as raised:
            await self.create()
        self.assertEqual(raised.exception.status, 429)
        await self.live.delete("front", "alice", live["session_id"])
        await self.create()
        with self.assertRaises(LiveWebRTCError) as raised:
            await self.live.create("front", "alice", ViewerBody(), 2000)
        self.assertEqual(raised.exception.status, 429)

    async def test_paused_viewer_consumes_viewer_slot_but_no_encoder(self):
        self.settings.max_viewers = 1
        self.live.settings = self.settings.model_copy(deep=True)
        result = await self.create()
        paused = await self.manager.control(
            "front",
            "alice",
            result["session_id"],
            RecordedControl(generation=1, action="pause"),
        )
        self.assertEqual(paused["state"], "paused")
        self.assertFalse(self.live.external_encoders)
        self.assertEqual(len(self.live.external_viewers), 1)
        with self.assertRaises(LiveWebRTCError) as raised:
            await self.live.create("front", "alice", ViewerBody(view="grid"), 2000)
        self.assertEqual(raised.exception.status, 429)

    async def test_pause_resume_has_new_generation_and_exact_native_cursor_mapping(
        self,
    ):
        result = await self.create()
        identifier = result["session_id"]
        first = self.native.created[0]
        await self.activate(result)
        paused = await self.manager.control(
            "front", "alice", identifier, RecordedControl(generation=1, action="pause")
        )
        self.assertEqual(paused["generation"], 2)
        self.assertEqual(paused["position_timestamp"], 106)
        self.assertFalse(paused["renegotiate"])
        self.assertIsNone(paused["offer"])
        self.assertFalse(first.directory.exists())
        resumed = await self.manager.control(
            "front", "alice", identifier, RecordedControl(generation=2, action="resume")
        )
        self.assertEqual(resumed["generation"], 3)
        self.assertEqual(resumed["window"]["start_timestamp"], 106)
        self.assertTrue(resumed["renegotiate"])
        self.assertNotEqual(first.group.stream, self.native.created[-1].group.stream)

    async def test_pause_refuses_to_fake_an_unknown_playhead(self):
        result = await self.create()
        self.native.current.clear()
        with self.assertRaises(LiveWebRTCError) as raised:
            await self.manager.control(
                "front",
                "alice",
                result["session_id"],
                RecordedControl(generation=1, action="pause"),
            )
        self.assertEqual(raised.exception.code, "recorded_webrtc_cursor_unavailable")
        self.assertEqual(self.manager.sessions[result["session_id"]].generation, 1)
        self.assertFalse(self.native.deleted)

    async def test_seek_rejects_stale_sdp_ice_quality_and_racing_seek(self):
        result = await self.create()
        identifier = result["session_id"]
        old_socket = self.native.created[0].peer.socket
        seek = RecordedControl(
            generation=1,
            action="seek",
            window=RecordedWindow(after=100, before=120, start_timestamp=110),
        )
        results = await asyncio.gather(
            self.manager.control("front", "alice", identifier, seek),
            self.manager.control("front", "alice", identifier, seek),
            return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(item, dict) for item in results), 1)
        self.assertEqual(
            [item.code for item in results if isinstance(item, LiveWebRTCError)],
            ["recorded_webrtc_stale_generation"],
        )
        for method, body in (
            (
                self.manager.answer,
                RecordedAnswer(generation=1, sdp="v=0\r\nsynthetic-only\r\n"),
            ),
            (self.manager.candidates, RecordedCandidates(generation=1, candidates=[])),
            (self.manager.quality, RecordedQuality(generation=1, height=720)),
        ):
            with self.assertRaises(LiveWebRTCError) as raised:
                await method("front", "alice", identifier, body)
            self.assertEqual(raised.exception.code, "recorded_webrtc_stale_generation")
        self.assertTrue(old_socket.closed)
        self.assertEqual(self.manager.sessions[identifier].generation, 2)

    async def test_answer_candidates_and_quality_keep_the_same_peer(self):
        result = await self.create()
        identifier = result["session_id"]
        await self.manager.answer(
            "front",
            "alice",
            identifier,
            RecordedAnswer(generation=1, sdp="v=0\r\nsynthetic-only\r\n"),
        )
        await self.manager.candidates(
            "front",
            "alice",
            identifier,
            RecordedCandidates(generation=1, candidates=[]),
        )
        await self.manager.quality(
            "front", "alice", identifier, RecordedQuality(generation=1, height=720)
        )
        messages = self.native.created[0].peer.socket.messages
        self.assertNotIn("generation", messages[0]["sdp"])
        self.assertEqual(messages[-1]["rendition_name"], "720p")
        self.assertEqual(len(self.native.created), 1)

    async def test_ownership_expiry_revocation_and_config_change_cleanup(self):
        result = await self.create()
        for camera, owner in (("garden", "alice"), ("front", "bob")):
            with self.assertRaises(LiveWebRTCError) as raised:
                await self.manager.renew(camera, owner, result["session_id"])
            self.assertEqual(raised.exception.status, 404)
        self.authorized = False
        await self.manager._maintain_once()
        self.assertFalse(self.manager.sessions)
        self.authorized = True
        await self.create()
        camera = self.config.cameras.pop("front")
        await self.manager._maintain_once()
        self.assertFalse(self.manager.sessions)
        self.config.cameras["front"] = camera
        await self.create()
        self.now += self.settings.lease_seconds + 1
        await self.manager._maintain_once()
        self.assertFalse(self.manager.sessions)
        self.assertFalse(self.live.external_encoders)

    async def test_disabled_camera_without_live_configuration_can_play_existing_media(
        self,
    ):
        self.config.cameras["front"] = SimpleNamespace(enabled=False)
        self.config.go2rtc.streams.clear()
        result = await self.create()
        await self.activate(result)
        await asyncio.wait_for(self.manager._maintain_once(), timeout=2)
        renewed = await self.manager.renew("front", "alice", result["session_id"])
        self.assertEqual(renewed["state"], "playing")
        paused = await self.manager.control(
            "front",
            "alice",
            result["session_id"],
            RecordedControl(generation=1, action="pause"),
        )
        self.assertEqual(paused["state"], "paused")
        resumed = await self.manager.control(
            "front",
            "alice",
            result["session_id"],
            RecordedControl(generation=2, action="resume"),
        )
        self.assertEqual(resumed["state"], "warming")
        await self.manager.delete("front", "alice", result["session_id"])
        self.assertFalse(self.live.external_encoders)
        self.assertFalse(self.live.external_viewers)

    async def test_disabling_current_capture_does_not_close_recorded_viewer(self):
        result = await self.create()
        await self.activate(result)
        active = self.manager.sessions[result["session_id"]].active
        self.config.cameras["front"].enabled = False
        self.config.go2rtc.streams.clear()
        await asyncio.wait_for(self.manager._maintain_once(), timeout=2)
        self.assertIs(self.manager.sessions[result["session_id"]].active, active)
        self.assertFalse(active.peer.socket.closed)
        self.assertEqual(
            (await self.manager.renew("front", "alice", result["session_id"]))["state"],
            "playing",
        )

    async def test_heartbeat_cannot_extend_jwt_hard_expiry(self):
        result = await self.manager.create("front", "alice", self.body, 1010)
        self.now = 1005
        renewed = await self.manager.renew("front", "alice", result["session_id"])
        self.assertEqual(renewed["expires_at"], 1010)
        self.now = 1011
        await self.manager._maintain_once()
        self.assertFalse(self.manager.sessions)

    async def test_uncertain_create_and_delete_keep_budget_until_cleanup_succeeds(self):
        self.native.create_error = self.native.delete_error = True
        with self.assertRaises(LiveWebRTCError):
            await self.create()
        self.assertFalse(self.manager.sessions)
        self.assertEqual(len(self.manager.generations), 1)
        self.assertEqual(len(self.live.external_encoders), 1)
        self.assertFalse(self.live.external_viewers)
        media = next(iter(self.manager.generations.values()))
        self.assertTrue(media.directory.exists())
        self.native.delete_error = False
        await self.manager._maintain_once()
        self.assertFalse(media.directory.exists())
        self.assertFalse(self.live.external_encoders)

    async def test_maintenance_retries_orphans_without_disconnecting_active_peer(self):
        result = await self.create()
        active = self.manager.sessions[result["session_id"]].active
        self.native.create_error = self.native.delete_error = True
        with self.assertRaises(LiveWebRTCError):
            await self.create()
        orphan = next(
            media for media in self.manager.generations.values() if media.orphaned
        )
        self.assertIsNone(orphan.peer)
        self.assertIsNone(orphan.watcher)
        await asyncio.wait_for(self.manager._maintain_once(), timeout=2)
        self.assertTrue(orphan.directory.exists())
        self.assertIn(orphan.group.stream, self.manager.generations)
        self.assertIn(orphan.group.stream, self.live.external_encoders)
        self.native.create_error = self.native.delete_error = False
        await asyncio.wait_for(self.manager._maintain_once(), timeout=2)
        self.assertFalse(orphan.directory.exists())
        self.assertNotIn(orphan.group.stream, self.manager.generations)
        self.assertNotIn(orphan.group.stream, self.live.external_encoders)
        self.assertIs(self.manager.sessions[result["session_id"]].active, active)
        self.assertFalse(active.peer.socket.closed)
        self.assertFalse(active.watcher.done())
        self.assertEqual(self.live.external_encoders, {active.group.stream})
        self.assertEqual(self.native.deleted, [orphan])

    async def test_create_cancellation_removes_uncertain_channel(self):
        self.native.gate = asyncio.Event()
        work = asyncio.create_task(self.create())
        await self.native.entered.wait()
        work.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await work
        self.assertEqual(len(self.native.deleted), 1)
        self.assertFalse(self.manager.sessions)
        self.assertFalse(self.manager.generations)
        self.assertFalse(self.live.external_viewers)
        self.assertFalse(self.live.external_encoders)

    async def test_reader_thread_is_joined_before_original_lease_release(self):
        entered, release, finished = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )

        def planner(*_):
            entered.set()
            release.wait(5)
            finished.set()
            return self.clips

        self.manager.planner = planner
        work = asyncio.create_task(self.create())
        await asyncio.to_thread(entered.wait, 5)
        work.cancel()
        await asyncio.sleep(0.02)
        lease = ReaderLease.instances[-1]
        self.assertFalse(work.done())
        self.assertFalse(lease.released)
        self.assertFalse(finished.is_set())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await work
        self.assertTrue(finished.is_set())
        self.assertTrue(lease.released)
        self.assertFalse(self.manager.generations)

    async def test_peer_disconnect_ends_only_its_current_generation(self):
        result = await self.create()
        socket = self.native.created[0].peer.socket
        await socket.close()
        for _ in range(20):
            if (
                self.manager.sessions[result["session_id"]].state == "ended"
                and not self.live.external_encoders
            ):
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.manager.sessions[result["session_id"]].state, "ended")
        self.assertFalse(self.live.external_encoders)

    async def test_cancelled_seek_does_not_leave_its_replacement_encoder(self):
        result = await self.create()
        self.native.gate = asyncio.Event()
        self.native.entered.clear()
        work = asyncio.create_task(
            self.manager.control(
                "front",
                "alice",
                result["session_id"],
                RecordedControl(
                    generation=1,
                    action="seek",
                    window=RecordedWindow(after=100, before=120, start_timestamp=110),
                ),
            )
        )
        await self.native.entered.wait()
        work.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await work
        self.assertFalse(self.live.external_encoders)
        self.assertEqual(self.manager.sessions[result["session_id"]].state, "paused")
        self.assertEqual(self.manager.sessions[result["session_id"]].generation, 2)

    async def test_pause_cleanup_failure_is_not_advertised_as_a_successful_pause(self):
        result = await self.create()
        self.native.delete_error = True
        with self.assertRaises(LiveWebRTCError):
            await self.manager.control(
                "front",
                "alice",
                result["session_id"],
                RecordedControl(generation=1, action="pause"),
            )
        state = await self.manager.renew("front", "alice", result["session_id"])
        self.assertEqual(state["state"], "cleanup_pending")
        self.assertTrue(self.live.external_encoders)
        self.native.delete_error = False
        await self.manager._maintain_once()
        state = await self.manager.renew("front", "alice", result["session_id"])
        self.assertEqual(state["state"], "paused")
        self.assertFalse(self.live.external_encoders)

    async def test_restart_recovers_only_private_server_generated_channels(self):
        await self.manager.close()
        stale = self.manager.root / "frigate_detail_1080_0" / ("fr_" + "b" * 32)
        stale.mkdir()
        (stale / "source-0.mp4").write_bytes(b"synthetic stale copy")
        foreign = stale.parent / "not-a-server-stream"
        foreign.mkdir()
        restarted = RecordedWebRTCManager(
            self.live,
            transport=self.native,
            planner=lambda *_: self.clips,
            lease_factory=ReaderLease,
            account_check=lambda *_: True,
        )
        self.addAsyncCleanup(restarted.close)
        await restarted.start()
        self.assertTrue(restarted.available())
        self.assertFalse(stale.exists())
        self.assertTrue(foreign.exists())
        self.assertFalse(self.live.external_encoders)

    async def test_configuration_change_closes_active_and_paused_leases(self):
        first = await self.create()
        await self.manager.control(
            "front",
            "alice",
            first["session_id"],
            RecordedControl(generation=1, action="pause"),
        )
        await self.create()
        self.config.live_webrtc = self.settings.model_copy(update={"detail_fps": 20})
        await self.manager._maintain_once()
        self.assertFalse(self.manager.sessions)
        self.assertFalse(self.live.external_viewers)
        self.assertFalse(self.live.external_encoders)

    async def test_finite_window_releases_encoding_without_faking_end_position(self):
        result = await self.create()
        await self.activate(result)
        self.now += 16 + 15
        session = self.manager.sessions[result["session_id"]]
        session.deadline = 1100
        await self.manager._maintain_once()
        self.assertEqual(session.state, "ended")
        self.assertIsNone(session.position)
        self.assertFalse(self.live.external_encoders)

    async def test_answer_does_not_consume_window_until_decoded_ready_activation(self):
        result = await self.create()
        identifier = result["session_id"]
        media = self.native.created[0]
        peer = media.peer
        self.assertEqual(result["state"], "warming")
        self.assertEqual(result["warmup_expires_at"], self.now + WARMUP_SECONDS)
        await self.manager.answer(
            "front",
            "alice",
            identifier,
            RecordedAnswer(generation=1, sdp="v=0\r\nsynthetic-only\r\n"),
        )
        self.assertEqual(self.manager.sessions[identifier].state, "warming")
        self.assertEqual(media.stop_at, 0)
        self.assertFalse(self.native.activated)
        self.now += 3
        activated = await self.manager.control(
            "front",
            "alice",
            identifier,
            RecordedControl(generation=1, action="activate", decoded_ready=True),
        )
        self.assertEqual(activated["state"], "playing")
        self.assertIsNone(activated["warmup_expires_at"])
        self.assertEqual(activated["generation"], 1)
        self.assertFalse(activated["renegotiate"])
        self.assertIsNone(activated["offer"])
        self.assertIs(media.peer, peer)
        self.assertTrue(media.audio)
        self.assertEqual(media.stop_at, self.now + 15 + 15)
        repeated = await self.manager.control(
            "front",
            "alice",
            identifier,
            RecordedControl(generation=1, action="activate", decoded_ready=True),
        )
        self.assertEqual(repeated["generation"], 1)
        self.assertEqual(len(self.native.activated), 1)

    async def test_decoded_activation_requires_answer_and_current_generation(self):
        result = await self.create()
        with self.assertRaises(LiveWebRTCError) as raised:
            await self.manager.control(
                "front",
                "alice",
                result["session_id"],
                RecordedControl(generation=1, action="activate", decoded_ready=True),
            )
        self.assertEqual(raised.exception.code, "recorded_webrtc_answer_required")
        await self.manager.control(
            "front",
            "alice",
            result["session_id"],
            RecordedControl(
                generation=1,
                action="seek",
                window=RecordedWindow(after=100, before=120, start_timestamp=110),
            ),
        )
        with self.assertRaises(LiveWebRTCError) as raised:
            await self.manager.control(
                "front",
                "alice",
                result["session_id"],
                RecordedControl(generation=1, action="activate", decoded_ready=True),
            )
        self.assertEqual(raised.exception.code, "recorded_webrtc_stale_generation")
        self.assertFalse(self.native.activated)

    async def test_warmup_deadline_is_not_renewed_by_heartbeat(self):
        result = await self.create()
        self.now += WARMUP_SECONDS - 1
        renewed = await self.manager.renew("front", "alice", result["session_id"])
        self.assertEqual(renewed["warmup_expires_at"], result["warmup_expires_at"])
        self.assertGreater(renewed["expires_at"], renewed["warmup_expires_at"])
        self.now += 2
        with self.assertRaises(LiveWebRTCError) as raised:
            await self.manager.control(
                "front",
                "alice",
                result["session_id"],
                RecordedControl(generation=1, action="activate", decoded_ready=True),
            )
        self.assertEqual(raised.exception.code, "recorded_webrtc_warmup_expired")
        await self.manager._maintain_once()
        self.assertEqual(self.manager.sessions[result["session_id"]].state, "ended")
        self.assertFalse(self.live.external_encoders)
        self.assertFalse(self.native.activated)

    async def test_cursor_requires_matching_native_program_in_each_phase(self):
        result = await self.create()
        identifier = result["session_id"]
        media = self.native.created[0]
        warming = await self.manager.renew("front", "alice", identifier)
        self.assertAlmostEqual(warming["position_timestamp"], 105.1)
        await self.activate(result)
        playing = await self.manager.renew("front", "alice", identifier)
        self.assertEqual(playing["position_timestamp"], 106)
        native_program = self.native.current[media.group.stream]["currentProgram"]
        native_program["name"] = media.warmup_name
        stale = await self.manager.renew("front", "alice", identifier)
        self.assertIsNone(stale["position_timestamp"])

    async def test_uncertain_patch_cancellation_retains_cache_and_encoder_reservation(
        self,
    ):
        result = await self.create()
        identifier = result["session_id"]
        await self.manager.answer(
            "front",
            "alice",
            identifier,
            RecordedAnswer(generation=1, sdp="v=0\r\nsynthetic-only\r\n"),
        )
        self.native.activate_gate = asyncio.Event()
        self.native.delete_error = True
        work = asyncio.create_task(
            self.manager.control(
                "front",
                "alice",
                identifier,
                RecordedControl(generation=1, action="activate", decoded_ready=True),
            )
        )
        await self.native.activate_entered.wait()
        media = self.native.created[0]
        work.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await work
        self.assertFalse(media.confirmed)
        self.assertGreater(media.attempted_at, 0)
        self.assertTrue(media.directory.exists())
        self.assertTrue(self.live.external_encoders)
        self.assertEqual(self.manager.sessions[identifier].state, "cleanup_pending")
        self.native.delete_error = False
        await self.manager._maintain_once()
        renewed = await self.manager.renew("front", "alice", identifier)
        self.assertEqual(renewed["state"], "ended")
        self.assertFalse(self.live.external_encoders)
        self.assertFalse(media.directory.exists())

    async def test_activation_and_seek_are_serialized_without_changing_activation_peer(
        self,
    ):
        result = await self.create()
        identifier = result["session_id"]
        await self.manager.answer(
            "front",
            "alice",
            identifier,
            RecordedAnswer(generation=1, sdp="v=0\r\nsynthetic-only\r\n"),
        )
        self.native.activate_gate = asyncio.Event()
        activate = asyncio.create_task(
            self.manager.control(
                "front",
                "alice",
                identifier,
                RecordedControl(generation=1, action="activate", decoded_ready=True),
            )
        )
        await self.native.activate_entered.wait()
        seek = asyncio.create_task(
            self.manager.control(
                "front",
                "alice",
                identifier,
                RecordedControl(
                    generation=1,
                    action="seek",
                    window=RecordedWindow(after=100, before=120, start_timestamp=110),
                ),
            )
        )
        await asyncio.sleep(0)
        self.assertFalse(seek.done())
        self.native.activate_gate.set()
        activated, sought = await asyncio.gather(activate, seek)
        self.assertEqual(activated["generation"], 1)
        self.assertEqual(activated["state"], "playing")
        self.assertEqual(sought["generation"], 2)
        self.assertEqual(sought["state"], "warming")
        self.assertEqual(len(self.native.activated), 1)
        self.assertTrue(self.native.created[0].peer.socket.closed)

    async def test_failed_activation_stops_repeating_input_instead_of_claiming_playback(
        self,
    ):
        result = await self.create()
        self.native.activate_error = True
        with self.assertRaises(LiveWebRTCError):
            await self.activate(result)
        self.assertEqual(self.manager.sessions[result["session_id"]].state, "ended")
        self.assertFalse(self.live.external_encoders)
        self.assertFalse(self.manager.generations)


class TestScheduledTransport(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = Path.cwd() / (".recorded-transport-test-" + uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.live = SimpleNamespace(
            runtime=self.root,
            transport=SimpleNamespace(
                stream_url=lambda group: f"http://loopback/{group.app}/streams"
            ),
        )
        self.transport = ScheduledTransport(self.live)
        self.media = RecordedMedia(
            MediaGroup("front", "frigate_detail_1080_0", "fr_" + "a" * 32, "detail"),
            self.root / ("fr_" + "a" * 32),
            items=[
                ScheduledItem("file://private/source-0.mp4", 5000, 10000, 105, 115, 0)
            ],
        )

    async def test_native_warmup_repeats_only_first_item_and_waits_for_real_stream(
        self,
    ):
        calls = []

        async def request(method, url, **kwargs):
            calls.append((method, url, kwargs))
            if method == "GET":
                return {} if len(calls) >= 3 else None

        self.transport.request = request
        await self.transport.create(self.media)
        payload = calls[0][2]["payload"]
        self.assertFalse(payload["stream"]["bypassTranscoder"])
        self.assertFalse(payload["stream"]["audioTrack"])
        program = payload["programs"][0]
        self.assertTrue(program["repeat"])
        self.assertEqual(program["name"], self.media.warmup_name)
        self.assertEqual(program["items"][0]["start"], 5000)
        self.assertEqual(program["items"][0]["duration"], 200)
        self.assertTrue(self.media.attempted)
        self.assertEqual(len(calls), 3)

    async def test_activation_patches_only_programs_and_waits_for_unique_program_name(
        self,
    ):
        calls = []
        self.media.audio = True
        self.media.confirmed = True

        async def request(method, url, **kwargs):
            calls.append((method, url, kwargs))
            if method == "GET":
                name = (
                    self.media.warmup_name
                    if len(calls) == 2
                    else self.media.playback_name
                )
                return {"currentProgram": {"name": name}}

        self.transport.request = request
        await self.transport.activate(self.media)
        self.assertEqual(calls[0][0], "PATCH")
        payload = calls[0][2]["payload"]
        self.assertEqual(set(payload), {"programs"})
        program = payload["programs"][0]
        self.assertFalse(program["repeat"])
        self.assertEqual(program["name"], self.media.playback_name)
        self.assertNotEqual(program["name"], self.media.warmup_name)
        self.assertEqual(program["items"][0]["start"], 5000)
        self.assertEqual(program["items"][0]["duration"], 10000)
        self.assertEqual(len(calls), 3)
        self.assertTrue(self.media.audio)
        self.assertTrue(self.media.confirmed)
        self.assertGreater(self.media.attempted_at, 0)

    async def test_uncertain_patch_invalidates_prior_create_confirmation(self):
        self.media.confirmed = True
        self.media.attempted_at = 1

        async def unavailable(*args, **kwargs):
            raise LiveWebRTCError(503, "recorded_webrtc_engine_unavailable")

        self.transport.request = unavailable
        with self.assertRaises(LiveWebRTCError):
            await self.transport.activate(self.media)
        self.assertFalse(self.media.confirmed)
        self.assertGreater(self.media.attempted_at, 1)

    async def test_uncertain_patch_keeps_cleanup_pending_during_late_write_grace(self):
        self.media.confirmed = False
        self.media.attempted_at = 100
        ticks = iter(range(103, 1000, 3))

        async def missing(*args, **kwargs):
            return None

        self.transport.request = missing
        with (
            patch(
                "frigate.comms.recorded_webrtc.time",
                SimpleNamespace(monotonic=lambda: next(ticks)),
            ),
            patch("frigate.comms.recorded_webrtc.asyncio.sleep", new=AsyncMock()),
        ):
            with self.assertRaises(LiveWebRTCError) as raised:
                await self.transport.delete(self.media)
            self.assertEqual(raised.exception.code, "recorded_webrtc_cleanup_pending")
            self.media.attempted_at = -100
            await self.transport.delete(self.media)

    async def test_delete_waits_for_stream_and_schedule_absence(self):
        schedule = (
            self.root
            / "schedules"
            / self.media.group.app
            / (self.media.group.stream + ".sch")
        )
        schedule.parent.mkdir(parents=True)
        schedule.write_text("synthetic private schedule")
        calls = []

        async def request(method, url, **kwargs):
            calls.append((method, url))
            return None

        self.transport.request = request
        await self.transport.delete(self.media)
        self.assertFalse(schedule.exists())
        self.assertTrue(any(method == "DELETE" for method, _ in calls))
        self.assertTrue(
            any(url.endswith("/streams/" + self.media.group.stream) for _, url in calls)
        )

    async def test_uncertain_schedule_file_is_removed_even_if_native_rest_is_down(self):
        schedule = (
            self.root
            / "schedules"
            / self.media.group.app
            / (self.media.group.stream + ".sch")
        )
        schedule.parent.mkdir(parents=True)
        schedule.write_text("synthetic private schedule")

        async def unavailable(*args, **kwargs):
            raise LiveWebRTCError(503, "recorded_webrtc_engine_unavailable")

        self.transport.request = unavailable
        with self.assertRaises(LiveWebRTCError):
            await self.transport.delete(self.media)
        self.assertFalse(schedule.exists())

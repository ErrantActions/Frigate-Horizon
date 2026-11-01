"""MP4 job, shared admission, revocation and cancellation regressions."""

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4


@unittest.skipIf(os.name == "nt", "Backend media/config imports require Linux resource")
class TestRecordedHTTPManager(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from frigate.api.defs.request.recorded_http_body import RecordedHTTPCreate
        from frigate.comms.live_webrtc import LiveWebRTCManager
        from frigate.comms.recorded_http import RecordedHTTPManager
        from frigate.config.live_webrtc import LiveWebRTCConfig
        from frigate.record.playback import SourceClip
        from frigate.test.test_live_webrtc import FakeTransport
        from frigate.test.test_recorded_webrtc import ReaderLease

        self.root = Path.cwd() / ".recorded-http-tests" / uuid4().hex
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root)
        self.revoked = False
        self.config = SimpleNamespace(
            live_webrtc=LiveWebRTCConfig(
                enabled=True,
                advertised_hosts=["10.1.1.4"],
                max_viewers=8,
                max_detail_cameras=2,
                max_height=1080,
            ),
            ui=SimpleNamespace(recorded_http=True, recording_compatibility=True),
            auth=SimpleNamespace(
                enabled=True, roles={"admin": []}, cookie_name="frigate_token"
            ),
            cameras={"front": SimpleNamespace(enabled=True)},
        )
        runtime = self.root / "runtime"
        runtime.mkdir()
        (runtime / "runtime.json").write_text(
            json.dumps(
                {
                    "settings_sha256": hashlib.sha256(
                        self.config.live_webrtc.model_dump_json().encode(),
                    ).hexdigest(),
                    "api_token": "synthetic-runtime-token-not-used-0000",
                    "turn_secret": "synthetic-runtime-secret-not-used-0000",
                }
            )
        )
        self.live = LiveWebRTCManager(
            lambda: self.config,
            runtime=runtime,
            transport=FakeTransport(),
        )
        await self.live.start()
        self.manager = RecordedHTTPManager(
            self.live,
            "unused",
            "unused",
            self.root / "cache",
            planner=lambda *_: [SourceClip("unused", 100, 20, 0, False, 100, 120)],
            builder=self.build,
            lease_factory=ReaderLease,
            account_check=lambda *_: not self.revoked,
        )
        await self.manager.start()
        self.addAsyncCleanup(self.live.close)
        self.addAsyncCleanup(self.manager.close)
        self.body = RecordedHTTPCreate(after=100, before=120, start_timestamp=105)

    def build(self, session, clips, directory, lease, ffmpeg, ffprobe):
        directory.mkdir()
        (directory / "media.mp4").write_bytes(b"0123456789" * 10)
        lease.check()
        return (
            [
                {
                    "source_start": 100,
                    "source_end": 120,
                    "media_start": 0,
                    "media_end": 20,
                }
            ],
            False,
            {"width": 1280, "height": 720},
            '"synthetic"',
        )

    async def create(self):
        result = await self.manager.create(
            "front",
            "alice",
            self.body,
            time.time() + 3600,
            auth_issued=time.time(),
        )
        self.assertEqual(result["state"], "preparing")
        session = self.manager.sessions[result["session_id"]]
        await session.task
        self.assertEqual(session.state, "ready", session.error)
        return session

    async def test_stable_window_mapping_and_polling_never_renew_hard_expiry(self):
        session = await self.create()
        first = self.manager.metadata(session)
        self.assertEqual(first["quality"], "720")
        self.assertEqual(first["timeline"][0]["source_start"], 100)
        self.assertEqual(first["media_url"], first["status_url"] + "/media.mp4")
        for _ in range(3):
            owned = await self.manager.owned("front", "alice", session.identifier)
            self.assertEqual(self.manager.metadata(owned), first)
        self.assertFalse(self.live.external_encoders)
        self.assertIn(session.identifier, self.live.external_viewers)
        await self.manager.delete("front", "alice", session.identifier)
        self.assertFalse(self.live.external_viewers)
        self.assertFalse(session.directory.exists())

    async def test_actual_current_account_password_role_and_deletion_revocation(self):
        from datetime import datetime

        from peewee import SqliteDatabase

        from frigate.comms.live_webrtc import LiveWebRTCError
        from frigate.models import User
        from frigate.record.access import recording_account_allows

        database = SqliteDatabase(
            ":memory:", check_same_thread=False, thread_safe=False
        )
        with database.bind_ctx([User]):
            database.create_tables([User])
            User.create(
                username="alice",
                password_hash="synthetic-only",
                role="admin",
                notification_tokens=[],
            )
            self.manager.account_check = recording_account_allows
            session = await self.create()
            await self.manager.owned("front", "alice", session.identifier)
            User.update(
                password_changed_at=datetime.fromtimestamp(
                    session.auth_issued + 10,
                )
            ).where(User.username == "alice").execute()
            with self.assertRaises(LiveWebRTCError) as raised:
                await self.manager.owned("front", "alice", session.identifier)
            self.assertEqual(raised.exception.status, 401)
            User.update(password_changed_at=None, role="unknown").execute()
            with self.assertRaises(LiveWebRTCError):
                await self.manager.owned("front", "alice", session.identifier)
            self.config.auth.roles["restricted"] = ["other"]
            User.update(role="restricted").execute()
            with self.assertRaises(LiveWebRTCError):
                await self.manager.owned("front", "alice", session.identifier)
            User.delete().execute()
            await self.manager._maintain_once()
            self.assertFalse(self.manager.sessions)
            self.assertFalse(self.live.external_viewers)
            database.close()

    async def test_hard_expiry_removes_cache_without_status_renewal(self):
        from frigate.comms.live_webrtc import LiveWebRTCError

        session = await self.create()
        session.deadline = self.live.clock() - 1
        with self.assertRaises(LiveWebRTCError) as raised:
            await self.manager.owned("front", "alice", session.identifier)
        self.assertEqual(raised.exception.status, 410)
        await self.manager._maintain_once()
        self.assertFalse(self.manager.sessions)
        self.assertFalse(session.directory.exists())
        self.assertFalse(self.live.external_viewers)

    async def test_current_revocation_ownership_and_policy_fail_closed(self):
        from frigate.comms.live_webrtc import LiveWebRTCError

        session = await self.create()
        for camera, owner in (("other", "alice"), ("front", "bob")):
            with self.assertRaises(LiveWebRTCError) as raised:
                await self.manager.owned(camera, owner, session.identifier)
            self.assertEqual(raised.exception.status, 404)
        self.revoked = True
        with self.assertRaises(LiveWebRTCError) as raised:
            await self.manager.owned("front", "alice", session.identifier)
        self.assertEqual(raised.exception.status, 401)
        self.revoked = False
        self.config.ui.recorded_http = False
        self.assertFalse(self.manager.available())
        self.assertTrue(self.live.available())
        with self.assertRaises(LiveWebRTCError) as raised:
            await self.manager.owned("front", "alice", session.identifier)
        self.assertEqual(raised.exception.status, 410)

    async def test_shared_viewer_and_encoder_capacity(self):
        from frigate.comms.live_webrtc import LiveWebRTCError

        for index in range(8):
            await self.live.reserve_external(f"live-viewer-{index}")
        with self.assertRaises(LiveWebRTCError) as raised:
            await self.create()
        self.assertEqual(raised.exception.status, 429)
        self.assertFalse(self.manager.sessions)
        for index in range(8):
            await self.live.release_external(f"live-viewer-{index}")
        for index in range(2):
            await self.live.reserve_external(f"live-encoder-{index}", encoder=True)
        result = await self.manager.create(
            "front",
            "alice",
            self.body,
            time.time() + 300,
            auth_issued=time.time(),
        )
        session = self.manager.sessions[result["session_id"]]
        await session.task
        self.assertEqual(session.state, "failed")
        self.assertEqual(session.error, "live_webrtc_encoding_capacity")
        self.assertFalse(session.directory.exists())

    async def test_https_scaling_is_independent_of_legacy_compatibility_flags(self):
        self.config.ui.recording_compatibility = False
        self.assertEqual(
            self.manager.qualities(), ["auto", "360", "720", "1080", "original"]
        )
        session = await self.create()
        self.assertEqual(session.quality, "720")
        self.assertEqual(session.state, "ready")
        self.assertFalse(self.live.external_encoders)

    async def test_forward_window_reports_effective_start_and_normal_empty_state(self):
        def gaps(session, clips, directory, lease, ffmpeg, ffprobe):
            result = self.build(session, clips, directory, lease, ffmpeg, ffprobe)
            return (
                [
                    {
                        "source_start": 100,
                        "source_end": 101.999,
                        "media_start": 0,
                        "media_end": 1.999,
                    },
                    {
                        "source_start": 105.005,
                        "source_end": 108,
                        "media_start": 1.999,
                        "media_end": 4.994,
                    },
                ],
                *result[1:],
            )

        self.manager.builder = gaps
        self.body = self.body.model_copy(
            update={
                "start_timestamp": 102.0001,
                "start_policy": "next",
            }
        )
        session = await self.create()
        metadata = self.manager.metadata(session)
        self.assertEqual(metadata["after"], 100)
        self.assertEqual(metadata["before"], 120)
        self.assertEqual(metadata["start_timestamp"], 102.0001)
        self.assertEqual(metadata["effective_start_timestamp"], 105.005)
        self.assertEqual(metadata["continuation_timestamp"], 120)
        self.assertEqual(metadata["next_timestamp"], 120)
        await self.manager.delete("front", "alice", session.identifier)
        for policy, state in (("next", "empty"), ("strict", "failed")):
            body = self.body.model_copy(
                update={"start_timestamp": 110, "start_policy": policy}
            )
            result = await self.manager.create(
                "front",
                "alice",
                body,
                time.time() + 300,
                auth_issued=time.time(),
            )
            session = self.manager.sessions[result["session_id"]]
            await session.task
            self.assertEqual(session.state, state)
            self.assertFalse(session.directory.exists())
            self.assertFalse(self.live.external_encoders)
            await self.manager.delete("front", "alice", session.identifier)

    async def test_hour_continuation_uses_requested_boundary_not_rounded_video_tail(
        self,
    ):
        from frigate.api.defs.request.recorded_http_body import RecordedHTTPCreate

        self.body = RecordedHTTPCreate(
            after=1793555980,
            before=1793556000,
            start_timestamp=1793555990,
        )

        def rounded(session, clips, directory, lease, ffmpeg, ffprobe):
            result = self.build(session, clips, directory, lease, ffmpeg, ffprobe)
            return (
                [
                    {
                        "source_start": 1793555980,
                        "source_end": 1793555999.999237,
                        "media_start": 0,
                        "media_end": 19.999237,
                    }
                ],
                *result[1:],
            )

        self.manager.builder = rounded
        session = await self.create()
        result = self.manager.metadata(session)
        self.assertLess(result["timeline"][-1]["source_end"], result["before"])
        self.assertEqual(result["before"], 1793556000)
        self.assertEqual(result["next_timestamp"], 1793556000)
        self.assertEqual(result["continuation_timestamp"], 1793556000)
        await self.manager.delete("front", "alice", session.identifier)

    async def test_resolve_current_authorization_bounds_and_cancellation(self):
        from frigate.api.defs.request.recorded_http_body import RecordedHTTPResolve
        from frigate.comms.live_webrtc import LiveWebRTCError

        body = RecordedHTTPResolve(after=100, before=3700, start_timestamp=102.001)
        calls = []

        def resolve(camera, request, lease, *_):
            lease.check()
            calls.append((camera, request.after, request.before))
            return {
                "effective_start_timestamp": 105.005,
                "exhausted": False,
                "searched_before": request.before,
                "reason": "frame_found",
            }

        self.manager.resolver = resolve
        result = await self.manager.resolve(
            "front",
            "alice",
            body,
            time.time() + 300,
            auth_issued=time.time(),
        )
        self.assertEqual(result["effective_start_timestamp"], 105.005)
        self.assertEqual(calls, [("front", 100, 3700)])
        self.assertFalse(self.manager.resolve_tasks)
        self.assertFalse(self.live.external_viewers)
        self.revoked = True
        with self.assertRaises(LiveWebRTCError) as raised:
            await self.manager.resolve(
                "front",
                "alice",
                body,
                time.time() + 300,
                auth_issued=time.time(),
            )
        self.assertEqual(raised.exception.status, 401)
        self.revoked = False
        entered, release = threading.Event(), threading.Event()

        def blocked(*_):
            entered.set()
            release.wait(5)
            return {
                "effective_start_timestamp": None,
                "exhausted": True,
                "searched_before": body.before,
                "reason": "no_frame_in_range",
            }

        self.manager.resolver = blocked
        task = asyncio.create_task(
            self.manager.resolve(
                "front",
                "alice",
                body,
                time.time() + 300,
                auth_issued=time.time(),
            )
        )
        self.assertTrue(await asyncio.to_thread(entered.wait, 5))
        task.cancel()
        await asyncio.sleep(0.01)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.manager.resolve_tasks)

    async def test_shared_catalog_resolver_skips_fractional_tail_and_real_file_gap(
        self,
    ):
        from peewee import SqliteDatabase

        from frigate.api.defs.request.recorded_http_body import RecordedHTTPResolve
        from frigate.models import Recordings
        from frigate.record.http_targets import resolve_source_target
        from frigate.test.test_recorded_webrtc import ReaderLease

        source = self.root / "source.mp4"
        source.write_bytes(b"synthetic")
        rows = [
            SimpleNamespace(
                path=str(source),
                start_time=100,
                end_time=102.003,
                duration=1.9998,
                keyframes=[0, 1000],
            ),
            SimpleNamespace(
                path=str(source),
                start_time=105.005,
                end_time=107.003,
                duration=1.998,
                keyframes=[0, 1000],
            ),
        ]
        limits = []

        class Query(list):
            def limit(self, value):
                limits.append(value)
                return self[:value]

        def query(camera, after, before, stream):
            return Query(
                row
                for row in rows
                if stream == "main" and row.end_time > after and row.start_time < before
            )

        database = SqliteDatabase(
            ":memory:", check_same_thread=False, thread_safe=False
        )
        with database.bind_ctx([Recordings]):
            database.connect()
            for target, before, expected in (
                (101.9992, 110, 105.005),
                (102.0001, 104, None),
                (101.9, 110, 101.96),
            ):
                body = RecordedHTTPResolve(
                    after=100, before=before, start_timestamp=target
                )
                async with ReaderLease("front", 100, before, owner="alice") as lease:
                    with patch("frigate.record.http_targets._rows_query", query):
                        result = await lease.guard(
                            lease.run_blocking(
                                resolve_source_target,
                                "front",
                                body,
                                lease,
                                "unused",
                                lambda *_: {"start": 0},
                                lambda *_: [
                                    {"pts_time": 0, "duration_time": 0.04},
                                    {"pts_time": 1.96, "duration_time": 0.04},
                                ],
                            )
                        )
                self.assertEqual(result["effective_start_timestamp"], expected)
                self.assertEqual(result["exhausted"], expected is None)
            database.close()
        self.assertTrue(limits)
        self.assertEqual(set(limits), {65})

    async def test_resolution_limit_does_not_claim_range_exhaustion(self):
        from peewee import SqliteDatabase

        from frigate.api.defs.request.recorded_http_body import RecordedHTTPResolve
        from frigate.models import Recordings
        from frigate.record.http_targets import resolve_source_target
        from frigate.test.test_recorded_webrtc import ReaderLease

        path = self.root / "source-limit.mp4"
        path.write_bytes(b"synthetic")
        rows = [
            SimpleNamespace(
                path=str(path),
                start_time=100 + index * 0.3,
                end_time=100 + index * 0.3 + 0.2,
                duration=0.2,
                keyframes=[0],
            )
            for index in range(65)
        ]

        class Query(list):
            def limit(self, value):
                return self[:value]

        database = SqliteDatabase(
            ":memory:", check_same_thread=False, thread_safe=False
        )
        with database.bind_ctx([Recordings]):
            database.connect()
            body = RecordedHTTPResolve(after=100, before=3700, start_timestamp=100)
            async with ReaderLease("front", 100, 3700, owner="alice") as lease:
                with patch(
                    "frigate.record.http_targets._rows_query",
                    side_effect=lambda c, a, b, s: Query(rows if s == "main" else []),
                ):
                    result = await lease.guard(
                        lease.run_blocking(
                            resolve_source_target,
                            "front",
                            body,
                            lease,
                            "unused",
                            lambda *_: {"start": 0},
                            lambda *_: [{"pts_time": 0.5, "duration_time": 0.04}],
                        )
                    )
            database.close()
        self.assertIsNone(result["effective_start_timestamp"])
        self.assertFalse(result["exhausted"])
        self.assertEqual(result["reason"], "search_limit")
        self.assertLess(result["searched_before"], 3700)

    async def test_bounded_bytes_and_readers(self):
        from frigate.comms.live_webrtc import LiveWebRTCError

        for _ in range(4):
            session = await self.create()
        with self.assertRaises(LiveWebRTCError) as raised:
            await self.create()
        self.assertEqual(raised.exception.status, 507)
        admitted = asyncio.Event()

        async def reader():
            task = await self.manager.acquire_reader(session)
            admitted.set()
            try:
                await asyncio.Event().wait()
            finally:
                session.readers.discard(task)

        tasks = []
        for _ in range(2):
            admitted.clear()
            tasks.append(asyncio.create_task(reader()))
            await admitted.wait()
        with self.assertRaises(LiveWebRTCError) as raised:
            await self.manager.acquire_reader(session)
        self.assertEqual(raised.exception.status, 429)
        await self.manager.delete("front", "alice", session.identifier)
        self.assertTrue(all(task.done() for task in tasks))
        self.assertFalse(session.readers)

    async def test_cancel_joins_actual_process_before_lease_and_quota_release(self):
        from frigate.test.test_recorded_webrtc import ReaderLease

        entered = threading.Event()
        child = []

        def blocked(session, clips, directory, lease, *_):
            directory.mkdir()
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            child.append(process)
            lease.process_started_blocking(process)
            entered.set()
            process.wait()
            lease.check()

        self.manager.builder = blocked
        result = await self.manager.create(
            "front",
            "alice",
            self.body,
            time.time() + 300,
            auth_issued=time.time(),
        )
        self.assertTrue(await asyncio.to_thread(entered.wait, 5))
        lease = ReaderLease.instances[-1]
        self.assertFalse(lease.released)
        await self.manager.delete("front", "alice", result["session_id"])
        self.assertIsNotNone(child[0].returncode)
        self.assertTrue(lease.released)
        self.assertFalse(self.live.external_encoders)
        self.assertFalse(self.live.external_viewers)

    async def test_timeout_and_explicit_builder_failure_never_publish_media(self):
        from frigate.comms.live_webrtc import LiveWebRTCError

        def fail(*_):
            raise LiveWebRTCError(422, "measured_timeline_invalid")

        self.manager.builder = fail
        result = await self.manager.create(
            "front",
            "alice",
            self.body,
            time.time() + 300,
            auth_issued=time.time(),
        )
        session = self.manager.sessions[result["session_id"]]
        await session.task
        self.assertEqual(session.state, "failed")
        self.assertEqual(session.error, "measured_timeline_invalid")
        self.assertFalse(session.directory.exists())
        with self.assertRaises(LiveWebRTCError) as raised:
            await self.manager.acquire_reader(session)
        self.assertEqual(raised.exception.status, 409)
        self.manager.planner = lambda *_: time.sleep(0.05)
        with patch("frigate.comms.recorded_http.PREPARE_SECONDS", 0.01):
            result = await self.manager.create(
                "front",
                "alice",
                self.body,
                time.time() + 300,
                auth_issued=time.time(),
            )
            session = self.manager.sessions[result["session_id"]]
            await session.task
        self.assertEqual(session.state, "failed")
        self.assertFalse(self.live.external_encoders)

    async def test_get_head_range_headers_if_range_and_revocation(self):
        import httpx
        from fastapi import FastAPI

        from frigate.api.notification import native_image_user
        from frigate.api.recorded_http import router

        app = FastAPI()
        app.frigate_config = self.config
        app.jwt_token = "synthetic"
        app.state.recorded_http_manager = self.manager
        user = {"username": "alice", "role": "admin"}
        for dependency in router.dependencies:
            app.dependency_overrides[dependency.dependency] = lambda: None
        app.dependency_overrides[native_image_user] = lambda: user
        app.include_router(router)
        operations = app.openapi()["paths"][
            "/{camera_name}/recordings/http/sessions/{session_id}/media.mp4"
        ]
        self.assertNotEqual(
            operations["get"]["operationId"], operations["head"]["operationId"]
        )
        for method in ("get", "head"):
            self.assertEqual(operations[method]["x-required-role"], "camera")
        session = await self.create()
        media = "/" + self.manager.metadata(session)["media_url"]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://synthetic",
        ) as client:
            discovered = await client.get("/front/recordings/http/status")
            self.assertEqual(discovered.status_code, 200)
            self.assertEqual(discovered.json()["max_window_seconds"], 30)
            self.assertEqual(
                discovered.json()["resolve_url"], "front/recordings/http/resolve"
            )
            self.manager.resolver = lambda *args: {
                "effective_start_timestamp": 105.005,
                "exhausted": False,
                "searched_before": 120,
                "reason": "frame_found",
            }
            with patch(
                "frigate.api.recorded_http.jwt.decode",
                return_value=SimpleNamespace(
                    claims={"exp": time.time() + 300, "iat": time.time()},
                ),
            ):
                target = await client.post(
                    "/front/recordings/http/resolve",
                    json={
                        "after": 100,
                        "before": 120,
                        "start_timestamp": 102.0001,
                    },
                )
            self.assertEqual(target.status_code, 200, target.text)
            self.assertEqual(target.json()["effective_start_timestamp"], 105.005)
            full = await client.get(media)
            self.assertEqual(full.status_code, 200)
            self.assertEqual(full.content, b"0123456789" * 10)
            self.assertEqual(full.headers["content-length"], "100")
            self.assertEqual(full.headers["accept-ranges"], "bytes")
            self.assertIn("no-store", full.headers["cache-control"])
            for method in ("GET", "HEAD"):
                for value, expected, content in (
                    ("bytes=10-19", "bytes 10-19/100", b"0123456789"),
                    ("bytes=95-", "bytes 95-99/100", b"56789"),
                    ("bytes=-3", "bytes 97-99/100", b"789"),
                ):
                    response = await client.request(
                        method, media, headers={"Range": value}
                    )
                    self.assertEqual(response.status_code, 206)
                    self.assertEqual(response.headers["content-range"], expected)
                    self.assertEqual(
                        response.headers["content-length"], str(len(content))
                    )
                    self.assertEqual(
                        response.content, content if method == "GET" else b""
                    )
                response = await client.request(
                    method, media, headers={"Range": "bytes=100-"}
                )
                self.assertEqual(response.status_code, 416)
                self.assertEqual(response.headers["content-range"], "bytes */100")
                self.assertEqual(response.headers["content-length"], "0")
            response = await client.get(
                media,
                headers={
                    "Range": "bytes=0-2",
                    "If-Range": full.headers["etag"],
                },
            )
            self.assertEqual(response.status_code, 206)
            response = await client.get(
                media,
                headers={
                    "Range": "bytes=0-2",
                    "If-Range": '"old"',
                },
            )
            self.assertEqual(response.status_code, 200)
            user["username"] = "bob"
            self.assertEqual((await client.get(media)).status_code, 404)
            user["username"] = "alice"
            self.revoked = True
            self.assertEqual((await client.get(media)).status_code, 401)
        self.assertFalse(session.readers)

    async def test_stream_delete_joins_inflight_read_thread_before_quota_release(self):
        from frigate.api.recorded_http import OwnedMP4Response

        session = await self.create()
        entered, release = threading.Event(), threading.Event()

        def blocked_read(*_):
            entered.set()
            release.wait(5)
            return b"0123456789"

        async def receive():
            await asyncio.Event().wait()

        async def send(_):
            return None

        response = OwnedMP4Response(
            self.manager,
            session,
            0,
            9,
            head=False,
            status_code=200,
            headers={"Content-Length": "10"},
        )
        with patch("frigate.api.recorded_http.read_slice", blocked_read):
            reader = asyncio.create_task(response({}, receive, send))
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            deletion = asyncio.create_task(
                self.manager.delete(
                    "front",
                    "alice",
                    session.identifier,
                )
            )
            await asyncio.sleep(0.02)
            self.assertFalse(deletion.done())
            self.assertIn(session.identifier, self.live.external_viewers)
            self.assertTrue(session.directory.exists())
            release.set()
            await deletion
            await asyncio.gather(reader, return_exceptions=True)
        self.assertFalse(self.live.external_viewers)
        self.assertFalse(session.readers)

    async def test_completed_response_disconnect_does_not_cancel_normal_cleanup(self):
        from frigate.api.recorded_http import OwnedMP4Response

        session = await self.create()
        completed = asyncio.Event()

        async def receive():
            await completed.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body" and not message["more_body"]:
                completed.set()
                await asyncio.sleep(0.01)

        response = OwnedMP4Response(
            self.manager,
            session,
            0,
            9,
            head=False,
            status_code=200,
            headers={"Content-Length": "10"},
        )
        await asyncio.create_task(response({}, receive, send))
        self.assertFalse(session.readers)

    async def test_disconnect_before_completion_still_cancels_owned_response(self):
        from frigate.api.recorded_http import OwnedMP4Response

        session = await self.create()
        sending = asyncio.Event()

        async def receive():
            await sending.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body" and message["more_body"]:
                sending.set()
                await asyncio.Event().wait()

        response = OwnedMP4Response(
            self.manager,
            session,
            0,
            9,
            head=False,
            status_code=200,
            headers={"Content-Length": "10"},
        )
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.create_task(response({}, receive, send))
        self.assertFalse(session.readers)

    @unittest.skipUnless(
        os.environ.get("FRIGATE_PLAYBACK_TEST_FFMPEG")
        and os.environ.get("FRIGATE_PLAYBACK_TEST_FFPROBE"),
        "Explicit synthetic-media FFmpeg and FFprobe tools required",
    )
    async def test_real_original_fractional_gop_arbitrary_offset_uses_packet_clock(
        self,
    ):
        from frigate.api.defs.request.recorded_http_body import RecordedHTTPCreate
        from frigate.comms.recorded_http import build_mp4, faststart
        from frigate.record.playback import SourceClip

        ffmpeg = os.environ["FRIGATE_PLAYBACK_TEST_FFMPEG"]
        self.manager.ffmpeg = ffmpeg
        self.manager.ffprobe = os.environ["FRIGATE_PLAYBACK_TEST_FFPROBE"]
        source = self.root / "fractional-gop.mp4"
        process = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=30000/1001",
            "-t",
            "6",
            "-c:v",
            "libx264",
            "-threads",
            "2",
            "-preset",
            "ultrafast",
            "-bf",
            "0",
            "-g",
            "59",
            "-keyint_min",
            "59",
            "-sc_threshold",
            "0",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(source),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), 30)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        self.assertEqual(process.returncode, 0, stderr.decode(errors="replace"))
        original = source.read_bytes()
        self.manager.builder = build_mp4
        self.manager.planner = lambda *_: [
            SourceClip(str(source), 101.968, 2.032, 1.968, False, 102.2, 104),
        ]
        self.body = RecordedHTTPCreate(
            after=102.2,
            before=104,
            start_timestamp=102.3,
            quality="original",
        )
        session = await self.create()
        self.assertAlmostEqual(
            session.timeline[0]["source_start"], 100 + 59059 / 30000, places=6
        )
        self.assertNotEqual(session.timeline[0]["source_start"], 101.968)
        self.assertLessEqual(session.timeline[0]["source_end"], 104)
        self.assertEqual(session.effective_start, 102.3)
        self.assertTrue(faststart(session.directory / "media.mp4"))
        self.assertEqual(source.read_bytes(), original)
        self.assertFalse(self.live.external_encoders)
        await self.manager.delete("front", "alice", session.identifier)
        self.assertFalse(self.live.external_viewers)

    @unittest.skipUnless(
        os.environ.get("FRIGATE_PLAYBACK_TEST_FFMPEG")
        and os.environ.get("FRIGATE_PLAYBACK_TEST_FFPROBE"),
        "Explicit synthetic-media FFmpeg and FFprobe tools required",
    )
    async def test_real_scaled_and_original_completed_faststart_measured_gap_spans(
        self,
    ):
        from frigate.api.defs.request.recorded_http_body import RecordedHTTPCreate
        from frigate.comms.recorded_http import build_mp4, faststart
        from frigate.record.playback import SourceClip

        ffmpeg = os.environ["FRIGATE_PLAYBACK_TEST_FFMPEG"]
        self.manager.ffmpeg = ffmpeg
        self.manager.ffprobe = os.environ["FRIGATE_PLAYBACK_TEST_FFPROBE"]
        source = self.root / "synthetic.mp4"
        process = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:size=640x360:rate=15",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "2",
            "-c:v",
            "libx264",
            "-threads",
            "2",
            "-preset",
            "ultrafast",
            "-bf",
            "0",
            "-g",
            "15",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(source),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), 30)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        self.assertEqual(process.returncode, 0, stderr.decode(errors="replace"))
        original = source.read_bytes()
        self.manager.builder = build_mp4
        self.manager.planner = lambda *_: [
            SourceClip(str(source), 100, 2, 0, True, 100, 102),
            SourceClip(str(source), 110, 2, 0, True, 110, 112),
        ]
        for quality in ("360", "original"):
            self.body = RecordedHTTPCreate(
                after=100,
                before=112,
                start_timestamp=101,
                quality=quality,
            )
            session = await self.create()
            self.assertTrue(faststart(session.directory / "media.mp4"))
            self.assertTrue(session.audio)
            self.assertEqual(len(session.timeline), 2)
            self.assertAlmostEqual(session.timeline[0]["source_start"], 100, places=2)
            self.assertAlmostEqual(session.timeline[1]["source_start"], 110, places=2)
            self.assertAlmostEqual(session.timeline[0]["source_end"], 102, delta=0.15)
            self.assertAlmostEqual(session.timeline[1]["source_end"], 112, delta=0.15)
            self.assertEqual(source.read_bytes(), original)
            self.assertFalse(list(session.directory.glob("p*.mp4")))
            self.assertFalse(self.live.external_encoders)
            await self.manager.delete("front", "alice", session.identifier)
        self.body = RecordedHTTPCreate(
            after=100,
            before=112,
            start_timestamp=102.0001,
            quality="360",
            start_policy="next",
        )
        session = await self.create()
        self.assertEqual(session.effective_start, 110)
        self.assertEqual(session.body.before, 112)
        await self.manager.delete("front", "alice", session.identifier)
        from peewee import SqliteDatabase

        from frigate.api.defs.request.recorded_http_body import RecordedHTTPResolve
        from frigate.models import Recordings

        catalog = [
            SimpleNamespace(
                path=str(source),
                start_time=100,
                end_time=102.003,
                duration=2,
                keyframes=[0, 1000],
            ),
            SimpleNamespace(
                path=str(source),
                start_time=110,
                end_time=112,
                duration=2,
                keyframes=[0, 1000],
            ),
        ]

        class Query(list):
            def limit(self, value):
                return self[:value]

        def rows(camera, after, before, stream):
            return Query(
                row
                for row in catalog
                if stream == "main" and row.end_time > after and row.start_time < before
            )

        database = SqliteDatabase(
            ":memory:", check_same_thread=False, thread_safe=False
        )
        with database.bind_ctx([Recordings]):
            database.connect()
            with patch("frigate.record.http_targets._rows_query", rows):
                for before, expected in ((112, 110), (109, None)):
                    target = await self.manager.resolve(
                        "front",
                        "alice",
                        RecordedHTTPResolve(
                            after=100, before=before, start_timestamp=101.9995
                        ),
                        time.time() + 300,
                        auth_issued=time.time(),
                    )
                    self.assertEqual(target["effective_start_timestamp"], expected)
                    self.assertEqual(target["exhausted"], expected is None)
            database.close()
        self.assertEqual(source.read_bytes(), original)

"""Mock-only RTSP replay authentication, pinning, range, and resource lifecycle tests."""

import asyncio
import contextlib
import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from frigate.ptz.replay import OnvifReplayManager, ReplayCapacityError
from frigate.ptz.replay_transport import (
    ReplayGateway,
    ReplaySource,
    ReplayUnsupported,
    clock_range,
    pin_replay_source,
    read_message,
)

NOW = datetime(2026, 10, 18, 6, tzinfo=UTC)
END = NOW + timedelta(seconds=30)
MP4 = b"\x00\x00\x00\x08ftyp\x00\x00\x00\x08moov\x00\x00\x00\x08moof"


def audio_mp4(rate=48000, codec=b"mp4a"):
    def box(kind, data=b""):
        return (len(data) + 8).to_bytes(4, "big") + kind + data

    entry = box(codec, bytes(24) + (rate << 16).to_bytes(4, "big"))
    stsd = box(b"stsd", bytes(4) + (1).to_bytes(4, "big") + entry)
    handler = box(b"hdlr", bytes(8) + b"soun" + bytes(12))
    mdia = box(b"mdia", handler + box(b"minf", box(b"stbl", stsd)))
    return box(b"ftyp") + box(b"moov", box(b"trak", mdia)) + box(b"moof")


def source(port=554):
    return ReplaySource(
        f"rtsp://camera.invalid:{port}/recording?private-query=vendor-token",
        "private-user",
        "private-password",
        "192.0.2.10",
        port,
        "camera.invalid",
    )


class TestReplayPinning(unittest.IsolatedAsyncioTestCase):
    async def test_only_the_saved_camera_is_resolved_and_query_tokens_remain_private(
        self,
    ):
        connection = SimpleNamespace(
            host="camera.invalid",
            username="private-user",
            password=SimpleNamespace(get_secret_value=lambda: "private-password"),
            tls_insecure=False,
        )
        resolver = AsyncMock(return_value=[(2, 1, 6, "", ("192.0.2.10", 554))])
        with patch.object(asyncio.get_running_loop(), "getaddrinfo", resolver):
            pinned = await pin_replay_source(
                "rtsp://url-user:url-password@camera.invalid/replay?token=private-query",
                connection,
            )
            self.assertEqual(pinned.address, "192.0.2.10")
            self.assertIn("token=private-query", pinned.uri)
            self.assertNotIn("url-password", repr(pinned))
            self.assertNotIn("private-query", repr(pinned))
            for uri in (
                "rtsp://elsewhere.invalid/replay",
                "http://camera.invalid/replay",
                "file:///config/config.yml",
                "rtsp://127.0.0.1/replay",
                "rtsp://169.254.169.254/metadata",
            ):
                with self.subTest(uri=uri), self.assertRaises(ReplayUnsupported):
                    await pin_replay_source(uri, connection)
        self.assertTrue(
            all(call.args[0] == "camera.invalid" for call in resolver.await_args_list)
        )

    def test_clock_range_is_absolute_and_bounded(self):
        self.assertEqual(
            clock_range(NOW, END),
            "clock=20261018T060000.000000Z-20261018T060030.000000Z",
        )


class FakeProcess:
    def __init__(self, chunks):
        self.stdout = SimpleNamespace(read=AsyncMock(side_effect=chunks))
        self.returncode = None
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


class FakeGateway:
    def __init__(self, *_args):
        self.error_code = None
        self.closed = False

    async def start_server(self):
        return "rtsp://127.0.0.1:10000/opaque-session"

    async def close(self):
        self.closed = True


class TestReplaySessions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.configuration = object()
        self.process = FakeProcess([MP4, b"mp4-frame", b""])
        self.factory = AsyncMock(return_value=self.process)
        self.manager = OnvifReplayManager(
            lambda: self.configuration,
            process_factory=self.factory,
            gateway_factory=FakeGateway,
        )

    async def asyncTearDown(self):
        await self.manager.shutdown()

    def reserve(self, camera="front"):
        return self.manager.create(
            camera, source(), NOW, END, "ffmpeg", self.configuration
        )

    async def test_session_and_ffmpeg_argv_never_expose_upstream_secrets(self):
        public = self.reserve()
        text = json.dumps(public)
        for private in (
            "private-password",
            "vendor-token",
            "camera.invalid",
            "rtsp://",
        ):
            self.assertNotIn(private, text)
        session, first = await self.manager.prepare("front", public["id"])
        command = " ".join(self.factory.await_args.args)
        arguments = self.factory.await_args.args
        self.assertEqual(arguments[arguments.index("-c:v") + 1], "libx264")
        self.assertEqual(arguments[arguments.index("-pix_fmt") + 1], "yuv420p")
        self.assertEqual(arguments[arguments.index("-profile:v") + 1], "baseline")
        self.assertEqual(arguments[arguments.index("-tag:v") + 1], "avc1")
        self.assertNotIn("copy", arguments)
        self.assertFalse(public["audio"])
        self.assertIn("0:a:0?", arguments)
        self.assertNotIn("-an", arguments)
        self.assertEqual(arguments[arguments.index("-c:a") + 1], "aac")
        self.assertEqual(arguments[arguments.index("-profile:a") + 1], "aac_low")
        self.assertEqual(arguments[arguments.index("-ar") + 1], "48000")
        self.assertIn("rtsp://127.0.0.1:10000/opaque-session", command)
        self.assertNotIn("private-password", command)
        self.assertNotIn("vendor-token", command)
        self.assertNotIn("camera.invalid", command)
        chunks = [
            chunk
            async for chunk in self.manager.stream(
                session,
                first,
                AsyncMock(return_value=True),
            )
        ]
        self.assertEqual(chunks, [MP4, b"mp4-frame"])
        self.assertTrue(self.process.terminated)
        self.assertTrue(session.gateway.closed)
        self.assertEqual(self.manager.sessions, {})

    async def test_audio_is_verified_from_output_and_completion_status_is_secret_free(
        self,
    ):
        self.process.stdout.read.side_effect = [audio_mp4(), b""]
        public = self.reserve()
        self.assertIsNone(public["audio"])
        session, first = await self.manager.prepare("front", public["id"])
        self.assertTrue(session.audio)
        self.assertEqual(session.public()["audio_codec"], "aac_lc")
        self.assertEqual(session.public()["audio_sample_rate"], 48000)
        _chunks = [
            chunk
            async for chunk in self.manager.stream(
                session, first, AsyncMock(return_value=True)
            )
        ]
        status = self.manager.status("front", public["id"])
        self.assertEqual(status["audio_status"], "available")
        self.assertNotIn("vendor-token", json.dumps(status))
        with self.assertRaises(KeyError):
            self.manager.status("back", public["id"])

    async def test_recordings_without_audio_are_explicit_and_never_get_synthetic_silence(
        self,
    ):
        descriptor = replace(source(), recorded_audio=False)
        public = self.manager.create(
            "front", descriptor, NOW, END, "ffmpeg", self.configuration
        )
        self.assertFalse(public["audio"])
        self.assertEqual(public["audio_status"], "not_recorded")
        session, _first = await self.manager.prepare("front", public["id"])
        self.assertFalse(session.audio)
        self.assertEqual(session.audio_status, "not_recorded")
        self.assertNotIn("anullsrc", self.factory.await_args.args)

    async def test_missing_audio_despite_recording_metadata_is_not_reported_as_preserved(
        self,
    ):
        descriptor = replace(source(), recorded_audio=True)
        public = self.manager.create(
            "front", descriptor, NOW, END, "ffmpeg", self.configuration
        )
        session, _first = await self.manager.prepare("front", public["id"])
        self.assertFalse(session.audio)
        self.assertEqual(session.audio_status, "unavailable")

    async def test_non_browser_audio_is_rejected_before_http_success(self):
        for rate, codec in ((32000, b"mp4a"), (48000, b"ulaw")):
            self.process.stdout.read.side_effect = [audio_mp4(rate, codec)]
            public = self.reserve()
            with self.assertRaises(ReplayUnsupported):
                await self.manager.prepare("front", public["id"])

    async def test_wrong_camera_and_duplicate_consumers_never_start_a_second_process(
        self,
    ):
        public = self.reserve()
        with self.assertRaises(KeyError):
            await self.manager.prepare("back", public["id"])
        await self.manager.prepare("front", public["id"])
        with self.assertRaises(ReplayUnsupported):
            await self.manager.prepare("front", public["id"])
        self.factory.assert_awaited_once()

    async def test_capacity_and_range_are_bounded_before_spawning(self):
        public = self.reserve()
        with self.assertRaises(ReplayCapacityError):
            self.reserve()
        self.reserve("back")
        with self.assertRaises(ReplayCapacityError):
            self.reserve("third")
        with self.assertRaises(ReplayUnsupported):
            self.manager.create(
                "third",
                source(),
                NOW,
                NOW + timedelta(hours=1),
                "ffmpeg",
                self.configuration,
            )
        self.factory.assert_not_awaited()
        await self.manager.close(self.manager.get("front", public["id"]))

    async def test_disconnect_and_explicit_cancel_release_process_and_gateway(self):
        public = self.reserve()
        session, first = await self.manager.prepare("front", public["id"])
        chunks = [
            chunk
            async for chunk in self.manager.stream(
                session,
                first,
                AsyncMock(return_value=False),
            )
        ]
        self.assertEqual(chunks, [])
        self.assertTrue(self.process.terminated)
        self.assertTrue(session.gateway.closed)
        self.assertEqual(self.manager.sessions, {})
        await self.manager.close(session)

    async def test_generator_cancel_and_shutdown_release_resources(self):
        public = self.reserve()
        session, first = await self.manager.prepare("front", public["id"])
        stream = self.manager.stream(session, first, AsyncMock(return_value=True))
        self.assertEqual(await anext(stream), MP4)
        await stream.aclose()
        self.assertTrue(session.gateway.closed)
        self.assertEqual(self.manager.sessions, {})

    async def test_failed_or_unsupported_camera_is_not_returned_as_playable(self):
        self.process.stdout.read.side_effect = [b""]
        public = self.reserve()
        with self.assertRaises(ReplayUnsupported):
            await self.manager.prepare("front", public["id"])
        self.assertTrue(self.process.terminated)
        self.assertEqual(self.manager.sessions, {})

    async def test_expired_unclaimed_session_does_not_spawn_or_contact_camera(self):
        public = self.reserve()
        session = self.manager.get("front", public["id"])
        await self.manager._expire(session, 0)
        self.assertEqual(self.manager.sessions, {})
        self.factory.assert_not_awaited()

    async def test_configuration_replacement_invalidates_a_reserved_session(self):
        public = self.reserve()
        self.configuration = object()
        with self.assertRaises(ReplayUnsupported):
            await self.manager.prepare("front", public["id"])
        self.assertEqual(self.manager.sessions, {})
        self.factory.assert_not_awaited()

    async def test_shutdown_releases_an_active_process_and_gateway(self):
        public = self.reserve()
        session, _first = await self.manager.prepare("front", public["id"])
        await self.manager.shutdown()
        self.assertTrue(self.process.terminated)
        self.assertTrue(session.gateway.closed)
        self.assertEqual(self.manager.sessions, {})
        with self.assertRaises(RuntimeError):
            self.reserve()

    async def test_cancel_during_process_creation_does_not_orphan_the_child(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def spawn(*args, **kwargs):
            started.set()
            await release.wait()
            return self.process

        self.manager.process_factory = spawn
        public = self.reserve()
        task = asyncio.create_task(self.manager.prepare("front", public["id"]))
        await started.wait()
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.process.terminated)
        self.assertEqual(self.manager.sessions, {})

    async def test_empty_mp4_metadata_without_video_fragments_is_not_success(self):
        self.process.stdout.read.side_effect = [b"\x00\x00\x00\x08ftyp", b""]
        public = self.reserve()
        with self.assertRaises(ReplayUnsupported):
            await self.manager.prepare("front", public["id"])
        self.assertTrue(self.process.terminated)

    async def test_discovery_is_bounded_before_contact_and_cancelled_on_shutdown(self):
        started = asyncio.Event()
        released = asyncio.Event()

        async def discovery():
            self.manager.reserve("front")
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.manager.finish_resolution("front")
                released.set()

        task = asyncio.create_task(discovery())
        await started.wait()
        with self.assertRaises(ReplayCapacityError):
            self.manager.reserve("front")
        self.manager.reserve("back")
        with self.assertRaises(ReplayCapacityError):
            self.manager.reserve("third")
        self.manager.finish_resolution("back")
        await self.manager.shutdown()
        self.assertTrue(task.cancelled())
        self.assertTrue(released.is_set())
        self.assertEqual(self.manager.resolving, set())

    async def test_unresponsive_transcoder_is_killed_and_gateway_still_closes(self):
        public = self.reserve()
        session, _first = await self.manager.prepare("front", public["id"])
        self.process.terminate = lambda: setattr(self.process, "terminated", True)
        self.process.wait = AsyncMock(side_effect=[TimeoutError(), 0])
        await self.manager.close(session)
        self.assertTrue(self.process.terminated)
        self.assertTrue(self.process.killed)
        self.assertTrue(session.gateway.closed)
        self.assertEqual(self.manager.sessions, {})


class TestReplayGateway(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_local_consumer_never_opens_a_camera_connection(self):
        gateway = ReplayGateway(source(), NOW, END)
        await gateway.start_server()
        reader, writer = await asyncio.open_connection(
            "127.0.0.1",
            gateway.server.sockets[0].getsockname()[1],
        )
        try:
            with patch(
                "frigate.ptz.replay_transport.asyncio.open_connection", AsyncMock()
            ) as connect:
                writer.write(
                    b"DESCRIBE rtsp://127.0.0.1/unknown RTSP/1.0\r\nCSeq: 1\r\n\r\n"
                )
                await writer.drain()
                self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
                connect.assert_not_awaited()
        finally:
            await gateway.close()
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def test_mock_rtsp_digest_auth_clock_range_and_private_track_rewriting(self):
        requests = []
        errors = []
        writers = []
        challenged = False
        packet = b"$\x00\x00\x04test"

        async def camera(reader, writer):
            nonlocal challenged
            writers.append(writer)
            try:
                while True:
                    line, headers, _body = await read_message(reader)
                    method, target, _protocol = line.split(" ")
                    requests.append((method, target, headers))
                    reply_headers = {"CSeq": headers["cseq"]}
                    body = b""
                    code = 200
                    if method == "DESCRIBE" and not challenged:
                        challenged = True
                        code = 401
                        reply_headers["WWW-Authenticate"] = (
                            'Digest realm="camera", nonce="nonce", qop="auth", algorithm=MD5'
                        )
                    elif method == "DESCRIBE":
                        self.assertIn(
                            'username="private-user"', headers["authorization"]
                        )
                        self.assertIn(
                            "private-query=vendor-token", headers["authorization"]
                        )
                        reply_headers["Content-Base"] = (
                            f"rtsp://camera.invalid:{port}/recording/"
                        )
                        body = (
                            "v=0\r\ns=Recording\r\nm=video 0 RTP/AVP 96\r\n"
                            "a=rtpmap:96 H264/90000\r\n"
                            f"a=control:rtsp://camera.invalid:{port}/recording/track1?private-query=vendor-token\r\n"
                        ).encode()
                    elif method == "SETUP":
                        self.assertIn("private-query=vendor-token", target)
                        reply_headers["Transport"] = (
                            "RTP/AVP/TCP;unicast;interleaved=0-1"
                        )
                        reply_headers["Session"] = "camera-private-session"
                    elif method == "PLAY":
                        self.assertEqual(headers["range"], clock_range(NOW, END))
                        self.assertEqual(headers["require"], "onvif-replay")
                        self.assertEqual(headers["session"], "camera-private-session")
                        reply_headers["Range"] = clock_range(NOW, END)
                    reply_headers["Content-Length"] = str(len(body))
                    payload = f"RTSP/1.0 {code} Response\r\n" + "".join(
                        f"{key}: {value}\r\n" for key, value in reply_headers.items()
                    )
                    writer.write(payload.encode() + b"\r\n" + body)
                    if method == "PLAY":
                        writer.write(packet)
                    await writer.drain()
                    if method == "TEARDOWN":
                        break
            except asyncio.IncompleteReadError:
                pass
            except BaseException as error:
                errors.append(error)
            finally:
                writer.close()

        server = await asyncio.start_server(camera, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        descriptor = source(port)
        descriptor = ReplaySource(
            descriptor.uri,
            descriptor.username,
            descriptor.password,
            "127.0.0.1",
            port,
            descriptor.hostname,
        )
        gateway = ReplayGateway(descriptor, NOW, END)
        local = await gateway.start_server()
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", gateway.server.sockets[0].getsockname()[1]
        )
        try:

            async def call(method, target, extra=""):
                writer.write(
                    f"{method} {target} RTSP/1.0\r\nCSeq: 1\r\n{extra}\r\n".encode()
                )
                await writer.drain()
                return await asyncio.wait_for(read_message(reader), 3)

            _, _, sdp = await call("DESCRIBE", local)
            self.assertNotIn(b"vendor-token", sdp)
            self.assertNotIn(b"camera.invalid", sdp)
            track = next(
                line[10:]
                for line in sdp.decode().splitlines()
                if line.startswith("a=control:")
            )
            _, setup, _ = await call(
                "SETUP", track, "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n"
            )
            self.assertNotIn("camera-private-session", setup["session"])
            await call("PLAY", local, "Range: npt=0-999999\r\n")
            self.assertEqual(
                await asyncio.wait_for(reader.readexactly(len(packet)), 3), packet
            )
        finally:
            await gateway.close()
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            server.close()
            await server.wait_closed()
            for camera_writer in writers:
                camera_writer.close()
                with contextlib.suppress(OSError):
                    await camera_writer.wait_closed()
        self.assertEqual(errors, [])
        self.assertTrue(any(method == "PLAY" for method, _, _ in requests))

    async def test_missing_or_wrong_clock_acknowledgement_is_explicitly_unsupported(
        self,
    ):
        gateway = ReplayGateway(source(), NOW, END)
        for value in (None, "npt=0-30", "clock=20200101T000000Z-"):
            with self.subTest(value=value), self.assertRaises(ReplayUnsupported):
                gateway._validate_range(value)

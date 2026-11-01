"""On-demand admission, ownership, leases, and TURN credential regressions."""

import asyncio
import base64
import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from frigate.comms.live_webrtc import (
    AnswerBody,
    CandidateBody,
    LiveWebRTCError,
    LiveWebRTCManager,
    MediaGroup,
    OvenTransport,
    QualityBody,
    ViewerBody,
    turn_ice_servers,
)
from frigate.config.live_webrtc import LiveWebRTCConfig


class FakeSocket:
    def __init__(self):
        self.messages = []
        self.incoming = asyncio.Queue()
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        value = await self.incoming.get()
        if value is None:
            raise StopAsyncIteration
        return value

    async def send_json(self, message):
        self.messages.append(message)

    async def close(self):
        self.closed = True
        self.incoming.put_nowait(None)


class FakeTransport(OvenTransport):
    def __init__(self):
        self.created = []
        self.deleted = []
        self.sockets = []
        self.create_error = False
        self.delete_error = False
        self.gate = None
        self.entered = asyncio.Event()

    async def create_pull(self, group, source):
        self.created.append((group, source))
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.create_error:
            raise LiveWebRTCError(503, "live_webrtc_source_unavailable")

    async def delete_pull(self, group):
        if self.delete_error:
            raise LiveWebRTCError(503, "live_webrtc_cleanup_pending")
        self.deleted.append(group)

    async def offer(self, group):
        socket = FakeSocket()
        self.sockets.append(socket)
        return socket, {
            "id": len(self.sockets),
            "peer_id": 0,
            "sdp": {"type": "offer", "sdp": "v=0\r\nsynthetic-only\r\n"},
            "candidates": [],
        }

    async def close(self):
        pass


class TestOvenOfferReadiness(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.group = MediaGroup("camera", "frigate_grid_0", "owned", "grid")
        self.offer = {
            "command": "offer",
            "code": 200,
            "id": 1,
            "peer_id": 0,
            "sdp": {"type": "offer", "sdp": "v=0\r\nsynthetic-only\r\n"},
            "candidates": [],
        }
        self.transport = OvenTransport.__new__(OvenTransport)

    def socket(self, message):
        return SimpleNamespace(
            send_json=AsyncMock(),
            receive_json=AsyncMock(return_value=message),
            close=AsyncMock(),
        )

    async def test_retries_not_ready_and_retains_only_the_valid_socket(self):
        pending = self.socket({"code": 404})
        ready = self.socket(self.offer)
        self.transport.http = SimpleNamespace(
            ws_connect=AsyncMock(side_effect=[pending, ready])
        )
        with patch(
            "frigate.comms.live_webrtc.asyncio.sleep", new_callable=AsyncMock
        ) as sleep:
            socket, offer = await self.transport.offer(self.group)
        self.assertIs(socket, ready)
        self.assertEqual(offer, self.offer)
        pending.close.assert_awaited_once()
        ready.close.assert_not_awaited()
        sleep.assert_awaited_once_with(0.2)

    async def test_rejected_offer_closes_without_retry(self):
        for message in ({"code": 403}, {**self.offer, "peer_id": "invalid"}, []):
            with self.subTest(message=message):
                socket = self.socket(message)
                self.transport.http = SimpleNamespace(
                    ws_connect=AsyncMock(return_value=socket)
                )
                with self.assertRaises(LiveWebRTCError):
                    await self.transport.offer(self.group)
                socket.close.assert_awaited_once()
                self.transport.http.ws_connect.assert_awaited_once()

    async def test_not_ready_retries_have_one_total_deadline(self):
        socket = self.socket({"code": 404})
        self.transport.http = SimpleNamespace(ws_connect=AsyncMock(return_value=socket))
        timeout = asyncio.timeout
        with patch(
            "frigate.comms.live_webrtc.asyncio.timeout",
            side_effect=lambda _: timeout(0.01),
        ) as deadline:
            with self.assertRaises(LiveWebRTCError) as error:
                await self.transport.offer(self.group)
        self.assertEqual(error.exception.code, "live_webrtc_signaling_unavailable")
        deadline.assert_called_once_with(12)
        socket.close.assert_awaited_once()

    async def test_cancellation_closes_the_unaccepted_socket(self):
        socket = self.socket(self.offer)
        entered = asyncio.Event()

        async def receive():
            entered.set()
            await asyncio.Event().wait()

        socket.receive_json.side_effect = receive
        self.transport.http = SimpleNamespace(ws_connect=AsyncMock(return_value=socket))
        task = asyncio.create_task(self.transport.offer(self.group))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        socket.close.assert_awaited_once()


class TestLiveWebRTCConfig(unittest.TestCase):
    def test_feature_and_relay_are_disabled_by_default(self):
        config = LiveWebRTCConfig()
        self.assertFalse(config.enabled)
        self.assertFalse(config.turn.enabled)

    def test_rejects_wildcard_loopback_and_injected_addresses(self):
        for address in ("*", "0.0.0.0", "127.0.0.1", "239.1.1.1", "host\ninjection"):
            with self.subTest(address=address), self.assertRaises(ValidationError):
                LiveWebRTCConfig(enabled=True, advertised_hosts=[address])

    def test_enabled_requires_explicit_media_candidates(self):
        with self.assertRaises(ValidationError):
            LiveWebRTCConfig(enabled=True)

    def test_requires_complete_relay_and_tls_configuration(self):
        for relay in (
            {"enabled": True},
            {"enabled": True, "hostname": "turn.example.test"},
            {"enabled": True, "hostname": "turn.example.test", "relay_ip": "10.1.1.4"},
        ):
            with self.subTest(relay=relay), self.assertRaises(ValidationError):
                LiveWebRTCConfig(
                    enabled=True, advertised_hosts=["10.1.1.4"], turn=relay
                )

    def test_rejects_overlapping_service_ports(self):
        with self.assertRaises(ValidationError):
            LiveWebRTCConfig(
                enabled=True,
                advertised_hosts=["10.1.1.4"],
                turn={
                    "enabled": True,
                    "hostname": "turn.example.test",
                    "relay_ip": "10.1.1.4",
                    "tls": False,
                    "port": 8189,
                },
            )

    def test_credentials_expire_without_disclosing_the_shared_secret(self):
        config = LiveWebRTCConfig(
            enabled=True,
            advertised_hosts=["10.1.1.4"],
            turn={
                "enabled": True,
                "hostname": "turn.example.test",
                "relay_ip": "10.1.1.4",
                "tls": False,
            },
        )
        secret = "synthetic-relay-secret-not-a-production-credential"
        result = turn_ice_servers(config, secret, "owned-session", 1000)
        self.assertEqual(result[0]["username"], "1120:owned-session")
        expected = base64.b64encode(
            hmac.new(secret.encode(), b"1120:owned-session", hashlib.sha1).digest()
        ).decode()
        self.assertEqual(result[0]["credential"], expected)
        self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(len(result[0]["urls"]), 2)

    def test_viewers_cannot_submit_sources_or_unbounded_signaling(self):
        for body in (
            {"source": "rtsp://untrusted.invalid"},
            {"view": "unknown"},
            {"height": 9999},
        ):
            with self.subTest(body=body), self.assertRaises(ValidationError):
                ViewerBody.model_validate(body)
        with self.assertRaises(ValidationError):
            AnswerBody(sdp="x" * 128001)
        with self.assertRaises(ValidationError):
            CandidateBody(
                candidates=[{"candidate": "x\ninjection", "sdpMLineIndex": 0}]
            )


class TestLiveWebRTCManager(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.runtime = Path(self.directory.name)
        self.now = 1000.0
        self.settings = LiveWebRTCConfig(enabled=True, advertised_hosts=["10.1.1.4"])
        self.config = SimpleNamespace(
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
        self.transport = FakeTransport()
        self.manager = LiveWebRTCManager(
            lambda: self.config,
            runtime=self.runtime,
            clock=lambda: self.now,
            transport=self.transport,
        )
        (self.runtime / "runtime.json").write_text(
            json.dumps(
                {
                    "settings_sha256": hashlib.sha256(
                        self.settings.model_dump_json().encode()
                    ).hexdigest(),
                    "api_token": "a" * 48,
                    "turn_secret": "b" * 48,
                }
            )
        )
        await self.manager.start()

    async def asyncTearDown(self):
        self.transport.delete_error = False
        await self.manager.close()
        self.directory.cleanup()

    async def create(self, camera="front", owner="viewer", **values):
        return await self.manager.create(
            camera, owner, ViewerBody(**values), self.now + 300
        )

    async def test_start_does_not_start_camera_media(self):
        self.assertTrue(self.manager.available())
        self.assertEqual(self.transport.created, [])
        self.assertEqual(self.manager.groups, {})

    async def test_viewers_share_one_pull_and_last_close_stops_it(self):
        one, two = await asyncio.gather(self.create(), self.create())
        self.assertEqual(len(self.transport.created), 1)
        self.assertEqual(self.transport.created[0][1], "front")
        await self.manager.delete("front", "viewer", one["session_id"])
        self.assertEqual(self.transport.deleted, [])
        await self.manager.delete("front", "viewer", two["session_id"])
        self.assertEqual(len(self.transport.deleted), 1)
        self.assertEqual(self.manager.groups, {})
        self.assertTrue(all(socket.closed for socket in self.transport.sockets))

    async def test_another_account_or_camera_cannot_use_a_session(self):
        session = await self.create()
        for camera, owner in (("front", "other"), ("garden", "viewer")):
            with (
                self.subTest(camera=camera, owner=owner),
                self.assertRaises(LiveWebRTCError) as error,
            ):
                await self.manager.renew(camera, owner, session["session_id"])
            self.assertEqual(error.exception.status, 404)
        self.assertEqual(len(self.manager.sessions), 1)

    async def test_grid_and_detail_use_separate_bounded_profiles(self):
        grid = await self.create(view="grid", height=360, audio=False)
        detail = await self.create()
        self.assertEqual(len(self.transport.created), 2)
        self.assertEqual(grid["renditions"], [{"height": 360, "bitrate": 500}])
        self.assertEqual(
            [value["height"] for value in detail["renditions"]], [720, 1080]
        )
        self.assertTrue(self.transport.created[0][0].app.startswith("frigate_grid_"))

    async def test_idle_lease_expiry_stops_source_without_client_delete(self):
        await self.create()
        self.now += self.settings.lease_seconds + 1
        await self.manager._maintain_once()
        self.assertEqual(self.manager.sessions, {})
        self.assertEqual(len(self.transport.deleted), 1)

    async def test_renewal_never_extends_authentication_hard_deadline(self):
        session = await self.manager.create(
            "front", "viewer", ViewerBody(), self.now + 40
        )
        self.now += 20
        renewed = await self.manager.renew("front", "viewer", session["session_id"])
        self.assertEqual(renewed["expires_at"], 1040)
        self.now = 1041
        await self.manager._maintain_once()
        self.assertEqual(self.manager.sessions, {})

    async def test_disabled_camera_or_changed_settings_closes_viewers(self):
        await self.create()
        self.config.cameras["front"].enabled = False
        await self.manager._maintain_once()
        self.assertEqual(self.manager.sessions, {})
        self.config.cameras["front"].enabled = True
        await self.create()
        self.config.live_webrtc = self.settings.model_copy(update={"grid_fps": 11})
        await self.manager._maintain_once()
        self.assertEqual(self.manager.sessions, {})
        self.assertFalse(self.manager.available())

    async def test_pending_admission_counts_toward_capacity(self):
        self.settings.max_viewers = 1
        self.manager.settings.max_viewers = 1
        self.transport.gate = asyncio.Event()
        first = asyncio.create_task(self.create())
        await self.transport.entered.wait()
        with self.assertRaises(LiveWebRTCError) as error:
            await self.create()
        self.assertEqual(error.exception.status, 429)
        self.transport.gate.set()
        await first
        self.assertEqual(self.manager.pending, 0)

    async def test_failed_pull_cleans_uncertain_source_and_releases_capacity(self):
        self.transport.create_error = True
        with self.assertRaises(LiveWebRTCError):
            await self.create()
        self.assertEqual(self.manager.pending, 0)
        self.assertEqual(self.manager.groups, {})
        self.assertEqual(len(self.transport.deleted), 1)

    async def test_failed_cleanup_is_visible_and_blocks_pipeline_reuse(self):
        session = await self.create()
        self.transport.delete_error = True
        with self.assertLogs("frigate.comms.live_webrtc", "WARNING"):
            await self.manager.delete("front", "viewer", session["session_id"])
        with self.assertRaises(LiveWebRTCError) as error:
            await self.create()
        self.assertEqual(error.exception.code, "live_webrtc_cleanup_pending")
        self.transport.delete_error = False
        await self.manager._maintain_once()
        self.assertEqual(self.manager.groups, {})

    async def test_answer_candidates_and_quality_stay_on_owned_peer(self):
        result = await self.create(height=720)
        identifier = result["session_id"]
        with self.assertRaises(LiveWebRTCError):
            await self.manager.candidates(
                "front", "viewer", identifier, CandidateBody(candidates=[])
            )
        await self.manager.answer(
            "front", "viewer", identifier, AnswerBody(sdp="v=0\r\nsynthetic-answer\r\n")
        )
        socket = self.transport.sockets[0]
        self.assertEqual(socket.messages[0]["command"], "answer")
        self.assertEqual(socket.messages[1]["rendition_name"], "720p")
        with self.assertRaises(LiveWebRTCError):
            await self.manager.answer(
                "front",
                "viewer",
                identifier,
                AnswerBody(sdp="v=0\r\nsecond-answer\r\n"),
            )
        await self.manager.candidates(
            "front", "viewer", identifier, CandidateBody(candidates=[])
        )
        await self.manager.quality("front", "viewer", identifier, QualityBody())
        self.assertTrue(socket.messages[-1]["auto"])
        self.assertEqual(len(self.transport.sockets), 1)
        with self.assertRaises(LiveWebRTCError):
            await self.manager.quality(
                "front", "viewer", identifier, QualityBody(height=2160)
            )

    async def test_source_names_cannot_reenter_existing_transcode_pipeline(self):
        self.config.cameras["front"].live.transcode.source = "front_transcode_720p"
        with self.assertRaises(LiveWebRTCError):
            await self.create()
        self.assertEqual(self.transport.created, [])

    async def test_relay_only_requires_authenticated_relay_configuration(self):
        with self.assertRaises(LiveWebRTCError) as error:
            await self.create(force_relay=True)
        self.assertEqual(error.exception.code, "live_webrtc_relay_not_configured")

    async def test_shutdown_cancels_pending_creation_before_closing_transport(self):
        self.transport.gate = asyncio.Event()
        pending = asyncio.create_task(self.create())
        await asyncio.wait_for(self.transport.entered.wait(), timeout=1)
        await asyncio.wait_for(self.manager.close(), timeout=1)
        self.assertTrue(pending.cancelled())
        self.assertEqual(self.manager.pending, 0)
        self.assertEqual(self.manager.creating, set())
        self.assertEqual(self.manager.groups, {})
        self.assertEqual(len(self.transport.deleted), 1)

    async def test_720_grid_reports_its_own_bitrate_not_the_detail_bitrate(self):
        self.settings.grid_height = 720
        self.manager.settings = self.settings.model_copy(deep=True)
        response = await self.create(view="grid")
        self.assertEqual(response["renditions"], [{"height": 720, "bitrate": 500}])

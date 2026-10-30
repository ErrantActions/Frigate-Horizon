"""Portable scoped peer tests; no microphone, media server or camera is used."""

import asyncio
import unittest

from frigate.comms.native_talkback import (
    NativeTalkbackConflict,
    NativeTalkbackLeases,
    NativeTalkbackUnavailable,
    OwnedTalkbackPeer,
    TalkbackIdentity,
)


class FakeOwnedTransport:
    def __init__(self):
        self.created = []
        self.renewed = []
        self.closed = []
        self.fail_close = False
        self.fail_renew = False
        self.gate = None

    async def create(self, stream, offer):
        if self.gate is not None:
            await self.gate.wait()
        peer = OwnedTalkbackPeer(f"{len(self.created) + 1:032x}", "synthetic-answer")
        self.created.append(peer)
        return peer

    async def renew_peer(self, identifier):
        if self.fail_renew:
            raise NativeTalkbackUnavailable("synthetic renewal failure")
        self.renewed.append(identifier)

    async def close_peer(self, identifier):
        if self.fail_close:
            raise NativeTalkbackUnavailable("synthetic cleanup failure")
        self.closed.append(identifier)

    async def close(self):
        pass


class NativeTalkbackLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.transport = FakeOwnedTransport()
        self.revoked = False
        self.now = 0.0

        async def authorize(identity, camera):
            if self.revoked:
                raise PermissionError("synthetic account revocation")

        self.manager = NativeTalkbackLeases(
            self.transport,
            authorize,
            clock=lambda: self.now,
            wall_clock=lambda: 1800000000 + self.now,
        )
        self.identity = TalkbackIdentity(
            "owner", "session-a", 1, 2000000000, "generation"
        )

    async def asyncTearDown(self):
        self.transport.fail_close = False
        await self.manager.close()

    async def open(self, camera="front"):
        return await self.manager.create(
            camera, self.identity, "configured-source", "synthetic-offer"
        )

    async def test_release_closes_only_its_peer_and_is_idempotent(self):
        first = await self.open("front")
        second = await self.open("garden")
        await self.manager.release(first["lease_id"], "front", self.identity)
        await self.manager.release(first["lease_id"], "front", self.identity)
        self.assertEqual(self.transport.closed, [self.transport.created[0].identifier])
        self.assertIn(second["lease_id"], self.manager.leases)

    async def test_one_camera_cannot_have_competing_owners(self):
        await self.open()
        with self.assertRaises(NativeTalkbackConflict):
            await self.open()
        self.assertEqual(len(self.transport.created), 1)

    async def test_renewal_is_bound_to_the_original_session_and_camera(self):
        value = await self.open()
        other = TalkbackIdentity(
            "owner", "different-session", 1, 2000000000, "generation"
        )
        with self.assertRaises(NativeTalkbackConflict):
            await self.manager.renew(value["lease_id"], "front", other)
        with self.assertRaises(NativeTalkbackConflict):
            await self.manager.renew(value["lease_id"], "garden", self.identity)

    async def test_revocation_closes_the_peer_before_renewal_can_succeed(self):
        value = await self.open()
        self.revoked = True
        with self.assertRaises(PermissionError):
            await self.manager.renew(value["lease_id"], "front", self.identity)
        self.assertEqual(self.transport.closed, [self.transport.created[0].identifier])
        self.assertNotIn(value["lease_id"], self.manager.leases)

    async def test_failed_renewal_stops_the_owned_peer(self):
        value = await self.open()
        self.transport.fail_renew = True
        with self.assertRaises(NativeTalkbackUnavailable):
            await self.manager.renew(value["lease_id"], "front", self.identity)
        self.assertNotIn(value["lease_id"], self.manager.leases)
        self.assertEqual(len(self.transport.closed), 1)

    async def test_expired_or_hard_limited_lease_cannot_be_extended(self):
        value = await self.open()
        for _ in range(23):
            self.now += 5
            result = await self.manager.renew(value["lease_id"], "front", self.identity)
            self.assertLessEqual(result["expires_at"], 1800000120)
        self.now = 121
        with self.assertRaises(NativeTalkbackConflict):
            await self.manager.renew(value["lease_id"], "front", self.identity)

    async def test_late_offer_after_release_is_collected_not_activated(self):
        self.transport.gate = asyncio.Event()
        opening = asyncio.create_task(self.open())
        await asyncio.sleep(0)
        identifier = next(iter(self.manager.leases))
        await self.manager.release(identifier, "front", self.identity)
        self.transport.gate.set()
        with self.assertRaises(NativeTalkbackConflict):
            await opening
        self.assertEqual(self.transport.closed, [self.transport.created[0].identifier])
        self.assertEqual(self.manager.leases, {})

    async def test_cleanup_failure_is_explicit_and_blocks_new_camera_ownership(self):
        value = await self.open()
        self.transport.fail_close = True
        with self.assertRaises(NativeTalkbackUnavailable):
            await self.manager.release(value["lease_id"], "front", self.identity)
        self.assertTrue(self.manager.leases[value["lease_id"]].closing)
        with self.assertRaises(NativeTalkbackConflict):
            await self.open()
        self.transport.fail_close = False
        await self.manager.release(value["lease_id"], "front", self.identity)
        self.assertEqual(self.manager.leases, {})

    async def test_other_account_cannot_delete_a_known_lease(self):
        value = await self.open()
        other = TalkbackIdentity("other", "other-session", 1, 2000000000, "generation")
        with self.assertRaises(NativeTalkbackConflict):
            await self.manager.release(value["lease_id"], "front", other)
        self.assertEqual(self.transport.closed, [])

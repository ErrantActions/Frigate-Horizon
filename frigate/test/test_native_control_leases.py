"""Portable native-control safety checks; every camera command is a fake."""

import asyncio
import threading
import unittest
from uuid import uuid4

from frigate.comms.native_controls import (
    NativeControlConflict,
    NativeControlLeases,
    NativeControlUnavailable,
    NativePTZBody,
)


class NativeControlLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.managers = []

    async def asyncTearDown(self):
        for manager in self.managers:
            await manager.close()

    def manager(self, **kwargs):
        async def transport(*values):
            self.calls.append(values)

        value = NativeControlLeases(transport, **kwargs)
        self.managers.append(value)
        return value

    def body(self, identifier, command="MOVE_UP"):
        return NativePTZBody(command=command, lease_id=identifier)

    async def test_stop_before_move_tombstones_the_uuid(self):
        manager = self.manager()
        identifier = uuid4()
        await manager.command("front", "owner", self.body(identifier, "STOP"))
        with self.assertRaises(NativeControlConflict):
            await manager.command("front", "owner", self.body(identifier))
        self.assertEqual(self.calls, [])

    async def test_stale_stop_cannot_interrupt_a_newer_lease(self):
        manager = self.manager()
        first, second = uuid4(), uuid4()
        await manager.command("front", "owner", self.body(first))
        await manager.command("front", "owner", self.body(first, "STOP"))
        await manager.command("front", "owner", self.body(second))
        before = len(self.calls)
        with self.assertRaises(NativeControlConflict):
            await manager.command("front", "owner", self.body(first, "STOP"))
        self.assertEqual(len(self.calls), before)
        self.assertEqual(manager.leases["front"].identifier, str(second))

    async def test_reserved_uuid_cannot_be_revived_after_a_manager_restart(self):
        used = set()

        def reserve(camera, owner, identity, timestamp):
            key = (camera, owner, identity)
            if key in used:
                return False
            used.add(key)
            return True

        identifier = uuid4()
        first = self.manager(reserve_tombstone=reserve)
        await first.command("front", "owner", self.body(identifier))
        await first.command("front", "owner", self.body(identifier, "STOP"))
        await first.close()
        second = self.manager(reserve_tombstone=reserve)
        before = len(self.calls)
        with self.assertRaises(NativeControlConflict):
            await second.command("front", "owner", self.body(identifier))
        await second.command("front", "owner", self.body(identifier, "STOP"))
        self.assertEqual(len(self.calls), before)

    async def test_persistent_store_failure_cannot_start_movement(self):
        def reserve(*_):
            raise NativeControlUnavailable("storage unavailable")

        manager = self.manager(reserve_tombstone=reserve)
        with self.assertRaises(NativeControlUnavailable):
            await manager.command("front", "owner", self.body(uuid4()))
        self.assertEqual(self.calls, [])
        self.assertEqual(manager.leases, {})

    async def test_shutdown_during_reservation_cannot_start_late_movement(self):
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        def reserve(*_):
            loop.call_soon_threadsafe(started.set)
            if not release.wait(5):
                raise NativeControlUnavailable("test reservation timed out")
            return True

        manager = self.manager(reserve_tombstone=reserve)
        command = asyncio.create_task(
            manager.command("front", "owner", self.body(uuid4()))
        )
        await started.wait()
        await manager.close()
        release.set()
        with self.assertRaises(NativeControlUnavailable):
            await command
        self.assertEqual(self.calls, [])

    async def test_capacity_fails_closed_instead_of_evicting_a_tombstone(self):
        manager = self.manager(max_entries=1)
        first = uuid4()
        await manager.command("front", "owner", self.body(first, "STOP"))
        with self.assertRaises(NativeControlUnavailable):
            await manager.command("front", "owner", self.body(uuid4()))
        with self.assertRaises(NativeControlConflict):
            await manager.command("front", "owner", self.body(first))
        self.assertEqual(self.calls, [])

    async def test_expired_deadman_stops_before_a_new_command(self):
        clock = [0.0]
        manager = self.manager(clock=lambda: clock[0])
        first = uuid4()
        await manager.command("front", "owner", self.body(first))
        clock[0] = 0.6
        await manager.command("front", "owner", self.body(uuid4(), "MOVE_DOWN"))
        self.assertEqual(
            [call[1] for call in self.calls], ["MOVE_UP", "STOP", "MOVE_DOWN"]
        )

    async def test_other_owner_cannot_renew_or_stop_current_movement(self):
        manager = self.manager()
        identifier = uuid4()
        await manager.command("front", "first", self.body(identifier))
        for command in ["MOVE_DOWN", "STOP"]:
            with self.assertRaises(NativeControlConflict):
                await manager.command("front", "other", self.body(identifier, command))
        self.assertEqual([call[1] for call in self.calls], ["MOVE_UP"])

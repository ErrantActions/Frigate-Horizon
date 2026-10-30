"""Serialized, short native PTZ leases with a server-owned deadman stop."""

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

logger = logging.getLogger(__name__)
PTZCommand = Literal[
    "MOVE_UP",
    "MOVE_DOWN",
    "MOVE_LEFT",
    "MOVE_RIGHT",
    "ZOOM_IN",
    "ZOOM_OUT",
    "STOP",
    "PRESET",
]


class NativeControlConflict(ValueError):
    """A stale control request must not affect a newer camera operation."""


class NativeControlUnavailable(ValueError):
    """A camera has no verified configured control transport."""


class NativePTZBody(BaseModel):
    """Native clients never choose camera credentials or hardware destinations."""

    model_config = ConfigDict(extra="forbid")
    command: PTZCommand
    preset: str | None = Field(default=None, min_length=1, max_length=256)
    lease_id: UUID
    duration_ms: Literal[500] = 500

    @model_validator(mode="after")
    def preset_only_for_preset(self):
        """Do not reinterpret an arbitrary preset value as a movement command."""
        if (self.command == "PRESET") != (self.preset is not None):
            raise ValueError("A preset ID is required only for PRESET")
        return self


ControlTransport = Callable[
    [str, PTZCommand, str | None, str, int, str], Awaitable[None]
]
TombstoneReservation = Callable[[str, str, str, float], bool]


@dataclass
class _Lease:
    owner: str
    identifier: str
    deadline: float
    hard_deadline: float
    timer: asyncio.Task | None = None


@dataclass
class _CameraLock:
    lock: asyncio.Lock
    users: int = 0


class NativeControlLeases:
    """500ms deadman, 30s hold cap and bounded replay tombstones.

    Terminated UUIDs are rejected for 150 seconds (the maximum hold plus a
    two-minute delayed-request window). Clients must use a fresh UUID per press.
    Capacity exhaustion fails closed instead of evicting live tombstones.
    """

    def __init__(
        self,
        transport: ControlTransport,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        max_entries: int = 4096,
        max_cameras: int = 128,
        reserve_tombstone: TombstoneReservation | None = None,
    ) -> None:
        self.transport = transport
        self.clock = clock
        self.wall_clock = wall_clock
        self.max_entries = max_entries
        self.max_cameras = max_cameras
        self.reserve_tombstone = reserve_tombstone
        self._entry_lock = asyncio.Lock()
        self.locks: dict[str, _CameraLock] = {}
        self.leases: dict[str, _Lease] = {}
        self.entries: OrderedDict[tuple[str, str, str], float] = OrderedDict()
        self.closed = False
        self._gc: asyncio.TimerHandle | None = None

    def _prune(self) -> None:
        self._gc = None
        now = self.clock()
        while self.entries and next(iter(self.entries.values())) <= now:
            self.entries.popitem(last=False)
        if self.entries and not self.closed:
            delay = max(0.01, next(iter(self.entries.values())) - now)
            self._gc = asyncio.get_running_loop().call_later(delay, self._prune)

    async def _remember(
        self, camera: str, owner: str, identity: str, *, require_new=False
    ) -> None:
        async with self._entry_lock:
            key = (camera, owner, identity)
            if key in self.entries:
                if require_new:
                    raise NativeControlConflict("PTZ lease has already been used")
                return
            if self._gc is not None:
                self._gc.cancel()
            self._prune()
            if len(self.entries) >= self.max_entries:
                raise NativeControlUnavailable("Camera control replay capacity is full")
            if self.reserve_tombstone is not None:
                accepted = await asyncio.to_thread(
                    self.reserve_tombstone, camera, owner, identity, self.wall_clock()
                )
                if not accepted and require_new:
                    raise NativeControlConflict("PTZ lease has already been used")
            if self.closed:
                raise NativeControlUnavailable("Camera controls are shutting down")
            self.entries[key] = self.clock() + 150
            if self._gc is None:
                self._gc = asyncio.get_running_loop().call_later(150, self._prune)

    @asynccontextmanager
    async def _camera_lock(self, camera: str):
        slot = self.locks.get(camera)
        if slot is None:
            if len(self.locks) >= self.max_cameras:
                raise NativeControlUnavailable("Camera control capacity is full")
            slot = self.locks[camera] = _CameraLock(asyncio.Lock())
        slot.users += 1
        try:
            async with slot.lock:
                yield
        finally:
            slot.users -= 1
            if not slot.users and camera not in self.leases:
                self.locks.pop(camera, None)

    async def command(self, camera: str, owner: str, body: NativePTZBody) -> dict:
        """Acknowledge movement only after its bounded transport call succeeds."""
        async with self._camera_lock(camera):
            if self.closed:
                raise NativeControlUnavailable("Camera controls are shutting down")
            old = self.leases.get(camera)
            identity = str(body.lease_id)
            if old is not None and old.deadline <= self.clock():
                await self._stop(camera, old)
                old = None
            if body.command == "STOP":
                # STOP can arrive before MOVE. Remember even an unknown UUID.
                await self._remember(camera, owner, identity)
                if old is not None and (
                    old.owner != owner or old.identifier != identity
                ):
                    raise NativeControlConflict("PTZ lease is stale")
                if old is not None:
                    await self._stop(camera, old)
                return {
                    "success": True,
                    "lease_id": identity,
                    "expires_at": self.wall_clock(),
                }
            if old is not None and (old.owner != owner or old.identifier != identity):
                raise NativeControlConflict("Camera already has an active PTZ lease")
            if old is None:
                if (camera, owner, identity) in self.entries:
                    raise NativeControlConflict("PTZ lease has ended")
                await self._remember(camera, owner, identity, require_new=True)
                now = self.clock()
                lease = _Lease(owner, identity, now + 0.5, now + 30)
                self.leases[camera] = lease
                lease.timer = asyncio.create_task(self._deadman(camera, lease))
            else:
                lease = old
                lease.deadline = min(self.clock() + 0.5, lease.hard_deadline)
                if lease.deadline <= self.clock():
                    await self._stop(camera, lease)
                    raise NativeControlConflict("PTZ hold limit reached")
            try:
                async with asyncio.timeout(0.5):
                    await self.transport(
                        camera,
                        body.command,
                        body.preset,
                        identity,
                        body.duration_ms,
                        owner,
                    )
            except BaseException:
                try:
                    await self._stop(camera, lease)
                except Exception as stop_error:
                    logger.error(
                        "Native PTZ safety stop failed for %s (%s)",
                        camera,
                        type(stop_error).__name__,
                    )
                raise
            return {
                "success": True,
                "lease_id": identity,
                "expires_at": self.wall_clock() + max(0, lease.deadline - self.clock()),
            }

    def _forget(self, camera: str, lease: _Lease) -> None:
        if self.leases.get(camera) is lease:
            self.leases.pop(camera)
        if lease.timer is not None and lease.timer is not asyncio.current_task():
            lease.timer.cancel()

    async def _stop(self, camera: str, lease: _Lease) -> None:
        if self.leases.get(camera) is not lease:
            raise NativeControlConflict("PTZ lease is stale")
        try:
            async with asyncio.timeout(2):
                await self.transport(
                    camera, "STOP", None, lease.identifier, 500, lease.owner
                )
        finally:
            self._forget(camera, lease)

    async def _deadman(self, camera: str, lease: _Lease) -> None:
        try:
            while self.leases.get(camera) is lease:
                await asyncio.sleep(max(0, lease.deadline - self.clock()))
                async with self._camera_lock(camera):
                    if (
                        self.leases.get(camera) is lease
                        and lease.deadline <= self.clock()
                    ):
                        await self._stop(camera, lease)
        except NativeControlConflict:
            logger.debug("Skipped superseded native PTZ stop for %s", camera)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error(
                "Native PTZ deadman failed for %s (%s)", camera, type(error).__name__
            )

    async def close(self) -> None:
        """Stop owned movement on shutdown, without issuing commands to idle cameras."""
        self.closed = True
        if self._gc is not None:
            self._gc.cancel()
            self._gc = None
        for camera, lease in list(self.leases.items()):
            async with self._camera_lock(camera):
                try:
                    await self._stop(camera, lease)
                except NativeControlConflict:
                    self._forget(camera, lease)
                except Exception as error:
                    logger.error(
                        "Native PTZ shutdown stop failed for %s (%s)",
                        camera,
                        type(error).__name__,
                    )
        self.entries.clear()

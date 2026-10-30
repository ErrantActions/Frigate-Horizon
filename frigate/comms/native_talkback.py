"""Owned, revocable talkback leases; no shared stream can be stopped or deleted."""

import asyncio
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import uuid4

import httpx

logger = logging.getLogger(__name__)


class NativeTalkbackUnavailable(ValueError):
    """The private media lifecycle service is not available."""


class NativeTalkbackConflict(ValueError):
    """A lease expired, changed ownership, or conflicts with another session."""


@dataclass(frozen=True)
class TalkbackIdentity:
    username: str
    session_id: str
    issued_at: float
    expires_at: float
    signing_generation: str

    def __post_init__(self) -> None:
        if (
            not self.username
            or not self.session_id
            or not self.signing_generation
            or not math.isfinite(self.issued_at)
            or not math.isfinite(self.expires_at)
            or self.expires_at <= self.issued_at
        ):
            raise NativeTalkbackConflict("The camera audio session identity is invalid")


@dataclass(frozen=True)
class OwnedTalkbackPeer:
    identifier: str
    answer: str


class OwnedTalkbackTransport:
    """Only the fixed private-loopback scoped-consumer API is used."""

    def __init__(self) -> None:
        self.client = httpx.AsyncClient(
            base_url="http://127.0.0.1:1984",
            timeout=httpx.Timeout(10, connect=2),
            follow_redirects=False,
            trust_env=False,
        )

    @staticmethod
    def _value(response: httpx.Response) -> dict:
        response.raise_for_status()
        if len(response.content) > 262144:
            raise NativeTalkbackUnavailable(
                "The owned media response exceeds its bound"
            )
        value = response.json()
        if not isinstance(value, dict):
            raise NativeTalkbackUnavailable("The owned media response is invalid")
        return value

    async def available(self) -> bool:
        try:
            value = self._value(await self.client.get("/api/horizon/webrtc"))
            return (
                value.get("protocol") == "horizon-owned-webrtc-v1"
                and value.get("owned_consumers") is True
                and value.get("lease_ttl_seconds") == 15
                and value.get("max_duration_seconds") == 120
            )
        except (httpx.HTTPError, ValueError) as error:
            logger.debug(
                "Owned talkback service unavailable (%s)", type(error).__name__
            )
            return False

    async def create(self, stream: str, offer: str) -> OwnedTalkbackPeer:
        if not await self.available():
            raise NativeTalkbackUnavailable(
                "The scoped camera audio service is not installed"
            )
        value = self._value(
            await self.client.post(
                "/api/horizon/webrtc",
                params={"src": stream},
                json={"type": "offer", "sdp": offer},
            )
        )
        identifier, answer = value.get("peer_id"), value.get("sdp")
        if (
            not isinstance(identifier, str)
            or re.fullmatch(r"[a-f0-9]{32}", identifier) is None
        ):
            raise NativeTalkbackUnavailable(
                "The scoped camera audio service returned no owned peer"
            )
        if (
            value.get("type") != "answer"
            or not isinstance(answer, str)
            or not answer
            or len(answer) > 131072
        ):
            await self.close_peer(identifier)
            raise NativeTalkbackUnavailable("The scoped camera audio answer is invalid")
        return OwnedTalkbackPeer(identifier, answer)

    async def renew_peer(self, identifier: str) -> None:
        value = self._value(
            await self.client.patch("/api/horizon/webrtc", params={"id": identifier})
        )
        if value.get("peer_id") != identifier or value.get("renew_after_ms") != 5000:
            raise NativeTalkbackUnavailable(
                "The scoped camera audio renewal is invalid"
            )

    async def close_peer(self, identifier: str) -> None:
        value = self._value(
            await self.client.delete("/api/horizon/webrtc", params={"id": identifier})
        )
        if value.get("success") is not True:
            raise NativeTalkbackUnavailable(
                "The scoped camera audio service did not confirm closure"
            )

    async def close(self) -> None:
        await self.client.aclose()


@dataclass
class _TalkbackLease:
    identifier: str
    camera: str
    identity: TalkbackIdentity
    deadline: float
    maximum: float
    peer: OwnedTalkbackPeer | None = None
    closing: bool = False
    cleanup_deadline: float | None = None
    task: asyncio.Task | None = None


AuthorizeTalkback = Callable[[TalkbackIdentity, str], Awaitable[None]]


class NativeTalkbackLeases:
    def __init__(
        self,
        transport,
        authorize: AuthorizeTalkback,
        *,
        clock=time.monotonic,
        wall_clock=time.time,
        maximum=32,
    ) -> None:
        self.transport = transport
        self.authorize = authorize
        self.clock = clock
        self.wall_clock = wall_clock
        self.maximum = maximum
        self.leases: dict[str, _TalkbackLease] = {}
        self.finished: dict[str, tuple[str, str, float]] = {}
        self.lock = asyncio.Lock()
        self.closed = False

    def _public(self, lease: _TalkbackLease) -> dict:
        return {
            "lease_id": lease.identifier,
            "expires_at": self.wall_clock() + max(0, lease.deadline - self.clock()),
            "renew_after_ms": 5000,
        }

    def _owned(
        self,
        identifier: str,
        camera: str,
        identity: TalkbackIdentity,
        *,
        renewing: bool,
    ) -> _TalkbackLease:
        lease = self.leases.get(identifier)
        if (
            lease is None
            or lease.camera != camera
            or lease.identity.username != identity.username
        ):
            raise NativeTalkbackConflict("This camera audio lease is unavailable")
        if renewing and (
            lease.identity.session_id != identity.session_id or lease.closing
        ):
            raise NativeTalkbackConflict("This camera audio session changed")
        return lease

    async def create(
        self, camera: str, identity: TalkbackIdentity, stream: str, offer: str
    ) -> dict:
        await self.authorize(identity, camera)
        async with self.lock:
            now = self.clock()
            self.finished = {
                key: value for key, value in self.finished.items() if value[2] > now
            }
            if self.closed or len(self.leases) >= self.maximum:
                raise NativeTalkbackUnavailable("Camera audio capacity is unavailable")
            if any(value.camera == camera for value in self.leases.values()):
                raise NativeTalkbackConflict(
                    "This camera already has an owned audio session"
                )
            lease = _TalkbackLease(str(uuid4()), camera, identity, now + 15, now + 120)
            self.leases[lease.identifier] = lease
            lease.task = asyncio.create_task(self._watch(lease))
        peer = None
        try:
            peer = await self.transport.create(stream, offer)
            async with self.lock:
                if (
                    self.leases.get(lease.identifier) is not lease
                    or lease.closing
                    or self.clock() >= lease.deadline
                ):
                    raise NativeTalkbackConflict("Camera audio negotiation expired")
                lease.peer = peer
            await self.authorize(identity, camera)
            await self.transport.renew_peer(peer.identifier)
            async with self.lock:
                if lease.closing or self.leases.get(lease.identifier) is not lease:
                    raise NativeTalkbackConflict(
                        "Camera audio negotiation was cancelled"
                    )
                lease.deadline = min(self.clock() + 15, lease.maximum)
            return {**self._public(lease), "type": "answer", "sdp": peer.answer}
        except BaseException:
            if peer is not None and lease.peer is None:
                try:
                    await self.transport.close_peer(peer.identifier)
                except (httpx.HTTPError, ValueError) as error:
                    logger.error(
                        "Unclaimed camera audio cleanup failed (%s)",
                        type(error).__name__,
                    )
            await self._terminate(lease, propagate=False)
            raise

    async def renew(
        self, identifier: str, camera: str, identity: TalkbackIdentity
    ) -> dict:
        async with self.lock:
            lease = self._owned(identifier, camera, identity, renewing=True)
            if self.clock() >= min(lease.deadline, lease.maximum) or lease.peer is None:
                raise NativeTalkbackConflict("This camera audio lease expired")
        try:
            await self.authorize(lease.identity, camera)
            await self.transport.renew_peer(lease.peer.identifier)
            async with self.lock:
                if (
                    self.leases.get(identifier) is not lease
                    or lease.closing
                    or self.clock() >= lease.maximum
                ):
                    raise NativeTalkbackConflict("This camera audio lease ended")
                lease.deadline = min(self.clock() + 15, lease.maximum)
                return self._public(lease)
        except BaseException:
            await self._terminate(lease, propagate=False)
            raise

    async def release(
        self, identifier: str, camera: str, identity: TalkbackIdentity
    ) -> None:
        async with self.lock:
            previous = self.finished.get(identifier)
            if (
                previous is not None
                and previous[:2] == (identity.username, camera)
                and previous[2] > self.clock()
            ):
                return
            lease = self._owned(identifier, camera, identity, renewing=False)
        await self._terminate(lease, propagate=True)

    async def _terminate(self, lease: _TalkbackLease, *, propagate: bool) -> None:
        async with self.lock:
            if self.leases.get(lease.identifier) is not lease:
                return
            lease.closing = True
            if lease.cleanup_deadline is None:
                lease.cleanup_deadline = self.clock() + 35
        try:
            if lease.peer is not None:
                await self.transport.close_peer(lease.peer.identifier)
        except (httpx.HTTPError, ValueError) as error:
            logger.error(
                "Owned camera audio cleanup pending (%s)", type(error).__name__
            )
            if propagate:
                raise NativeTalkbackUnavailable(
                    "Camera audio cleanup is pending its bounded lease expiry"
                ) from error
            return
        async with self.lock:
            if self.leases.get(lease.identifier) is lease:
                self.leases.pop(lease.identifier)
                self.finished[lease.identifier] = (
                    lease.identity.username,
                    lease.camera,
                    self.clock() + 180,
                )
                while len(self.finished) > 1024:
                    self.finished.pop(next(iter(self.finished)))
        if lease.task is not None and lease.task is not asyncio.current_task():
            lease.task.cancel()

    async def _watch(self, lease: _TalkbackLease) -> None:
        try:
            while self.leases.get(lease.identifier) is lease:
                await asyncio.sleep(1)
                if (
                    lease.closing
                    and lease.cleanup_deadline is not None
                    and self.clock() >= lease.cleanup_deadline
                ):
                    logger.warning(
                        "Owned camera audio cleanup exceeded acknowledgement window; media TTL is exhausted"
                    )
                    async with self.lock:
                        if self.leases.get(lease.identifier) is lease:
                            self.leases.pop(lease.identifier)
                    return
                if lease.closing or self.clock() >= min(lease.deadline, lease.maximum):
                    await self._terminate(lease, propagate=False)
                else:
                    try:
                        await self.authorize(lease.identity, lease.camera)
                    except (ValueError, PermissionError) as error:
                        logger.warning(
                            "Camera audio authorization revoked (%s)",
                            type(error).__name__,
                        )
                        await self._terminate(lease, propagate=False)
        except asyncio.CancelledError:
            raise

    async def close(self) -> None:
        self.closed = True
        for lease in tuple(self.leases.values()):
            await self._terminate(lease, propagate=False)
        tasks = [lease.task for lease in self.leases.values() if lease.task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.leases.clear()
        await self.transport.close()

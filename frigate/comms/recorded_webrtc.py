"""Bounded original-recording copies feeding viewer-owned native WebRTC channels.

Only catalog planning is reused from compatibility playback. No HLS is generated.
Each generation warms with a bounded repeating first item in a private cache.
Only a decoded-ready activation replaces it with a finite, nonrepeating program.
Pause removes its encoder. Resume and seek return a new offer and generation.
Native currentPosition is an input read cursor, not a decoded-frame timestamp.
"""

import asyncio
import json
import logging
import math
import os
import re
import shutil
import stat
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import aiohttp
from peewee import DatabaseError

from frigate.api.defs.request.playback_body import PlaybackCreateBody
from frigate.api.defs.request.recorded_webrtc_body import (
    RecordedAnswer,
    RecordedCandidates,
    RecordedControl,
    RecordedCreate,
    RecordedQuality,
    RecordedWindow,
)
from frigate.comms.live_webrtc import (
    DETAIL_BITRATES,
    DETAIL_HEIGHTS,
    LiveWebRTCError,
    LiveWebRTCManager,
    MediaGroup,
    ViewerSession,
    application_name,
    turn_ice_servers,
)
from frigate.const import MAX_SEGMENT_DURATION
from frigate.models import Recordings
from frigate.record.access import recording_account_allows
from frigate.record.lease_async import AsyncRecordingLease, AsyncRecordingLeaseLost
from frigate.record.playback import PlaybackError, SourceClip, plan_sources

logger = logging.getLogger(__name__)
GENERATION_BYTES = 128 * 1024 * 1024
TOTAL_BYTES = 512 * 1024 * 1024
MAX_WINDOW_SECONDS = 30
PREPARE_SECONDS = 45
NATIVE_WAIT_SECONDS = 12
UNCERTAIN_CREATE_SECONDS = 30
WARMUP_SECONDS = 20
WARMUP_ITEM_MS = 200
STREAM_NAME = re.compile(r"fr_[0-9a-f]{32}")


async def finish_cleanup(work):
    """Join cleanup even if a disconnected request is repeatedly cancelled."""
    task = asyncio.create_task(work)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return result


@dataclass
class ScheduledItem:
    """Private file identity and public wall-clock to compressed-media mapping."""

    url: str
    offset_ms: int
    duration_ms: int
    source_start: float
    source_end: float
    media_start: float

    def public(self) -> dict:
        """Do not serialize private file names to clients."""
        return {
            "source_start": self.source_start,
            "source_end": self.source_end,
            "media_start": self.media_start,
            "media_end": self.media_start + self.duration_ms / 1000,
        }


def load_sources(camera: str, window: RecordedWindow) -> list[SourceClip]:
    """Use the original main/archive catalog and close worker-owned DB connections."""
    database = Recordings._meta.database
    if database is None:
        raise LiveWebRTCError(503, "recorded_webrtc_catalog_unavailable")
    opened_here = database.is_closed()
    try:
        return plan_sources(
            camera,
            PlaybackCreateBody(
                **window.model_dump(),
                client_id="recorded-webrtc",
                generation=0,
            ),
        )
    except PlaybackError as error:
        raise LiveWebRTCError(error.status, error.code) from None
    finally:
        if opened_here:
            database.close()


def current_account_allows(session: "RecordedSession", config) -> bool:
    """Revoke running or paused viewers on current account/password/role changes."""
    return recording_account_allows(
        session.owner, session.camera, session.auth_issued, config
    )


def copy_sources(
    clips: list[SourceClip],
    window: RecordedWindow,
    directory: Path,
    lease: AsyncRecordingLease,
) -> list[ScheduledItem]:
    """Copy only bounded read-only originals, entirely off the event/media threads."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    copied: dict[str, str] = {}
    items = []
    total = 0
    media_start = 0.0
    for clip in clips:
        lease.check()
        start = max(
            clip.start,
            clip.wall_start if clip.wall_start is not None else clip.start,
            window.start_timestamp,
        )
        end = min(
            clip.start + clip.duration,
            clip.wall_end if clip.wall_end is not None else clip.start + clip.duration,
            window.before,
        )
        if end - start < 0.001:
            continue
        if clip.path not in copied:
            name = f"source-{len(copied)}.mp4"
            target = directory / name
            # Paths come exclusively from the existing catalog, including archives.
            with open(clip.path, "rb") as source:
                details = os.fstat(source.fileno())
                if not stat.S_ISREG(details.st_mode):
                    raise LiveWebRTCError(422, "recorded_webrtc_source_not_regular")
                if details.st_size > GENERATION_BYTES - total:
                    raise LiveWebRTCError(507, "recorded_webrtc_copy_capacity")
                remaining = details.st_size
                with target.open("xb") as output:
                    target.chmod(0o600)
                    while remaining:
                        lease.check()
                        data = source.read(min(1024 * 1024, remaining))
                        if not data:
                            raise LiveWebRTCError(503, "recorded_webrtc_source_changed")
                        output.write(data)
                        total += len(data)
                        remaining -= len(data)
                    if source.read(1):
                        raise LiveWebRTCError(503, "recorded_webrtc_source_changed")
            copied[clip.path] = name
        offset_ms = round((clip.offset + start - clip.start) * 1000)
        duration_ms = math.floor((end - start) * 1000)
        if duration_ms <= 0:
            continue
        items.append(
            ScheduledItem(
                f"file://{directory.name}/{copied[clip.path]}",
                offset_ms,
                duration_ms,
                start,
                start + duration_ms / 1000,
                media_start,
            )
        )
        media_start += duration_ms / 1000
    if not items or not items[0].source_start <= window.start_timestamp + 0.002:
        raise LiveWebRTCError(422, "playback_start_in_gap")
    if media_start > MAX_WINDOW_SECONDS + 0.001:
        raise LiveWebRTCError(422, "recorded_webrtc_window_too_large")
    lease.check()
    return items


class ScheduledTransport:
    """Pinned OME v0.21 Scheduled Channel REST with asynchronous lifecycle barriers."""

    def __init__(self, live: LiveWebRTCManager):
        self.live = live

    async def request(self, method: str, url: str, *, payload=None, missing=False):
        """Never return native error bodies or source paths across the API boundary."""
        try:
            async with self.live.transport.http.request(
                method, url, json=payload, allow_redirects=False
            ) as response:
                if response.status == 404 and missing:
                    return None
                if response.status not in (200, 201, 202, 204):
                    raise LiveWebRTCError(503, "recorded_webrtc_engine_unavailable")
                if method == "GET":
                    data = await response.json(content_type=None)
                    if not isinstance(data, dict):
                        raise ValueError("Invalid native response")
                    return data.get("response", data)
        except (aiohttp.ClientError, TimeoutError, ValueError, TypeError):
            raise LiveWebRTCError(503, "recorded_webrtc_engine_unavailable") from None

    @staticmethod
    def channel_url(group: MediaGroup) -> str:
        """Address only server-generated stream and application identities."""
        return (
            "http://127.0.0.1:8081/v1/vhosts/default/apps/"
            f"{group.app}/scheduledChannels/{group.stream}"
        )

    async def state(self, group: MediaGroup):
        """Read native schedule state privately; currentPosition is relative input time."""
        return await self.request("GET", self.channel_url(group), missing=True)

    async def create(self, generation: "RecordedMedia") -> None:
        """Warm the requested input offset without consuming the full media window."""
        group = generation.group
        payload = {
            "stream": {
                "name": group.stream,
                "bypassTranscoder": False,
                "videoTrack": True,
                "audioTrack": generation.audio,
            },
            "programs": [self.program(generation, warming=True)],
        }
        generation.attempted = True
        generation.attempted_at = time.monotonic()
        await self.request(
            "POST", self.channel_url(group).rsplit("/", 1)[0], payload=payload
        )
        deadline = time.monotonic() + NATIVE_WAIT_SECONDS
        stream_url = self.live.transport.stream_url(group) + "/" + group.stream
        while time.monotonic() < deadline:
            if await self.request("GET", stream_url, missing=True) is not None:
                generation.confirmed = True
                return
            await asyncio.sleep(0.1)
        raise LiveWebRTCError(503, "recorded_webrtc_source_not_ready")

    @staticmethod
    def program(generation: "RecordedMedia", *, warming: bool) -> dict:
        """Keep the original track definition and private copied-file identities."""
        return {
            "name": generation.warmup_name if warming else generation.playback_name,
            "scheduled": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "repeat": warming,
            "items": [
                {
                    "url": item.url,
                    "start": item.offset_ms,
                    "duration": min(WARMUP_ITEM_MS, item.duration_ms)
                    if warming
                    else item.duration_ms,
                }
                for item in (generation.items[:1] if warming else generation.items)
            ],
        }

    async def activate(self, generation: "RecordedMedia") -> None:
        """Replace only programs and confirm the new program on the existing stream."""
        # A timed-out PATCH can recreate a schedule after DELETE. Treat every write,
        # not just the initial POST, as uncertain until its own phase is observed.
        generation.attempted_at = time.monotonic()
        generation.confirmed = False
        await self.request(
            "PATCH",
            self.channel_url(generation.group),
            payload={"programs": [self.program(generation, warming=False)]},
        )
        deadline = time.monotonic() + NATIVE_WAIT_SECONDS
        while time.monotonic() < deadline:
            state = await self.state(generation.group)
            program = state.get("currentProgram") if isinstance(state, dict) else None
            if (
                isinstance(program, dict)
                and program.get("name") == generation.playback_name
            ):
                generation.confirmed = True
                return
            await asyncio.sleep(0.1)
        raise LiveWebRTCError(503, "recorded_webrtc_activation_not_ready")

    async def delete(self, generation: "RecordedMedia") -> None:
        """Retain cache/admission until both native channel and stream are absent.

        REST writes schedule files asynchronously. Repeated deletion and private
        file removal handle a POST or PATCH accepted before a request timeout.
        """
        group = generation.group
        stream_url = self.live.transport.stream_url(group) + "/" + group.stream
        schedule = self.live.runtime / "schedules" / group.app / (group.stream + ".sch")
        deadline = time.monotonic() + NATIVE_WAIT_SECONDS
        absent_since = None
        while time.monotonic() < deadline:
            try:
                await self.request("DELETE", self.channel_url(group), missing=True)
            finally:
                await asyncio.to_thread(schedule.unlink, missing_ok=True)
            channel = await self.state(group)
            stream = await self.request("GET", stream_url, missing=True)
            if channel is None and stream is None:
                if absent_since is None:
                    absent_since = time.monotonic()
                settled_mutation = (
                    generation.confirmed
                    or time.monotonic() - generation.attempted_at
                    >= UNCERTAIN_CREATE_SECONDS
                )
                if time.monotonic() - absent_since >= 1 and settled_mutation:
                    return
            else:
                absent_since = None
            await asyncio.sleep(0.1)
        raise LiveWebRTCError(503, "recorded_webrtc_cleanup_pending")


@dataclass
class RecordedMedia:
    group: MediaGroup
    directory: Path
    items: list[ScheduledItem] = field(default_factory=list)
    audio: bool = False
    audio_reason: str | None = None
    attempted: bool = False
    attempted_at: float = 0
    confirmed: bool = False
    warmup_name: str = field(default_factory=lambda: "warm_" + uuid.uuid4().hex)
    playback_name: str = field(default_factory=lambda: "play_" + uuid.uuid4().hex)
    warmup_deadline: float = 0
    peer: ViewerSession | None = None
    watcher: asyncio.Task | None = None
    stop_at: float = 0
    cleanup_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    orphaned: bool = False


@dataclass
class RecordedSession:
    identifier: str
    owner: str
    camera: str
    options: RecordedCreate
    window: RecordedWindow
    deadline: float
    hard_deadline: float
    auth_issued: float
    generation: int = 1
    state: str = "preparing"
    active: RecordedMedia | None = None
    pending_cleanup: RecordedMedia | None = None
    cleanup_state: str = "paused"
    position: float | None = None
    timeline: list[dict] = field(default_factory=list)
    audio: bool = False
    audio_reason: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closing: bool = False


class RecordedWebRTCManager:
    """Own recorded viewers while sharing live CPU, viewer and signaling resources."""

    def __init__(
        self,
        live: LiveWebRTCManager,
        *,
        transport: ScheduledTransport | None = None,
        planner=load_sources,
        lease_factory=AsyncRecordingLease,
        account_check=current_account_allows,
    ):
        self.live = live
        self.transport = transport or ScheduledTransport(live)
        self.planner = planner
        self.lease_factory = lease_factory
        self.account_check = account_check
        self.root = live.runtime / "recorded-media"
        self.sessions: dict[str, RecordedSession] = {}
        self.generations: dict[str, RecordedMedia] = {}
        self.operations: set[asyncio.Task] = set()
        self.worker: asyncio.Task | None = None
        self.closed = False
        self.ready = False

    async def start(self) -> None:
        """Recover only this manager's persisted private generations after restart."""
        if self.live.available():
            try:
                metadata = await asyncio.to_thread(
                    lambda: json.loads((self.live.runtime / "runtime.json").read_text())
                )
                if metadata.get("recorded_webrtc_version") != 1:
                    logger.error(
                        "Recorded WebRTC requires regenerated media configuration"
                    )
                    self.worker = asyncio.create_task(self._maintain())
                    return
                await asyncio.to_thread(
                    self.root.mkdir, mode=0o700, parents=True, exist_ok=True
                )
                stale = await asyncio.to_thread(self._stale)
                for app, stream, directory in stale:
                    media = RecordedMedia(
                        MediaGroup("", app, stream, "detail"),
                        directory,
                        attempted=True,
                        orphaned=True,
                    )
                    self.generations[stream] = media
                    # Recovery is already consuming resources, not new admission.
                    async with self.live.admission:
                        self.live.external_encoders.add(stream)
                    await self._stop_media(media)
                self.ready = True
            except (OSError, ValueError, TypeError):
                logger.error("Recorded WebRTC cache is unavailable")
        self.worker = asyncio.create_task(self._maintain())

    def _stale(self):
        applications = {
            application_name("detail", height, audio)
            for height in DETAIL_HEIGHTS
            for audio in (False, True)
        }
        return [
            (app.name, directory.name, directory)
            for app in self.root.iterdir()
            if app.name in applications and app.is_dir() and not app.is_symlink()
            for directory in app.iterdir()
            if STREAM_NAME.fullmatch(directory.name)
            and directory.is_dir()
            and not directory.is_symlink()
        ]

    def available(self) -> bool:
        """Do not advertise recorded viewing without the shared configured runtime."""
        return (
            self.ready
            and not self.closed
            and self.live.available()
            and not self.live.config().ui.recorded_http
        )

    def _check_camera(self, camera: str) -> None:
        if camera not in self.live.config().cameras:
            raise LiveWebRTCError(404, "camera_not_available")

    def owned(self, camera: str, owner: str, identifier: str) -> RecordedSession:
        """Camera and account ownership apply even to paused viewer leases."""
        session = self.sessions.get(identifier)
        if (
            session is None
            or session.closing
            or session.owner != owner
            or session.camera != camera
        ):
            raise LiveWebRTCError(404, "recorded_webrtc_session_not_found")
        if session.deadline <= self.live.clock() or not self.available():
            raise LiveWebRTCError(410, "recorded_webrtc_session_expired")
        self._check_camera(camera)
        return session

    def _validate_generation(self, session: RecordedSession, generation: int) -> None:
        if session.closing or self.sessions.get(session.identifier) is not session:
            raise LiveWebRTCError(404, "recorded_webrtc_session_not_found")
        if session.deadline <= self.live.clock() or not self.available():
            raise LiveWebRTCError(410, "recorded_webrtc_session_expired")
        self._check_camera(session.camera)
        if generation != session.generation:
            raise LiveWebRTCError(409, "recorded_webrtc_stale_generation")
        if (
            session.state == "warming"
            and session.active is not None
            and session.active.warmup_deadline <= self.live.clock()
        ):
            raise LiveWebRTCError(410, "recorded_webrtc_warmup_expired")

    def _heights(self, session: RecordedSession) -> tuple[int, ...]:
        maximum = min(session.options.max_height, self.live.settings.max_height)
        return tuple(height for height in DETAIL_HEIGHTS if height <= maximum)

    async def create(
        self,
        camera: str,
        owner: str,
        body: RecordedCreate,
        auth_expires: float,
        *,
        auth_issued: float | None = None,
    ) -> dict:
        """Reserve the shared viewer slot before planning or copying any originals."""
        if not self.available():
            raise LiveWebRTCError(503, "recorded_webrtc_not_available")
        self._check_camera(camera)
        now = self.live.clock()
        hard = min(auth_expires, now + self.live.settings.max_session_seconds)
        if hard <= now:
            raise LiveWebRTCError(401, "recorded_webrtc_session_expired")
        session = RecordedSession(
            str(uuid.uuid4()),
            owner,
            camera,
            body.model_copy(deep=True),
            RecordedWindow(**body.model_dump(include=set(RecordedWindow.model_fields))),
            min(now + self.live.settings.lease_seconds, hard),
            hard,
            now if auth_issued is None else auth_issued,
        )
        if body.height is not None and body.height not in self._heights(session):
            raise LiveWebRTCError(422, "recorded_webrtc_rendition_not_available")
        if body.force_relay and not self.live.settings.turn.enabled:
            raise LiveWebRTCError(409, "recorded_webrtc_relay_not_configured")
        task = asyncio.current_task()
        self.operations.add(task)
        admitted = False
        try:
            await self.live.reserve_external(session.identifier)
            self.sessions[session.identifier] = session
            async with session.lock:
                try:
                    await self._prepare(session)
                    if not await asyncio.to_thread(
                        self.account_check, session, self.live.config()
                    ):
                        raise LiveWebRTCError(401, "recorded_webrtc_session_revoked")
                    self._validate_generation(session, session.generation)
                    admitted = True
                    return self.metadata(session, offer=True)
                finally:
                    if not admitted:
                        session.closing = True
                        self.sessions.pop(session.identifier, None)
                        await finish_cleanup(self._dispose(session))
        finally:
            self.operations.discard(task)

    async def _prepare(self, session: RecordedSession) -> None:
        stream = "fr_" + uuid.uuid4().hex
        maximum = min(session.options.max_height, self.live.settings.max_height)
        # Audio app is chosen only after inspecting every selected source span.
        media = RecordedMedia(
            MediaGroup(session.camera, "", stream, "detail"), self.root / stream
        )
        async with self.live.admission:
            if (len(self.generations) + 1) * GENERATION_BYTES > TOTAL_BYTES:
                raise LiveWebRTCError(507, "recorded_webrtc_copy_capacity")
            # Prevent simultaneous preparations from overcommitting the byte budget.
            self.generations[stream] = media
        session.active = media
        encoder_reserved = False
        try:
            await self.live.reserve_external(stream, encoder=True)
            encoder_reserved = True
            async with asyncio.timeout(PREPARE_SECONDS):
                window = session.window
                # The lease also covers any keyframe back-snap used by the planner.
                async with self.lease_factory(
                    session.camera,
                    max(0, window.after - MAX_SEGMENT_DURATION),
                    window.before,
                    owner=session.owner,
                ) as lease:
                    clips = await lease.guard(
                        lease.run_blocking(self.planner, session.camera, window)
                    )
                    media.audio = session.options.audio and all(
                        clip.has_audio is True for clip in clips
                    )
                    media.audio_reason = (
                        None
                        if media.audio
                        else (
                            "not_requested"
                            if not session.options.audio
                            else "source_audio_absent_heterogeneous_or_unknown"
                        )
                    )
                    media.group.app = application_name("detail", maximum, media.audio)
                    media.directory = self.root / media.group.app / stream
                    media.items = await lease.guard(
                        lease.run_blocking(
                            copy_sources, clips, window, media.directory, lease
                        )
                    )
                media.warmup_deadline = min(
                    self.live.clock() + WARMUP_SECONDS, session.hard_deadline
                )
                await self.transport.create(media)
                socket, message = await self.live.transport.offer(media.group)
                media.peer = ViewerSession(
                    stream,
                    session.owner,
                    session.camera,
                    media.group,
                    session.deadline,
                    session.hard_deadline,
                    socket,
                    message["id"],
                    message["peer_id"],
                    message["sdp"],
                    message["candidates"],
                    heights=self._heights(session),
                    initial_height=session.options.height,
                )
                session.timeline = [item.public() for item in media.items]
                session.audio = media.audio
                session.audio_reason = media.audio_reason
                session.position = None
                session.state = "warming"
                if media.warmup_deadline <= self.live.clock():
                    raise LiveWebRTCError(410, "recorded_webrtc_warmup_expired")
                media.watcher = asyncio.create_task(self._watch(session, media))
        except (OSError, DatabaseError, AsyncRecordingLeaseLost, TimeoutError):
            raise LiveWebRTCError(503, "recorded_webrtc_source_unavailable") from None
        finally:
            if media.peer is None:
                session.active = None
                media.orphaned = True
                if not encoder_reserved:
                    self.generations.pop(stream, None)
                else:
                    session.pending_cleanup = media
                    session.cleanup_state = "paused"
                    session.state = "cleanup_pending"
                    if await finish_cleanup(self._stop_media(media)):
                        session.pending_cleanup = None
                        session.state = "paused"

    def metadata(self, session: RecordedSession, *, offer: bool = False) -> dict:
        """REST-relative URLs and honest source mapping, never native URLs or paths."""
        base = (
            f"{quote(session.camera, safe='')}/recordings/webrtc/sessions/"
            f"{session.identifier}"
        )
        result = {
            "version": 1,
            "protocol": "webrtc",
            "session_id": session.identifier,
            "generation": session.generation,
            "state": session.state,
            "warmup_expires_at": (
                session.active.warmup_deadline
                if session.state == "warming" and session.active is not None
                else None
            ),
            "expires_at": session.deadline,
            "heartbeat_seconds": max(5, self.live.settings.lease_seconds // 3),
            "answer_url": base + "/answer",
            "candidates_url": base + "/candidates",
            "quality_url": base + "/quality",
            "control_url": base + "/control",
            "heartbeat_url": base,
            "close_url": base,
            "ice_servers": turn_ice_servers(
                self.live.settings,
                self.live.turn_secret,
                f"{session.identifier}:{session.generation}",
                self.live.clock(),
            ),
            "ice_transport_policy": "relay" if session.options.force_relay else "all",
            "renditions": [
                {"height": height, "bitrate": DETAIL_BITRATES[height]}
                for height in self._heights(session)
            ],
            "initial_height": session.options.height,
            "window": session.window.model_dump(),
            "timeline": session.timeline,
            "position_timestamp": session.position,
            "position_kind": "input_read_cursor",
            "gap_policy": "skip",
            "audio": session.audio,
            "audio_reason": session.audio_reason,
            "renegotiate": offer and session.active is not None,
            "offer": None,
            "ice_candidates": [],
        }
        if offer and session.active is not None and session.active.peer is not None:
            result["offer"] = session.active.peer.offer
            result["ice_candidates"] = session.active.peer.candidates
        return result

    def _peer(self, session: RecordedSession) -> ViewerSession:
        if session.active is None or session.active.peer is None:
            raise LiveWebRTCError(409, "recorded_webrtc_peer_not_active")
        return session.active.peer

    async def answer(
        self, camera: str, owner: str, identifier: str, body: RecordedAnswer
    ):
        """Apply an answer exactly once, only to the current server generation."""
        session = self.owned(camera, owner, identifier)
        async with session.lock:
            self._validate_generation(session, body.generation)
            peer = self._peer(session)
            if peer.answered:
                raise LiveWebRTCError(409, "recorded_webrtc_already_answered")
            await self._send(
                peer,
                {
                    "command": "answer",
                    "id": peer.peer_id,
                    "peer_id": peer.remote_peer_id,
                    "sdp": {"type": body.type, "sdp": body.sdp},
                },
            )
            peer.answered = True
            await self.live._quality(peer, session.options.height)

    async def _send(self, peer: ViewerSession, message: dict):
        try:
            await peer.socket.send_json(message)
        except (aiohttp.ClientError, ConnectionError):
            raise LiveWebRTCError(503, "recorded_webrtc_signaling_closed") from None

    async def candidates(
        self, camera: str, owner: str, identifier: str, body: RecordedCandidates
    ):
        """Reject stale ICE even when a seek races a previously queued request."""
        session = self.owned(camera, owner, identifier)
        async with session.lock:
            self._validate_generation(session, body.generation)
            peer = self._peer(session)
            if not peer.answered:
                raise LiveWebRTCError(409, "recorded_webrtc_answer_required")
            await self._send(
                peer,
                {
                    "command": "candidate",
                    "id": peer.peer_id,
                    "peer_id": peer.remote_peer_id,
                    "candidates": [
                        item.model_dump(by_alias=True, exclude_none=True)
                        for item in body.candidates
                    ],
                },
            )

    async def quality(
        self, camera: str, owner: str, identifier: str, body: RecordedQuality
    ):
        """Switch ABR/manual rendition on the existing answered peer."""
        session = self.owned(camera, owner, identifier)
        async with session.lock:
            self._validate_generation(session, body.generation)
            peer = self._peer(session)
            if not peer.answered:
                raise LiveWebRTCError(409, "recorded_webrtc_answer_required")
            await self.live._quality(peer, body.height)
            session.options.height = body.height

    async def _position(self, session: RecordedSession) -> float | None:
        media = session.active
        if media is None:
            return session.position
        state = await self.transport.state(media.group)
        if not isinstance(state, dict):
            return None
        program = state.get("currentProgram")
        expected = (
            media.warmup_name if session.state == "warming" else media.playback_name
        )
        if not isinstance(program, dict) or program.get("name") != expected:
            return None
        item = program.get("currentItem") if isinstance(program, dict) else None
        if not isinstance(item, dict):
            return None
        cursor = item.get("currentPosition")
        if (
            isinstance(cursor, bool)
            or not isinstance(cursor, (int, float))
            or not math.isfinite(cursor)
            or cursor < 0
        ):
            return None
        items = media.items[:1] if session.state == "warming" else media.items
        for index, selected in enumerate(items):
            duration_ms = (
                min(WARMUP_ITEM_MS, selected.duration_ms)
                if session.state == "warming"
                else selected.duration_ms
            )
            if (
                item.get("url") == selected.url
                and item.get("start") == selected.offset_ms
                and item.get("duration") == duration_ms
            ):
                if (
                    session.state != "warming"
                    and cursor >= selected.duration_ms
                    and index + 1 < len(media.items)
                ):
                    return media.items[index + 1].source_start
                return selected.source_start + min(cursor, duration_ms) / 1000
        return None

    async def _activate(self, session: RecordedSession) -> dict:
        """A client decoded-ready assertion, not an SDP answer, starts real playback."""
        peer = self._peer(session)
        if not peer.answered:
            raise LiveWebRTCError(409, "recorded_webrtc_answer_required")
        if session.state == "playing":
            return self.metadata(session)
        if session.state != "warming":
            raise LiveWebRTCError(409, "recorded_webrtc_not_warming")
        media = session.active
        activated = False
        try:
            remaining = min(media.warmup_deadline, session.deadline) - self.live.clock()
            if remaining <= 0:
                raise LiveWebRTCError(410, "recorded_webrtc_warmup_expired")
            async with asyncio.timeout(remaining):
                await self.transport.activate(media)
            self._validate_generation(session, session.generation)
            session.state = "playing"
            session.position = None
            media.stop_at = (
                self.live.clock()
                + sum(item.duration_ms for item in media.items) / 1000
                + 15
            )
            activated = True
            return self.metadata(session)
        except TimeoutError:
            raise LiveWebRTCError(503, "recorded_webrtc_activation_not_ready") from None
        finally:
            if not activated:
                session.active = None
                session.position = None
                session.state = "cleanup_pending"
                session.pending_cleanup = media
                session.cleanup_state = "ended"
                media.orphaned = True
                if await finish_cleanup(self._stop_media(media)):
                    session.pending_cleanup = None
                    session.state = "ended"

    async def control(
        self, camera: str, owner: str, identifier: str, body: RecordedControl
    ) -> dict:
        """Pause destroys the channel; resume/seek require a new peer and answer."""
        session = self.owned(camera, owner, identifier)
        task = asyncio.current_task()
        self.operations.add(task)
        try:
            async with session.lock:
                self._validate_generation(session, body.generation)
                if body.action == "activate":
                    return await self._activate(session)
                if session.pending_cleanup is not None:
                    if not await finish_cleanup(
                        self._stop_media(session.pending_cleanup)
                    ):
                        raise LiveWebRTCError(503, "recorded_webrtc_cleanup_pending")
                    session.pending_cleanup = None
                    session.state = session.cleanup_state
                if body.action == "pause" and session.state == "paused":
                    return self.metadata(session)
                if body.action == "resume" and session.state != "paused":
                    raise LiveWebRTCError(409, "recorded_webrtc_not_paused")
                if body.action == "pause":
                    position = await self._position(session)
                    if position is None:
                        raise LiveWebRTCError(503, "recorded_webrtc_cursor_unavailable")
                    session.position = position
                session.generation += 1
                media, session.active = session.active, None
                session.state = "paused"
                if media is not None:
                    media.orphaned = True
                    session.pending_cleanup = media
                    session.cleanup_state = "paused"
                    session.state = "cleanup_pending"
                    if not await finish_cleanup(self._stop_media(media)):
                        raise LiveWebRTCError(503, "recorded_webrtc_cleanup_pending")
                    session.pending_cleanup = None
                    session.state = session.cleanup_state
                if body.action == "pause":
                    return self.metadata(session)
                if body.action == "seek":
                    session.window = body.window.model_copy(deep=True)
                else:
                    if (
                        session.position is None
                        or session.position >= session.window.before - 0.001
                        or (
                            session.timeline
                            and session.position
                            >= session.timeline[-1]["source_end"] - 0.001
                        )
                    ):
                        raise LiveWebRTCError(409, "recorded_webrtc_window_ended")
                    session.window = RecordedWindow(
                        **session.window.model_dump(exclude={"start_timestamp"}),
                        start_timestamp=session.position,
                    )
                try:
                    await self._prepare(session)
                    self._validate_generation(session, session.generation)
                except BaseException:
                    session.state = (
                        "cleanup_pending"
                        if session.pending_cleanup is not None
                        else "paused"
                    )
                    media, session.active = session.active, None
                    if media is not None:
                        media.orphaned = True
                        await finish_cleanup(self._stop_media(media))
                    raise
                return self.metadata(session, offer=True)
        finally:
            self.operations.discard(task)

    async def renew(self, camera: str, owner: str, identifier: str) -> dict:
        """Heartbeat never extends JWT expiry; cursor failures are explicitly unknown."""
        session = self.owned(camera, owner, identifier)
        async with session.lock:
            self._validate_generation(session, session.generation)
            if (
                session.pending_cleanup is not None
                and session.pending_cleanup.group.stream not in self.generations
            ):
                session.pending_cleanup = None
                session.state = session.cleanup_state
            try:
                session.position = await self._position(session)
            except LiveWebRTCError:
                session.position = None
            session.deadline = min(
                self.live.clock() + self.live.settings.lease_seconds,
                session.hard_deadline,
            )
            return self.metadata(session)

    async def delete(self, camera: str, owner: str, identifier: str) -> None:
        """An expired owned viewer can still be explicitly closed."""
        session = self.sessions.get(identifier)
        if session is None or session.owner != owner or session.camera != camera:
            raise LiveWebRTCError(404, "recorded_webrtc_session_not_found")
        await finish_cleanup(self._end(session))

    async def _watch(self, session: RecordedSession, media: RecordedMedia):
        try:
            async for message in media.peer.socket:
                if message.type in (
                    aiohttp.WSMsgType.ERROR,
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                ):
                    break
                if message.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(message.data)
                    if (
                        not isinstance(data, dict)
                        or data.get("code", 200) != 200
                        or data.get("command") == "error"
                    ):
                        break
        except asyncio.CancelledError:
            return
        except (aiohttp.ClientError, ConnectionError, ValueError, TypeError):
            pass
        async with session.lock:
            if session.active is media and not session.closing:
                session.active = None
                session.state = "ended"
                session.position = None
                media.orphaned = True
                await self._stop_media(media)

    async def _stop_media(self, media: RecordedMedia) -> bool:
        async with media.cleanup_lock:
            if media.group.stream not in self.generations:
                return True
            if (
                media.watcher is not None
                and media.watcher is not asyncio.current_task()
            ):
                media.watcher.cancel()
                await asyncio.gather(media.watcher, return_exceptions=True)
            if media.peer is not None:
                try:
                    await asyncio.wait_for(media.peer.socket.close(), 3)
                except (aiohttp.ClientError, TimeoutError):
                    pass
            try:
                if media.attempted:
                    await self.transport.delete(media)
                await asyncio.to_thread(self._remove_directory, media.directory)
            except (LiveWebRTCError, OSError):
                media.orphaned = True
                logger.warning("Recorded WebRTC cleanup pending")
                return False
            self.generations.pop(media.group.stream, None)
            await self.live.release_external(media.group.stream)
            return True

    @staticmethod
    def _remove_directory(directory: Path):
        if directory.exists():
            shutil.rmtree(directory)

    async def _dispose(self, session: RecordedSession):
        media, session.active = session.active, None
        if media is not None:
            media.orphaned = True
            await self._stop_media(media)
        if session.pending_cleanup is not None:
            await self._stop_media(session.pending_cleanup)
            session.pending_cleanup = None
        await self.live.release_external(session.identifier)

    async def _end(self, session: RecordedSession):
        async with session.lock:
            if self.sessions.get(session.identifier) is not session:
                return
            session.closing = True
            self.sessions.pop(session.identifier, None)
            await self._dispose(session)

    async def _maintain_once(self):
        for session in list(self.sessions.values()):
            camera = self.live.config().cameras.get(session.camera)
            if (
                session.deadline <= self.live.clock()
                or not self.available()
                or camera is None
                or not await asyncio.to_thread(
                    self.account_check, session, self.live.config()
                )
            ):
                await self._end(session)
            elif self._media_expired(session):
                async with session.lock:
                    if not self._media_expired(session):
                        continue
                    media, session.active = session.active, None
                    if media is not None:
                        session.state = "ended"
                        session.position = None
                        media.orphaned = True
                        await self._stop_media(media)

        for media in list(self.generations.values()):
            if media.orphaned:
                await self._stop_media(media)

    def _media_expired(self, session: RecordedSession) -> bool:
        media = session.active
        if media is None:
            return False
        if session.state == "warming":
            return media.warmup_deadline <= self.live.clock()
        return session.state == "playing" and media.stop_at <= self.live.clock()

    async def _maintain(self):
        while True:
            await asyncio.sleep(2)
            await self._maintain_once()

    async def close(self):
        """Stop recorded work before the owner closes the shared native transport."""
        self.closed = True
        if self.worker is not None:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        tasks = [task for task in self.operations if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for session in list(self.sessions.values()):
            await self._end(session)
        for media in list(self.generations.values()):
            await self._stop_media(media)

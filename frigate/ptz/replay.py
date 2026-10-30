"""Bounded browser replay sessions with secret-free FFmpeg invocation and cleanup."""

import asyncio
import contextlib
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime

from frigate.ptz.replay_transport import ReplayGateway, ReplaySource, ReplayUnsupported

MAX_REPLAY_SECONDS = 300
MAX_SESSIONS = 2
UNCLAIMED_SECONDS = 20
READ_TIMEOUT = 15
WALL_TIMEOUT = 360


class ReplayCapacityError(RuntimeError):
    """The bounded replay pool already has a session for this camera or is full."""


@dataclass
class ReplaySession:
    camera: str
    source: ReplaySource = field(repr=False)
    start: datetime
    end: datetime
    ffmpeg: str
    configuration: object = field(repr=False)
    identifier: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    created: float = field(default_factory=time.monotonic)
    phase: str = "ready"
    gateway: ReplayGateway | None = field(default=None, repr=False)
    process: asyncio.subprocess.Process | None = field(default=None, repr=False)
    spawn_task: asyncio.Task[asyncio.subprocess.Process] | None = field(
        default=None, repr=False
    )
    cleanup_task: asyncio.Task | None = field(default=None, repr=False)
    deadline: asyncio.Task | None = field(default=None, repr=False)
    released: bool = False
    error_code: str | None = None
    audio: bool | None = None
    audio_status: str = "pending"

    def public(self):
        """Return only an opaque identity, fixed range, and non-secret status."""
        return {
            "id": self.identifier,
            "state": self.phase,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "expires_in": max(0, UNCLAIMED_SECONDS - (time.monotonic() - self.created))
            if self.phase == "ready"
            else WALL_TIMEOUT,
            "max_duration_seconds": MAX_REPLAY_SECONDS,
            "audio": self.audio,
            "audio_status": self.audio_status,
            "audio_codec": "aac_lc" if self.audio else None,
            "audio_sample_rate": 48000 if self.audio else None,
            "error_code": self.error_code,
        }


class OnvifReplayManager:
    """Own all browser replay resources in the FastAPI event loop."""

    def __init__(self, config_provider, *, process_factory=None, gateway_factory=None):
        self.config_provider = config_provider
        self.sessions: dict[str, ReplaySession] = {}
        self.resolving: set[str] = set()
        self.discovery_tasks: dict[str, asyncio.Task] = {}
        self.process_factory = process_factory or asyncio.create_subprocess_exec
        self.gateway_factory = gateway_factory or ReplayGateway
        self.closing = False
        self.finished: dict[str, tuple[str, dict, float]] = {}

    def reserve(self, camera):
        """Bound discovery too, before any camera RPC or DNS lookup is started."""
        if self.closing:
            raise RuntimeError("Replay is stopping")
        if (
            len(self.sessions) + len(self.resolving) >= MAX_SESSIONS
            or camera in self.resolving
            or any(session.camera == camera for session in self.sessions.values())
        ):
            raise ReplayCapacityError("Replay capacity is in use")
        self.resolving.add(camera)
        task = asyncio.current_task()
        if task is not None:
            self.discovery_tasks[camera] = task

    def finish_resolution(self, camera):
        """Release a cancellable discovery reservation on every exit path."""
        self.resolving.discard(camera)
        self.discovery_tasks.pop(camera, None)

    def create(self, camera, source, start, end, ffmpeg, configuration):
        """Reserve a bounded, unclaimed session; do not start any process yet."""
        duration = (end - start).total_seconds()
        if self.closing:
            raise RuntimeError("Replay is stopping")
        if not 0 < duration <= MAX_REPLAY_SECONDS:
            raise ReplayUnsupported("Replay range exceeds the supported duration")
        if self.config_provider() is not configuration:
            raise ReplayUnsupported("Saved camera configuration changed")
        self.finish_resolution(camera)
        if len(self.sessions) + len(self.resolving) >= MAX_SESSIONS or any(
            session.camera == camera for session in self.sessions.values()
        ):
            raise ReplayCapacityError("Replay capacity is in use")
        session = ReplaySession(camera, source, start, end, ffmpeg, configuration)
        if source.recorded_audio is False:
            session.audio = False
            session.audio_status = "not_recorded"
        self.sessions[session.identifier] = session
        session.deadline = asyncio.create_task(self._expire(session, UNCLAIMED_SECONDS))
        return session.public()

    def get(self, camera, identifier):
        """Do not allow a session identifier to change its authorized camera scope."""
        session = self.sessions.get(identifier)
        if session is None or session.camera != camera:
            raise KeyError("Replay session is not available")
        return session

    def status(self, camera, identifier):
        """Keep bounded, secret-free completion metadata for the browser's audio UI."""
        session = self.sessions.get(identifier)
        if session is not None and session.camera == camera:
            return session.public()
        self._prune_finished()
        completed = self.finished.get(identifier)
        if completed is None or completed[0] != camera:
            raise KeyError("Replay session is not available")
        return completed[1]

    def _prune_finished(self):
        for identifier, (_camera, _public, expires) in list(self.finished.items()):
            if expires <= time.monotonic():
                self.finished.pop(identifier, None)
        while len(self.finished) > 32:
            self.finished.pop(next(iter(self.finished)))

    async def _expire(self, session, seconds):
        await asyncio.sleep(seconds)
        session.phase = "expired"
        await self.close(session)

    def command(self, session, local_uri):
        """Pass no camera URI, password, or query token to FFmpeg argv."""
        return [
            session.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-threads",
            "2",
            "-rtsp_transport",
            "tcp",
            "-protocol_whitelist",
            "rtsp,tcp,rtp",
            "-i",
            local_uri,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-map_metadata",
            "-1",
            "-map_metadata:s:v",
            "-1",
            "-map_metadata:s:a",
            "-1",
            "-map_chapters",
            "-1",
            "-t",
            str((session.end - session.start).total_seconds()),
            "-vf",
            "scale=w='trunc(min(1280,iw)/2)*2':h=-2",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-profile:v",
            "baseline",
            "-tag:v",
            "avc1",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-profile:a",
            "aac_low",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-b:a",
            "128k",
            "-threads",
            "2",
            "-filter_threads",
            "1",
            "-g",
            "30",
            "-b:v",
            "2500k",
            "-maxrate",
            "4000k",
            "-bufsize",
            "8000k",
            "-avoid_negative_ts",
            "make_zero",
            "-frag_duration",
            "1000000",
            "-movflags",
            "frag_keyframe+empty_moov+default_base_moof",
            "-f",
            "mp4",
            "pipe:1",
        ]

    async def prepare(self, camera, identifier):
        """Claim once and verify initial media before committing an HTTP 200."""
        session = self.get(camera, identifier)
        if session.phase != "ready" or session.released:
            raise ReplayUnsupported("Replay sessions may only be consumed once")
        if self.config_provider() is not session.configuration:
            await self.close(session)
            raise ReplayUnsupported("Saved camera configuration changed")
        session.phase = "starting"
        if session.deadline:
            session.deadline.cancel()
        session.deadline = asyncio.create_task(self._expire(session, WALL_TIMEOUT))
        try:
            session.gateway = self.gateway_factory(
                session.source, session.start, session.end
            )
            uri = await session.gateway.start_server()
            session.spawn_task = asyncio.create_task(
                self.process_factory(
                    *self.command(session, uri),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            )
            session.process = await asyncio.wait_for(
                asyncio.shield(session.spawn_task), READ_TIMEOUT
            )
            if session.released:
                raise ReplayUnsupported("Replay was cancelled")
            if session.process.stdout is None:
                raise ReplayUnsupported("Replay video pipe is unavailable")
            first = bytearray()
            async with asyncio.timeout(READ_TIMEOUT):
                while not self._has_fragment(first):
                    chunk = await session.process.stdout.read(65536)
                    if not chunk:
                        raise ReplayUnsupported(
                            "Camera did not provide replay video frames"
                        )
                    first.extend(chunk)
                    if len(first) > 2_000_000:
                        raise ReplayUnsupported(
                            "Replay startup exceeds the supported size"
                        )
            if session.gateway.error_code not in (None, "camera_ended"):
                raise ReplayUnsupported("Camera did not provide bounded replay video")
            audio = self.audio_track(bytes(first))
            if audio is not None and audio != ("mp4a", 48000):
                raise ReplayUnsupported(
                    "Replay did not produce browser-compatible AAC at 48 kHz"
                )
            session.audio = audio is not None
            session.audio_status = (
                "available"
                if session.audio
                else "not_recorded"
                if session.source.recorded_audio is False
                else "unavailable"
                if session.source.recorded_audio is True
                else "not_present"
            )
            session.phase = "streaming"
            return session, bytes(first)
        except BaseException:
            await self.close(session)
            raise

    @staticmethod
    def audio_track(content):
        """Read only the bounded MP4 audio sample entry produced by our transcoder."""

        def boxes(data):
            offset = 0
            while offset + 8 <= len(data):
                size = int.from_bytes(data[offset : offset + 4], "big")
                header = 8
                if size == 1:
                    if offset + 16 > len(data):
                        raise ReplayUnsupported("Invalid replay MP4 metadata")
                    size = int.from_bytes(data[offset + 8 : offset + 16], "big")
                    header = 16
                if size < header or offset + size > len(data):
                    break
                yield (
                    data[offset + 4 : offset + 8],
                    data[offset + header : offset + size],
                )
                offset += size

        def find(data, path):
            if not path:
                return [data]
            return [
                found
                for kind, child in boxes(data)
                if kind == path[0]
                for found in find(child, path[1:])
            ]

        audio = []
        for track in find(content, (b"moov", b"trak")):
            handlers = find(track, (b"mdia", b"hdlr"))
            if not any(handler[8:12] == b"soun" for handler in handlers):
                continue
            descriptions = find(track, (b"mdia", b"minf", b"stbl", b"stsd"))
            if len(descriptions) != 1 or len(descriptions[0]) < 8:
                raise ReplayUnsupported("Invalid replay audio metadata")
            entries = list(boxes(descriptions[0][8:]))
            if len(entries) != 1 or len(entries[0][1]) < 28:
                raise ReplayUnsupported("Invalid replay audio sample entry")
            codec, sample = entries[0]
            try:
                name = codec.decode("ascii")
            except UnicodeError:
                raise ReplayUnsupported("Invalid replay audio codec") from None
            audio.append((name, int.from_bytes(sample[24:28], "big") >> 16))
        if len(audio) > 1:
            raise ReplayUnsupported("Unexpected replay audio track count")
        return audio[0] if audio else None

    @staticmethod
    def _has_fragment(content):
        offset = 0
        while offset + 8 <= len(content):
            size = int.from_bytes(content[offset : offset + 4], "big")
            kind = content[offset + 4 : offset + 8]
            header = 8
            if size == 1:
                if offset + 16 > len(content):
                    return False
                size = int.from_bytes(content[offset + 8 : offset + 16], "big")
                header = 16
            if size < header or size > 2_000_000:
                raise ReplayUnsupported("Invalid replay MP4 output")
            if offset + size > len(content):
                return False
            if kind == b"moof":
                return True
            offset += size
        return False

    async def stream(self, session, first, connected):
        """Stream bounded fragmented MP4 and release every resource on disconnect."""
        try:
            if session.process is None or session.process.stdout is None:
                raise ReplayUnsupported("Replay video pipe is unavailable")
            if not await connected():
                return
            yield first
            while (
                not session.released
                and self.config_provider() is session.configuration
                and await connected()
            ):
                chunk = await asyncio.wait_for(
                    session.process.stdout.read(65536), READ_TIMEOUT
                )
                if not chunk:
                    break
                if session.gateway.error_code not in (None, "camera_ended"):
                    session.error_code = session.gateway.error_code
                    break
                yield chunk
            session.phase = "ended"
        except (OSError, TimeoutError):
            session.error_code = "camera_unavailable"
            session.phase = "failed"
        finally:
            await self.close(session)

    async def close(self, session):
        """Shield cleanup from HTTP cancellation and await its single owned task."""
        if session.cleanup_task is None:
            session.cleanup_task = asyncio.create_task(self._cleanup(session))
        await asyncio.shield(session.cleanup_task)

    async def _cleanup(self, session):
        session.released = True
        if session.deadline and session.deadline is not asyncio.current_task():
            session.deadline.cancel()
        try:
            try:
                if session.spawn_task and session.process is None:
                    try:
                        session.process = await asyncio.wait_for(
                            asyncio.shield(session.spawn_task), 3
                        )
                    except (OSError, TimeoutError, asyncio.CancelledError):
                        session.spawn_task.cancel()
                        await asyncio.gather(session.spawn_task, return_exceptions=True)
                process = session.process
                if process and process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), 3)
                    except TimeoutError:
                        with contextlib.suppress(ProcessLookupError):
                            process.kill()
                        with contextlib.suppress(TimeoutError):
                            await asyncio.wait_for(process.wait(), 2)
            finally:
                if session.gateway:
                    await session.gateway.close()
        finally:
            self.finished[session.identifier] = (
                session.camera,
                session.public(),
                time.monotonic() + 60,
            )
            self._prune_finished()
            self.sessions.pop(session.identifier, None)

    async def shutdown(self):
        """Stop all replay processes and gateways before the API event loop closes."""
        self.closing = True
        discoveries = [
            task
            for task in self.discovery_tasks.values()
            if task is not None
            and task is not asyncio.current_task()
            and not task.done()
        ]
        for task in discoveries:
            task.cancel()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(*discoveries, return_exceptions=True), 15
            )
        self.discovery_tasks.clear()
        self.resolving.clear()
        await asyncio.gather(
            *(self.close(session) for session in list(self.sessions.values())),
            return_exceptions=True,
        )

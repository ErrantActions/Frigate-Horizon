"""Bounded browser replay sessions with secret-free FFmpeg invocation and cleanup."""

import asyncio
import contextlib
import re
import secrets
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from frigate.ptz.replay_transport import ReplayGateway, ReplaySource, ReplayUnsupported

MAX_REPLAY_SECONDS = 300
MAX_SESSIONS = 2
UNCLAIMED_SECONDS = 20
READ_TIMEOUT = 15
WALL_TIMEOUT = 360
IDLE_TIMEOUT = 30
MAX_RESOURCE_BYTES = 8 * 1024 * 1024
MAX_SESSION_BYTES = 256 * 1024 * 1024
HLS_RESOURCE = re.compile(r"(?:index\.m3u8|init\.mp4|segment_\d{5}\.m4s)")


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
    audio_requested: bool = True
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
    directory: tempfile.TemporaryDirectory | None = field(default=None, repr=False)
    media_path: Path | None = field(default=None, repr=False)
    prepare_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    monitor: asyncio.Task | None = field(default=None, repr=False)
    last_request: float = field(default_factory=time.monotonic)

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
            "protocol": "hls",
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

    def create(self, camera, source, start, end, ffmpeg, configuration, *, audio=True):
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
        session = ReplaySession(
            camera, source, start, end, ffmpeg, configuration, audio_requested=audio
        )
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
        if session.media_path is None:
            raise ReplayUnsupported("Replay HLS storage is unavailable")
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
            *(["-map", "0:a:0?"] if session.audio_requested else []),
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
            *(
                [
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
                ]
                if session.audio_requested
                else ["-an"]
            ),
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
            "-f",
            "hls",
            "-hls_time",
            "1",
            "-hls_list_size",
            "0",
            "-hls_playlist_type",
            "event",
            "-hls_segment_type",
            "fmp4",
            "-hls_fmp4_init_filename",
            "init.mp4",
            "-hls_flags",
            "independent_segments+temp_file",
            "-hls_segment_filename",
            str(session.media_path / "segment_%05d.m4s"),
            str(session.media_path / "index.m3u8"),
        ]

    async def prepare(self, camera, identifier):
        """Coalesce HLS startup and verify real initialization and fragment data."""
        session = self.get(camera, identifier)
        async with session.prepare_lock:
            session.last_request = time.monotonic()
            if session.released:
                raise KeyError("Replay session is not available")
            if self.config_provider() is not session.configuration:
                await self.close(session)
                raise ReplayUnsupported("Saved camera configuration changed")
            if session.phase in ("streaming", "complete"):
                return session
            return await self._prepare(session)

    async def _prepare(self, session):
        if session.phase != "ready" or session.released:
            raise ReplayUnsupported("Replay session is not ready")
        session.phase = "starting"
        if session.deadline:
            session.deadline.cancel()
        session.deadline = asyncio.create_task(self._expire(session, WALL_TIMEOUT))
        try:
            session.directory = await asyncio.to_thread(
                tempfile.TemporaryDirectory, prefix="frigate-onvif-hls-"
            )
            session.media_path = Path(session.directory.name)
            session.gateway = self.gateway_factory(
                session.source, session.start, session.end
            )
            uri = await session.gateway.start_server()
            session.spawn_task = asyncio.create_task(
                self.process_factory(
                    *self.command(session, uri),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            )
            session.process = await asyncio.wait_for(
                asyncio.shield(session.spawn_task), READ_TIMEOUT
            )
            if session.released:
                raise ReplayUnsupported("Replay was cancelled")
            async with asyncio.timeout(READ_TIMEOUT):
                while True:
                    first = await asyncio.to_thread(self._startup_media, session)
                    if first is not None:
                        break
                    if session.process.returncode is not None:
                        raise ReplayUnsupported(
                            "Camera did not provide replay video frames"
                        )
                    await asyncio.sleep(0.05)
            if session.gateway.error_code not in (None, "camera_ended"):
                raise ReplayUnsupported("Camera did not provide bounded replay video")
            initialization, fragment = first
            if not self._has_fragment(initialization + fragment):
                raise ReplayUnsupported("Replay HLS did not contain a video fragment")
            audio = self.audio_track(initialization)
            if audio is not None and audio != ("mp4a", 48000):
                raise ReplayUnsupported(
                    "Replay did not produce browser-compatible AAC at 48 kHz"
                )
            session.audio = audio is not None
            session.audio_status = (
                "available"
                if session.audio
                else "not_requested"
                if not session.audio_requested
                else "not_recorded"
                if session.source.recorded_audio is False
                else "unavailable"
                if session.source.recorded_audio is True
                else "not_present"
            )
            session.phase = "streaming"
            session.monitor = asyncio.create_task(self._watch(session))
            return session
        except BaseException:
            await self.close(session)
            raise

    @staticmethod
    def _read_resource(path, maximum=MAX_RESOURCE_BYTES):
        with path.open("rb") as source:
            data = source.read(maximum + 1)
        if len(data) > maximum:
            raise ReplayUnsupported("Replay HLS resource exceeds its size limit")
        return data

    @classmethod
    def _startup_media(cls, session):
        try:
            manifest = cls._read_resource(
                session.media_path / "index.m3u8", 65536
            ).decode("utf-8")
            segments = [
                line
                for line in manifest.splitlines()
                if line and not line.startswith("#")
            ]
            if not segments:
                return None
            if not all(re.fullmatch(r"segment_\d{5}\.m4s", name) for name in segments):
                raise ReplayUnsupported("Invalid replay HLS segment path")
            return (
                cls._read_resource(session.media_path / "init.mp4", 2_000_000),
                cls._read_resource(session.media_path / segments[0], 2_000_000),
            )
        except FileNotFoundError:
            return None

    async def resource(self, camera, identifier, resource):
        """Read only one bounded file from this camera's authorized replay session."""
        if HLS_RESOURCE.fullmatch(resource) is None:
            raise KeyError("Replay HLS resource is not available")
        session = await self.prepare(camera, identifier)
        data = await asyncio.to_thread(
            self._read_resource, session.media_path / resource
        )
        if session.released or self.config_provider() is not session.configuration:
            raise ReplayUnsupported("Saved replay session changed")
        session.last_request = time.monotonic()
        return data

    @staticmethod
    def _storage_bytes(session):
        total = 0
        for path in session.media_path.iterdir():
            try:
                total += path.stat().st_size
            except FileNotFoundError:
                continue
        return total

    async def _watch(self, session):
        while not session.released:
            if time.monotonic() - session.last_request > IDLE_TIMEOUT:
                session.phase = "expired"
                await self.close(session)
                return
            if (
                self.config_provider() is not session.configuration
                or session.gateway.error_code not in (None, "camera_ended")
                or await asyncio.to_thread(self._storage_bytes, session)
                > MAX_SESSION_BYTES
            ):
                session.phase = "failed"
                session.error_code = "replay_unavailable"
                await self.close(session)
                return
            if session.process.returncode is not None:
                if session.process.returncode != 0:
                    session.phase = "failed"
                    session.error_code = "replay_encoding_failed"
                    await self.close(session)
                    return
                session.phase = "complete"
            await asyncio.sleep(0.5)

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

    async def close(self, session):
        """Shield cleanup from HTTP cancellation and await its single owned task."""
        if session.cleanup_task is None:
            session.cleanup_task = asyncio.create_task(
                self._cleanup(session, asyncio.current_task())
            )
        await asyncio.shield(session.cleanup_task)

    async def _cleanup(self, session, initiator=None):
        session.released = True
        background = [
            task
            for task in (session.deadline, session.monitor)
            if task is not None and task is not initiator
        ]
        for task in background:
            task.cancel()
        if background:
            await asyncio.gather(*background, return_exceptions=True)
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
                try:
                    if session.gateway:
                        await session.gateway.close()
                finally:
                    if session.directory is not None:
                        await asyncio.to_thread(session.directory.cleanup)
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

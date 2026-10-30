"""Owned, short-lived H.264/AAC derivatives of stored recordings.

Original media is opened read-only. Each coverage span gets a separate encoder
and HLS discontinuity, including transitions between heterogeneous sources.

ASGI integration: assign PlaybackManager with the FFmpeg path and a per-camera
get_hwaccel_args callback to app.playback_manager, await start(), include the
frigate.api.playback router, and await close() in the same loop's lifespan
finally block. Do not share one manager between ASGI event loops or workers.
"""

import asyncio
import math
import os
import re
import secrets
import shlex
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from peewee import DatabaseError

from frigate.api.defs.request.playback_body import PlaybackCreateBody
from frigate.api.defs.response.playback_response import (
    PlaybackStatus,
    PlaybackTimelineSpan,
)
from frigate.const import CACHE_DIR
from frigate.ffmpeg_presets import (
    EncodeTypeEnum,
    parse_preset_hardware_acceleration_encode,
)
from frigate.models import Recordings
from frigate.util.recording_coverage import build_spans, plan_clip, resolve_coverage

SESSION_BYTES = 32 * 1024 * 1024
TOTAL_BYTES = 128 * 1024 * 1024
CACHE_HEADROOM_BYTES = 64 * 1024 * 1024
MAX_SESSIONS = TOTAL_BYTES // SESSION_BYTES
MAX_ENCODERS = 2
IDLE_SECONDS = 90
SPAN_TIMEOUT = 45
SESSION_TIMEOUT = 120
MAX_SPANS = 64
PLANNING_TIMEOUT = 10
NAMESPACE_PREFIX = "playback-compatibility-"
MAX_CLIENT_GENERATIONS = 128
MAX_PENDING_CREATES = 8
GENERATION_HISTORY_SECONDS = 300
RESOURCE = re.compile(r"^(?:index\.m3u8|p\d+-\d+\.ts)$")


class PlaybackError(Exception):
    """A safe public failure code with an HTTP status."""

    def __init__(self, code: str, status: int = 503) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass
class SourceClip:
    """A shared-planner realization with no public source path."""

    path: str
    start: float
    duration: float
    offset: float
    has_audio: bool | None
    wall_start: float | None = None
    wall_end: float | None = None


@dataclass
class PlaybackSession:
    """One owner's cache reservation and encoder lifetime."""

    id: str
    owner: str
    camera: str
    body: PlaybackCreateBody
    directory: Path
    clips: list[SourceClip]
    touched: float = field(default_factory=time.monotonic)
    state: str = "preparing"
    error: str | None = None
    timeline: list[PlaybackTimelineSpan] = field(default_factory=list)
    entries: list[str] = field(default_factory=list)
    resources: set[str] = field(default_factory=set)
    target_duration: int = 3
    readers: int = 0
    task: asyncio.Task | None = None
    process: Any = None
    deleting: bool = False
    cleanup_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    hwaccel_args: str | list[str] = field(default_factory=list)


def plan_sources(camera: str, body: PlaybackCreateBody) -> list[SourceClip]:
    """Use the Review planner without removing audio-bearing source spans."""
    intervals = resolve_coverage(camera, body.after, body.before)
    spans = build_spans(intervals, None if body.stream == "auto" else body.stream)
    if not spans:
        raise PlaybackError("recordings_unavailable", 404)
    if len(spans) > MAX_SPANS:
        raise PlaybackError("too_many_recording_spans", 422)
    clips = []
    for row, start, end, _ in spans:
        plan = plan_clip(row, start, end)
        if plan.skipped:
            raise PlaybackError("recording_span_unplayable", 422)
        offset = (plan.clip_from_ms or 0) / 1000
        clips.append(
            SourceClip(
                row.path,
                row.start_time + offset,
                plan.duration_ms / 1000,
                offset,
                row.has_audio,
                start,
                end,
            )
        )
    # Back-snapping can add source before the requested range. It is included
    # in the measured mapping, but cannot turn a short request into a long job.
    if sum(clip.duration for clip in clips) > 32:
        raise PlaybackError("snapped_window_too_large", 422)
    if not any(
        clip.start <= body.start_timestamp < clip.start + clip.duration
        for clip in clips
    ):
        raise PlaybackError("playback_start_in_gap", 422)
    return clips


def parse_playlist(path: Path, prefix: str) -> tuple[list[str], float, set[str], int]:
    """Validate FFmpeg output and measure durations, not requested estimates."""
    lines = path.read_text(encoding="utf-8").splitlines()
    if "#EXT-X-ENDLIST" not in lines:
        raise PlaybackError("incomplete_encoder_output")
    entries: list[str] = []
    names: set[str] = set()
    duration = 0.0
    pending: float | None = None
    target = 1
    for line in lines:
        if line.startswith("#EXTINF:"):
            if pending is not None:
                raise PlaybackError("invalid_encoder_output")
            pending = float(line.split(":", 1)[1].rstrip(","))
            if not math.isfinite(pending) or not 0 < pending <= 3:
                raise PlaybackError("invalid_encoder_output")
        elif line and not line.startswith("#"):
            if (
                pending is None
                or not re.fullmatch(re.escape(prefix) + r"-\d+\.ts", line)
                or not (path.parent / line).is_file()
            ):
                raise PlaybackError("invalid_encoder_output")
            entries.extend([f"#EXTINF:{pending:.6f},", line])
            names.add(line)
            duration += pending
            target = max(target, math.ceil(pending))
            pending = None
    if pending is not None or not names or duration > 33:
        raise PlaybackError("invalid_encoder_output")
    return entries, duration, names, target


def _load_sources(camera: str, body: PlaybackCreateBody) -> list[SourceClip]:
    database = Recordings._meta.database
    if database is None:
        raise PlaybackError("playback_recordings_not_started")
    opened_here = database.is_closed()
    try:
        return plan_sources(camera, body)
    finally:
        # Request DB middleware does not own connections opened by a worker
        # thread. Leave pre-existing connections (including test binds) alone.
        if opened_here:
            database.close()


class PlaybackManager:
    """Manage bounded local-cache sessions on the ASGI event loop."""

    def __init__(
        self,
        ffmpeg_path: str,
        cache_dir: str = CACHE_DIR,
        *,
        get_hwaccel_args: Callable[[str], str | list[str]] | None = None,
        compatibility_enabled: Callable[[], bool] | None = None,
    ) -> None:
        self.ffmpeg_path = ffmpeg_path
        self.get_hwaccel_args = get_hwaccel_args
        self.compatibility_enabled = compatibility_enabled
        self.cache_dir = Path(cache_dir)
        self.root: Path | None = None
        self.sessions: dict[str, PlaybackSession] = {}
        self._lock = asyncio.Lock()
        self._reaper: asyncio.Task | None = None
        self._closing = False
        self._planners: set[asyncio.Task] = set()
        self._stop = asyncio.Event()
        self._generations: dict[
            tuple[str, str, str], tuple[PlaybackCreateBody, float]
        ] = {}
        self._creating: dict[tuple[str, str, str], int] = {}

    async def start(self) -> None:
        """Create a private runtime namespace and start owned expiry cleanup."""
        if self.root is not None:
            return
        root = self.cache_dir / (NAMESPACE_PREFIX + secrets.token_hex(12))
        try:
            await asyncio.to_thread(root.mkdir, mode=0o700, parents=True)
        except OSError:
            raise PlaybackError("playback_cache_unavailable", 507) from None
        self.root = root
        self._reaper = asyncio.create_task(self._expire())

    async def create(
        self, camera: str, owner: str, body: PlaybackCreateBody
    ) -> PlaybackSession:
        """Reserve the newest seek before any asynchronous preparation."""
        self.check_enabled()
        key = (owner, camera, body.client_id)
        now = time.monotonic()
        live = {
            (session.owner, session.camera, session.body.client_id)
            for session in self.sessions.values()
        }
        for old_key, (_, touched) in list(self._generations.items()):
            if (
                old_key not in live
                and old_key not in self._creating
                and now - touched > GENERATION_HISTORY_SECONDS
            ):
                del self._generations[old_key]
        current = self._generations.get(key)
        if current is not None:
            previous, _ = current
            if body.generation < previous.generation:
                raise PlaybackError("playback_generation_superseded", 409)
            if body.generation == previous.generation and body != previous:
                raise PlaybackError("playback_generation_conflict", 409)
        elif len(self._generations) >= MAX_CLIENT_GENERATIONS:
            raise PlaybackError("playback_client_capacity_exhausted", 429)
        self._generations[key] = (body.model_copy(), now)
        if sum(self._creating.values()) >= MAX_PENDING_CREATES:
            raise PlaybackError("playback_create_capacity_exhausted", 429)
        self._creating[key] = self._creating.get(key, 0) + 1
        try:
            return await self._create(camera, owner, body)
        finally:
            self._creating[key] -= 1
            if not self._creating[key]:
                del self._creating[key]

    def _check_generation(
        self, camera: str, owner: str, body: PlaybackCreateBody
    ) -> None:
        current = self._generations.get((owner, camera, body.client_id))
        if current is None or current[0].generation != body.generation:
            raise PlaybackError("playback_generation_superseded", 409)

    async def _create(
        self, camera: str, owner: str, body: PlaybackCreateBody
    ) -> PlaybackSession:
        """Reserve before querying or spawning, with no unbounded encoder queue."""
        async with self._lock:
            if self.root is None or self._closing:
                raise PlaybackError("playback_not_started")
            self._check_generation(camera, owner, body)
            for session in self.sessions.values():
                if (
                    session.owner == owner
                    and session.camera == camera
                    and session.body == body
                    and not session.deleting
                ):
                    session.touched = time.monotonic()
                    return session
            active = sum(
                s.task is not None and not s.task.done() for s in self.sessions.values()
            )
            if (
                len(self.sessions) >= MAX_SESSIONS
                or active + len(self._planners) >= MAX_ENCODERS
            ):
                raise PlaybackError("playback_capacity_exhausted", 429)
            if sum(s.owner == owner for s in self.sessions.values()) >= 2:
                raise PlaybackError("playback_owner_capacity_exhausted", 429)
            try:
                free = (await asyncio.to_thread(shutil.disk_usage, self.root)).free
                if free < SESSION_BYTES + CACHE_HEADROOM_BYTES:
                    raise PlaybackError("playback_cache_full", 507)
                stale_bytes = await asyncio.to_thread(self._other_namespace_bytes)
                if stale_bytes + (len(self.sessions) + 1) * SESSION_BYTES > TOTAL_BYTES:
                    raise PlaybackError("playback_cache_capacity_exhausted", 507)
                planner = asyncio.create_task(
                    asyncio.to_thread(_load_sources, camera, body)
                )
                self._planners.add(planner)
                planner.add_done_callback(self._planner_finished)
                clips = await asyncio.wait_for(
                    asyncio.shield(planner), PLANNING_TIMEOUT
                )
            except DatabaseError:
                raise PlaybackError("playback_recordings_unavailable") from None
            except OSError:
                raise PlaybackError("playback_cache_unavailable", 507) from None
            except TimeoutError:
                raise PlaybackError("playback_planning_timeout", 504) from None
            except ValueError:
                raise PlaybackError("recording_metadata_invalid", 422) from None
            self._check_generation(camera, owner, body)
            try:
                hwaccel_args = (
                    self.get_hwaccel_args(camera) if self.get_hwaccel_args else []
                )
            except KeyError:
                raise PlaybackError("playback_camera_unavailable", 404) from None
            if isinstance(hwaccel_args, list):
                hwaccel_args = hwaccel_args.copy()
            session_id = secrets.token_hex(16)
            directory = self.root / session_id
            try:
                await asyncio.to_thread(directory.mkdir, mode=0o700)
            except OSError:
                raise PlaybackError("playback_cache_unavailable", 507) from None
            try:
                self._check_generation(camera, owner, body)
            except PlaybackError:
                await asyncio.to_thread(directory.rmdir)
                raise
            session = PlaybackSession(
                session_id,
                owner,
                camera,
                body,
                directory,
                clips,
                hwaccel_args=hwaccel_args,
            )
            self.sessions[session_id] = session
            session.task = asyncio.create_task(self._run(session))
            return session

    def _other_namespace_bytes(self) -> int:
        # Never remove a different runtime's output. Account for leftovers from
        # crashes so restarts cannot silently consume unlimited local cache.
        return sum(
            path.stat().st_size
            for directory in self.cache_dir.glob(NAMESPACE_PREFIX + "*")
            if directory != self.root
            and directory.is_dir()
            and not directory.is_symlink()
            for path in directory.rglob("*")
            if path.is_file() and not path.is_symlink()
        )

    def _planner_finished(self, planner: asyncio.Task) -> None:
        self._planners.discard(planner)
        if not planner.cancelled():
            # A timed-out read may finish later. Observe its result without
            # treating it as a new session or leaving an unhandled exception.
            planner.exception()

    def get(self, session_id: str, camera: str, owner: str) -> PlaybackSession:
        """Find only sessions belonging to the currently authorized identity."""
        session = self.sessions.get(session_id)
        if (
            session is None
            or session.camera != camera
            or session.owner != owner
            or session.deleting
        ):
            raise PlaybackError("playback_not_found", 404)
        session.touched = time.monotonic()
        return session

    def status(self, session: PlaybackSession) -> PlaybackStatus:
        """Return only safe metadata and measured timeline entries."""
        body = session.body
        encoding = shlex.split(
            parse_preset_hardware_acceleration_encode(
                "ffmpeg", session.hwaccel_args, "", "", EncodeTypeEnum.playback
            )
        )
        codec = encoding[encoding.index("-c:v") + 1]
        return PlaybackStatus(
            id=session.id,
            camera=session.camera,
            client_id=body.client_id,
            generation=body.generation,
            state=session.state,
            manifest_path=(
                f"{session.camera}/recordings/playback-compatibility/"
                f"{session.id}/media/index.m3u8"
            ),
            timeline=list(session.timeline),
            encoder=f"{codec} (NVIDIA GPU)"
            if codec == "h264_nvenc"
            else f"{codec} (software)",
            error=session.error,
            after=body.after,
            before=body.before,
            start_timestamp=body.start_timestamp,
            ready_through=session.timeline[-1].end_time if session.timeline else None,
        )

    def check_enabled(self) -> None:
        """Enforce the current administrator policy without a filesystem probe."""
        if self.compatibility_enabled is not None and not self.compatibility_enabled():
            raise PlaybackError("playback_compatibility_disabled", 409)

    def command(
        self, session: PlaybackSession, clip: SourceClip, index: int
    ) -> list[str]:
        """Build bounded H.264 playback using the configured encoder preset.

        No B-frame reorder delay or TS mux delay is added. make_zero shifts
        AAC priming and video together, preserving their common clock origin.
        """
        prefix = f"p{index}"
        command = [
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-threads",
            "2",
            "-filter_threads",
            "1",
            "-protocol_whitelist",
            "file",
            "-ss",
            str(clip.offset),
            "-i",
            str(Path(clip.path).resolve()),
        ]
        if clip.has_audio is False:
            command += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        output = [
            "-t",
            str(clip.duration),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0" if clip.has_audio is False else "0:a:0?",
            "-vf",
            "scale=w=1280:h=720:force_original_aspect_ratio=decrease:"
            "force_divisible_by=2,fps=15,setsar=1,"
            "tpad=stop_mode=clone:stop=-1",
            "-profile:v",
            "baseline",
            "-bf",
            "0",
            "-level:v",
            "3.1",
            "-pix_fmt",
            "yuv420p",
            "-b:v",
            "1500k",
            "-maxrate",
            "2000k",
            "-bufsize",
            "4000k",
            "-g",
            "30",
            "-c:a",
            "aac",
            "-profile:a",
            "aac_low",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-b:a",
            "96k",
            "-max_muxing_queue_size",
            "256",
            "-af",
            "aresample=async=1:first_pts=0",
            "-avoid_negative_ts",
            "make_zero",
            "-muxdelay",
            "0",
            "-muxpreload",
            "0",
            "-f",
            "hls",
            "-hls_time",
            "2",
            "-hls_list_size",
            "0",
            "-hls_playlist_type",
            "event",
            "-hls_flags",
            "temp_file+independent_segments",
            "-hls_segment_filename",
            str(session.directory / f"{prefix}-%03d.ts"),
            str(session.directory / f"{prefix}.m3u8"),
        ]
        return shlex.split(
            parse_preset_hardware_acceleration_encode(
                shlex.quote(self.ffmpeg_path),
                session.hwaccel_args,
                shlex.join(command),
                shlex.join(output),
                EncodeTypeEnum.playback,
            )
        )

    async def _encode(
        self, session: PlaybackSession, clip: SourceClip, index: int
    ) -> None:
        self.check_enabled()
        if not await asyncio.to_thread(Path(clip.path).is_file):
            raise PlaybackError("recording_source_unavailable", 410)
        process = await asyncio.create_subprocess_exec(
            *self.command(session, clip, index),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        session.process = process
        waiter = asyncio.create_task(process.wait())
        deadline = time.monotonic() + SPAN_TIMEOUT
        try:
            while True:
                self.check_enabled()
                done, _ = await asyncio.wait({waiter}, timeout=0.25)
                size = await asyncio.to_thread(
                    lambda: sum(p.stat().st_size for p in session.directory.iterdir())
                )
                if size > SESSION_BYTES - 1024 * 1024:
                    raise PlaybackError("playback_cache_limit", 507)
                if (
                    await asyncio.to_thread(shutil.disk_usage, session.directory)
                ).free < CACHE_HEADROOM_BYTES:
                    raise PlaybackError("playback_cache_full", 507)
                if time.monotonic() >= deadline:
                    raise PlaybackError("playback_encoder_timeout", 504)
                if done:
                    if waiter.result() != 0:
                        raise PlaybackError("playback_encoder_failed")
                    return
        finally:
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()
            await waiter
            session.process = None

    async def _publish(
        self, session: PlaybackSession, entries: list[str], complete: bool
    ) -> None:
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            f"#EXT-X-TARGETDURATION:{session.target_duration}",
            "#EXT-X-MEDIA-SEQUENCE:0",
            "#EXT-X-PLAYLIST-TYPE:EVENT",
            "#EXT-X-INDEPENDENT-SEGMENTS",
            *entries,
        ]
        if complete:
            lines.append("#EXT-X-ENDLIST")
        pending = session.directory / "index.pending"
        await asyncio.to_thread(
            pending.write_text, "\n".join(lines) + "\n", encoding="utf-8"
        )
        await asyncio.to_thread(os.replace, pending, session.directory / "index.m3u8")

    async def _run(self, session: PlaybackSession) -> None:
        try:
            async with asyncio.timeout(SESSION_TIMEOUT):
                for index, clip in enumerate(session.clips):
                    self.check_enabled()
                    await self._encode(session, clip, index)
                    entries, duration, resources, target = await asyncio.to_thread(
                        parse_playlist,
                        session.directory / f"p{index}.m3u8",
                        f"p{index}",
                    )
                    if abs(duration - clip.duration) > 0.1:
                        raise PlaybackError("playback_span_duration_mismatch")
                    combined = [
                        *session.entries,
                        *(["#EXT-X-DISCONTINUITY"] if session.entries else []),
                        *entries,
                    ]
                    session.target_duration = max(session.target_duration, target)
                    session.resources.update(resources)
                    complete = index == len(session.clips) - 1
                    await self._publish(session, combined, complete)
                    session.entries = combined
                    session.resources.add("index.m3u8")
                    wall_start = (
                        clip.wall_start if clip.wall_start is not None else clip.start
                    )
                    wall_end = (
                        clip.wall_end
                        if clip.wall_end is not None
                        else clip.start + duration
                    )
                    session.timeline.append(
                        PlaybackTimelineSpan(
                            start_time=wall_start,
                            end_time=wall_end,
                            duration=duration,
                            lead_in_ms=max(0, (wall_start - clip.start) * 1000),
                        )
                    )
                    if any(
                        span.start_time <= session.body.start_timestamp < span.end_time
                        for span in session.timeline
                    ):
                        session.state = "complete" if complete else "ready"
                if session.state not in ("ready", "complete"):
                    raise PlaybackError("playback_start_not_realized")
        except PlaybackError as error:
            session.error = error.code
            session.state = "failed"
        except TimeoutError:
            session.error = "playback_session_timeout"
            session.state = "failed"
        except (OSError, ValueError, RuntimeError):
            session.error = "playback_storage_or_encoder_error"
            session.state = "failed"

    async def cancel(self, session: PlaybackSession) -> None:
        """Stop the exact owned process, then delete only its private directory."""
        async with session.cleanup_lock:
            session.deleting = True
            if session.task is not None and not session.task.done():
                if not session.task.cancelling():
                    session.task.cancel()
                # A disconnected DELETE must not deliver a second cancellation
                # into FFmpeg's terminate/kill/wait cleanup.
                await asyncio.shield(
                    asyncio.gather(session.task, return_exceptions=True)
                )
            if session.readers:
                return
            if self.root is not None and session.directory.parent == self.root:
                try:
                    await asyncio.to_thread(shutil.rmtree, session.directory)
                except FileNotFoundError:
                    pass
                except OSError:
                    raise PlaybackError("playback_cleanup_failed") from None
            self.sessions.pop(session.id, None)
            if self._closing and not self.sessions:
                await self._remove_root()

    async def _remove_root(self) -> None:
        if self.root is not None:
            try:
                await asyncio.to_thread(self.root.rmdir)
            except FileNotFoundError:
                pass
            except OSError:
                raise PlaybackError("playback_cleanup_failed") from None

    async def _expire(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), 5)
            except TimeoutError:
                await self._expire_sessions()

    async def _expire_sessions(self) -> None:
        for session in list(self.sessions.values()):
            if not session.readers and (
                session.deleting or time.monotonic() - session.touched > IDLE_SECONDS
            ):
                try:
                    await self.cancel(session)
                except PlaybackError:
                    # Keep the quota reservation and retry the exact owned
                    # directory instead of forgetting failed cleanup.
                    continue

    async def close(self) -> None:
        """Stop owned tasks and encoders before removing this runtime namespace."""
        async with self._lock:
            self._closing = True
        self._stop.set()
        if self._reaper is not None:
            await self._reaper
        for session in list(self.sessions.values()):
            try:
                await self.cancel(session)
            except PlaybackError:
                continue
        # In-flight readers own their session directory until their finalizer.
        if self.root is not None and not self.sessions:
            await self._remove_root()
        elif any(not session.readers for session in self.sessions.values()):
            raise PlaybackError("playback_cleanup_failed")

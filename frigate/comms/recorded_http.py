"""Finite private MP4 preparation, admission, authorization and reader ownership."""

import asyncio
import hashlib
import json
import logging
import math
import os
import shutil
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote
from uuid import uuid4

from peewee import DatabaseError

from frigate.api.defs.request.recorded_http_body import RecordedHTTPCreate
from frigate.api.defs.request.recorded_webrtc_body import RecordedWindow
from frigate.comms.live_webrtc import DETAIL_BITRATES, LiveWebRTCError
from frigate.comms.recorded_webrtc import finish_cleanup
from frigate.const import CACHE_DIR, MAX_SEGMENT_DURATION
from frigate.record.access import recording_account_allows
from frigate.record.http_targets import load_http_sources, resolve_source_target
from frigate.record.http_timeline import (
    PacketTimelineError,
    effective_window_start,
    measure_packet_spans,
    measure_packet_stream,
    original_packet_span,
    original_seek_plan,
)
from frigate.record.lease_async import AsyncRecordingLease, AsyncRecordingLeaseLost

logger = logging.getLogger(__name__)
JOB_BYTES = 128 * 1024 * 1024
TOTAL_BYTES = 512 * 1024 * 1024
MAX_JOBS = 2
MAX_READERS = 8
MAX_SESSION_READERS = 2
PREPARE_SECONDS = 60
LIFETIME_SECONDS = 180
RESOLVE_SECONDS = 10
MAX_RESOLVERS = 2
HEIGHTS = (360, 720, 1080, 2160)


def faststart(path: Path) -> bool:
    """Require a complete top-level moov before mdat, not a growing fragment."""
    size = path.stat().st_size
    moov = False
    mdat = False
    with path.open("rb") as source:
        position = 0
        while position < size:
            header = source.read(8)
            if len(header) != 8:
                return False
            length, kind = struct.unpack(">I4s", header)
            header_size = 8
            if length == 1:
                extended = source.read(8)
                if len(extended) != 8:
                    return False
                length = struct.unpack(">Q", extended)[0]
                header_size = 16
            if length < header_size or position + length > size or kind == b"moof":
                return False
            if kind == b"moov":
                moov = True
            if kind == b"mdat":
                if not moov:
                    return False
                mdat = True
            position += length
            source.seek(position)
    return moov and mdat


def _run(lease, command: list[str], *, capture=False) -> bytes:
    lease.check()
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    lease.process_started_blocking(process)
    output = process.stdout.read(1024 * 1024 + 1) if capture else b""
    if capture and len(output) > 1024 * 1024:
        raise LiveWebRTCError(422, "recorded_http_probe_too_large")
    process.wait()
    lease.check()
    if process.returncode:
        logger.error(
            "Recorded HTTP media process failed with exit %s", process.returncode
        )
        raise LiveWebRTCError(503, "recorded_http_media_process_failed")
    return output or b""


def _probe(lease, ffprobe: str, path: Path) -> dict:
    result = json.loads(
        _run(
            lease,
            [
                ffprobe,
                "-v",
                "error",
                "-show_data_hash",
                "sha256",
                "-show_entries",
                "stream=codec_type,codec_name,width,height,time_base,start_time,duration,"
                "extradata_hash,sample_rate,channels,profile,level:"
                "format=duration",
                "-of",
                "json",
                str(path),
            ],
            capture=True,
        )
    )
    video = next(
        (stream for stream in result["streams"] if stream["codec_type"] == "video"),
        None,
    )
    if video is None:
        raise LiveWebRTCError(422, "recorded_http_video_unavailable")
    duration = float(video["duration"])
    start = float(video.get("start_time", 0))
    container = float(result["format"]["duration"])
    if not all(math.isfinite(value) for value in (start, duration, container)):
        raise LiveWebRTCError(422, "recorded_http_invalid_timing")
    if duration <= 0 or start < 0 or container < start + duration - 0.1:
        raise LiveWebRTCError(422, "recorded_http_invalid_timing")
    return {
        "video": video,
        "duration": duration,
        "start": start,
        "container": container,
        "streams": result["streams"],
    }


def _media_packets(lease, ffprobe, path, *, audio=False):
    packets = json.loads(
        _run(
            lease,
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "a:0" if audio else "v:0",
                "-show_data_hash",
                "sha256",
                "-show_entries",
                "packet=pts,dts,duration,pts_time,dts_time,duration_time,data_hash,flags",
                "-of",
                "json",
                str(path),
            ],
            capture=True,
        )
    )["packets"]
    if not 0 < len(packets) <= 10000:
        raise LiveWebRTCError(422, "recorded_http_packet_capacity")
    return packets


def build_mp4(session, clips, directory: Path, lease, ffmpeg: str, ffprobe: str):
    """Read leased originals only; publish only a verified, completed faststart file."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    original = session.quality == "original"
    audio = bool(clips) and all(clip.has_audio is True for clip in clips)
    spans = []
    parts = []
    source_packets = []
    source_audio_packets = []
    cursor = 0.0
    signature = None
    output_signature = None
    for clip in clips:
        lease.check()
        # Shared planning back-snaps to a keyframe. Remux retains that real start;
        # encoding can accurately trim the requested coverage boundary.
        start = (
            clip.start if original else max(clip.start, clip.wall_start or clip.start)
        )
        end = min(clip.start + clip.duration, session.body.before)
        if not original and clip.wall_end is not None:
            end = min(end, clip.wall_end)
        duration = end - start
        if duration <= 0:
            continue
        source = Path(clip.path)
        if not source.is_file() or source.is_symlink():
            raise LiveWebRTCError(422, "recorded_http_source_not_regular")
        if original:
            info = _probe(lease, ffprobe, source)
            video = info["video"]
            audios = [s for s in info["streams"] if s["codec_type"] == "audio"]
            if video["codec_name"] not in ("h264", "hevc") or (
                audio and (not audios or audios[0]["codec_name"] != "aac")
            ):
                raise LiveWebRTCError(422, "recorded_http_original_codec_unsupported")
            if "extradata_hash" not in video:
                raise LiveWebRTCError(422, "recorded_http_original_config_unknown")
            current = (
                video["width"],
                video["height"],
                video["codec_name"],
                video["time_base"],
                video["extradata_hash"],
                tuple(
                    (
                        s.get("extradata_hash"),
                        s.get("sample_rate"),
                        s.get("channels"),
                        s.get("time_base"),
                    )
                    for s in audios
                )
                if audio
                else (),
            )
            if signature is not None and current != signature:
                raise LiveWebRTCError(422, "recorded_http_original_spans_incompatible")
            signature = current
            input_packets = _media_packets(lease, ffprobe, source)
            wall_origin = clip.start - clip.offset
            end = min(
                clip.wall_end if clip.wall_end is not None else end,
                session.body.before,
            )
            try:
                original_plan = original_seek_plan(
                    input_packets,
                    video["time_base"],
                    info["start"],
                    wall_origin,
                    max(
                        clip.wall_start if clip.wall_start is not None else clip.start,
                        session.body.after,
                    ),
                    end,
                )
            except PacketTimelineError as error:
                raise LiveWebRTCError(422, error.code) from None
        part = directory / f"p{len(parts)}.mp4"
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-threads",
            "2",
            "-filter_threads",
            "1",
            "-protocol_whitelist",
            "file,crypto,data",
            *(["-seek_timestamp", "1"] if original else []),
            "-ss",
            original_plan["seek"]
            if original
            else str(clip.offset + start - clip.start),
            "-i",
            str(source.resolve()),
            "-t",
            original_plan["duration"] if original else str(duration),
            "-map",
            "0:v:0",
        ]
        if audio:
            command += ["-map", "0:a:0"]
        else:
            command += ["-an"]
        if original:
            command += [
                "-c",
                "copy",
                "-tag:v",
                "hvc1" if video["codec_name"] == "hevc" else "avc1",
            ]
        else:
            height = int(session.quality)
            width = round(height * 16 / 9 / 2) * 2
            command += [
                "-vf",
                f"fps=15,scale=w={width}:h={height}:force_original_aspect_ratio=decrease:"
                f"force_divisible_by=2,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
                "setsar=1",
                "-c:v",
                "libx264",
                "-threads",
                "2",
                "-preset",
                "veryfast",
                "-pix_fmt",
                "yuv420p",
                "-bf",
                "0",
                "-g",
                "30",
                "-b:v",
                str(DETAIL_BITRATES.get(height, 600) * 1000),
            ]
            if audio:
                command += [
                    "-c:a",
                    "aac",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-b:a",
                    "96k",
                    "-af",
                    "aresample=async=1:first_pts=0",
                ]
        command += [
            "-avoid_negative_ts",
            "make_zero",
            "-movflags",
            "+faststart",
            "-fs",
            str(JOB_BYTES // 2),
            str(part),
        ]
        _run(lease, command)
        info = _probe(lease, ffprobe, part)
        current_output = (
            info["video"]["codec_name"],
            info["video"]["width"],
            info["video"]["height"],
            info["video"]["time_base"],
            info["video"].get("extradata_hash"),
        )
        if current_output[-1] is None or (
            output_signature is not None and current_output != output_signature
        ):
            raise LiveWebRTCError(422, "recorded_http_output_spans_incompatible")
        output_signature = current_output
        part_packets = _media_packets(lease, ffprobe, part)
        source_packets.append(part_packets)
        if audio:
            source_audio_packets.append(
                _media_packets(lease, ffprobe, part, audio=True)
            )
        if original:
            try:
                start, source_end = original_packet_span(
                    input_packets,
                    part_packets,
                    original_plan,
                    video["time_base"],
                    info["video"]["time_base"],
                    wall_origin,
                    end,
                )
            except PacketTimelineError as error:
                logger.error(
                    "Recorded HTTP original packet verification failed (%s)", error.code
                )
                raise LiveWebRTCError(422, error.code) from None
            logger.debug(
                "Recorded HTTP original requested %.6f-%.6f source %.6f-%.6f "
                "seek %s duration %s packets %s",
                clip.wall_start if clip.wall_start is not None else clip.start,
                end,
                start,
                source_end,
                original_plan["seek"],
                original_plan["duration"],
                len(part_packets),
            )
        elif abs(info["duration"] - duration) > 0.15:
            raise LiveWebRTCError(422, "recorded_http_span_timing_mismatch")
        if (
            sum(path.stat().st_size for path in parts) + part.stat().st_size
            > JOB_BYTES // 2
        ):
            raise LiveWebRTCError(507, "recorded_http_byte_capacity")
        spans.append(
            {
                "source_start": start,
                "source_end": source_end
                if original
                else min(end, start + info["duration"]),
                "media_start": cursor + info["start"],
                "media_end": cursor + info["start"] + info["duration"],
            }
        )
        cursor += info["container"]
        parts.append(part)
    if (
        not parts
        or effective_window_start(
            spans,
            session.body.start_timestamp,
            session.body.after,
            session.body.before,
            forward=session.body.start_policy == "next",
        )
        is None
    ):
        raise LiveWebRTCError(422, "playback_start_in_gap")
    manifest = directory / "concat.txt"
    manifest.write_text(
        "".join(f"file '{part.name}'\n" for part in parts), encoding="utf-8"
    )
    output = directory / "pending.mp4"
    _run(
        lease,
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-protocol_whitelist",
            "file",
            "-f",
            "concat",
            "-safe",
            "1",
            "-auto_convert",
            "0",
            "-i",
            str(manifest),
            "-map",
            "0:v:0",
            *(["-map", "0:a:0"] if audio else ["-an"]),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            "-fs",
            str(JOB_BYTES // 2),
            str(output),
        ],
    )
    final = _probe(lease, ffprobe, output)
    if (
        not faststart(output)
        or abs(final["container"] - cursor) > 0.15
        or final["container"] > 33
        or abs(final["start"] - spans[0]["media_start"]) > 0.01
        or abs(final["start"] + final["duration"] - spans[-1]["media_end"]) > 0.15
    ):
        raise LiveWebRTCError(422, "recorded_http_final_timing_mismatch")
    packets = _media_packets(lease, ffprobe, output)
    try:
        measured_spans = measure_packet_spans(spans, source_packets, packets)
    except PacketTimelineError as error:
        logger.error("Recorded HTTP final packet attribution failed (%s)", error.code)
        raise LiveWebRTCError(422, error.code) from None
    for index, (nominal, measured) in enumerate(
        zip(spans, measured_spans, strict=True)
    ):
        logger.debug(
            "Recorded HTTP span %s nominal %.6f-%.6f measured %.6f-%.6f "
            "catalog %.6f-%.6f packets %s",
            index,
            nominal["media_start"],
            nominal["media_end"],
            measured["media_start"],
            measured["media_end"],
            measured["source_start"],
            measured["source_end"],
            len(source_packets[index]),
        )
    spans = measured_spans
    logger.debug(
        "Recorded HTTP final container %.6f video metadata %.6f+%.6f "
        "video PTS %.6f-%.6f",
        final["container"],
        final["start"],
        final["duration"],
        spans[0]["media_start"],
        spans[-1]["media_end"],
    )
    if (
        abs(final["start"] - spans[0]["media_start"]) > 0.01
        or spans[-1]["media_end"] > final["container"] + 0.002
        or spans[-1]["media_end"] > 33
    ):
        raise LiveWebRTCError(422, "recorded_http_final_sample_timing_mismatch")
    audio_start = None
    audio_end = None
    if audio:
        final_audio_packets = _media_packets(lease, ffprobe, output, audio=True)
        try:
            audio_start, audio_end = measure_packet_stream(
                source_audio_packets, final_audio_packets
            )
        except PacketTimelineError as error:
            logger.error(
                "Recorded HTTP final audio attribution failed (%s)", error.code
            )
            raise LiveWebRTCError(422, error.code) from None
        audio_streams = [
            stream for stream in final["streams"] if stream["codec_type"] == "audio"
        ]
        if len(audio_streams) != 1 or audio_streams[0]["codec_name"] != "aac":
            raise LiveWebRTCError(422, "recorded_http_final_audio_codec_mismatch")
        rate = int(audio_streams[0]["sample_rate"])
        logger.debug(
            "Recorded HTTP final audio PTS %.6f-%.6f AAC rate %s",
            audio_start,
            audio_end,
            rate,
        )
        if (
            rate <= 0
            or audio_start < -(1024 / rate) - 0.002
            or audio_end <= max(0, audio_start)
            or audio_end > final["container"] + 0.002
            or audio_end > 33
        ):
            raise LiveWebRTCError(422, "recorded_http_final_audio_timing_mismatch")
    logger.debug(
        "Recorded HTTP final container %.6f video metadata %.6f+%.6f "
        "video PTS %.6f-%.6f audio PTS %s-%s",
        final["container"],
        final["start"],
        final["duration"],
        spans[0]["media_start"],
        spans[-1]["media_end"],
        audio_start,
        audio_end,
    )
    digest = hashlib.sha256()
    with output.open("rb") as source:
        while data := source.read(1024 * 1024):
            lease.check()
            digest.update(data)
    target = directory / "media.mp4"
    output.chmod(0o600)
    os.replace(output, target)
    for part in parts:
        part.unlink()
    manifest.unlink()
    return spans, audio, final["video"], f'"{digest.hexdigest()}"'


@dataclass
class HTTPSession:
    identifier: str
    camera: str
    owner: str
    auth_issued: float
    deadline: float
    body: RecordedHTTPCreate
    quality: str
    directory: Path
    state: str = "preparing"
    error: str | None = None
    audio: bool = False
    width: int | None = None
    height: int | None = None
    timeline: list[dict] = field(default_factory=list)
    etag: str = ""
    size: int = 0
    modified: int = 0
    effective_start: float | None = None
    task: asyncio.Task | None = None
    readers: set[asyncio.Task] = field(default_factory=set)
    closing: bool = False
    cleanup_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class RecordedHTTPManager:
    """Bound jobs, cached bytes, lifetime and readers in the live admission pool."""

    def __init__(
        self,
        live,
        ffmpeg: str,
        ffprobe: str,
        root=None,
        *,
        planner=load_http_sources,
        builder=build_mp4,
        lease_factory=AsyncRecordingLease,
        account_check=recording_account_allows,
        resolver=resolve_source_target,
    ):
        self.live, self.ffmpeg, self.ffprobe = live, ffmpeg, ffprobe
        self.root = Path(root or (Path(CACHE_DIR) / "recorded-http"))
        self.planner, self.builder, self.lease_factory = planner, builder, lease_factory
        self.account_check = account_check
        self.resolver = resolver
        self.resolve_tasks = set()
        self.sessions = {}
        self.jobs = set()
        self.closed = False
        self.ready = False
        self.worker = None
        self.lock = asyncio.Lock()

    async def start(self):
        """Remove only this process's private stale cache before accepting requests."""
        try:
            if self.root.is_symlink():
                raise OSError("Cache root is a symlink")
            try:
                await asyncio.to_thread(shutil.rmtree, self.root)
            except FileNotFoundError:
                pass
            await asyncio.to_thread(
                self.root.mkdir, mode=0o700, parents=True, exist_ok=True
            )
            self.ready = True
        except OSError:
            logger.exception("Recorded HTTP private cache startup failed")
        self.worker = asyncio.create_task(self._maintain())

    def available(self):
        """Recording policy never changes live transport availability."""
        config = self.live.config()
        return (
            self.ready
            and not self.closed
            and config.ui.recorded_http
            and config.auth.enabled
            and self.live.available()
        )

    def qualities(self):
        """Advertise only admitted output sizes and explicit original remux."""
        return (
            ["auto"]
            + [
                str(height)
                for height in HEIGHTS
                if height <= self.live.settings.max_height
            ]
            + ["original"]
        )

    async def authorized(self, session):
        """Consult current account and password state, never a cached role claim."""
        try:
            return await finish_cleanup(
                asyncio.to_thread(
                    self.account_check,
                    session.owner,
                    session.camera,
                    session.auth_issued,
                    self.live.config(),
                )
            )
        except Exception:
            logger.exception("Recorded HTTP current account authorization failed")
            return False

    async def owned(self, camera, owner, identifier):
        """Fail closed at every control and new reader boundary."""
        session = self.sessions.get(identifier)
        if (
            session is None
            or session.closing
            or session.owner != owner
            or session.camera != camera
        ):
            raise LiveWebRTCError(404, "recorded_http_session_not_found")
        if not self.available() or session.deadline <= self.live.clock():
            raise LiveWebRTCError(410, "recorded_http_session_expired")
        if not await self.authorized(session):
            raise LiveWebRTCError(401, "recorded_http_session_revoked")
        return session

    def metadata(self, session):
        """Stable owned URLs carry no capability token or original file identity."""
        base = f"{quote(session.camera, safe='')}/recordings/http/sessions/{session.identifier}"
        return {
            "version": 1,
            "session_id": session.identifier,
            "camera": session.camera,
            "state": session.state,
            "quality": session.quality,
            "audio": session.audio,
            "status_url": base,
            "close_url": base,
            "media_url": base + "/media.mp4",
            "hard_expires_at": session.deadline,
            "timeline": session.timeline,
            "error": session.error,
            "width": session.width,
            "height": session.height,
            "after": session.body.after,
            "before": session.body.before,
            "start_timestamp": session.body.start_timestamp,
            "effective_start_timestamp": session.effective_start,
            "continuation_timestamp": session.body.before,
            "next_timestamp": session.body.before,
        }

    async def create(self, camera, owner, body, auth_expires, *, auth_issued):
        """Reserve before launching a cancellable finite preparation job."""
        if not self.available():
            raise LiveWebRTCError(503, "recorded_http_not_available")
        if camera not in self.live.config().cameras:
            raise LiveWebRTCError(404, "camera_not_available")
        if body.quality not in self.qualities():
            raise LiveWebRTCError(422, "recorded_http_quality_not_available")
        quality = body.quality
        if quality == "auto":
            quality = str(
                max(h for h in HEIGHTS if h <= min(720, self.live.settings.max_height))
            )
        deadline = min(
            auth_expires,
            self.live.clock() + LIFETIME_SECONDS,
            self.live.clock() + self.live.settings.max_session_seconds,
        )
        if deadline <= self.live.clock():
            raise LiveWebRTCError(401, "recorded_http_session_expired")
        identifier = str(uuid4())
        session = HTTPSession(
            identifier,
            camera,
            owner,
            auth_issued,
            deadline,
            body.model_copy(deep=True),
            quality,
            self.root / identifier,
        )
        if not await self.authorized(session):
            raise LiveWebRTCError(401, "recorded_http_session_revoked")
        async with self.lock:
            if not self.available():
                raise LiveWebRTCError(503, "recorded_http_not_available")
            if (len(self.sessions) + 1) * JOB_BYTES > TOTAL_BYTES:
                raise LiveWebRTCError(507, "recorded_http_byte_capacity")
            if len(self.jobs) >= MAX_JOBS:
                raise LiveWebRTCError(429, "recorded_http_job_capacity")
            await self.live.reserve_external(identifier)
            self.sessions[identifier] = session
            self.jobs.add(identifier)
            session.task = asyncio.create_task(self._prepare(session))
        return self.metadata(session)

    async def resolve(self, camera, owner, body, auth_expires, *, auth_issued):
        """Return a bounded frame-backed target; no window or hard expiry is renewed."""
        if not self.available():
            raise LiveWebRTCError(503, "recorded_http_not_available")
        if camera not in self.live.config().cameras:
            raise LiveWebRTCError(404, "camera_not_available")
        account = SimpleNamespace(owner=owner, camera=camera, auth_issued=auth_issued)
        task = asyncio.current_task()
        async with self.lock:
            if not self.available():
                raise LiveWebRTCError(503, "recorded_http_not_available")
            if len(self.resolve_tasks) >= MAX_RESOLVERS:
                raise LiveWebRTCError(429, "recorded_http_target_capacity")
            self.resolve_tasks.add(task)
        try:
            if auth_expires <= self.live.clock() or not await self.authorized(account):
                raise LiveWebRTCError(401, "recorded_http_session_revoked")
            async with asyncio.timeout(RESOLVE_SECONDS):
                async with self.lease_factory(
                    camera,
                    max(0, body.start_timestamp - MAX_SEGMENT_DURATION),
                    body.before,
                    owner=owner,
                ) as lease:
                    result = await lease.guard(
                        lease.run_blocking(
                            self.resolver,
                            camera,
                            body,
                            lease,
                            self.ffprobe,
                            _probe,
                            _media_packets,
                        )
                    )
            if (
                not self.available()
                or auth_expires <= self.live.clock()
                or not await self.authorized(account)
            ):
                raise LiveWebRTCError(401, "recorded_http_session_revoked")
            return {
                "version": 1,
                "after": body.after,
                "before": body.before,
                "start_timestamp": body.start_timestamp,
                **result,
            }
        except TimeoutError:
            logger.warning("Recorded HTTP target resolution timed out")
            raise LiveWebRTCError(503, "recorded_http_target_timeout") from None
        except PacketTimelineError as error:
            logger.warning(
                "Recorded HTTP target packet validation failed (%s)", error.code
            )
            raise LiveWebRTCError(422, error.code) from None
        except LiveWebRTCError as error:
            logger.warning("Recorded HTTP target resolution failed (%s)", error.code)
            raise
        except (
            AsyncRecordingLeaseLost,
            DatabaseError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
        ):
            logger.exception("Recorded HTTP target resolution failed")
            raise LiveWebRTCError(503, "recorded_http_target_unavailable") from None
        finally:
            self.resolve_tasks.discard(task)

    async def _prepare(self, session):
        encoder = "http-encoder:" + session.identifier
        reserved = False
        try:
            if session.quality != "original":
                await self.live.reserve_external(encoder, encoder=True)
                reserved = True
            async with asyncio.timeout(PREPARE_SECONDS):
                window = RecordedWindow(
                    after=session.body.after,
                    before=session.body.before,
                    start_timestamp=session.body.start_timestamp,
                )
                async with self.lease_factory(
                    session.camera,
                    max(0, window.after - MAX_SEGMENT_DURATION),
                    window.before,
                    owner=session.owner,
                ) as lease:
                    clips = await lease.guard(
                        lease.run_blocking(self.planner, session.camera, window)
                    )
                    result = await lease.guard(
                        lease.run_blocking(
                            self.builder,
                            session,
                            clips,
                            session.directory,
                            lease,
                            self.ffmpeg,
                            self.ffprobe,
                        )
                    )
                if (
                    not await self.authorized(session)
                    or not self.available()
                    or session.deadline <= self.live.clock()
                ):
                    raise LiveWebRTCError(401, "recorded_http_session_revoked")
                session.timeline, session.audio, video, session.etag = result
                session.effective_start = effective_window_start(
                    session.timeline,
                    session.body.start_timestamp,
                    session.body.after,
                    session.body.before,
                    forward=session.body.start_policy == "next",
                )
                if session.effective_start is None:
                    raise LiveWebRTCError(422, "playback_start_in_gap")
                session.width, session.height = video["width"], video["height"]
                details = await finish_cleanup(
                    asyncio.to_thread((session.directory / "media.mp4").stat)
                )
                session.size, session.modified = details.st_size, int(details.st_mtime)
                if not 0 < session.size <= JOB_BYTES:
                    raise LiveWebRTCError(507, "recorded_http_byte_capacity")
                session.state = "ready"
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if (
                session.body.start_policy == "next"
                and isinstance(error, LiveWebRTCError)
                and error.code in {"recordings_unavailable", "playback_start_in_gap"}
            ):
                session.state = "empty"
                session.error = None
                session.timeline = []
                session.effective_start = None
                session.audio = False
                logger.info("Recorded HTTP window contains no usable next frame")
                return
            session.state = "failed"
            session.error = (
                error.code
                if isinstance(error, LiveWebRTCError)
                else "recorded_http_preparation_failed"
            )
            logger.exception("Recorded HTTP preparation failed (%s)", session.error)
        finally:
            await finish_cleanup(self._finish_preparation(session, encoder, reserved))

    async def _finish_preparation(self, session, encoder, reserved):
        try:
            if session.state != "ready":
                try:
                    await asyncio.to_thread(shutil.rmtree, session.directory)
                except FileNotFoundError:
                    pass
                except OSError:
                    session.state = "cleanup_pending"
                    logger.exception("Recorded HTTP failed output cleanup")
        finally:
            if reserved:
                await self.live.release_external(encoder)
            self.jobs.discard(session.identifier)

    async def acquire_reader(self, session):
        """Reserve a reader before any response headers or filesystem read."""
        if (
            await self.owned(session.camera, session.owner, session.identifier)
            is not session
        ):
            raise LiveWebRTCError(404, "recorded_http_session_not_found")
        if session.state != "ready":
            raise LiveWebRTCError(409, session.error or "recorded_http_not_ready")
        task = asyncio.current_task()
        async with self.lock:
            if session.closing:
                raise LiveWebRTCError(410, "recorded_http_session_expired")
            if (
                len(session.readers) >= MAX_SESSION_READERS
                or sum(len(item.readers) for item in self.sessions.values())
                >= MAX_READERS
            ):
                raise LiveWebRTCError(429, "recorded_http_reader_capacity")
            session.readers.add(task)
        return task

    async def delete(self, camera, owner, identifier):
        """Cancellation joins actual children and readers before releasing quota."""
        session = self.sessions.get(identifier)
        if session is None or session.camera != camera or session.owner != owner:
            raise LiveWebRTCError(404, "recorded_http_session_not_found")
        await finish_cleanup(self._dispose(session))

    async def _dispose(self, session):
        async with session.cleanup_lock:
            session.closing = True
            if session.task is not None and not session.task.done():
                session.task.cancel()
                await asyncio.gather(session.task, return_exceptions=True)
            readers = tuple(session.readers)
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            try:
                await asyncio.to_thread(shutil.rmtree, session.directory)
            except FileNotFoundError:
                pass
            except OSError:
                session.state = "cleanup_pending"
                logger.exception("Recorded HTTP session cleanup failed")
                raise LiveWebRTCError(503, "recorded_http_cleanup_pending") from None
            await self.live.release_external(session.identifier)
            self.sessions.pop(session.identifier, None)

    async def _maintain(self):
        while not self.closed:
            await asyncio.sleep(1)
            await self._maintain_once()

    async def _maintain_once(self):
        for session in tuple(self.sessions.values()):
            try:
                if (
                    session.closing
                    or session.deadline <= self.live.clock()
                    or not self.available()
                    or not await self.authorized(session)
                ):
                    await self._dispose(session)
            except Exception:
                logger.exception("Recorded HTTP maintenance failed")

    async def close(self):
        """Stop new admission, then drain jobs and readers before live pool shutdown."""
        await finish_cleanup(self._close())

    async def _close(self):
        async with self.lock:
            self.closed = True
            resolving = tuple(self.resolve_tasks)
        for task in resolving:
            task.cancel()
        await asyncio.gather(*resolving, return_exceptions=True)
        if self.worker is not None:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        for session in tuple(self.sessions.values()):
            try:
                await finish_cleanup(self._dispose(session))
            except LiveWebRTCError:
                logger.exception("Recorded HTTP shutdown cleanup remains pending")

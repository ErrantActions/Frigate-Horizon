"""Offline, journaled audio-only repair of indexed local recording segments.

Run with ``python -m frigate.record.audio_repair --help``. Dry-run is the default.
Stop Frigate, archive workers, and cleanup jobs before applying or recovering.
Backups and journals must be retained together until the repair is accepted.
"""

import argparse
import hashlib
import json
import math
import os
import shutil
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from threading import Event as ThreadEvent

import psutil
from filelock import FileLock, Timeout
from peewee import DatabaseError, SqliteDatabase

from frigate.const import (
    CACHE_DIR,
    CLIPS_DIR,
    DEFAULT_DB_PATH,
    DEFAULT_FFMPEG_VERSION,
    EXPORT_DIR,
    RECORD_DIR,
)
from frigate.models import Event, Recordings
from frigate.record.archive import storage_lock
from frigate.record.policy import CapturePadding, recording_is_protected

METADATA = (
    "duration",
    "segment_size",
    "keyframes",
    "has_audio",
    "audio_rate",
    "audio_codec",
    "video_codec",
)
DEFAULT_FFMPEG = (
    f"/usr/lib/ffmpeg/{DEFAULT_FFMPEG_VERSION}/bin/ffmpeg"
    if DEFAULT_FFMPEG_VERSION
    else "ffmpeg"
)
DEFAULT_FFPROBE = (
    f"/usr/lib/ffmpeg/{DEFAULT_FFMPEG_VERSION}/bin/ffprobe"
    if DEFAULT_FFMPEG_VERSION
    else "ffprobe"
)


class RepairError(RuntimeError):
    """A repair could not be verified or safely published."""


class RepairCancelled(RepairError):
    """A user stopped work before publication."""


def assert_offline() -> None:
    """Refuse known Frigate/media workers, including separate cleanup processes."""
    try:
        for process in psutil.process_iter(["pid", "name", "cmdline"]):
            if process.info["pid"] == os.getpid():
                continue
            name = (process.info["name"] or "").lower()
            args = [
                arg.lower().replace("\\", "/") for arg in process.info["cmdline"] or []
            ]
            if (
                "frigate" in name
                or name in ("ffmpeg", "ffmpeg.exe")
                or any(
                    arg == "frigate"
                    or arg.startswith("frigate.")
                    or arg.endswith("/frigate/__main__.py")
                    for arg in args
                )
            ):
                raise RepairError("Media workers are active; stop all workers first")
    except (psutil.AccessDenied, psutil.NoSuchProcess) as error:
        raise RepairError(
            "Cannot verify offline process state; retry offline"
        ) from error


def _plain_path(path: Path) -> None:
    if (
        not path.is_absolute()
        or path.resolve() != path
        or any(part.is_symlink() for part in (path, *path.parents))
    ):
        raise RepairError("Paths must be absolute and must not contain symlinks")


def _sync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _fingerprint(path: Path) -> list[int]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise RepairError("Source must be a regular, non-hardlinked local file")
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _number(value: object) -> float:
    try:
        result = float(str(value))
    except ValueError as error:
        raise RepairError("Missing or invalid media timestamp") from error
    if not math.isfinite(result):
        raise RepairError("Non-finite media timestamp")
    return result


class AudioRepair:
    """Repair indexed segments while retaining verified originals and recovery data."""

    SUPPORTED_VIDEO_CODECS = frozenset(("hevc", "h264"))

    def __init__(
        self,
        database: SqliteDatabase,
        recordings: Path,
        cache: Path,
        backup: Path,
        ffmpeg: str = DEFAULT_FFMPEG,
        ffprobe: str = DEFAULT_FFPROBE,
        timeout: float = 120,
        padding: CapturePadding = CapturePadding(30, 30, 30, 30),
        archive: Path | None = None,
        online: bool = False,
        cancel: ThreadEvent | None = None,
        padding_provider: Callable[[], CapturePadding] | None = None,
    ) -> None:
        """Set explicit storage boundaries and bounded media-tool execution."""
        self.database = database
        self.recordings = recordings.absolute()
        self.cache = cache.absolute()
        self.backup = backup.absolute()
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.timeout = timeout
        self.padding = padding
        self.padding_provider = padding_provider
        self.online = online
        self.cancel = cancel or ThreadEvent()
        for path in (self.recordings, self.cache, self.backup):
            _plain_path(path)
        managed = [
            self.recordings,
            self.cache,
            Path(RECORD_DIR),
            Path(CACHE_DIR),
            Path(EXPORT_DIR),
            Path(CLIPS_DIR),
        ]
        if archive is not None:
            _plain_path(archive)
            managed.append(archive)
        if any(
            self.backup == root
            or root in self.backup.parents
            or self.backup in root.parents
            for root in managed
        ):
            raise RepairError("Backup directory must be separate from managed media")
        if timeout <= 0 or not math.isfinite(timeout):
            raise RepairError("Timeout must be positive and finite")

    def _run(self, command: list[str]) -> bytes:
        process = None
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + self.timeout
            while True:
                if self.cancel.is_set():
                    raise RepairCancelled("Stopped before publication")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RepairError("Media subprocess timed out")
                try:
                    output, _ = process.communicate(timeout=min(remaining, 0.2))
                    break
                except subprocess.TimeoutExpired:
                    continue
        except OSError as error:
            raise RepairError("Cannot execute media tool") from error
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()
        if process.returncode:
            # Do not echo subprocess stderr, which can contain private media paths.
            raise RepairError(f"Media tool failed with exit code {process.returncode}")
        return output

    def probe(self, path: Path) -> dict:
        """Read streams and video packet hashes/timestamps, not decoded frame hashes."""
        output = self._run(
            [
                self.ffprobe,
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-show_packets",
                "-show_data_hash",
                "sha256",
                "-of",
                "json",
                str(path),
            ]
        )
        try:
            result = json.loads(output)
            streams = result["streams"]
            video = [stream for stream in streams if stream["codec_type"] == "video"]
            audio = [stream for stream in streams if stream["codec_type"] == "audio"]
            if (
                len(video) != 1
                or len(audio) > 1
                or len(streams) != len(video) + len(audio)
            ):
                raise RepairError(
                    "Expected exactly one video and at most one audio stream"
                )
            if video[0]["codec_name"] not in self.SUPPORTED_VIDEO_CODECS:
                raise RepairError(
                    "Recording video codec is unsupported for this operation"
                )
            packets = result["packets"]
            if not packets:
                raise RepairError("Recording contains no packets")
            if _number(result["format"]["duration"]) <= 0:
                raise RepairError("Recording has no positive duration")
            for stream in streams:
                _number(stream.get("start_time"))
                if _number(stream.get("duration")) <= 0:
                    raise RepairError("Stream has no positive duration")
            result["video"] = video[0]
            result["audio"] = audio[0] if audio else None
            return result
        except (KeyError, TypeError, ValueError) as error:
            raise RepairError("Invalid ffprobe response") from error

    @staticmethod
    def _packets(probe: dict, stream: dict) -> list[dict]:
        return [
            packet
            for packet in probe["packets"]
            if packet["stream_index"] == stream["index"]
        ]

    def validate(self, original: dict, repaired: dict, path: Path) -> dict:
        """Verify copied video, original timeline, audio alignment, and full decoding."""
        before = self._packets(original, original["video"])
        after = self._packets(repaired, repaired["video"])
        if not before or len(before) != len(after):
            raise RepairError("Video packet count changed")
        for old, new in zip(before, after):
            if old.get("data_hash") is None or old["data_hash"] != new.get("data_hash"):
                raise RepairError("Video packet bitstream changed")
            for field in ("pts_time", "dts_time", "duration_time"):
                if abs(_number(old.get(field)) - _number(new.get(field))) > 0.00001:
                    raise RepairError("Video packet timing changed")
        for field in (
            "codec_name",
            "codec_tag_string",
            "extradata_hash",
            "width",
            "height",
            "r_frame_rate",
            "pix_fmt",
            "sample_aspect_ratio",
            "color_range",
            "color_transfer",
            "color_primaries",
        ):
            if original["video"].get(field) != repaired["video"].get(field):
                raise RepairError("Video stream configuration changed")
        audio = repaired["audio"]
        if (
            audio is None
            or audio.get("codec_name") != "aac"
            or audio.get("profile") != "LC"
            or audio.get("sample_rate") != "48000"
            or audio.get("channels") != 2
        ):
            raise RepairError("Output is not AAC-LC 48 kHz stereo")
        for probe in (original, repaired):
            packets = self._packets(probe, probe["audio"])
            if not packets:
                raise RepairError("Audio stream contains no packets")
            for previous, following in zip(packets, packets[1:]):
                previous_end = _number(previous.get("pts_time")) + _number(
                    previous.get("duration_time")
                )
                if abs(previous_end - _number(following.get("pts_time"))) > 0.025:
                    raise RepairError(
                        "Discontinuous audio timeline requires manual review"
                    )
        # AAC-LC uses 1024-sample frames. MP4 edit lists trim zero-based priming,
        # but nonzero timelines can retain a priming/padding frame. Bound each
        # endpoint independently to one 48 kHz frame plus muxer rounding, never
        # allow a video shift, and do not confuse two endpoint deltas with drift.
        for stream_name in ("video", "audio"):
            old = original[stream_name]
            new = repaired[stream_name]
            old_start = _number(old.get("start_time"))
            new_start = _number(new.get("start_time"))
            old_end = old_start + _number(old.get("duration"))
            new_end = new_start + _number(new.get("duration"))
            tolerance = 0.025 if stream_name == "audio" else 0.00001
            if (
                abs(old_start - new_start) > tolerance
                or abs(old_end - new_end) > tolerance
            ):
                raise RepairError("Stream boundary or A/V alignment changed")
        duration = _number(repaired["format"]["duration"])
        if abs(duration - _number(original["format"]["duration"])) > 0.05:
            raise RepairError("Container duration changed")
        self._run(
            [
                self.ffmpeg,
                "-hide_banner",
                "-nostdin",
                "-v",
                "error",
                "-xerror",
                "-err_detect",
                "explode",
                "-i",
                str(path),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
                "-f",
                "null",
                "-",
            ]
        )
        return {
            "duration": duration,
            "segment_size": path.stat().st_size / 2**20,
            "keyframes": [
                int(_number(packet["pts_time"]) * 1000)
                for packet in after
                if "K" in packet.get("flags", "")
            ],
            "has_audio": True,
            "audio_rate": 48000,
            "audio_codec": "aac",
            "video_codec": repaired["video"]["codec_name"],
        }

    def skip_reason(self, row: Recordings) -> str | None:
        """Exclude unavailable, protected, in-progress, or nonlocal footage."""
        path = Path(row.path)
        if row.storage_tier != "local":
            return "archived"
        if row.delete_requested:
            return "deletion-pending"
        if row.end_time is None or row.end_time >= time.time() - 60:
            return "not-completed"
        if path.suffix.lower() != ".mp4" or self.recordings not in path.parents:
            return "outside-recordings"
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            return "symlink"
        if path.resolve() != path or any(
            root == path or root in path.parents
            for root in (Path(EXPORT_DIR), Path(CLIPS_DIR), self.cache)
        ):
            return "outside-recordings"
        if not path.is_file():
            return "missing"
        if recording_is_protected(
            row.camera,
            row.start_time,
            row.end_time,
            self.padding_provider() if self.padding_provider else self.padding,
        ):
            return "protected"
        return None

    def _load(self, recording_id: str) -> Recordings | None:
        return (
            Recordings.select()
            .where(Recordings.id == recording_id)
            .bind(self.database)
            .first()
        )

    def _update(self, recording_id: str, metadata: dict) -> None:
        if (
            Recordings.update(**metadata)
            .where(Recordings.id == recording_id)
            .bind(self.database)
            .execute()
            != 1
        ):
            raise RepairError("Recording disappeared before metadata commit")

    def _unchanged(
        self, old: dict, fingerprint: list[int], digest: str | None = None
    ) -> None:
        row = self._load(old["id"])
        if row is None or row.__data__ != old or self.skip_reason(row):
            raise RepairError("Indexed recording changed or became protected")
        path = Path(row.path)
        if _fingerprint(path) != fingerprint or (
            digest is not None and _digest(path) != digest
        ):
            raise RepairError("Source recording changed")

    def _journal(self, path: Path, data: dict) -> None:
        pending = path.with_suffix(f".{uuid.uuid4().hex}.pending")
        with pending.open("x", encoding="utf-8") as target:
            os.chmod(pending, 0o600)
            json.dump(data, target, sort_keys=True)
            target.flush()
            os.fsync(target.fileno())
        os.replace(pending, path)
        _sync_directory(path.parent)

    @staticmethod
    def _copy(source: Path, destination: Path, source_info: os.stat_result) -> None:
        with source.open("rb") as incoming, destination.open("xb") as outgoing:
            os.chmod(destination, 0o600)
            shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        if os.name != "nt":
            os.chown(destination, source_info.st_uid, source_info.st_gid)
        os.chmod(destination, stat.S_IMODE(source_info.st_mode))
        if os.name != "nt":
            with destination.open("rb") as handle:
                os.fsync(handle.fileno())

    def _space(self, source: Path, duration: float = 0) -> None:
        needed = source.stat().st_size * 3 + math.ceil(duration * 20000) + 64 * 2**20
        if any(
            shutil.disk_usage(root).free < needed
            for root in (source.parent, self.backup)
        ):
            raise RepairError("Insufficient disk space for repair and rollback")

    def repair(self, row: Recordings, apply: bool = False) -> str:
        """Inspect or repair one indexed recording; never synthesize missing audio."""
        reason = self.skip_reason(row)
        if reason:
            return f"skip:{reason}"
        path = Path(row.path)
        snapshot = {name: getattr(row, name) for name in Recordings._meta.fields}
        fingerprint = _fingerprint(path)
        original = self.probe(path)
        audio = original["audio"]
        if audio is None:
            return "skip:no-audio"
        if (
            audio.get("codec_name") == "aac"
            and audio.get("profile") == "LC"
            and audio.get("sample_rate") == "48000"
            and audio.get("channels") == 2
        ):
            return "skip:already-compatible"
        if not apply:
            return "would-repair"
        if not self.online:
            assert_offline()
        if self.cancel.is_set():
            raise RepairCancelled("Stopped before publication")
        self.backup.mkdir(parents=True, exist_ok=True, mode=0o700)
        _plain_path(self.backup)
        self._space(path, _number(original["format"]["duration"]))
        digest = _digest(path)
        self._unchanged(snapshot, fingerprint, digest)
        token = uuid.uuid4().hex
        saved = self.backup / f"{token}.original.mp4"
        staged = path.parent / f".audio-repair-{token}.mp4"
        journal = self.backup / f"{token}.json"
        source_info = path.stat()
        self._copy(path, saved, source_info)
        if _digest(saved) != digest:
            raise RepairError("Backup validation failed")
        _sync_directory(saved.parent)
        data = {
            "version": 1,
            "database": str(Path(self.database.database).absolute()),
            "row": snapshot,
            "source_hash": digest,
            "backup": str(saved),
            "staged": str(staged),
            "state": "prepared",
        }
        self._journal(journal, data)
        try:
            with staged.open("xb"):
                os.chmod(staged, 0o600)
            self._run(
                [
                    self.ffmpeg,
                    "-hide_banner",
                    "-nostdin",
                    "-v",
                    "error",
                    "-xerror",
                    "-y",
                    "-copyts",
                    "-i",
                    str(path),
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0",
                    "-map_metadata",
                    "0",
                    "-c:v",
                    "copy",
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
                    "-avoid_negative_ts",
                    "disabled",
                    "-movflags",
                    "+faststart",
                    "-fs",
                    str(
                        path.stat().st_size
                        + math.ceil(_number(audio["duration"]) * 20000)
                        + 2 * 2**20
                    ),
                    str(staged),
                ]
            )
            metadata = self.validate(original, self.probe(staged), staged)
            with staged.open("rb+") as handle:
                os.fsync(handle.fileno())
            if os.name != "nt":
                os.chown(staged, source_info.st_uid, source_info.st_gid)
            os.chmod(staged, stat.S_IMODE(source_info.st_mode))
            data.update(output_hash=_digest(staged), metadata=metadata, state="ready")
            self._journal(journal, data)
            # Hashing, conversion, decoding and backup I/O stay outside the
            # shared lock. Completed files are immutable; the final stat/CAS
            # catches any archive/delete/protection/source changes meanwhile.
            self._unchanged(snapshot, fingerprint, digest)
            self._space(path)
            if not self.online:
                assert_offline()
            with storage_lock() if self.online else nullcontext():
                try:
                    if self.cancel.is_set():
                        raise RepairCancelled("Stopped before publication")
                    with self.database.atomic("IMMEDIATE"):
                        self._unchanged(snapshot, fingerprint)
                        os.replace(staged, path)
                        _sync_directory(path.parent)
                        self._update(row.id, metadata)
                    data["state"] = "committed"
                    self._journal(journal, data)
                except (RepairError, OSError, DatabaseError, ValueError):
                    # Do not let a retain/archive/delete claim enter between
                    # a failed publication and its rollback.
                    self.recover(journal, _locked=True)
                    raise
        except (
            RepairError,
            OSError,
            DatabaseError,
            ValueError,
            subprocess.SubprocessError,
        ):
            # Recovery chooses rollback from a durable original, even if the
            # process died after replacement but before the DB commit/journal.
            self.recover(journal)
            raise
        finally:
            if staged.exists():
                staged.unlink()
        return "repaired"

    def recover(self, journal: Path, *, _locked: bool = False) -> str:
        """Roll back a nonfinal journal, preserving originals and DB identity."""
        if not self.online:
            assert_offline()
        _plain_path(journal)
        if journal.parent != self.backup:
            raise RepairError("Journal must be in the explicit backup directory")
        try:
            data = json.loads(journal.read_text(encoding="utf-8"))
            if data["version"] != 1 or data["database"] != str(
                Path(self.database.database).absolute()
            ):
                raise RepairError("Journal belongs to another database")
            old = data["row"]
            path = Path(old["path"])
            saved = Path(data["backup"])
            staged = Path(data["staged"])
            if data["state"] in ("committed", "rolled-back"):
                return data["state"]
            if (
                self.recordings not in path.parents
                or saved.parent != self.backup
                or staged.parent != path.parent
                or not staged.name.startswith(".audio-repair-")
            ):
                raise RepairError("Invalid journal paths")
            for item in (path, saved, staged):
                _plain_path(item)
            if _digest(saved) != data["source_hash"]:
                raise RepairError("Original backup is missing or damaged")
            current_hash = _digest(path)
            current_fingerprint = _fingerprint(path)
            if current_hash not in (data["source_hash"], data.get("output_hash")):
                raise RepairError(
                    "Recording changed after repair; manual recovery required"
                )
            restored = path.parent / f".audio-repair-restore-{uuid.uuid4().hex}.mp4"
            with (
                storage_lock() if self.online and not _locked else nullcontext(),
                self.database.atomic("IMMEDIATE"),
            ):
                current = self._load(old["id"])
                if current is None or any(
                    current.__data__.get(key) != value
                    for key, value in old.items()
                    if key not in METADATA
                ):
                    raise RepairError(
                        "Recording identity/protection changed; manual recovery required"
                    )
                if _fingerprint(path) != current_fingerprint:
                    raise RepairError("Recording changed during recovery")
                if (
                    self.online
                    and current_hash != data["source_hash"]
                    and self.skip_reason(current)
                ):
                    raise RepairError(
                        "Recording is no longer eligible for online rollback"
                    )
                if any(
                    getattr(current, key)
                    not in (old.get(key), data.get("metadata", {}).get(key))
                    for key in METADATA
                ):
                    raise RepairError(
                        "Recording metadata changed; manual recovery required"
                    )
                if current_hash != data["source_hash"]:
                    self._copy(saved, restored, saved.stat())
                    os.replace(restored, path)
                    _sync_directory(path.parent)
                self._update(old["id"], {key: old.get(key) for key in METADATA})
            staged.unlink(missing_ok=True)
            data["state"] = "rolled-back"
            self._journal(journal, data)
            return "rolled-back"
        except (KeyError, TypeError, ValueError) as error:
            raise RepairError("Invalid recovery journal") from error


def main(argv: list[str] | None = None) -> int:
    """Run a dry-run selection or explicitly acknowledged offline repair."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path(DEFAULT_DB_PATH))
    parser.add_argument("--recordings-dir", type=Path, default=Path(RECORD_DIR))
    parser.add_argument("--cache-dir", type=Path, default=Path(CACHE_DIR))
    parser.add_argument("--archive-dir", type=Path)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--camera", action="append", help="Repeat for multiple cameras")
    parser.add_argument(
        "--start", type=float, help="Inclusive segment start, Unix seconds"
    )
    parser.add_argument("--end", type=float, help="Exclusive segment end, Unix seconds")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--capture-padding",
        type=float,
        default=30,
        help="Conservative saved-event padding in seconds; use at least your configured maximum",
    )
    parser.add_argument("--ffmpeg", default=DEFAULT_FFMPEG)
    parser.add_argument("--ffprobe", default=DEFAULT_FFPROBE)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--offline-maintenance",
        action="store_true",
        help="Acknowledge all Frigate, archive, cleanup, and media writers are stopped",
    )
    parser.add_argument(
        "--recover",
        type=Path,
        help="Roll back an unfinished journal (requires --apply)",
    )
    args = parser.parse_args(argv)
    if (
        args.limit <= 0
        or args.capture_padding < 0
        or not math.isfinite(args.capture_padding)
    ):
        parser.error(
            "limit must be positive and capture padding nonnegative and finite"
        )
    if any(
        value is not None and not math.isfinite(value)
        for value in (args.start, args.end)
    ):
        parser.error("time bounds must be finite")
    if args.start is not None and args.end is not None and args.start >= args.end:
        parser.error("start must precede end")
    if (args.apply and not args.offline_maintenance) or (
        args.recover and not args.apply
    ):
        parser.error(
            "--apply requires --offline-maintenance; --recover also requires --apply"
        )
    database = None
    try:
        _plain_path(args.database)
        if not args.database.is_file():
            raise RepairError("Database does not exist")
        database = SqliteDatabase(str(args.database), timeout=5)
        with database.bind_ctx([Recordings, Event]):
            database.connect()
            repair = AudioRepair(
                database,
                args.recordings_dir,
                args.cache_dir,
                args.backup_dir,
                args.ffmpeg,
                args.ffprobe,
                args.timeout,
                CapturePadding(*([args.capture_padding] * 4)),
                args.archive_dir,
            )
            if not args.apply:
                database.execute_sql("PRAGMA query_only=ON")

            def execute() -> None:
                if args.recover:
                    print(repair.recover(args.recover))
                    return
                if repair.backup.exists() and any(
                    json.loads(journal.read_text(encoding="utf-8")).get("state")
                    not in ("committed", "rolled-back")
                    for journal in repair.backup.glob("*.json")
                ):
                    raise RepairError(
                        "Unfinished repair journal exists; recover it first"
                    )
                query = Recordings.select().order_by(
                    Recordings.start_time, Recordings.id
                )
                if args.camera:
                    query = query.where(Recordings.camera.in_(args.camera))
                if args.start is not None:
                    query = query.where(Recordings.start_time >= args.start)
                if args.end is not None:
                    query = query.where(Recordings.end_time <= args.end)
                for row in query.limit(args.limit):
                    print(f"{row.id}: {repair.repair(row, args.apply)}", flush=True)

            if args.apply:
                assert_offline()
                repair.backup.mkdir(parents=True, exist_ok=True, mode=0o700)
                repair.cache.mkdir(parents=True, exist_ok=True)
                with (
                    FileLock(str(repair.backup / "audio-repair.lock"), timeout=0),
                    FileLock(str(repair.cache / "recording-storage.lock"), timeout=0),
                ):
                    assert_offline()
                    execute()
            else:
                execute()
    except (RepairError, OSError, DatabaseError, Timeout, ValueError) as error:
        print(f"Audio repair failed: {error}", file=sys.stderr)
        return 1
    finally:
        if database is not None and not database.is_closed():
            database.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

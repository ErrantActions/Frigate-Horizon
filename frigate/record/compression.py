"""Verified selectable video conversion using the audio-maintenance recovery primitives."""

import os
import shutil
import time
from fractions import Fraction
from pathlib import Path

from frigate.config.compression import StorageCompressionConfig
from frigate.models import ArchiveTransfer, RecordingLease, Recordings
from frigate.record.audio_repair import (
    AudioRepair,
    RepairError,
    _number,
)
from frigate.record.compression_storage import CompressionStorage
from frigate.record.lease_reader import RenewingRecordingLease
from frigate.record.leases import recording_has_lease


class CompressionError(RepairError):
    """Compression cannot safely proceed."""


def fixed_rate(probe: dict) -> Fraction:
    """Reject VFR and ambiguous timelines rather than inventing a frame rate."""
    try:
        video = probe["video"]
        rate = Fraction(video["r_frame_rate"])
        average = Fraction(video["avg_frame_rate"])
        packets = AudioRepair._packets(probe, video)
        durations = [_number(packet["duration_time"]) for packet in packets]
        pts = sorted(_number(packet["pts_time"]) for packet in packets)
    except (KeyError, ValueError, ZeroDivisionError, TypeError) as error:
        raise CompressionError("unknown-frame-rate") from error
    if (
        not 0 < rate <= 240
        or abs(float(average / rate) - 1) > 0.001
        or not pts
        or any(abs(value - float(1 / rate)) > 0.0001 for value in durations)
        or any(abs(b - a - float(1 / rate)) > 0.0001 for a, b in zip(pts, pts[1:]))
    ):
        raise CompressionError("variable-frame-rate")
    return rate


def _color_range(video: dict) -> str:
    # Match FFmpeg's decoded YUV default when a source omits range signaling.
    return video.get("color_range") or (
        "pc" if video.get("pix_fmt", "").startswith("yuvj") else "tv"
    )


class RecordingCompression(AudioRepair):
    """Retain durable originals and atomically publish only verified smaller outputs."""

    SUPPORTED_VIDEO_CODECS = frozenset(("hevc", "h264", "av1"))

    def __init__(
        self,
        *args,
        policy: StorageCompressionConfig,
        readers_leased: bool = False,
        completed=None,
        deadline: float | None = None,
        archive_settings_provider=None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.policy = policy
        self.readers_leased = readers_leased
        self.completed = completed or (lambda _id: False)
        self.reader = None
        self.deadline = deadline
        self.last_journal = None
        self.archive_settings_provider = archive_settings_provider
        self.storage = CompressionStorage(self)
        self._active_token = None

    def archive_identity(self) -> dict:
        """Snapshot the mounted archive identity and budget, never an arbitrary path."""
        if self.archive_settings_provider is None:
            raise CompressionError("archive-not-configured")
        settings = self.archive_settings_provider()
        if not settings.enabled:
            raise CompressionError("archive-disabled")
        return {
            "root": str(Path(settings.archive_path).absolute()),
            "mount_id": settings.mount_id,
            "budget_gib": settings.max_archive_gib,
        }

    def _run(self, command: list[str]) -> bytes:
        if self.reader is not None:
            self.reader.check()
        timeout = self.timeout
        if self.deadline is not None:
            self.timeout = min(timeout, self.deadline - time.monotonic())
            if self.timeout <= 0:
                raise CompressionError("batch-runtime-exhausted")
        try:
            if self.policy.encoder.startswith("lib") and command[0] in (
                self.ffmpeg,
                self.ffprobe,
            ):
                if (
                    not hasattr(os, "sched_getaffinity")
                    or shutil.which("taskset") is None
                ):
                    raise CompressionError("cpu-affinity-unavailable")
                cpus = sorted(os.sched_getaffinity(0))[-self.policy.cpu_threads :]
                if not cpus:
                    raise CompressionError("cpu-affinity-unavailable")
                command = ["taskset", "--cpu-list", ",".join(map(str, cpus)), *command]
            return super()._run(command)
        finally:
            self.timeout = timeout

    def skip_reason(self, row: Recordings) -> str | None:
        """Apply fresh age, protection, lease and permanent completion exclusions."""
        if not self.policy.enabled:
            return "compression-disabled"
        reason = (
            super().skip_reason(row)
            if row.storage_tier == "local"
            else self._archive_skip_reason(row)
        )
        if reason:
            return reason
        if not self.readers_leased:
            return "reader-leases-not-certified"
        if row.end_time > time.time() - self.policy.min_age_days * 86400:
            return "too-young"
        if self.completed(row.id):
            return "already-compressed"
        if ArchiveTransfer.select().where(ArchiveTransfer.id == row.id).exists():
            reservation = self.database.execute_sql(
                f"SELECT kind,owner FROM {ArchiveTransfer._meta.table_name} WHERE id=?",
                (row.id,),
            ).fetchone()
            if (
                reservation != ("compression", self._active_token)
                or self._active_token is None
            ):
                return "storage-reserved"
        return "active-reader" if self.other_readers(row) else None

    def _archive_skip_reason(self, row: Recordings) -> str | None:
        if row.storage_tier != "archive":
            return "unknown-tier"
        if row.delete_requested:
            return "deletion-pending"
        if row.end_time is None or row.end_time >= time.time() - 60:
            return "not-completed"
        path = Path(row.path)
        if path.suffix.lower() != ".mp4" or self.recordings not in path.parents:
            return "outside-recordings"
        if row.archive_bytes is None or row.archive_bytes < 0:
            return "archive-accounting-pending"
        from frigate.record.policy import recording_is_protected

        if recording_is_protected(
            row.camera,
            row.start_time,
            row.end_time,
            self.padding_provider() if self.padding_provider else self.padding,
        ):
            return "protected"
        return None

    def other_readers(self, row: Recordings) -> bool:
        """Exclude the worker's own renewable lease, but no other reader."""
        if self.reader is None:
            return recording_has_lease(row)
        others = RecordingLease.select().where(
            RecordingLease.camera == row.camera,
            RecordingLease.start_time <= row.end_time,
            RecordingLease.end_time >= row.start_time,
            RecordingLease.expires > time.time(),
            RecordingLease.id != self.reader.token,
        )
        return others.exists()

    def command(self, source: Path, target: Path, probe: dict) -> list[str]:
        """Use the selected encoder without implicit codec or hardware fallbacks."""
        rate = (
            Fraction(self.policy.frame_rate)
            if self.policy.frame_rate is not None
            else fixed_rate(probe)
        )
        audio = probe["audio"]
        if audio and audio["codec_name"] not in ("aac", "mp3", "ac3", "eac3", "alac"):
            raise CompressionError("incompatible-audio")
        bitrate = str(round(self.policy.target_mbps * 1_000_000))
        command = [
            self.ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-v",
            "error",
            "-xerror",
            "-y",
            "-copyts",
            "-threads",
            str(min(self.policy.cpu_threads, 4)),
            "-i",
            str(source),
            "-map",
            "0:v:0",
        ]
        if audio:
            command += ["-map", "0:a:0", "-c:a", "copy"]
        else:
            command += ["-an"]
        command += [
            "-map_metadata",
            "0",
            "-c:v",
            self.policy.encoder,
            "-threads",
            str(self.policy.cpu_threads),
        ]
        match self.policy.encoder:
            case "h264_nvenc" | "hevc_nvenc" | "av1_nvenc":
                command += ["-preset", "p5", "-rc", "vbr"]
            case "libsvtav1":
                command += [
                    "-preset",
                    str(self.policy.svt_preset),
                    "-svtav1-params",
                    f"lp={self.policy.cpu_threads}:rc=1:fps-num={rate.numerator}:fps-denom={rate.denominator}",
                ]
            case "libaom-av1":
                command += ["-cpu-used", "8", "-row-mt", "1"]
            case "libx264":
                command += ["-preset", "medium"]
            case "libx265":
                command += ["-preset", "medium"]
        if self.policy.frame_rate is not None:
            expected = round(_number(probe["video"]["duration"]) * rate)
            if expected < 1:
                raise CompressionError("invalid-frame-rate-duration")
            command += ["-vf", f"fps={rate},trim=end_frame={expected}"]
        command += ["-b:v", bitrate]
        if self.policy.encoder != "libsvtav1":
            command += [
                "-maxrate",
                str(int(bitrate) * 2),
                "-bufsize",
                str(int(bitrate) * 4),
            ]
        command += [
            "-fps_mode",
            "cfr",
            "-r",
            str(rate),
            "-enc_time_base",
            str(1 / rate),
            "-tag:v",
            {"h264": "avc1", "hevc": "hvc1", "av1": "av01"}[self.policy.video_codec],
            "-color_range",
            _color_range(probe["video"]),
            "-avoid_negative_ts",
            "disabled",
            "-movflags",
            "+faststart",
            "-fs",
            str(source.stat().st_size),
            str(target),
        ]
        return command

    def validate(self, original: dict, output: dict, path: Path) -> dict:
        """Verify frame cadence/count, geometry, timing, copied audio and full decode."""
        rate = (
            Fraction(self.policy.frame_rate)
            if self.policy.frame_rate is not None
            else fixed_rate(original)
        )
        if fixed_rate(output) != rate:
            raise CompressionError("frame-rate-changed")
        before = self._packets(original, original["video"])
        after = self._packets(output, output["video"])
        expected_frames = (
            round(_number(original["video"]["duration"]) * rate)
            if self.policy.frame_rate is not None
            else len(before)
        )
        if expected_frames != len(after):
            raise CompressionError("frame-count-changed")
        if output["video"]["codec_name"] != self.policy.video_codec:
            raise CompressionError("output-codec-mismatch")
        video_tolerance = (
            1 / float(rate) if self.policy.frame_rate is not None else 0.0001
        )
        for field in (
            "width",
            "height",
            "pix_fmt",
            "sample_aspect_ratio",
            "color_space",
            "color_transfer",
            "color_primaries",
        ):
            if original["video"].get(field) != output["video"].get(field):
                raise CompressionError("video-configuration-changed")
        if _color_range(original["video"]) != _color_range(output["video"]):
            raise CompressionError("video-range-changed")
        for name in ("video", "audio"):
            old, new = original[name], output[name]
            if (old is None) != (new is None):
                raise CompressionError("audio-presence-changed")
            if old is None:
                continue
            for field in ("start_time", "duration"):
                tolerance = video_tolerance if name == "video" else 0.0001
                if abs(_number(old[field]) - _number(new[field])) > tolerance:
                    raise CompressionError("stream-timeline-changed")
            if name == "audio":
                for field in (
                    "codec_name",
                    "sample_rate",
                    "channels",
                    "extradata_hash",
                ):
                    if old.get(field) != new.get(field):
                        raise CompressionError("audio-configuration-changed")
                old_packets, new_packets = (
                    self._packets(original, old),
                    self._packets(output, new),
                )
                if len(old_packets) != len(new_packets):
                    raise CompressionError("audio-packet-count-changed")
                for a, b in zip(old_packets, new_packets):
                    if not a.get("data_hash") or a["data_hash"] != b.get("data_hash"):
                        raise CompressionError("audio-content-changed")
                    for field in ("pts_time", "dts_time", "duration_time"):
                        if abs(_number(a[field]) - _number(b[field])) > 0.0001:
                            raise CompressionError("audio-timeline-changed")
        old_pts = sorted(_number(packet["pts_time"]) for packet in before)
        new_pts = sorted(_number(packet["pts_time"]) for packet in after)
        expected_pts = (
            [new_pts[0] + index / float(rate) for index in range(expected_frames)]
            if self.policy.frame_rate is not None
            else old_pts
        )
        if any(abs(a - b) > 0.0001 for a, b in zip(expected_pts, new_pts)):
            raise CompressionError("video-timeline-changed")
        duration = _number(output["format"]["duration"])
        if abs(duration - _number(original["format"]["duration"])) > video_tolerance:
            raise CompressionError("container-timeline-changed")
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
                "0:a:0?",
                "-f",
                "null",
                "-",
            ]
        )
        audio = output["audio"]
        origin = _number(output["video"]["start_time"])
        keyframes = [
            round((_number(packet["pts_time"]) - origin) * 1000)
            for packet in after
            if "K" in packet.get("flags", "")
        ]
        if not keyframes or keyframes[0] != 0:
            raise CompressionError("missing-initial-keyframe")
        return {
            "duration": duration,
            "segment_size": path.stat().st_size / 2**20,
            "keyframes": keyframes,
            "has_audio": audio is not None,
            "audio_rate": int(audio["sample_rate"]) if audio else None,
            "audio_codec": audio["codec_name"] if audio else None,
            "video_codec": self.policy.video_codec,
        }

    def compress(self, row: Recordings) -> tuple[str, int]:
        """Compress once; an interrupted or failed publication keeps its rollback."""
        reason = self.skip_reason(row)
        if reason:
            return reason, 0
        with RenewingRecordingLease(row.camera, row.start_time, row.end_time) as reader:
            self.reader = reader
            try:
                return self.storage.compress(row)
            finally:
                self.reader = None
                self._active_token = None

    def recover(self, journal: Path, *, _locked: bool = False) -> str:
        """Recover owned tier journals, retaining compatibility with earlier local intents."""
        import json

        data = json.loads(journal.read_text(encoding="utf-8"))
        if data.get("version") == 2:
            return self.storage.recover(journal, _locked=_locked, data=data)
        return super().recover(journal, _locked=_locked)

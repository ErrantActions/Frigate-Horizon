"""Handle storage retention and usage."""

import logging
import shutil
import threading
import time
from collections.abc import Iterable
from multiprocessing.synchronize import Event as MpEvent
from pathlib import Path
from queue import Empty
from typing import TYPE_CHECKING, Any, cast

from peewee import SQL, Case, DatabaseError, fn

from frigate.config import FrigateConfig
from frigate.config.camera.updater import (
    CameraConfigUpdateEnum,
    CameraConfigUpdateSubscriber,
)
from frigate.const import (
    RECORD_DIR,
    REPLAY_CAMERA_PREFIX,
    STREAM_TYPE_MAIN,
    STREAM_TYPE_SUB,
)
from frigate.models import Recordings
from frigate.record.local_mirror import (
    cleanup_local_mirrors,
    release_local_copy,
    validate_local_mirror_mount,
)
from frigate.record.policy import (
    CapturePadding,
    delete_automatic_recording,
    recording_event_windows,
)
from frigate.util.storage import (
    CameraStorageUsage,
    RecordingStorageUsage,
    empty_recording_usage,
    invalidate_storage_capacity,
    recording_storage_usage,
)

if TYPE_CHECKING:
    from frigate.record.archive_manager import ArchiveProcess

logger = logging.getLogger(__name__)
bandwidth_equation = Recordings.segment_size / (
    Recordings.end_time - Recordings.start_time
)

MAX_CALCULATED_BANDWIDTH = 10000  # 10Gb/hr
BANDWIDTH_SAMPLE_TARGET = 50


class StorageMaintainer(threading.Thread):
    """Maintain frigates recording storage."""

    def __init__(self, config: FrigateConfig, stop_event: MpEvent) -> None:
        super().__init__(name="storage_maintainer")
        self.config = config
        self.stop_event = stop_event
        self.camera_storage_stats: dict[str, dict] = {}

        # read by stats; true while maintenance has to delete retained recordings
        self.retention_unmet = False
        self.archive_process: ArchiveProcess | None = None
        self._archive_lock = threading.RLock()
        self.archive_status: dict[str, Any] = {
            "state": "starting",
            "updated_at": time.time(),
            "error": None,
        }
        self._tier_usage: dict[str, RecordingStorageUsage] = {}
        self._camera_tier_usage: dict[str, dict[str, RecordingStorageUsage]] = {}
        self._tier_usage_updated = float("-inf")
        self._tier_usage_sampled_at: float | None = None
        self._tier_usage_error: str | None = None
        self._tier_usage_lock = threading.Lock()
        self.config_subscriber = CameraConfigUpdateSubscriber(
            self.config,
            self.config.cameras,
            [CameraConfigUpdateEnum.record],
        )

    def start_archive(self) -> None:
        """Run remote I/O in a separate process, even if the share stalls."""
        from frigate.record.archive_manager import ArchiveProcess

        with self._archive_lock:
            if self.config.safe_mode:
                self.archive_status = {
                    "state": "disabled",
                    "updated_at": 0,
                    "error": None,
                }
                return
            self.archive_process = ArchiveProcess(self.config, self.stop_event)
            self.archive_process.start()

    def update_config(self, config: FrigateConfig) -> None:
        """Rebind live storage settings without restarting recording processes."""
        with self._archive_lock:
            self.config = config
            self.config_subscriber.config = config
            self.config_subscriber.camera_configs = config.cameras
            if self.archive_process is not None:
                self.archive_process.update_config(config)

    def archive_config_pending(self) -> bool:
        """Return whether the worker has acknowledged the latest persisted config."""
        with self._archive_lock:
            return self.archive_process is not None and (
                self.archive_process.config_revision.value
                != self.archive_process.applied_revision.value
            )

    def get_archive_status(self) -> dict[str, Any]:
        """Read cached health only; never probe NFS from a request thread."""
        with self._archive_lock:
            if self.archive_process is not None:
                while True:
                    try:
                        self.archive_status = self.archive_process.status.get_nowait()
                    except Empty:
                        break
                if not self.archive_process.is_alive():
                    return self._unavailable_archive_status(
                        {
                            **self.archive_status,
                            "state": "blocked",
                            "error": "worker_stopped",
                            "settings_pending": self.archive_config_pending(),
                        }
                    )
                if time.time() - self.archive_status["updated_at"] > 60:
                    return self._unavailable_archive_status(
                        {
                            **self.archive_status,
                            "state": "blocked",
                            "error": "worker_unresponsive",
                            "settings_pending": self.archive_config_pending(),
                        }
                    )
            transfer = self.archive_status.get("transfer")
            transfer_stale = (
                transfer is not None
                and time.time() - transfer.get("sampled_at", 0) > 15
            )
            return {
                **self.archive_status,
                "settings_pending": self.archive_config_pending(),
                "transfer": {
                    **transfer,
                    "stale": True,
                    "rate_bytes_per_second": None,
                    "copy_rate_bytes_per_second": None,
                    "verification_rate_bytes_per_second": None,
                    "eta_seconds": None,
                    "buffer_seconds": None,
                }
                if transfer_stale
                else transfer,
                "capacity": {
                    key: (
                        invalidate_storage_capacity(value, "stale")
                        if value.get("sampled_at") is not None
                        and time.time() - value["sampled_at"] > 60
                        else value
                    )
                    for key, value in self.archive_status.get("capacity", {}).items()
                },
            }

    def _unavailable_archive_status(self, status: dict[str, Any]) -> dict[str, Any]:
        """Expire cached headroom and estimates without touching remote storage."""
        capacities = status.get("capacity", {})
        transfer = status.get("transfer")
        return {
            **status,
            "capacity": {
                key: invalidate_storage_capacity(value, "stale")
                for key, value in capacities.items()
            },
            "transfer": {
                **transfer,
                "rate_bytes_per_second": None,
                "copy_rate_bytes_per_second": None,
                "verification_rate_bytes_per_second": None,
                "eta_seconds": None,
                "buffer_seconds": None,
            }
            if transfer is not None
            else None,
        }

    def get_storage_usage(
        self,
    ) -> tuple[dict[str, RecordingStorageUsage], dict[str, CameraStorageUsage]]:
        """Read the last completed snapshot without querying SQLite or the NAS."""
        with self._tier_usage_lock:
            config = self.config
            cameras: dict[str, CameraStorageUsage] = {}
            for name in sorted(set(config.cameras) | set(self._camera_tier_usage)):
                camera = config.cameras.get(name)
                tiers = self._camera_tier_usage.get(name, {})
                cameras[name] = {
                    "friendly_name": camera.get_formatted_name() if camera else name,
                    "tiers": {
                        tier: usage.copy()
                        for tier, usage in {
                            "local": empty_recording_usage(),
                            "archive": empty_recording_usage(),
                            **tiers,
                        }.items()
                    },
                }
            return (
                {tier: usage.copy() for tier, usage in self._tier_usage.items()},
                cameras,
            )

    def refresh_storage_usage(self) -> None:
        """Build aggregates in the maintainer, publishing only complete snapshots."""
        try:
            totals, cameras = recording_storage_usage()
        except (DatabaseError, ValueError):
            logger.exception("Unable to refresh recording storage usage")
            with self._tier_usage_lock:
                self._tier_usage_error = "storage_usage_unavailable"
            return
        with self._tier_usage_lock:
            self._tier_usage = totals
            self._camera_tier_usage = cameras
            self._tier_usage_updated = time.monotonic()
            self._tier_usage_sampled_at = time.time()
            self._tier_usage_error = None

    def get_storage_usage_status(self) -> dict[str, Any]:
        """Expose snapshot age and failures separately from archive worker health."""
        with self._tier_usage_lock:
            return {
                "sampled_at": self._tier_usage_sampled_at,
                "stale": time.monotonic() - self._tier_usage_updated > 90,
                "error": self._tier_usage_error,
            }

    def get_tier_usage(self) -> dict[str, RecordingStorageUsage]:
        """Return cached global totals using the same snapshot as camera breakdowns."""
        return self.get_storage_usage()[0]

    def _recording_stream_types(self, camera: str) -> tuple[str, ...]:
        """Return the stream types the camera is currently recording."""
        camera_config = self.config.cameras.get(camera)

        if camera_config is None or not camera_config.record.enabled:
            return ()

        if camera_config.record.sub.enabled:
            return (STREAM_TYPE_MAIN, STREAM_TYPE_SUB)

        return (STREAM_TYPE_MAIN,)

    def expected_hourly_bandwidth(self) -> float:
        """Return the MB/hr the cameras are expected to write.

        Only the streams a camera currently records are counted, so toggling
        recording or sub stream recording is reflected without waiting for the
        existing segments of a stopped stream to expire.
        """
        total = 0.0

        for camera, stats in self.camera_storage_stats.items():
            stream_bandwidths = stats.get("bandwidth_by_stream", {})
            total += sum(
                stream_bandwidths.get(stream_type, 0)
                for stream_type in self._recording_stream_types(camera)
            )

        return round(total, 2)

    def _recent_stream_bandwidth(
        self, camera: str, stream_type: str, window: int
    ) -> float | None:
        """Average MB/s over a stream's most recent rows, or None if no sample.

        Zero-size rows are excluded inside the projection, not the WHERE
        clause: a segment_size predicate baits the planner into the
        (camera, segment_size) index plus a full sort of the camera's
        history instead of the time-ordered index.
        """
        recent = (
            Recordings.select(
                Case(
                    None,
                    [(Recordings.segment_size > 0, bandwidth_equation)],
                    None,
                ).alias("bw")
            )
            .where(
                Recordings.camera == camera,
                Recordings.stream_type == stream_type,
            )
            .order_by(Recordings.start_time.desc())
            .limit(window)
            .alias("recent")
        )
        avg: float | None = Recordings.select(fn.AVG(SQL("bw"))).from_(recent).scalar()
        return avg

    def _stream_sample_count(self, camera: str, stream_type: str) -> int:
        """Count a stream's non-zero segments, stopping at the sample target."""
        count: int = (
            Recordings.select(Recordings.id)
            .where(
                Recordings.camera == camera,
                Recordings.stream_type == stream_type,
                Recordings.segment_size > 0,
            )
            .limit(BANDWIDTH_SAMPLE_TARGET)
            .count()
        )
        return count

    def _needs_refresh(self, camera: str) -> bool:
        """Return whether a stream the camera records still lacks samples.

        Counted per stream rather than per camera: a stream that starts
        recording later has no samples of its own yet, and a camera-wide count
        would report it settled on the strength of another stream's history.
        """
        return any(
            self._stream_sample_count(camera, stream_type) < BANDWIDTH_SAMPLE_TARGET
            for stream_type in self._recording_stream_types(camera)
        )

    def calculate_camera_bandwidth(self) -> None:
        """Calculate an average MB/hr for each camera."""
        for camera in self.config.cameras.keys():
            # Skip replay cameras
            if camera.startswith(REPLAY_CAMERA_PREFIX):
                continue

            if not self.camera_storage_stats.get(camera, {}).get("needs_refresh", True):
                continue

            # calculate MB/hr from the last 100 segments of each stream
            # type and sum the rates; mixing streams would average small
            # sub segments against large main segments and underestimate
            # the true write rate
            bandwidth_by_stream: dict[str, float] = {}
            for stream_type in (STREAM_TYPE_MAIN, STREAM_TYPE_SUB):
                avg_bw = self._recent_stream_bandwidth(camera, stream_type, 100)
                if avg_bw is None:
                    # the recent window can be all zero-size ingest
                    # glitches; look further back before concluding
                    # the stream writes nothing
                    avg_bw = self._recent_stream_bandwidth(camera, stream_type, 1000)
                if avg_bw is not None:
                    bandwidth_by_stream[stream_type] = round(avg_bw * 3600, 2)

            bandwidth = round(sum(bandwidth_by_stream.values()), 2)

            if bandwidth > MAX_CALCULATED_BANDWIDTH:
                logger.warning(
                    f"{camera} has a bandwidth of {bandwidth} MB/hr which exceeds the expected maximum. This typically indicates an issue with the cameras recordings."
                )
                # scale each stream so the per stream values still sum to
                # the clamped total the UI displays alongside them
                scale = MAX_CALCULATED_BANDWIDTH / bandwidth
                bandwidth_by_stream = {
                    stream_type: round(value * scale, 2)
                    for stream_type, value in bandwidth_by_stream.items()
                }
                bandwidth = MAX_CALCULATED_BANDWIDTH

            self.camera_storage_stats[camera] = {
                "needs_refresh": self._needs_refresh(camera),
                "bandwidth": bandwidth,
                "bandwidth_by_stream": bandwidth_by_stream,
            }
            logger.debug(f"{camera} has a bandwidth of {bandwidth} MiB/hr.")

    def calculate_camera_usages(self) -> dict[str, dict]:
        """Calculate the storage usage of each camera."""
        usages: dict[str, dict] = {}

        for camera in self.config.cameras.keys():
            # Skip replay cameras
            if camera.startswith(REPLAY_CAMERA_PREFIX):
                continue

            stream_usages = {
                row["stream_type"]: row["usage"] or 0
                for row in cast(
                    Iterable[dict[str, Any]],
                    Recordings.select(
                        Recordings.stream_type,
                        fn.SUM(Recordings.segment_size).alias("usage"),
                    )
                    .where(
                        Recordings.camera == camera,
                        Recordings.segment_size != 0,
                        Recordings.storage_tier == "local",
                    )
                    .group_by(Recordings.stream_type)
                    .dicts(),
                )
            }
            stream_bandwidths = self.camera_storage_stats.get(camera, {}).get(
                "bandwidth_by_stream", {}
            )

            usages[camera] = {
                "friendly_name": self.config.cameras[camera].get_formatted_name(),
                "usage": sum(stream_usages.values()),
                "bandwidth": self.camera_storage_stats.get(camera, {}).get(
                    "bandwidth", 0
                ),
                # only streams with segments on disk are reported, so a camera
                # keeps its sub entry until sub retention expires those segments.
                # bandwidth is null rather than 0 when the cache holds no sample
                # for the stream, since 0 would claim it writes nothing
                "streams": {
                    stream_type: {
                        "usage": stream_usages[stream_type],
                        "bandwidth": stream_bandwidths.get(stream_type),
                    }
                    for stream_type in (STREAM_TYPE_MAIN, STREAM_TYPE_SUB)
                    if stream_usages.get(stream_type)
                },
            }

        return usages

    def check_storage_needs_cleanup(self) -> bool:
        """Return if storage needs cleanup."""
        if self.config.storage.mode == "mirror":
            validate_local_mirror_mount()
        # currently runs cleanup if less than 1 hour of space is left
        # disk_usage should not spin up disks
        hourly_bandwidth = self.expected_hourly_bandwidth()
        remaining_storage = round(shutil.disk_usage(RECORD_DIR).free / pow(2, 20), 1)
        if self.config.storage.enabled and self.config.storage.mode == "mirror":
            local_usage = (
                Recordings.select(fn.SUM(Recordings.segment_size))
                .where(Recordings.storage_tier == "local")
                .scalar()
                or 0
            )
            remaining_storage = min(
                remaining_storage,
                self.config.storage.local_budget_gib * 1024 - local_usage,
            )
        logger.debug(
            f"Storage cleanup check: {hourly_bandwidth} hourly with remaining storage: {remaining_storage}."
        )
        return remaining_storage < float(hourly_bandwidth)

    def reduce_storage_consumption(self) -> None:
        """Reclaim ordinary footage first, then unsaved events, never saved evidence."""
        if self.config.storage.mode == "mirror":
            validate_local_mirror_mount()
        logger.debug("Starting storage cleanup")
        self.retention_unmet = True
        deleted_segments_size = 0.0
        hourly_bandwidth = self.expected_hourly_bandwidth()
        default = CapturePadding.from_record(self.config.record)
        padding = {
            name: CapturePadding.from_record(camera.record)
            for name, camera in self.config.cameras.items()
        }
        try:
            windows = recording_event_windows(padding, default)
        except ValueError:
            logger.exception(
                "Storage cleanup blocked by invalid capture-window metadata"
            )
            return

        # Redundant verified copies take precedence over any loss of originals,
        # even when the redundant copy contains an unsaved event.
        for row in (
            Recordings.select(Recordings.id, Recordings.camera)
            .where(
                Recordings.storage_tier == "local",
                Recordings.archive_bytes.is_null(False),
            )
            .order_by(Recordings.start_time, Recordings.id)
            .iterator()
        ):
            if deleted_segments_size >= hourly_bandwidth:
                break
            reclaimed = release_local_copy(
                row.id,
                Path(self.config.storage.archive_path),
                padding.get(row.camera, default),
            )
            if reclaimed is not None:
                deleted_segments_size += reclaimed

        for event_footage in (False, True):
            if deleted_segments_size >= hourly_bandwidth:
                break
            recordings = (
                Recordings.select(
                    Recordings.id,
                    Recordings.camera,
                    Recordings.start_time,
                    Recordings.end_time,
                )
                .where(
                    Recordings.storage_tier == "local",
                    Recordings.archive_bytes.is_null(),
                    Recordings.end_time < time.time() - 60,
                )
                .order_by(Recordings.start_time.asc(), Recordings.id.asc())
                .namedtuples()
                .iterator()
            )
            for recording in recordings:
                if deleted_segments_size >= hourly_bandwidth:
                    break
                try:
                    is_event = windows.overlaps(
                        recording.camera, recording.start_time, recording.end_time
                    )
                except ValueError:
                    logger.exception(
                        "Preserving recording %s with invalid timeline metadata",
                        recording.id,
                    )
                    continue
                if is_event != event_footage:
                    continue
                reclaimed = delete_automatic_recording(
                    recording.id,
                    "emergency",
                    padding.get(recording.camera, default),
                    clear=True,
                )
                if reclaimed is not None:
                    deleted_segments_size += reclaimed

        self.retention_unmet = deleted_segments_size < hourly_bandwidth
        if self.retention_unmet:
            logger.error(
                "Could only reclaim %.2f of %.2f MiB; protected footage was preserved",
                deleted_segments_size,
                hourly_bandwidth,
            )
        else:
            logger.info("Cleaned up %.2f MiB of recordings", deleted_segments_size)

    def run(self) -> None:
        """Check every 5 minutes if storage needs to be cleaned up."""
        if self.config.safe_mode:
            logger.info("Safe mode enabled, skipping storage maintenance")
            self.config_subscriber.stop()
            return

        self.calculate_camera_bandwidth()
        self.refresh_storage_usage()
        last_usage_attempt = time.monotonic()
        last_maintenance = time.monotonic()
        while not self.stop_event.wait(5):
            try:
                if self.config.storage.enabled and self.config.storage.mode == "mirror":
                    validate_local_mirror_mount()
                cleanup_local_mirrors(self.config)
            except (OSError, DatabaseError):
                logger.exception(
                    "Local mirror cleanup failed; preserving pending progress"
                )
            if time.monotonic() - last_usage_attempt >= 60:
                self.refresh_storage_usage()
                last_usage_attempt = time.monotonic()
            with self._archive_lock:
                if (
                    self.archive_process is not None
                    and not self.archive_process.is_alive()
                ):
                    logger.error(
                        "Archive worker stopped unexpectedly; restarting with current settings"
                    )
                    self.archive_process.updates.close()
                    self.archive_process.status.close()
                    self.start_archive()
            if time.monotonic() - last_maintenance >= 300:
                self._maintain_once()
                last_maintenance = time.monotonic()

        self.config_subscriber.stop()
        logger.info("Exiting storage maintainer...")

    def _maintain_once(self) -> None:
        updated_topics = self.config_subscriber.check_for_updates()

        for camera in updated_topics.get(CameraConfigUpdateEnum.record.name, []):
            if camera in self.camera_storage_stats:
                self.camera_storage_stats[camera]["needs_refresh"] = True

        if not self.camera_storage_stats or True in [
            r["needs_refresh"] for r in self.camera_storage_stats.values()
        ]:
            self.calculate_camera_bandwidth()
            logger.debug(f"Default camera bandwidths: {self.camera_storage_stats}.")

        try:
            needs_cleanup = self.check_storage_needs_cleanup()
        except OSError:
            logger.exception("Local storage cleanup is blocked by unavailable storage")
            self.retention_unmet = True
            return
        if needs_cleanup:
            if not self.config.storage.emergency_delete:
                logger.error(
                    "Local storage critically low; emergency deletion is disabled"
                )
                self.retention_unmet = True
                return
            logger.warning(
                "Local storage critically low; attempting protected-aware reclamation"
            )
            logger.info(
                "Less than 1 hour of recording space left, running storage maintenance..."
            )
            self.reduce_storage_consumption()
        else:
            self.retention_unmet = False

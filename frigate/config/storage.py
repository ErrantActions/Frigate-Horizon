"""Settings for the local recording tier and an optional NFS archive."""

import posixpath
from typing import Literal, Self

from pydantic import Field, model_validator

from .base import FrigateBaseModel
from .compression import StorageCompressionConfig


class StorageConfig(FrigateBaseModel):
    compression: StorageCompressionConfig = Field(
        default_factory=StorageCompressionConfig,
        title="Older-footage compression",
    )
    enabled: bool = Field(
        default=False,
        title="Enable recording archive",
        description="Move older retained recordings to an NFS archive without changing their playback URLs.",
    )
    archive_path: str = Field(
        default="/media/frigate/archive",
        title="Archive directory",
        description="Absolute container path to a dedicated NFS archive directory. Mount the share before enabling archiving.",
    )
    mount_id: str = Field(
        default="frigate-archive",
        min_length=1,
        max_length=128,
        title="Archive identity",
        description="Must match the contents of .frigate-archive on the NFS share. Frigate never creates this marker automatically.",
    )
    local_budget_gib: float = Field(
        default=1024,
        gt=0,
        allow_inf_nan=False,
        title="Local recording budget (GiB)",
        description="Target maximum local recording usage, independent of other files on the filesystem.",
    )
    start_percent: int = Field(
        default=100,
        ge=1,
        le=100,
        title="Local archive start (%)",
        description="Start moving older recordings when local usage reaches this percentage of the local budget. Must be greater than the drain target. Existing configurations default to starting at the full budget.",
    )
    drain_percent: int = Field(
        default=90,
        ge=0,
        le=99,
        title="Local drain target (%)",
        description="After archiving starts, move oldest recordings until local usage reaches this percentage of the budget. Must be less than the start percentage. Physical free-space pressure can keep transfers running below this target.",
    )
    retain_days: float = Field(
        default=90,
        gt=0,
        allow_inf_nan=False,
        title="Archive retention days",
        description="Minimum lifetime of retained recordings, measured from recording time, not transfer time. Existing longer camera retention and saved events are honored.",
    )
    max_age_days: float | None = Field(
        default=None,
        gt=0,
        allow_inf_nan=False,
        title="Maximum archive age (days)",
        description="Optional maximum capture age for archived footage, overriding longer camera retention. Saved evidence and active recording leases are exempt. Unset preserves existing retention.",
    )
    max_archive_gib: float | None = Field(
        default=None,
        ge=1 / (2**30),
        le=(2**63 - 1) / (2**30),
        allow_inf_nan=False,
        title="Maximum archive recording usage (GiB)",
        description="Optional exact file-content budget, including incoming transfer reservations. 6144 GiB equals 6 TiB. Excludes other NAS data and filesystem overhead. Protected footage may exceed the budget and block transfers.",
    )
    archive_full_behavior: Literal["pause", "expire_oldest"] = Field(
        default="pause",
        title="Archive full behavior",
        description="Pause transfers at the archive budget by default. Explicitly choose expire_oldest to delete youngest-needed unprotected footage, oldest ordinary footage first, unsaved events last. This can override minimum retention. Saved evidence and active recording leases are never automatically deleted.",
    )
    archive_reserve_gib: float = Field(
        default=0,
        ge=0,
        allow_inf_nan=False,
        title="Archive filesystem reserve (GiB)",
        description="Keep this much filesystem-available space unused by incoming archive copies, in addition to the recording content budget.",
    )
    transfer_mib_per_second: float = Field(
        default=0,
        ge=0,
        allow_inf_nan=False,
        title="Transfer limit (MiB/s)",
        description="Aggregate background copy and verification bandwidth across all parallel transfers. Zero means unlimited.",
    )
    parallel_transfers: int = Field(
        default=1,
        ge=1,
        le=50,
        title="Parallel archive transfers",
        description="Maximum concurrent verified file transfers in each bounded batch. Multiple workers can overlap storage latency but do not guarantee multiple NFS connections or LACP link distribution.",
    )
    emergency_delete: bool = Field(
        default=True,
        title="Emergency local deletion",
        description="Reclaim ordinary local footage first and unsaved event footage only as a last resort when less than one estimated hour of free space remains. Protected footage is never automatically deleted; recording may stop if no eligible space remains.",
    )

    @model_validator(mode="after")
    def validate_archive(self) -> Self:
        """Keep the archive identity and directory separate from local managed data."""
        if self.drain_percent >= self.start_percent:
            raise ValueError(
                "Local archive drain target must be below the start threshold"
            )
        path = posixpath.normpath(self.archive_path)
        protected = (
            "/media/frigate/recordings",
            "/media/frigate/clips",
            "/media/frigate/exports",
            "/config",
            "/tmp",
            "/dev",
        )
        if (
            not path.startswith("/")
            or path in ("/", "/media", "/media/frigate")
            or any(
                path == root
                or path.startswith(root + "/")
                or root.startswith(path + "/")
                for root in protected
            )
        ):
            raise ValueError(
                "Archive directory must be separate from recording, config, cache, and device directories"
            )
        if self.mount_id.strip() != self.mount_id or not self.mount_id.isprintable():
            raise ValueError("Archive identity must be a single non-empty line")
        self.archive_path = path
        return self

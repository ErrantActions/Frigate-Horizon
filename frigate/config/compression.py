"""Opt-in, bounded GPU compression of older unprotected recordings."""

from typing import Literal

from pydantic import Field

from .base import FrigateBaseModel


class CompressionWindowConfig(FrigateBaseModel):
    """An optional weekly UTC maintenance window, independent of archive transfer."""

    enabled: bool = Field(default=False, title="Enable weekly compression")
    weekday: int = Field(default=6, ge=0, le=6, title="UTC weekday (Monday is 0)")
    start_hour: int = Field(default=2, ge=0, le=23, title="UTC start hour")
    duration_minutes: int = Field(
        default=120, ge=1, le=1440, title="Window length (minutes)"
    )


class StorageCompressionConfig(FrigateBaseModel):
    """Preserve capture age, resolution and fixed frame rate without quota overrides."""

    enabled: bool = Field(default=False, title="Enable older-footage compression")
    encoder: Literal[
        "h264_nvenc",
        "hevc_nvenc",
        "av1_nvenc",
        "libx264",
        "libx265",
        "libsvtav1",
        "libaom-av1",
    ] = Field(
        default="hevc_nvenc",
        title="Compression encoder",
        description="Select the codec and implementation explicitly. NVIDIA encoders require matching hardware support; failures never switch to a different encoder automatically.",
    )
    cpu_threads: int = Field(
        default=8,
        ge=1,
        le=128,
        title="CPU core and thread limit",
        description="CPU encoders are restricted to at most this many available logical CPUs. The limit also covers their decoding and validation subprocesses.",
    )
    svt_preset: int = Field(
        default=12,
        ge=0,
        le=13,
        title="SVT-AV1 speed preset",
        description="Higher values prioritize speed over compression efficiency. Used only by the SVT-AV1 CPU encoder.",
    )
    frame_rate: int | None = Field(
        default=None,
        ge=1,
        le=60,
        title="Fixed output frame rate",
        description="Unset requires the original constant cadence. Setting a frame rate explicitly normalizes timing and may duplicate or drop frames; resolution is preserved.",
    )
    min_age_days: float = Field(
        default=14,
        ge=1,
        le=3650,
        allow_inf_nan=False,
        title="Original quality lifetime (days)",
        description="Minimum age since capture end, never since transfer or compression.",
    )
    target_mbps: float = Field(
        default=4,
        ge=0.1,
        le=100,
        allow_inf_nan=False,
        title="Target video bitrate (Mbps)",
        description="Target variable video bitrate for the selected encoder. Actual averages can differ. Completed files are never automatically recompressed when settings change.",
    )
    max_files: int = Field(default=100, ge=1, le=1000, title="Maximum files per batch")
    max_batch_minutes: int = Field(
        default=30, ge=1, le=1440, title="Maximum batch runtime (minutes)"
    )
    min_free_gib: float = Field(
        default=2,
        ge=0.25,
        le=1024,
        allow_inf_nan=False,
        title="Free-space reserve (GiB)",
    )
    window: CompressionWindowConfig = Field(
        default_factory=CompressionWindowConfig, title="Weekly maintenance window"
    )

    @property
    def video_codec(self) -> str:
        """Return the codec that must be verified before a replacement is published."""
        return {
            "h264_nvenc": "h264",
            "libx264": "h264",
            "hevc_nvenc": "hevc",
            "libx265": "hevc",
            "av1_nvenc": "av1",
            "libsvtav1": "av1",
            "libaom-av1": "av1",
        }[self.encoder]

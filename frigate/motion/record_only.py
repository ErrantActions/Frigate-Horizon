"""Explicit operator-requested recording-only mode, with no video decode or fake detection frames."""

import json
import logging
import math
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from frigate.config import CameraConfig
from frigate.config.camera.ffmpeg import CameraRoleEnum
from frigate.motion.native_person import native_person_active
from frigate.plugins.inference import lease_status

MODE_PATH = Path("/opt/frigate/recording-only.json")
TIMING_CONTROL_PATH = Path("/dev/shm/horizon-timing-calibration.json")
PASSIVE_TIMING_PATH = Path("/opt/frigate/timing-calibration.json")
logger = logging.getLogger(__name__)
PLUGIN_MODES_PATH = Path("/config/plugins/recording-only.json")


@lru_cache(maxsize=1)
def _plugin_modes(path: str, modified: int) -> dict[str, bool]:
    data = json.loads(Path(path).read_bytes())
    if not isinstance(data, dict) or any(
        not isinstance(name, str) or enabled is not True
        for name, enabled in data.items()
    ):
        raise ValueError("Invalid plugin recording-only camera selection")
    return data


def plugin_recording_only(camera: str | None) -> bool:
    """Only provisioned plugin cameras may opt out of continuous decoding."""
    try:
        modified = PLUGIN_MODES_PATH.stat().st_mtime_ns
    except FileNotFoundError:
        return False
    return camera in _plugin_modes(str(PLUGIN_MODES_PATH), modified)


def plugin_inference_active(camera: str | None) -> bool:
    """Keep legacy motion permission and verified native-person grants separate."""
    return (
        plugin_recording_only(camera) and lease_status(camera)["active"]
    ) or native_person_active(camera)


def detection_capture_active(camera: str | None) -> bool:
    """Keep storage in copy-only mode while independently permitting AI frames."""
    return not recording_only(camera) or plugin_inference_active(camera)


def event_detection_command(camera: CameraConfig) -> list[str]:
    """Build a detection-only process that cannot duplicate recording outputs."""
    inputs = [
        item for item in camera.ffmpeg.inputs if CameraRoleEnum.detect in item.roles
    ]
    if len(inputs) != 1:
        raise ValueError("Event inference requires one unambiguous detection input")
    source = inputs[0].model_copy(deep=True)
    source.roles = [CameraRoleEnum.detect]
    command = camera._get_ffmpeg_cmd(source)
    if command is None or "pipe:" not in command:
        raise ValueError("Event inference did not produce a raw-frame output")
    return command


class TimingCalibrationOptions(BaseModel):
    """One explicitly selected, bounded original-detector reference run."""

    model_config = ConfigDict(extra="forbid")
    camera: str = Field(min_length=1)
    job_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    duration_seconds: int = Field(default=900, ge=30, le=900)
    reference_mode: Literal["temporary_decoder", "existing_detector"] = (
        "temporary_decoder"
    )


class TimingCalibrationLease(BaseModel):
    """A monotonic lease cannot keep the original decoder enabled indefinitely."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    camera: str
    job_id: str
    started_monotonic: float
    expires_monotonic: float


class RecordingOnlyMode(BaseModel):
    """Image-scoped override that ordinary builds do not enable."""

    model_config = ConfigDict(extra="forbid")
    mode: Literal["recording_only"]
    cameras: set[str] = Field(min_length=1)
    all_enabled_cameras: bool = False
    timing_calibration: TimingCalibrationOptions | None = None


@lru_cache(maxsize=1)
def configuration() -> RecordingOnlyMode | None:
    """Fail explicitly on malformed activation, never default to a legacy decoder."""
    try:
        data = MODE_PATH.read_bytes()
    except FileNotFoundError:
        return None
    return RecordingOnlyMode.model_validate_json(data)


def recording_only(camera: str | None) -> bool:
    """Select only the cameras explicitly requested by the operator."""
    if plugin_recording_only(camera):
        return True
    settings = configuration()
    return (
        settings is not None
        and camera is not None
        and (settings.all_enabled_cameras or camera in settings.cameras)
        and not timing_calibration_active(camera)
    )


def timing_calibration_active(camera: str | None) -> bool:
    """Permit decoding only for the selected camera while its short lease is valid."""
    settings = configuration()
    options = settings.timing_calibration if settings is not None else None
    if options is None or camera != options.camera:
        return False
    try:
        lease = TimingCalibrationLease.model_validate_json(
            TIMING_CONTROL_PATH.read_bytes()
        )
    except FileNotFoundError:
        return False
    except (OSError, ValidationError):
        logger.exception(
            "Invalid temporary timing lease; original decoding remains disabled"
        )
        return False
    duration = lease.expires_monotonic - lease.started_monotonic
    valid = (
        lease.camera == camera
        and lease.job_id == options.job_id
        and 0 < duration <= options.duration_seconds
        and lease.started_monotonic <= time.monotonic() < lease.expires_monotonic
    )
    if not valid and duration > options.duration_seconds:
        logger.error("Temporary timing lease exceeds its hard limit; decoding disabled")
    return valid


def timing_calibration_options() -> TimingCalibrationOptions | None:
    """Read an explicit calibration scope without requiring recording-only operation."""
    settings = configuration()
    if settings is not None and settings.timing_calibration is not None:
        return settings.timing_calibration
    try:
        data = PASSIVE_TIMING_PATH.read_bytes()
    except FileNotFoundError:
        return None
    return TimingCalibrationOptions.model_validate_json(data)


def recording_only_commands(camera: CameraConfig) -> list[dict[str, Any]]:
    """Preserve original stream-copy recording outputs while removing detection outputs."""
    if not camera.enabled:
        return []
    if not recording_only(camera.name):
        return [entry for entry in camera.ffmpeg_cmds if "detect" not in entry["roles"]]
    if not camera.record.enabled or camera.record.continuous.days <= 0:
        raise ValueError("Recording-only mode requires existing continuous recording")
    if camera.record.sub.enabled and camera.record.sub.continuous.days <= 0:
        raise ValueError(
            "Recording-only substreams require existing continuous retention"
        )
    commands = []
    for original in camera.ffmpeg.inputs:
        item = original.model_copy(deep=True)
        item.roles = [role for role in item.roles if role != CameraRoleEnum.detect]
        command = camera._get_ffmpeg_cmd(item)
        if command is not None:
            if "pipe:" in command or "-hwaccel" in command:
                raise ValueError(
                    "Recording-only command must not decode detection frames"
                )
            commands.append({"roles": item.roles, "cmd": command})
    if not any("record" in entry["roles"] for entry in commands):
        raise ValueError("Recording-only mode has no main recording output")
    return commands


def recording_metadata_cutoff(camera: CameraConfig, frame_time: float) -> float:
    """Continuous copy recordings do not require nonexistent decoded-frame metadata."""
    if not recording_only(camera.name):
        return frame_time
    if camera.record.continuous.days <= 0:
        raise ValueError("Metadata-independent recording requires continuous retention")
    now = time.time()
    if not math.isfinite(now):
        raise ValueError("Invalid recording ingestion clock")
    return now

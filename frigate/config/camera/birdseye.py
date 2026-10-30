import logging
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from ..base import FrigateBaseModel

__all__ = [
    "BirdseyeCameraConfig",
    "BirdseyeConfig",
    "BirdseyeLayoutConfig",
    "BirdseyeModeEnum",
    "birdseye_modes_from_mqtt_payload",
    "birdseye_modes_to_mqtt_payload",
]

# canonical MQTT payload for an empty mode list
MQTT_NO_MODES = "NONE"
logger = logging.getLogger(__name__)


class BirdseyeModeEnum(str, Enum):
    continuous = "continuous"
    motion = "motion"
    all_objects = "all_objects"
    alerts = "alerts"
    detections = "detections"


def birdseye_modes_from_mqtt_payload(payload: str) -> list[BirdseyeModeEnum] | None:
    """Parse an uppercase MQTT payload into activity modes, or None when invalid."""
    raw_modes = payload.split(",")

    if any(not raw_mode or raw_mode != raw_mode.upper() for raw_mode in raw_modes):
        return None

    if raw_modes == [MQTT_NO_MODES]:
        return []

    modes: list[BirdseyeModeEnum] = []

    for raw_mode in raw_modes:
        try:
            mode = BirdseyeModeEnum(raw_mode.lower())
        except ValueError:
            return None

        if mode in modes:
            return None

        modes.append(mode)

    return modes


def birdseye_modes_to_mqtt_payload(modes: list[BirdseyeModeEnum]) -> str:
    """Serialize activity modes for MQTT state topics."""
    payload = ",".join(mode.value.upper() for mode in BirdseyeModeEnum if mode in modes)
    return payload or MQTT_NO_MODES


def default_birdseye_modes() -> list[BirdseyeModeEnum]:
    """Return the default Birdseye activity modes."""
    return [BirdseyeModeEnum.all_objects]


class BirdseyeLayoutConfig(FrigateBaseModel):
    scaling_factor: float = Field(
        default=2.0,
        title="Scaling factor",
        description="Scaling factor used by the layout calculator (range 1.0 to 5.0).",
        ge=1.0,
        le=5.0,
    )
    max_cameras: int | None = Field(
        default=None,
        title="Max cameras",
        description="Maximum number of cameras to display at once in Birdseye; shows the most recent cameras.",
    )


class BirdseyeConfig(FrigateBaseModel):
    @model_validator(mode="before")
    @classmethod
    def remove_retired_encoder_quality(cls, value: Any) -> Any:
        """Migrate the removed MPEG quality option to explicit HLS bitrate settings."""
        if isinstance(value, dict) and "quality" in value:
            logger.warning(
                "Ignoring retired Birdseye quality setting; HLS uses birdseye.bitrate"
            )
            return {key: item for key, item in value.items() if key != "quality"}
        return value

    enabled: bool = Field(
        default=True,
        title="Enable Birdseye",
        description="Enable or disable the Birdseye view feature.",
    )
    modes: list[BirdseyeModeEnum] = Field(
        default_factory=default_birdseye_modes,
        title="Activity types",
        description="Activity types that include cameras in Birdseye.",
    )

    restream: bool = Field(
        default=False,
        title="Restream RTSP",
        description="Re-stream the Birdseye output as an RTSP feed; enabling this will keep Birdseye running continuously.",
    )
    width: int = Field(
        default=1280,
        title="Width",
        description="Output width (pixels) of the composed Birdseye frame.",
    )
    height: int = Field(
        default=720,
        title="Height",
        description="Output height (pixels) of the composed Birdseye frame.",
    )
    bitrate: int = Field(
        default=2500,
        ge=64,
        title="HLS restream bitrate",
        description="Target and maximum video bitrate in kbps for the H264 Birdseye restream used by HLS viewing. This does not change camera recordings.",
    )
    inactivity_threshold: int = Field(
        default=30,
        title="Inactivity threshold",
        description="Seconds of inactivity after which a camera will stop being shown in Birdseye.",
        gt=0,
    )
    layout: BirdseyeLayoutConfig = Field(
        default_factory=BirdseyeLayoutConfig,
        title="Layout",
        description="Layout options for the Birdseye composition.",
    )
    idle_heartbeat_fps: float = Field(
        default=0.0,
        ge=0.0,
        le=10.0,
        title="Idle heartbeat FPS",
        description="Frames-per-second to resend the last composed Birdseye frame when idle; set to 0 to disable.",
    )


# uses BaseModel because some global attributes are not available at the camera level
class BirdseyeCameraConfig(BaseModel):
    enabled: bool = Field(
        default=True,
        title="Enable Birdseye",
        description="Enable or disable the Birdseye view feature.",
    )
    modes: list[BirdseyeModeEnum] = Field(
        default_factory=default_birdseye_modes,
        title="Activity types",
        description="Activity types that include cameras in Birdseye.",
    )

    order: int = Field(
        default=0,
        title="Position",
        description="Numeric position controlling the camera's ordering in the Birdseye layout.",
    )

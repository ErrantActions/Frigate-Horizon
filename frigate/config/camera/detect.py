from pydantic import Field, model_validator

from frigate.detectors.detector_config import DEFAULT_SCENE, SCENE_PATTERN

from ..base import FrigateBaseModel

__all__ = ["DetectConfig", "StationaryConfig", "StationaryMaxFramesConfig"]


class DynamicResolutionConfig(FrigateBaseModel):
    enabled: bool = Field(
        default=False,
        title="Automatic analysis resolution",
        description="Vary the object detector's analysis copy within the selected range when processing falls behind. Capture, recordings, motion analysis, masks and tracking coordinates stay unchanged. Lower resolutions can miss small objects. The AI model's input tensor size does not change.",
    )
    min_height: int = Field(
        default=360,
        ge=160,
        multiple_of=4,
        title="Minimum analysis height",
        description="Lowest permitted AI analysis image height in pixels. Width follows the camera's aspect ratio. Stay at or above the resolution needed to see distant objects.",
    )
    max_height: int | None = Field(
        default=None,
        ge=160,
        multiple_of=4,
        title="Maximum analysis height",
        description="Highest permitted AI analysis image height in pixels. Empty uses the configured capture height. Cannot exceed the capture resolution. Automatic mode starts here and gradually returns here when processing recovers.",
    )

    @model_validator(mode="after")
    def validate_range(self) -> "DynamicResolutionConfig":
        """Reject reversed analysis bounds."""
        if self.max_height is not None and self.min_height > self.max_height:
            raise ValueError("Minimum analysis height must not exceed the maximum")
        return self

    def validate_capture_height(self, height: int | None) -> None:
        """Reject enabled bounds that cannot fit the camera's capture image."""
        if self.enabled and height is None:
            raise ValueError("Automatic analysis requires a known capture height")
        if (
            self.enabled
            and height is not None
            and (
                self.min_height > height
                or (self.max_height is not None and self.max_height > height)
            )
        ):
            raise ValueError("Analysis resolution must not exceed the capture height")


class StationaryMaxFramesConfig(FrigateBaseModel):
    default: int | None = Field(
        default=None,
        title="Default max frames",
        description="Default maximum frames to track a stationary object before stopping.",
        ge=1,
    )
    objects: dict[str, int] = Field(
        default_factory=dict,
        title="Object max frames",
        description="Per-object overrides for maximum frames to track stationary objects.",
    )


class StationaryConfig(FrigateBaseModel):
    interval: int | None = Field(
        default=None,
        title="Stationary interval",
        description="How often (in frames) to run a detection check to confirm a stationary object.",
        gt=0,
    )
    threshold: int | None = Field(
        default=None,
        title="Stationary threshold",
        description="Number of frames with no position change required to mark an object as stationary.",
        ge=1,
    )
    max_frames: StationaryMaxFramesConfig = Field(
        default_factory=StationaryMaxFramesConfig,
        title="Max frames",
        description="Limits how long stationary objects are tracked before being discarded.",
    )
    classifier: bool = Field(
        default=True,
        title="Enable visual classifier",
        description="Use a visual classifier to detect truly stationary objects even when bounding boxes jitter.",
    )
    parked_vehicle_memory: bool = Field(
        default=False,
        title="Remember parked vehicles",
        description="Keep a stationary vehicle's track through brief detection losses while its location and appearance still match. Uncertain matches use normal detection. Memory is per-camera, in RAM, and disabled during autotracking.",
    )
    parked_vehicle_memory_seconds: int = Field(
        default=120,
        title="Parked vehicle memory duration",
        description="Maximum seconds to preserve a parked vehicle track without another detection. Appearance changes clear its memory sooner. Does not persist across camera or Frigate restarts.",
        ge=1,
        le=600,
    )


class DetectConfig(FrigateBaseModel):
    enabled: bool = Field(
        default=False,
        title="Enable object detection",
        description="Enable or disable object detection for all cameras; can be overridden per-camera.",
    )
    height: int | None = Field(
        default=None,
        title="Detect height",
        description="Height (pixels) of frames used for the detect stream; leave empty to use the native stream resolution.",
    )
    width: int | None = Field(
        default=None,
        title="Detect width",
        description="Width (pixels) of frames used for the detect stream; leave empty to use the native stream resolution.",
    )
    scene: str = Field(
        default=DEFAULT_SCENE,
        pattern=SCENE_PATTERN,
        title="Detect scene",
        description="The environment this camera looks at, used to pick which of the configured models runs on it. Cameras left on 'default' run the model configured with a scene of 'default'.",
    )
    fps: int = Field(
        default=5,
        title="Detect FPS",
        description="Desired frames per second to run detection on; lower values reduce CPU usage (recommended value is 5, only set higher - at most 10 - if tracking extremely fast moving objects).",
    )
    min_initialized: int | None = Field(
        default=None,
        title="Minimum initialization frames",
        description="Number of consecutive detection hits required before creating a tracked object. Increase to reduce false initializations. Default value is fps divided by 2.",
        ge=2,
    )
    max_disappeared: int | None = Field(
        default=None,
        title="Maximum disappeared frames",
        description="Number of frames without a detection before a tracked object is considered gone.",
    )
    stationary: StationaryConfig = Field(
        default_factory=StationaryConfig,
        title="Stationary objects config",
        description="Settings to detect and manage objects that remain stationary for a period of time.",
    )
    dynamic_resolution: DynamicResolutionConfig = Field(
        default_factory=DynamicResolutionConfig,
        title="Automatic analysis resolution",
        description="Live-adjustable bounds for a separate object-detection analysis image. Does not resize the capture stream or require a camera restart.",
    )
    annotation_offset: int = Field(
        default=0,
        title="Annotation offset",
        description="Milliseconds to shift detect annotations to better align timeline bounding boxes with recordings; can be positive or negative.",
    )

    @model_validator(mode="after")
    def validate_dimensions(self) -> "DetectConfig":
        if (self.width is None) != (self.height is None):
            raise ValueError(
                "detect -> both width and height must be specified together, or both omitted"
            )
        if self.height is not None:
            self.dynamic_resolution.validate_capture_height(self.height)
        if self.dynamic_resolution.enabled and self.fps < 1:
            raise ValueError("Automatic analysis requires a positive detect FPS")
        return self

from typing import Any, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_serializer, field_validator, model_validator

from ..base import FrigateBaseModel
from .mask import MotionMaskConfig

__all__ = ["MotionConfig"]


class AdaptiveMotionConfig(FrigateBaseModel):
    grid_height: int = Field(
        default=500,
        ge=4,
        le=500,
        title="Grid squares vertically",
        description="Number of grid squares from top to bottom, not pixels. Frigate reads the current camera frame dimensions, calculates square cell size and the horizontal count automatically, and scales motion analysis as needed. AI resolution is unchanged. Larger grids use more processing and memory; native source resolution limits real detail.",
    )
    short_edge: int = Field(
        default=500,
        ge=4,
        le=500,
        title="Grid cells on shorter edge",
        description="Number of cells along the shorter image edge. The other dimension follows the camera aspect ratio. Motion analysis scales automatically when enabled, without changing AI resolution. Larger grids increase processing and memory use; source-pixel precision is the upper limit.",
        json_schema_extra={"readOnly": True},
    )
    timezone: str = Field(
        default="UTC",
        title="Profile timezone",
        description="IANA timezone used for the 48 recurring half-hour profiles. Repeated daylight-saving windows share the same local-time profile; missing windows receive no observations.",
    )
    wind_band_mph: Literal[5, 10] = Field(
        default=10,
        title="Wind gust band width",
        description="Spacing of independently learned gust conditions at 10 mph and above. Gusts below 10 mph always use separate 0 to 5 and 5 to 10 mph bands. Outputs interpolate between supported neighboring conditions; PID history is not mixed. Existing 10-mph histories are preserved separately and relearned after this split.",
    )
    enabled: bool = Field(
        default=False,
        title="Adaptive motion",
        description="Enable independent per-cell motion learning. Does not change AI models or object confidence thresholds.",
    )
    mode: Literal["shadow", "active"] = Field(
        default="shadow",
        title="Adaptive mode",
        description="Shadow records recommendations without changing motion detection. Active applies bounded per-cell threshold adjustments.",
    )
    columns: int = Field(
        default=500,
        ge=4,
        le=8192,
        title="Grid columns",
        description="Computed grid width for the camera aspect ratio. Legacy configurations with rows/columns remain accepted; use the shorter-edge setting for new changes.",
        json_schema_extra={"readOnly": True},
    )
    rows: int = Field(
        default=500,
        ge=4,
        le=8192,
        title="Grid rows",
        description="Computed grid height for the camera aspect ratio. Compatible existing spatial estimates provide a conservative starting point when the grid changes.",
        json_schema_extra={"readOnly": True},
    )
    max_adjustment: float = Field(
        default=12,
        ge=0,
        le=40,
        title="Maximum local adjustment",
        description="Maximum increase above the configured motion threshold in an individual cell.",
    )
    learning_seconds: float = Field(
        default=3600,
        ge=300,
        le=86400,
        title="Learning time constant",
        description="Seconds of observed activity used to smooth each cell's activity, independently in each half-hour and wind condition.",
    )
    minimum_observation_seconds: float = Field(
        default=1800,
        ge=60,
        le=86400,
        title="Minimum observed history",
        description="Observed seconds required before a condition profile can increase suppression.",
    )
    minimum_episodes: int = Field(
        default=6,
        ge=2,
        le=1000,
        title="Minimum independent episodes",
        description="Independent recurring episodes required in a cell before suppression may increase.",
    )
    episode_gap_seconds: float = Field(
        default=60,
        ge=10,
        le=3600,
        title="Episode separation",
        description="Minimum observed separation between counted motion episodes in the same cell.",
    )
    target_activity: float = Field(
        default=0.1,
        ge=0.01,
        le=0.9,
        title="Target background activity",
        description="Target fraction of sampled observations containing unheld background motion.",
    )
    kp: float = Field(default=6, ge=0, le=100, title="Proportional gain")
    ki: float = Field(default=0.002, ge=0, le=0.1, title="Integral gain")
    kd: float = Field(default=0.5, ge=0, le=100, title="Derivative gain")
    decay_seconds: float = Field(
        default=86400,
        ge=300,
        le=2592000,
        title="Memory decay",
        description="Elapsed-time decay toward baseline, including when a saved half-hour profile is inactive.",
    )
    quiet_seconds: float = Field(
        default=300,
        ge=30,
        le=86400,
        title="Quiet recovery delay",
        description="Observed quiet seconds before local suppression decays faster. Missing frames and inactive periods do not count as observed quiet.",
    )
    quiet_decay_multiplier: float = Field(
        default=4, ge=1, le=20, title="Quiet decay multiplier"
    )
    max_change_per_minute: float = Field(
        default=0.25,
        gt=0,
        le=2,
        title="Maximum adjustment per minute",
        description="Limits increases in local suppression so transient pedestrians and passing vehicles cannot rapidly change settings.",
    )
    object_hold_seconds: float = Field(
        default=120,
        ge=10,
        le=3600,
        title="Useful detection hold",
        description="Hold adaptation in cells occupied by reliable tracked objects.",
    )
    probe_interval_seconds: float = Field(
        default=300, ge=30, le=3600, title="Baseline retest interval"
    )
    probe_seconds: float = Field(
        default=5, ge=1, le=20, title="Baseline retest duration"
    )
    direction_sectors: int = Field(
        default=16,
        ge=4,
        le=32,
        title="Wind direction sectors",
        description="Independent meteorological wind-direction sectors within each half-hour and 5 mph gust band.",
    )
    max_profiles: int = Field(
        default=256,
        ge=48,
        le=2048,
        title="Maximum saved condition profiles",
        description="Per-camera storage bound. At the limit, an unseen condition stays at baseline and reports the limit instead of deleting learned history.",
    )
    max_state_mib: int = Field(
        default=512,
        ge=32,
        le=4096,
        title="Maximum learned state size",
        description="Per-camera persistent storage limit, including preserved grid/view histories. A full store stops adaptive suppression and reports an error; histories are not silently discarded.",
    )

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        """Require an actual timezone before selecting recurring profiles."""
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise ValueError("Invalid adaptive profile timezone") from None
        return value

    @model_validator(mode="after")
    def valid_probe_interval(self) -> Self:
        """A baseline probe must leave an interval for normal operation."""
        if self.probe_seconds >= self.probe_interval_seconds:
            raise ValueError("Baseline probe duration must be less than its interval")
        return self

    def resolve_grid(self, frame_shape: tuple[int, int]) -> None:
        """Fit square spatial cells to the shorter native edge without upsampling evidence."""
        height, width = frame_shape
        if height < 4 or width < 4:
            raise ValueError(
                "Adaptive motion requires at least four native pixels on each edge"
            )
        if "grid_height" in self.model_fields_set or not (
            {"short_edge", "columns", "rows"} & self.model_fields_set
        ):
            requested_height = min(self.grid_height, height)
            rows = requested_height
            columns = max(4, min(width, round(rows * width / height)))
            if rows * columns > 2_000_000 or max(rows, columns) > 8192:
                raise ValueError(
                    "Adaptive grid exceeds its memory bound; reduce grid squares vertically"
                )
            self.model_fields_set.add("grid_height")
            self.rows = rows
            self.columns = columns
            return
        requested = self.short_edge
        if "short_edge" not in self.model_fields_set and (
            {"columns", "rows"} & self.model_fields_set
        ):
            requested = min(self.columns, self.rows, 500)
        edge = min(requested, height, width)
        rows = max(1, round(edge * height / min(height, width)))
        columns = max(1, round(edge * width / min(height, width)))
        if max(rows, columns) > 8192 or rows * columns > 2_000_000:
            raise ValueError(
                "Adaptive grid exceeds its memory bound; reduce the shorter-edge setting"
            )
        self.short_edge = requested
        self.rows = rows
        self.columns = columns


def motion_analysis_shape(
    config: "MotionConfig", frame_shape: tuple[int, int]
) -> tuple[int, int]:
    """Keep legacy analysis unchanged unless adaptive motion needs more real pixels."""
    height, width = frame_shape
    analysis_height = config.frame_height or height
    analysis_width = analysis_height * width // height
    if config.adaptive.enabled:
        config.adaptive.resolve_grid(frame_shape)
        analysis_height = min(height, max(analysis_height, config.adaptive.rows))
        analysis_width = min(
            width,
            max(
                round(analysis_height * width / height),
                config.adaptive.columns,
            ),
        )
    return analysis_height, analysis_width


class MotionConfig(FrigateBaseModel):
    backend: Literal["pixel", "compressed"] = Field(
        default="pixel",
        title="Motion detection system",
        description="Choose pixel motion (the existing default) or experimental compressed-stream motion. Compressed motion requires a build containing the native H264 extractor, native 4K capture, and no intermediate dynamic analysis copy. Switching systems requires a restart. Pixel and compressed learning histories are separate.",
    )
    compressed_fallback: bool = Field(
        default=False,
        title="Full-decode fallback for compressed motion",
        description="Opt in to full native 4K decoding if the experimental compressed-stream extractor fails. Disabled by default: report the failure and restart the compressed worker instead of silently changing systems. Independent recording continues. Does not resize video to 1080p or enable this fallback for the pixel backend. Changing this setting requires a restart.",
    )
    compressed_min_distance: float = Field(
        default=0.5,
        ge=0,
        le=4096,
        allow_inf_nan=False,
        title="Minimum compressed motion distance",
        description="Minimum H264 motion-vector displacement in native image pixels per coded reference. Smaller vectors are excluded from vector activity. This is not physical distance from the camera or object size. Coded-block changes use their own activity threshold. Changing this setting requires a restart.",
    )
    compressed_max_distance: float | None = Field(
        default=None,
        gt=0,
        le=16384,
        allow_inf_nan=False,
        title="Maximum expected compressed motion distance",
        description="Optional upper end of the expected vector-displacement range, in native image pixels per coded reference. Larger vectors are uncertain and wake decoding instead of being ignored. Leave unset for no upper bound. Must exceed the minimum distance. Changing this setting requires a restart.",
    )
    compressed_min_activity: float = Field(
        default=0,
        ge=0,
        le=1,
        allow_inf_nan=False,
        title="Minimum compressed cell activity",
        description="Minimum fraction of a compressed grid cell that must change before requesting decoding (0 to 1). Applies to both accepted vector activity and coded-block changes, separately from displacement distance. Higher values can ignore small or distant objects. Zero preserves the conservative any-change baseline. Changing this setting requires a restart.",
    )

    @model_validator(mode="after")
    def validate_compressed_distance(self) -> Self:
        """Reject reversed distance ranges rather than silently disabling motion evidence."""
        if (
            self.compressed_max_distance is not None
            and self.compressed_max_distance <= self.compressed_min_distance
        ):
            raise ValueError(
                "Compressed maximum motion distance must exceed the minimum"
            )
        return self

    adaptive: AdaptiveMotionConfig = Field(
        default_factory=AdaptiveMotionConfig,
        title="Adaptive motion grid",
        description="Opt-in local motion sensitivity profiles. AI detection remains unchanged.",
    )
    enabled: bool = Field(
        default=True,
        title="Enable motion detection",
        description="Enable or disable motion detection for all cameras; can be overridden per-camera.",
    )
    threshold: int = Field(
        default=30,
        title="Motion threshold",
        description="Pixel difference threshold used by the motion detector; higher values reduce sensitivity (range 1-255).",
        ge=1,
        le=255,
    )
    lightning_threshold: float = Field(
        default=0.8,
        title="Lightning threshold",
        description="Threshold to detect and ignore brief lighting spikes (lower is more sensitive, values between 0.3 and 1.0). This does not prevent motion detection entirely; it merely causes the detector to stop analyzing additional frames once the threshold is exceeded. Motion-based recordings are still created during these events.",
        ge=0.3,
        le=1.0,
    )
    skip_motion_threshold: float | None = Field(
        default=None,
        title="Skip motion threshold",
        description="If set to a value between 0.0 and 1.0, and more than this fraction of the image changes in a single frame, the detector will return no motion boxes and immediately recalibrate. This can save CPU and reduce false positives during lightning, storms, etc., but may miss real events such as a PTZ camera auto‑tracking an object. The trade‑off is between dropping a few megabytes of recordings versus reviewing a couple short clips. Leave unset (None) to disable this feature.",
        ge=0.0,
        le=1.0,
    )
    improve_contrast: bool = Field(
        default=True,
        title="Improve contrast",
        description="Apply contrast improvement to frames before motion analysis to help detection.",
    )
    contour_area: int | None = Field(
        default=10,
        title="Contour area",
        description="Minimum contour area in pixels required for a motion contour to be counted.",
    )
    delta_alpha: float = Field(
        default=0.2,
        title="Delta alpha",
        description="Alpha blending factor used in frame differencing for motion calculation.",
    )
    frame_alpha: float = Field(
        default=0.01,
        title="Frame alpha",
        description="Alpha value used when blending frames for motion preprocessing.",
    )
    frame_height: int | None = Field(
        default=100,
        title="Frame height",
        description="Height in pixels to scale frames to when computing motion.",
    )
    mask: dict[str, MotionMaskConfig | None] = Field(
        default_factory=dict,
        title="Mask coordinates",
        description="Ordered x,y coordinates defining the motion mask polygon used to include/exclude areas.",
    )
    mqtt_off_delay: int = Field(
        default=30,
        title="MQTT off delay",
        description="Seconds to wait after last motion before publishing an MQTT 'off' state.",
    )
    enabled_in_config: bool | None = Field(
        default=None,
        title="Original motion state",
        description="Indicates whether motion detection was enabled in the original static configuration.",
    )
    raw_mask: dict[str, MotionMaskConfig | None] = Field(
        default_factory=dict, exclude=True
    )

    @field_serializer("mask", when_used="json")
    def serialize_mask(self, value: Any, info):
        if self.raw_mask:
            return self.raw_mask
        return value

    @field_serializer("raw_mask", when_used="json")
    def serialize_raw_mask(self, value: Any, info):
        return None

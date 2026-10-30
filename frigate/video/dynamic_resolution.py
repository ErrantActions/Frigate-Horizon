"""Bounded AI analysis rasters with canonical capture and tracking coordinates."""

import math
from dataclasses import dataclass

import cv2
import numpy as np
from numpy.typing import NDArray

from frigate.config.camera.detect import DynamicResolutionConfig


@dataclass(frozen=True)
class AnalysisSize:
    width: int
    height: int
    source_width: int
    source_height: int

    @property
    def native(self) -> bool:
        """Whether analysis can use the original capture buffer unchanged."""
        return (self.width, self.height) == (self.source_width, self.source_height)

    def region_minimum(self, model_minimum: int) -> int:
        """Keep a model-sized minimum region in the analysis coordinate space."""
        scale = max(self.source_width / self.width, self.source_height / self.height)
        return math.ceil(model_minimum * scale / 4) * 4

    def project_region(
        self, region: tuple[int, int, int, int] | list[int]
    ) -> tuple[tuple[int, int, int, int], tuple[float, float, float, float]]:
        """Return an aligned analysis crop and its exact source-space extent."""
        sx, sy = self.width / self.source_width, self.height / self.source_height
        x, y = math.floor(region[0] * sx / 4) * 4, math.floor(region[1] * sy / 4) * 4
        side = max(
            4,
            math.ceil(max(region[2] * sx - x, region[3] * sy - y) / 4) * 4,
        )
        crop = (x, y, x + side, y + side)
        return crop, (x / sx, y / sy, (x + side) / sx, (y + side) / sy)


def resize_analysis_frame(
    frame: NDArray[np.uint8], size: AnalysisSize
) -> NDArray[np.uint8]:
    """Resize I420 planes without changing or converting the shared source frame."""
    if frame.shape != (size.source_height * 3 // 2, size.source_width):
        raise ValueError("Analysis input does not match its capture geometry")
    if size.native:
        return frame
    output = np.empty((size.height * 3 // 2, size.width), dtype=np.uint8)
    source, target = frame.reshape(-1), output.reshape(-1)
    source_area, target_area = (
        size.source_width * size.source_height,
        size.width * size.height,
    )
    planes = (
        (0, source_area, 0, target_area, 1),
        (source_area, source_area // 4, target_area, target_area // 4, 2),
        (
            source_area * 5 // 4,
            source_area // 4,
            target_area * 5 // 4,
            target_area // 4,
            2,
        ),
    )
    for start, length, offset, count, divisor in planes:
        plane = source[start : start + length].reshape(
            size.source_height // divisor, size.source_width // divisor
        )
        target[offset : offset + count] = cv2.resize(
            plane,
            (size.width // divisor, size.height // divisor),
            interpolation=cv2.INTER_AREA,
        ).reshape(-1)
    return output


class DynamicResolutionController:
    """Prefer maximum detail, backing off only under sustained processing pressure."""

    def __init__(
        self,
        frame_shape: tuple[int, int],
        config: DynamicResolutionConfig,
        fps: int,
        now: float,
    ) -> None:
        self.source_height, self.source_width = frame_shape
        self.config = config.model_copy(deep=True)
        self.fps = fps
        config.validate_capture_height(self.source_height)
        maximum = (config.max_height or self.source_height) // 4 * 4
        self.levels = (
            sorted(
                {config.min_height, maximum}
                | {
                    height
                    for height in (360, 480, 720, 1080, 1440, 2160)
                    if config.min_height <= height <= maximum
                }
            )
            if config.enabled
            else [self.source_height]
        )
        self.index = len(self.levels) - 1
        self.started_at = self.changed_at = now
        self.overloaded_since: float | None = None
        self.healthy_since: float | None = None
        self.load: float | None = None
        self.last_observed_at: float | None = None
        self.skipped_fraction = 0.0
        self.reason = "warming_up" if config.enabled else "disabled"

    def matches(self, config: DynamicResolutionConfig, fps: int) -> bool:
        """Whether the camera worker has applied the currently saved bounds."""
        return self.config == config and self.fps == fps

    def observe(self, work_seconds: float, skipped_fraction: float, now: float) -> None:
        """Record measured processing work, excluding capture queue waits."""
        if not math.isfinite(work_seconds) or work_seconds < 0:
            raise ValueError(
                "Analysis processing duration must be finite and nonnegative"
            )
        if not math.isfinite(skipped_fraction) or not 0 <= skipped_fraction <= 1:
            raise ValueError("Skipped-frame fraction must be between zero and one")
        load = work_seconds * self.fps
        self.load = load if self.load is None else self.load * 0.9 + load * 0.1
        self.skipped_fraction = skipped_fraction
        self.last_observed_at = now

    def select(self, now: float) -> AnalysisSize:
        """Select this frame's analysis raster with startup grace and hysteresis."""
        if self.last_observed_at is not None and now - self.last_observed_at > 3:
            self.overloaded_since = self.healthy_since = None
            self.load = None
        if (
            self.config.enabled
            and now - self.started_at >= 30
            and self.load is not None
        ):
            overloaded = self.load > 0.9 or self.skipped_fraction >= 0.05
            healthy = self.load < 0.6 and self.skipped_fraction == 0
            if overloaded:
                self.healthy_since = None
                if self.overloaded_since is None:
                    self.overloaded_since = now
                if now - self.overloaded_since >= 5 and now - self.changed_at >= 10:
                    if self.index > 0:
                        self.index -= 1
                        self.changed_at = now
                        self.overloaded_since = now
                    self.reason = "minimum" if self.index == 0 else "overloaded"
            elif healthy:
                self.overloaded_since = None
                if self.healthy_since is None:
                    self.healthy_since = now
                if now - self.healthy_since >= 60:
                    if self.index < len(self.levels) - 1:
                        self.index += 1
                        self.changed_at = now
                        self.healthy_since = now
                    self.reason = (
                        "maximum"
                        if self.index == len(self.levels) - 1
                        else "recovering"
                    )
            else:
                self.overloaded_since = self.healthy_since = None
            if self.reason == "warming_up":
                self.reason = "maximum"
        height = self.levels[self.index]
        width = (
            self.source_width
            if height == self.source_height
            else max(4, int(self.source_width * height / self.source_height) // 4 * 4)
        )
        return AnalysisSize(width, height, self.source_width, self.source_height)

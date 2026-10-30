"""Adaptive localized-glare analysis, independent of camera/device transport."""

from collections.abc import Mapping
from math import exp, isfinite
from typing import Protocol

import numpy as np
from numpy.typing import NDArray


class GlareSettings(Protocol):
    enabled: bool
    brightness_threshold: int
    minimum_bright_fraction: float
    minimum_fraction_increase: float
    minimum_luminance_increase: int
    maximum_frame_gap_seconds: float
    baseline_seconds: float
    cooldown_seconds: float
    global_change_fraction: float


class GlareDetector:
    """Compare zone changes against both an adaptive baseline and the preceding frame."""

    def __init__(
        self, settings: GlareSettings, masks: Mapping[str, NDArray[np.bool_]]
    ) -> None:
        if not masks:
            raise ValueError("Glare analysis requires explicit zone masks")
        self.settings = settings
        self.masks: dict[str, NDArray[np.bool_]] = {}
        self.counts: dict[str, int] = {}
        self.shape: tuple[int, int] | None = None
        for name, mask in masks.items():
            if mask.ndim != 2 or mask.dtype != np.bool_:
                raise ValueError("Glare masks must be two-dimensional boolean arrays")
            shape = (mask.shape[0], mask.shape[1])
            if self.shape is not None and self.shape != shape:
                raise ValueError("Glare masks must have matching dimensions")
            count = int(np.count_nonzero(mask))
            if count == 0:
                raise ValueError("Glare zones must contain analysis pixels")
            self.shape = shape
            self.masks[name] = mask.copy()
            self.counts[name] = count
        self._previous: NDArray[np.uint8] | None = None
        self._previous_fractions: dict[str, float] = {}
        self._baselines: dict[str, float] = {}
        self._last_time: float | None = None
        self._cooldown_until = 0.0

    @property
    def ready(self) -> bool:
        """Report whether at least one valid baseline frame has been observed."""
        return self._previous is not None

    def update(
        self,
        luma: NDArray[np.uint8],
        monotonic_now: float,
        *,
        lighting_transition: bool = False,
    ) -> tuple[str, ...]:
        """Return newly triggered zones, not a vehicle/headlight classification."""
        if luma.ndim != 2 or luma.dtype != np.uint8 or luma.shape != self.shape:
            raise ValueError(
                "Glare input must match the mask dimensions and uint8 luma"
            )
        if not isfinite(monotonic_now) or monotonic_now < 0:
            raise ValueError("Invalid glare sample clock")
        if self._last_time is not None and monotonic_now < self._last_time:
            raise ValueError("Glare sample clock must not move backward")
        if self._last_time == monotonic_now:
            return ()
        bright = luma >= self.settings.brightness_threshold
        fractions = {
            name: float(np.count_nonzero(bright & mask)) / self.counts[name]
            for name, mask in self.masks.items()
        }
        if (
            self._previous is None
            or self._last_time is None
            or monotonic_now - self._last_time > self.settings.maximum_frame_gap_seconds
        ):
            self._baselines = dict(fractions)
            self._previous_fractions = dict(fractions)
            self._previous = luma.copy()
            self._last_time = monotonic_now
            return ()
        difference = luma.astype(np.int16) - self._previous.astype(np.int16)
        global_fraction = (
            float(
                np.count_nonzero(
                    np.abs(difference) >= self.settings.minimum_luminance_increase
                )
            )
            / luma.size
        )
        excluded = (
            lighting_transition
            or global_fraction >= self.settings.global_change_fraction
        )
        newly_bright = bright & (self._previous < self.settings.brightness_threshold)
        alpha = 1 - exp(
            -(monotonic_now - self._last_time) / self.settings.baseline_seconds
        )
        zones: list[str] = []
        for name, mask in self.masks.items():
            fraction = fractions[name]
            changed = newly_bright & mask
            if (
                self.settings.enabled
                and not excluded
                and monotonic_now >= self._cooldown_until
                and fraction >= self.settings.minimum_bright_fraction
                and fraction - self._baselines[name]
                >= self.settings.minimum_fraction_increase
                and fraction - self._previous_fractions[name]
                >= self.settings.minimum_fraction_increase
                and np.any(changed)
                and float(difference[changed].mean())
                >= self.settings.minimum_luminance_increase
            ):
                zones.append(name)
            self._baselines[name] = (
                fraction
                if excluded
                else self._baselines[name] + alpha * (fraction - self._baselines[name])
            )
        self._previous_fractions = fractions
        self._previous = luma.copy()
        self._last_time = monotonic_now
        if zones:
            self._cooldown_until = monotonic_now + self.settings.cooldown_seconds
        return tuple(zones)

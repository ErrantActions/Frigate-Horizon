from abc import ABC, abstractmethod

from numpy import ndarray

from frigate.config import MotionConfig


class MotionDetector(ABC):
    def view_verified(self) -> bool:
        """Report only independently verified pixel-view alignment."""
        return False

    def set_tracked_boxes(
        self, boxes: list[tuple[int, int, int, int]], *, detection_enabled: bool = True
    ) -> None:
        """Accept useful-object evidence when a motion implementation supports it."""

    @abstractmethod
    def __init__(
        self,
        frame_shape: tuple[int, int, int],
        config: MotionConfig,
        fps: int,
        improve_contrast: bool,
        threshold: int,
        contour_area: int | None,
    ) -> None:
        pass

    @abstractmethod
    def detect(self, frame: ndarray) -> list:
        """Detect motion and return motion boxes."""
        pass

    @abstractmethod
    def is_calibrating(self) -> bool:
        """Return if motion is recalibrating."""
        pass

    @abstractmethod
    def update_mask(self) -> None:
        """Update the motion mask after a config change."""
        pass

    @abstractmethod
    def stop(self) -> None:
        """Stop any ongoing work and processes."""
        pass

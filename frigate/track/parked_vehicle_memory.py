"""Bounded appearance memory for stationary vehicle track continuity."""

import logging
from collections.abc import Sequence
from dataclasses import dataclass

import cv2
import numpy as np

from frigate.util.image import get_histogram, intersection_over_union

logger = logging.getLogger(__name__)

VEHICLE_LABELS = frozenset(("bicycle", "boat", "car", "motorcycle", "tractor", "truck"))


@dataclass
class ParkedVehicleReference:
    """A stable vehicle appearance associated with an existing track."""

    track_id: str
    label: str
    box: tuple[int, int, int, int]
    crop: np.ndarray
    histogram: np.ndarray
    last_seen: float


class ParkedVehicleMemory:
    """Keep only unambiguous, continuously visible parked-vehicle matches."""

    MAX_OBJECTS = 32
    CROP_SIZE = 64
    MINIMUM_IOU = 0.7
    MINIMUM_CORRELATION = 0.95
    MINIMUM_COLOR_CORRELATION = 0.9

    def __init__(self) -> None:
        self.references: dict[str, ParkedVehicleReference] = {}
        self.frame_shape: tuple[int, ...] | None = None

    def clear(self) -> None:
        """Forget all references, including their frame geometry."""
        self.references.clear()
        self.frame_shape = None

    @staticmethod
    def _crop(frame: np.ndarray, box: tuple[int, int, int, int]) -> np.ndarray | None:
        height = frame.shape[0] * 2 // 3
        width = frame.shape[1]
        x1, y1, x2, y2 = box
        if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
            return None
        if x2 - x1 < 16 or y2 - y1 < 16:
            return None
        crop = cv2.resize(
            frame[y1:y2, x1:x2],
            (ParkedVehicleMemory.CROP_SIZE, ParkedVehicleMemory.CROP_SIZE),
            interpolation=cv2.INTER_AREA,
        )
        # Uniform patches cannot establish a reliable appearance match.
        if float(np.std(crop)) < 8:
            return None
        return crop

    @staticmethod
    def _histogram(
        frame: np.ndarray, box: tuple[int, int, int, int], crop: np.ndarray
    ) -> np.ndarray:
        height = frame.shape[0] * 2 // 3
        width = frame.shape[1]
        y_size = height * width
        uv_size = y_size // 4
        flat = frame.reshape(-1)
        x1, y1, x2, y2 = box
        planes = [crop.ravel()]
        for offset in (y_size, y_size + uv_size):
            plane = flat[offset : offset + uv_size].reshape(height // 2, width // 2)
            planes.append(
                cv2.resize(
                    plane[y1 // 2 : y2 // 2, x1 // 2 : x2 // 2],
                    (ParkedVehicleMemory.CROP_SIZE // 2,) * 2,
                    interpolation=cv2.INTER_AREA,
                ).ravel()
            )
        small_frame = np.concatenate(planes).reshape(
            ParkedVehicleMemory.CROP_SIZE * 3 // 2,
            ParkedVehicleMemory.CROP_SIZE,
        )
        return get_histogram(
            small_frame,
            0,
            0,
            ParkedVehicleMemory.CROP_SIZE,
            ParkedVehicleMemory.CROP_SIZE,
        )

    def validate(
        self, frame: np.ndarray, frame_time: float, maximum_age: float
    ) -> None:
        """Expire references when time, geometry, or appearance no longer match."""
        if self.frame_shape != frame.shape:
            self.clear()
            self.frame_shape = frame.shape

        for id, reference in list(self.references.items()):
            age = frame_time - reference.last_seen
            if age < 0 or age > maximum_age:
                del self.references[id]
                continue
            crop = self._crop(frame, reference.box)
            if crop is None:
                del self.references[id]
                continue
            correlation = float(
                cv2.matchTemplate(crop, reference.crop, cv2.TM_CCOEFF_NORMED)[0, 0]
            )
            color_correlation = cv2.compareHist(
                self._histogram(frame, reference.box, crop),
                reference.histogram,
                cv2.HISTCMP_CORREL,
            )
            if (
                not np.isfinite(correlation)
                or not np.isfinite(color_correlation)
                or correlation < self.MINIMUM_CORRELATION
                or color_correlation < self.MINIMUM_COLOR_CORRELATION
            ):
                logger.debug("Forgetting changed parked vehicle %s", id)
                del self.references[id]

    def remember(
        self,
        id: str,
        track_id: str,
        label: str,
        box: tuple[int, int, int, int],
        frame: np.ndarray,
        frame_time: float,
    ) -> None:
        """Remember a confirmed stationary vehicle without replacing its anchor."""
        if label not in VEHICLE_LABELS:
            return
        reference = self.references.get(id)
        if reference is not None:
            reference.last_seen = frame_time
            reference.track_id = track_id
            return
        if len(self.references) >= self.MAX_OBJECTS:
            logger.debug("Not remembering parked vehicle %s: memory limit reached", id)
            return
        crop = self._crop(frame, box)
        if crop is None:
            logger.debug("Not remembering parked vehicle %s: unreliable crop", id)
            return
        self.references[id] = ParkedVehicleReference(
            track_id=track_id,
            label=label,
            box=box,
            crop=crop,
            histogram=self._histogram(frame, box, crop),
            last_seen=frame_time,
        )

    def match(
        self,
        label: str,
        box: tuple[int, int, int, int],
        detected_track_ids: set[str],
        candidate_boxes: Sequence[tuple[str, tuple[int, int, int, int]]],
    ) -> str | None:
        """Return an existing ID only when exactly one inactive reference matches."""
        matches = [
            id
            for id, reference in self.references.items()
            if reference.label == label
            and reference.track_id not in detected_track_ids
            and intersection_over_union(reference.box, box) >= self.MINIMUM_IOU
        ]
        if len(matches) != 1:
            return None
        reference = self.references[matches[0]]
        overlapping_candidates = sum(
            candidate_label == label
            and intersection_over_union(reference.box, candidate_box)
            >= self.MINIMUM_IOU
            for candidate_label, candidate_box in candidate_boxes
        )
        return matches[0] if overlapping_candidates == 1 else None

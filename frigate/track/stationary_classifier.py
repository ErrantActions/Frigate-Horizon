"""Tools for determining if an object is stationary."""

import logging
from dataclasses import dataclass, field
from typing import Any, cast

import cv2
import numpy as np
from scipy.ndimage import gaussian_filter

logger = logging.getLogger(__name__)


@dataclass
class StationaryThresholds:
    """IOU thresholds and history parameters for stationary object classification.

    This allows different sensitivity settings for different object types.
    """

    # Objects to apply these thresholds to
    # If None, apply to all objects
    objects: list[str] = field(default_factory=list)

    # Threshold of IoU that causes the object to immediately be considered active
    # Below this threshold, assume object is active
    known_active_iou: float = 0.2

    # IOU threshold for checking if stationary object has moved
    # If mean and median IOU drops below this, assume object is no longer stationary
    stationary_check_iou: float = 0.6

    # IOU threshold for checking if active object has changed position
    # Higher threshold makes it more difficult for the object to be considered stationary
    active_check_iou: float = 0.9

    # Maximum number of bounding boxes to keep in stationary history
    max_stationary_history: int = 10

    # Whether to use the motion classifier
    motion_classifier_enabled: bool = False


# Thresholds for objects that are expected to be stationary
STATIONARY_OBJECT_THRESHOLDS = StationaryThresholds(
    objects=["bbq_grill", "package", "waste_bin"],
    known_active_iou=0.0,
    motion_classifier_enabled=True,
)

# Thresholds for objects that are active but can be stationary for longer periods of time
DYNAMIC_OBJECT_THRESHOLDS = StationaryThresholds(
    objects=["bicycle", "boat", "car", "motorcycle", "tractor", "truck"],
    active_check_iou=0.75,
    motion_classifier_enabled=True,
)

# Thresholds for objects that are not expected to be stationary
NON_STATIONARY_OBJECT_THRESHOLDS = StationaryThresholds(
    objects=["license_plate"],
    known_active_iou=0.9,
    stationary_check_iou=0.9,
    max_stationary_history=4,
)

# Default thresholds for any other object label
DEFAULT_OBJECT_THRESHOLDS = StationaryThresholds()


def get_stationary_threshold(label: str) -> StationaryThresholds:
    """Get the stationary thresholds for a given object label."""

    if label in STATIONARY_OBJECT_THRESHOLDS.objects:
        return STATIONARY_OBJECT_THRESHOLDS

    if label in DYNAMIC_OBJECT_THRESHOLDS.objects:
        return DYNAMIC_OBJECT_THRESHOLDS

    if label in NON_STATIONARY_OBJECT_THRESHOLDS.objects:
        return NON_STATIONARY_OBJECT_THRESHOLDS

    return DEFAULT_OBJECT_THRESHOLDS


class StationaryMotionClassifier:
    """Fallback classifier to prevent false flips from stationary to active.

    Uses appearance consistency over matching spatial regions in a bounded
    scene reference to distinguish actual movement from box variations.
    """

    CROP_SIZE = 96
    NCC_KEEP_THRESHOLD = 0.90  # High correlation = keep stationary
    NCC_ACTIVE_THRESHOLD = 0.85  # Low correlation = consider active
    SHIFT_KEEP_THRESHOLD = 0.02  # Small shift = keep stationary
    SHIFT_ACTIVE_THRESHOLD = 0.04  # Large shift = consider active
    CHANGED_FRAMES_TO_FLIP = 2
    MAX_REFERENCES = 32
    MAX_FRAME_EDGE = 640

    def __init__(self) -> None:
        self.anchor_crops: dict[str, np.ndarray] = {}
        self.anchor_boxes: dict[str, tuple[int, int, int, int]] = {}
        self.anchor_frames: dict[str, np.ndarray] = {}
        self.anchor_frame_shapes: dict[str, tuple[int, int]] = {}
        self.changed_counts: dict[str, int] = {}
        self.capacity_warning = False

        # Pre-compute Hanning window for phase correlation
        hann = np.hanning(self.CROP_SIZE).astype(np.float64)
        self._hann2d = np.outer(hann, hann)

    def reset(self, id: str) -> None:
        logger.debug("StationaryMotionClassifier.reset: id=%s", id)
        if id in self.anchor_crops:
            del self.anchor_crops[id]
        if id in self.anchor_boxes:
            del self.anchor_boxes[id]
        self.anchor_frames.pop(id, None)
        self.anchor_frame_shapes.pop(id, None)
        self.changed_counts.pop(id, None)
        if len(self.anchor_frames) < self.MAX_REFERENCES:
            self.capacity_warning = False

    @classmethod
    def _reference_frame(cls, yuv_frame: np.ndarray) -> np.ndarray:
        height = yuv_frame.shape[0] // 3 * 2
        width = yuv_frame.shape[1]
        scale = min(1.0, cls.MAX_FRAME_EDGE / max(height, width))
        resized_height = max(2, round(height * scale / 2) * 2)
        resized_width = max(2, round(width * scale / 2) * 2)
        reference = np.full(
            (resized_height * 3 // 2, resized_width), 128, dtype=np.uint8
        )
        reference[:resized_height] = cv2.resize(
            yuv_frame[:height],
            (resized_width, resized_height),
            interpolation=cv2.INTER_AREA,
        )
        return reference

    def _extract_y_crop(
        self, yuv_frame: np.ndarray, box: tuple[int, int, int, int]
    ) -> np.ndarray:
        """Extract and normalize Y-plane crop from bounding box."""
        y_height = yuv_frame.shape[0] // 3 * 2
        width = yuv_frame.shape[1]
        x1 = max(0, min(width - 1, box[0]))
        y1 = max(0, min(y_height - 1, box[1]))
        x2 = max(0, min(width - 1, box[2]))
        y2 = max(0, min(y_height - 1, box[3]))

        if x2 <= x1:
            x2 = min(width - 1, x1 + 1)
        if y2 <= y1:
            y2 = min(y_height - 1, y1 + 1)

        # Extract Y-plane crop, resize, and blur
        y_plane = yuv_frame[0:y_height, 0:width]
        crop = y_plane[y1:y2, x1:x2]
        crop_resized = cv2.resize(
            crop, (self.CROP_SIZE, self.CROP_SIZE), interpolation=cv2.INTER_AREA
        )
        result = cast(np.ndarray[Any, Any], gaussian_filter(crop_resized, sigma=0.5))
        logger.debug(
            "_extract_y_crop: box=%s clamped=(%d,%d,%d,%d) crop_shape=%s",
            box,
            x1,
            y1,
            x2,
            y2,
            crop.shape if "crop" in locals() else None,
        )
        return result

    def ensure_anchor(
        self, id: str, yuv_frame: np.ndarray, median_box: tuple[int, int, int, int]
    ) -> None:
        """Remember a bounded scene reference for a newly tracked or stationary object."""
        if id not in self.anchor_crops:
            if len(self.anchor_frames) >= self.MAX_REFERENCES:
                if not self.capacity_warning:
                    logger.warning(
                        "Stationary appearance reference limit reached; using geometric tracking"
                    )
                    self.capacity_warning = True
                return
            self.anchor_boxes[id] = median_box
            self.anchor_crops[id] = self._extract_y_crop(yuv_frame, median_box)
            self.anchor_frames[id] = self._reference_frame(yuv_frame)
            self.anchor_frame_shapes[id] = (
                yuv_frame.shape[0] // 3 * 2,
                yuv_frame.shape[1],
            )
            self.changed_counts[id] = 0
            logger.debug(
                "ensure_anchor: initialized id=%s median_box=%s crop_shape=%s",
                id,
                median_box,
                self.anchor_crops[id].shape,
            )

    def on_active(self, id: str) -> None:
        """Reset state when object becomes active to allow re-anchoring."""
        logger.debug("on_active: id=%s became active; resetting state", id)
        self.reset(id)

    def evaluate(
        self, id: str, yuv_frame: np.ndarray, current_box: tuple[int, int, int, int]
    ) -> bool:
        """Return True to keep stationary, False to flip to active.

        Compares the union of the anchor and current boxes in a bounded scene
        reference, so box jitter is not movement and newly covered pixels count.
        """

        if id not in self.anchor_frames:
            logger.debug("evaluate: id=%s has no appearance reference", id)
            return False

        anchor_box = self.anchor_boxes[id]
        height, width = self.anchor_frame_shapes[id]
        if (yuv_frame.shape[0] // 3 * 2, yuv_frame.shape[1]) != (height, width):
            self.reset(id)
            return False
        if current_box[0] >= current_box[2] or current_box[1] >= current_box[3]:
            logger.warning("Invalid stationary comparison box for %s", id)
            return False
        if max(anchor_box[0], current_box[0]) >= min(
            anchor_box[2], current_box[2]
        ) or max(anchor_box[1], current_box[1]) >= min(anchor_box[3], current_box[3]):
            return False
        reference = self.anchor_frames[id]
        reference_height = reference.shape[0] // 3 * 2
        reference_width = reference.shape[1]
        box = (
            int(min(anchor_box[0], current_box[0]) * reference_width / width),
            int(min(anchor_box[1], current_box[1]) * reference_height / height),
            int(np.ceil(max(anchor_box[2], current_box[2]) * reference_width / width)),
            int(
                np.ceil(max(anchor_box[3], current_box[3]) * reference_height / height)
            ),
        )
        anchor_crop = self._extract_y_crop(reference, box)
        curr_crop = self._extract_y_crop(self._reference_frame(yuv_frame), box)

        # Compute appearance and motion metrics
        ncc = cv2.matchTemplate(curr_crop, anchor_crop, cv2.TM_CCOEFF_NORMED)[0, 0]
        a64 = anchor_crop.astype(np.float64)
        c64 = curr_crop.astype(np.float64)
        a64 = (a64 - a64.mean()) * self._hann2d
        c64 = (c64 - c64.mean()) * self._hann2d
        (shift_x, shift_y), _ = cv2.phaseCorrelate(a64, c64)
        shift_norm = float(np.hypot(shift_x, shift_y)) / float(self.CROP_SIZE)
        if not np.isfinite(ncc) or not np.isfinite(shift_norm):
            logger.debug("evaluate: id=%s has unreliable appearance evidence", id)
            return False

        logger.debug(
            "evaluate: id=%s metrics ncc=%.4f shift_norm=%.4f (shift_x=%.3f, shift_y=%.3f)",
            id,
            float(ncc),
            shift_norm,
            float(shift_x),
            float(shift_y),
        )

        # Early exit for clear stationary case
        if ncc >= self.NCC_KEEP_THRESHOLD and shift_norm < self.SHIFT_KEEP_THRESHOLD:
            self.changed_counts[id] = 0
            logger.debug(
                "evaluate: id=%s early-stationary keep=True (ncc>=%.2f and shift<%.2f)",
                id,
                self.NCC_KEEP_THRESHOLD,
                self.SHIFT_KEEP_THRESHOLD,
            )
            return True

        # Check for movement indicators
        movement_detected = (
            ncc < self.NCC_ACTIVE_THRESHOLD or shift_norm >= self.SHIFT_ACTIVE_THRESHOLD
        )

        if movement_detected:
            cnt = self.changed_counts.get(id, 0) + 1
            self.changed_counts[id] = cnt
            if cnt >= self.CHANGED_FRAMES_TO_FLIP:
                logger.debug(
                    "evaluate: id=%s flip_to_active=True cnt=%d threshold=%d",
                    id,
                    cnt,
                    self.CHANGED_FRAMES_TO_FLIP,
                )
                return False
            logger.debug(
                "evaluate: id=%s movement_detected cnt=%d keep_until_cnt>=%d",
                id,
                cnt,
                self.CHANGED_FRAMES_TO_FLIP,
            )
        else:
            self.changed_counts[id] = 0
            logger.debug("evaluate: id=%s no_movement keep=True", id)

        return True

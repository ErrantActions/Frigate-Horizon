"""Conservative camera-view change checks from distributed background features."""

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass(frozen=True)
class ViewAlignment:
    state: str
    translation_pixels: float = 0
    rotation_degrees: float = 0
    inliers: int = 0


class CameraViewMonitor:
    """Require coherent multi-region motion, not moving trees or one foreground object."""

    def __init__(self) -> None:
        self.reference: np.ndarray | None = None
        self.shift_observations = 0
        self.recovery_reference: np.ndarray | None = None
        self.recovery_observations = 0

    def check(
        self, image: np.ndarray, excluded: np.ndarray | None = None
    ) -> ViewAlignment:
        """Compare stable edge/corner landmarks without claiming semantic identity."""
        if image.ndim != 2 or image.dtype != np.uint8:
            raise ValueError("Camera alignment requires a grayscale uint8 frame")
        if self.reference is None:
            self.reference = image.copy()
            return ViewAlignment("warming_up")
        if self.reference.shape != image.shape:
            self.reference = image.copy()
            self.shift_observations = 0
            self.recovery_reference = None
            self.recovery_observations = 0
            return ViewAlignment("moved")
        result = self._compare(self.reference, image, excluded)
        if result.state == "uncertain":
            self.shift_observations = 0
            if self.recovery_reference is not None:
                recovery = self._compare(self.recovery_reference, image, excluded)
                if recovery.state == "stable":
                    self.recovery_observations += 1
                    if self.recovery_observations >= 5:
                        # Old thresholds must not survive an unverified view transition.
                        self.reference = image.copy()
                        self.recovery_reference = None
                        self.recovery_observations = 0
                        return ViewAlignment(
                            "moved",
                            recovery.translation_pixels,
                            recovery.rotation_degrees,
                            recovery.inliers,
                        )
                    return result
            self.recovery_reference = image.copy()
            self.recovery_observations = 0
            return result
        self.recovery_reference = None
        self.recovery_observations = 0
        self.shift_observations = (
            self.shift_observations + 1 if result.state == "checking" else 0
        )
        if self.shift_observations >= 3:
            self.reference = image.copy()
            self.shift_observations = 0
            return ViewAlignment(
                "moved",
                result.translation_pixels,
                result.rotation_degrees,
                result.inliers,
            )
        return result

    @staticmethod
    def _compare(
        reference: np.ndarray, image: np.ndarray, excluded: np.ndarray | None
    ) -> ViewAlignment:
        # Optical flow assumes constant brightness, which camera exposure violates.
        reference = cv2.equalizeHist(reference)
        image = cv2.equalizeHist(image)
        mask = None if excluded is None else np.where(excluded, 0, 255).astype(np.uint8)
        points = cv2.goodFeaturesToTrack(
            reference,
            maxCorners=200,
            qualityLevel=0.02,
            minDistance=4,
            mask=mask,
        )
        if points is None or len(points) < 20:
            return ViewAlignment("uncertain")
        tracked, status, _ = cv2.calcOpticalFlowPyrLK(
            reference,
            image,
            points,
            points.copy(),
            winSize=(21, 21),
            maxLevel=3,
        )
        original = points.reshape(-1, 2)[status.ravel() == 1]
        matched = tracked.reshape(-1, 2)[status.ravel() == 1]
        if len(original) < 20:
            return ViewAlignment("uncertain")
        transform, inlier_mask = cv2.estimateAffinePartial2D(
            original,
            matched,
            method=cv2.RANSAC,
            ransacReprojThreshold=1.5,
        )
        if transform is None or inlier_mask is None or not np.isfinite(transform).all():
            return ViewAlignment("uncertain")
        inliers = inlier_mask.ravel() == 1
        landmarks = original[inliers]
        height, width = image.shape
        quadrants = {(int(x >= width / 2), int(y >= height / 2)) for x, y in landmarks}
        if np.count_nonzero(inliers) < 20 or inliers.mean() < 0.7 or len(quadrants) < 3:
            return ViewAlignment("uncertain")
        shift = float(np.hypot(transform[0, 2], transform[1, 2]))
        rotation = float(np.degrees(np.arctan2(transform[1, 0], transform[0, 0])))
        scale = float(np.hypot(transform[0, 0], transform[1, 0]))
        shifted = shift > 2 or abs(rotation) > 0.5 or abs(scale - 1) > 0.01
        return ViewAlignment(
            "checking" if shifted else "stable", shift, rotation, int(inliers.sum())
        )

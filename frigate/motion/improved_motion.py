import hashlib
import json
import logging

import cv2
import numpy as np
from scipy.ndimage import gaussian_filter

from frigate.camera import PTZMetrics
from frigate.config.camera.motion import motion_analysis_shape
from frigate.config.config import RuntimeMotionConfig
from frigate.const import REPLAY_CAMERA_PREFIX
from frigate.motion import MotionDetector
from frigate.motion.adaptive_runtime import AdaptiveMotionRuntime
from frigate.util.image import grab_cv2_contours

logger = logging.getLogger(__name__)


class ImprovedMotionDetector(MotionDetector):
    def __init__(
        self,
        frame_shape: tuple[int, ...],
        config: RuntimeMotionConfig,
        fps: int,
        ptz_metrics: PTZMetrics | None = None,
        autotracking_enabled: bool = False,
        name: str = "improved",
        blur_radius: int = 1,
        interpolation: int = cv2.INTER_NEAREST,
        contrast_frame_history: int = 50,
        view_identity: str = "",
    ) -> None:
        self.name = name
        self.view_identity = view_identity
        self.adaptive: AdaptiveMotionRuntime | None = None
        self.adaptive_signature = ""
        self.tracked_boxes: list[tuple[int, int, int, int]] = []
        self.object_detection_enabled = True
        self.last_ptz_start = 0.0
        self.config = config
        self.frame_shape = frame_shape
        self.motion_frame_size = motion_analysis_shape(
            config, (frame_shape[0], frame_shape[1])
        )
        self.resize_factor = frame_shape[0] / self.motion_frame_size[0]
        self.resize_factor_x = frame_shape[1] / self.motion_frame_size[1]
        self.avg_frame = np.zeros(self.motion_frame_size, np.float32)
        self.motion_frame_count = 0
        self.frame_counter = 0
        self.update_mask()
        self.save_images = False
        self.calibrating = True
        self.blur_radius = blur_radius
        self.interpolation = interpolation
        self.contrast_values = np.zeros((contrast_frame_history, 2), np.uint8)
        self.contrast_values[:, 1:2] = 255
        self.contrast_values_index = 0
        self.ptz_metrics = ptz_metrics
        self.autotracking_enabled = autotracking_enabled
        self.last_stop_time: float | None = None

    def is_calibrating(self) -> bool:
        return self.calibrating

    def view_verified(self) -> bool:
        """Reuse actual decoded-view checks, never compressed-score guesses."""
        return (
            not self.calibrating
            and self.adaptive is not None
            and self.adaptive.alignment_loaded
            and self.adaptive.alignment_warning is None
        )

    def detect(self, frame: np.ndarray) -> list[tuple[int, int, int, int]]:
        motion_boxes: list[tuple[int, int, int, int]] = []

        if not self.config.enabled:
            return motion_boxes

        # if ptz motor is moving from autotracking, quickly return
        # a single box that is 80% of the frame
        if self.ptz_metrics is not None and (
            self.autotracking_enabled and not self.ptz_metrics.motor_stopped.is_set()
        ):
            return [
                (
                    int(self.frame_shape[1] * 0.1),
                    int(self.frame_shape[0] * 0.1),
                    int(self.frame_shape[1] * 0.9),
                    int(self.frame_shape[0] * 0.9),
                )
            ]

        gray = frame[0 : self.frame_shape[0], 0 : self.frame_shape[1]]

        # resize frame
        resized_frame = cv2.resize(
            gray,
            dsize=(self.motion_frame_size[1], self.motion_frame_size[0]),
            interpolation=self.interpolation,
        )

        if self.save_images:
            resized_saved = resized_frame.copy()

        # Improve contrast
        if self.config.improve_contrast:
            # TODO tracking moving average of min/max to avoid sudden contrast changes
            min_value = np.percentile(resized_frame, 4).astype(np.uint8)
            max_value = np.percentile(resized_frame, 96).astype(np.uint8)
            # skip contrast calcs if the image is a single color
            if min_value < max_value:
                # keep track of the last 50 contrast values
                self.contrast_values[self.contrast_values_index] = [
                    min_value,
                    max_value,
                ]
                self.contrast_values_index += 1
                if self.contrast_values_index == len(self.contrast_values):
                    self.contrast_values_index = 0

                avg_min, avg_max = np.mean(self.contrast_values, axis=0)

                resized_frame = np.clip(resized_frame, avg_min, avg_max)
                resized_frame = (
                    ((resized_frame - avg_min) / (avg_max - avg_min)) * 255
                ).astype(np.uint8)

        if self.save_images:
            contrasted_saved = resized_frame.copy()

        # mask frame
        # this has to come after contrast improvement
        # Setting masked pixels to zero, to match the average frame at startup
        resized_frame[self.mask] = [0]

        resized_frame = gaussian_filter(resized_frame, sigma=1, radius=self.blur_radius)

        if self.save_images:
            blurred_saved = resized_frame.copy()

        if self.save_images or self.calibrating:
            self.frame_counter += 1
        # compare to average
        frameDelta = cv2.absdiff(resized_frame, cv2.convertScaleAbs(self.avg_frame))

        # compute the threshold image for the current frame
        settings = self.config.adaptive
        signature = json.dumps(settings.model_dump(), sort_keys=True) + str(
            self.config.threshold
        )
        if signature != self.adaptive_signature:
            if self.adaptive is not None:
                self.adaptive.stop()
                self.adaptive = None
            if settings.enabled and not self.name.startswith(REPLAY_CAMERA_PREFIX):
                identity = hashlib.sha256(
                    self.view_identity.encode() + self.config.rasterized_mask.tobytes()
                ).hexdigest()
                self.adaptive = AdaptiveMotionRuntime(
                    self.name,
                    settings,
                    (self.frame_shape[0], self.frame_shape[1]),
                    self.config.threshold,
                    identity,
                )
            self.adaptive_signature = signature
        scene_change = (
            np.count_nonzero(frameDelta > self.config.threshold) / frameDelta.size
            > self.config.lightning_threshold
        )
        if self.adaptive is not None:
            ptz_start = (
                self.ptz_metrics.start_time.value
                if self.ptz_metrics is not None
                and hasattr(self.ptz_metrics, "start_time")
                else 0
            )
            moved = ptz_start > 0 and ptz_start != self.last_ptz_start
            self.last_ptz_start = ptz_start
            self.adaptive.submit(
                frameDelta,
                self.tracked_boxes,
                paused=self.calibrating
                or scene_change
                or not self.object_detection_enabled,
                reset=moved,
                reference=gray,
            )
            thresh = (
                cv2.threshold(
                    frameDelta, self.config.threshold, 255, cv2.THRESH_BINARY
                )[1]
                if self.calibrating
                or scene_change
                or moved
                or not self.object_detection_enabled
                else self.adaptive.threshold(frameDelta, self.tracked_boxes)
            )
        else:
            thresh = cv2.threshold(
                frameDelta, self.config.threshold, 255, cv2.THRESH_BINARY
            )[1]

        # dilate the thresholded image to fill in holes, then find contours
        # on thresholded image
        thresh_dilated = cv2.dilate(thresh, None, iterations=1)  # type: ignore[call-overload]
        contours = cv2.findContours(
            thresh_dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        contours = grab_cv2_contours(contours)

        # loop over the contours
        total_contour_area: float = 0
        base_height = self.config.frame_height or self.frame_shape[0]
        base_width = base_height * self.frame_shape[1] // self.frame_shape[0]
        area_scale = (
            self.motion_frame_size[0]
            * self.motion_frame_size[1]
            / (base_height * base_width)
            if self.config.adaptive.enabled
            else 1
        )
        for c in contours:
            # if the contour is big enough, count it as motion
            contour_area = cv2.contourArea(c)
            total_contour_area += contour_area
            if contour_area > (self.config.contour_area or 0) * area_scale:
                x, y, w, h = cv2.boundingRect(c)
                motion_boxes.append(
                    (
                        int(x * self.resize_factor_x),
                        int(y * self.resize_factor),
                        int((x + w) * self.resize_factor_x),
                        int((y + h) * self.resize_factor),
                    )
                )

        pct_motion = total_contour_area / (
            self.motion_frame_size[0] * self.motion_frame_size[1]
        )

        # check if the motor has just stopped from autotracking
        # if so, reassign the average to the current frame so we begin with a new baseline
        if self.ptz_metrics is not None and (
            # ensure we only do this for cameras with autotracking enabled
            self.autotracking_enabled
            and self.ptz_metrics.motor_stopped.is_set()
            and (
                self.last_stop_time is None
                or self.ptz_metrics.stop_time.value != self.last_stop_time
            )
            # value is 0 on startup or when motor is moving
            and self.ptz_metrics.stop_time.value != 0
        ):
            self.last_stop_time = self.ptz_metrics.stop_time.value

            self.avg_frame = resized_frame.astype(np.float32)
            motion_boxes = []
            pct_motion = 0

        # skip motion entirely if the scene change percentage exceeds configured
        # threshold. this is useful to ignore lighting storms, IR mode switches,
        # etc. rather than registering them as brief motion and then recalibrating.
        # note: skipping means the frame is dropped and **no recording will be
        # created**, which could hide a legitimate object if the camera is actively
        # auto‑tracking. the alternative is to allow motion and accept a small
        # recording that can be reviewed in the timeline. disabled by default (None).
        if (
            self.config.skip_motion_threshold is not None
            and pct_motion > self.config.skip_motion_threshold
        ):
            # recalibrate so we transition to the new background. the frame
            # still has to be blended in here, otherwise the background stays
            # frozen and every subsequent frame skips as well
            self.calibrating = True
            cv2.accumulateWeighted(resized_frame, self.avg_frame, 0.2)
            self.motion_frame_count = 0
            return []

        # once the motion is less than 5% and the number of contours is < 4, assume its calibrated
        if pct_motion < 0.05 and len(motion_boxes) <= 4:
            self.calibrating = False

        # if calibrating or the motion contours are > 80% of the image area
        # (lightning, ir, ptz) recalibrate. the lightning threshold does **not**
        # stop motion detection entirely; it simply halts additional processing for
        # the current frame once the percentage crosses the threshold. this helps
        # reduce false positive object detections and CPU usage during high‑motion
        # events. recordings continue to be generated because users expect data
        # while a PTZ camera is moving.
        if self.calibrating or pct_motion > self.config.lightning_threshold:
            self.calibrating = True

        if self.save_images:
            thresh_dilated = cv2.cvtColor(thresh_dilated, cv2.COLOR_GRAY2BGR)
            for b in motion_boxes:
                cv2.rectangle(
                    thresh_dilated,
                    (int(b[0] / self.resize_factor_x), int(b[1] / self.resize_factor)),
                    (int(b[2] / self.resize_factor_x), int(b[3] / self.resize_factor)),
                    (0, 0, 255),
                    2,
                )
            frames = [
                cv2.cvtColor(resized_saved, cv2.COLOR_GRAY2BGR),
                cv2.cvtColor(contrasted_saved, cv2.COLOR_GRAY2BGR),
                cv2.cvtColor(blurred_saved, cv2.COLOR_GRAY2BGR),
                cv2.cvtColor(frameDelta, cv2.COLOR_GRAY2BGR),
                cv2.cvtColor(thresh, cv2.COLOR_GRAY2BGR),
                thresh_dilated,
            ]
            cv2.imwrite(
                f"debug/frames/{self.name}-{self.frame_counter}.jpg",
                (
                    cv2.hconcat(frames)
                    if self.frame_shape[0] > self.frame_shape[1]
                    else cv2.vconcat(frames)
                ),
            )

        if len(motion_boxes) > 0:
            self.motion_frame_count += 1
            if self.motion_frame_count >= 10:
                # only average in the current frame if the difference persists for a bit
                cv2.accumulateWeighted(
                    resized_frame,
                    self.avg_frame,
                    0.2 if self.calibrating else self.config.frame_alpha,
                )
        else:
            # when no motion, just keep averaging the frames together
            cv2.accumulateWeighted(
                resized_frame,
                self.avg_frame,
                0.2 if self.calibrating else self.config.frame_alpha,
            )
            self.motion_frame_count = 0

        return motion_boxes

    def update_mask(self) -> None:
        if self.adaptive is not None:
            self.adaptive.stop()
            self.adaptive = None
        self.adaptive_signature = ""
        self.motion_frame_size = motion_analysis_shape(
            self.config,
            (self.frame_shape[0], self.frame_shape[1]),
        )
        self.resize_factor = self.frame_shape[0] / self.motion_frame_size[0]
        self.resize_factor_x = self.frame_shape[1] / self.motion_frame_size[1]
        resized_mask = cv2.resize(
            self.config.rasterized_mask,
            dsize=(self.motion_frame_size[1], self.motion_frame_size[0]),
            interpolation=cv2.INTER_AREA,
        )
        self.mask = np.where(resized_mask == [0])

        # Reset motion detection state when mask changes
        # so motion detection can quickly recalibrate with the new mask
        self.avg_frame = np.zeros(self.motion_frame_size, np.float32)
        self.calibrating = True
        self.motion_frame_count = 0

    def stop(self) -> None:
        """stop the motion detector."""
        if self.adaptive is not None:
            self.adaptive.stop()

    def set_tracked_boxes(
        self, boxes: list[tuple[int, int, int, int]], *, detection_enabled: bool = True
    ) -> None:
        """Hold local adaptation for reliable existing AI tracks."""
        self.tracked_boxes = boxes
        self.object_detection_enabled = detection_enabled

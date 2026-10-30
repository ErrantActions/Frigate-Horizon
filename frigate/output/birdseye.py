"""Compose Birdseye frames for the private go2rtc HLS source."""

import datetime
import glob
import logging
import math
import os
import queue
import select
import threading
import time
import traceback
from dataclasses import dataclass
from multiprocessing.synchronize import Event as MpEvent
from typing import Any

import cv2
import numpy as np

from frigate.comms.inter_process import InterProcessRequestor
from frigate.comms.review_updater import ReviewDataSubscriber
from frigate.config import BirdseyeModeEnum, FrigateConfig
from frigate.const import BASE_DIR, BIRDSEYE_PIPE, INSTALL_DIR, UPDATE_BIRDSEYE_LAYOUT
from frigate.review.types import SeverityEnum
from frigate.util.image import (
    SharedMemoryFrameManager,
    copy_yuv_to_position,
    get_yuv_crop,
)

logger = logging.getLogger(__name__)

# Minimum seconds to hold the current camera before switching to a different one
CAMERA_HOLD_SECONDS = 5


@dataclass(frozen=True, slots=True)
class BirdseyeActivity:
    """Activity signals used to decide whether a camera is shown in Birdseye."""

    has_object: bool
    has_motion: bool
    severity: str | None


def get_standard_aspect_ratio(width: int, height: int) -> tuple[int, int]:
    """Ensure that only standard aspect ratios are used."""
    # it is important that all ratios have the same scale
    known_aspects = [
        (16, 9),
        (9, 16),
        (20, 10),
        (16, 3),  # max wide camera
        (16, 6),  # reolink duo 2
        (32, 9),  # panoramic cameras
        (12, 9),
        (9, 12),
        (22, 15),  # Amcrest, NTSC DVT
        (1, 1),  # fisheye
    ]  # aspects are scaled to have common relative size
    known_aspects_ratios = list(
        map(lambda aspect: aspect[0] / aspect[1], known_aspects)
    )
    closest = min(
        known_aspects_ratios,
        key=lambda x: abs(x - (width / height)),
    )
    return known_aspects[known_aspects_ratios.index(closest)]


def get_canvas_shape(width: int, height: int) -> tuple[int, int]:
    """Get birdseye canvas shape."""
    canvas_width = width
    canvas_height = height
    a_w, a_h = get_standard_aspect_ratio(width, height)

    if round(a_w / a_h, 2) != round(width / height, 2):
        canvas_width = int(width // 4 * 4)
        canvas_height = int((canvas_width / a_w * a_h) // 4 * 4)
        logger.error(
            f"Birdseye resolution {width}x{height} is not a supported aspect ratio "
            f"and may cause visual distortion; falling back to {canvas_width}x{canvas_height}. "
            f"Set width and height to a supported aspect ratio (16:9, 20:10, 16:6, 32:9, 12:9, 22:15, 9:16, 9:12, 16:3, or 1:1)"
        )

    return (canvas_width, canvas_height)


class Canvas:
    def __init__(
        self,
        canvas_width: int,
        canvas_height: int,
        scaling_factor: float,
    ) -> None:
        self.scaling_factor = scaling_factor
        gcd = math.gcd(canvas_width, canvas_height)
        self.aspect = get_standard_aspect_ratio(
            int(canvas_width / gcd), int(canvas_height / gcd)
        )
        self.width = canvas_width
        self.height: float = (self.width * self.aspect[1]) / self.aspect[0]
        self.coefficient_cache: dict[int, float] = {}
        self.aspect_cache: dict[str, tuple[int, int]] = {}

    def get_aspect(self, coefficient: float) -> tuple[float, float]:
        return (self.aspect[0] * coefficient, self.aspect[1] * coefficient)

    def get_coefficient(self, camera_count: int) -> float:
        return self.coefficient_cache.get(camera_count, self.scaling_factor)

    def set_coefficient(self, camera_count: int, coefficient: float) -> None:
        self.coefficient_cache[camera_count] = coefficient

    def get_camera_aspect(
        self, cam_name: str, camera_width: int, camera_height: int
    ) -> tuple[int, int]:
        cached = self.aspect_cache.get(cam_name)

        if cached:
            return cached

        gcd = math.gcd(camera_width, camera_height)
        camera_aspect = get_standard_aspect_ratio(
            int(camera_width / gcd), int(camera_height / gcd)
        )
        self.aspect_cache[cam_name] = camera_aspect
        return camera_aspect


class BirdseyeRestreamWriter(threading.Thread):
    """Deliver fixed-rate raw frames to the private go2rtc HLS source."""

    def __init__(self, input_queue: queue.Queue, stop_event: MpEvent):
        super().__init__(name="birdseye_restream_writer")
        self.input_queue = input_queue
        self.stop_event = stop_event
        self.bd_pipe: int | None = None
        self.reading_birdseye = False
        self.recreate_birdseye_pipe()

    def recreate_birdseye_pipe(self) -> None:
        """Discard incomplete frames after the only reader disconnects."""
        if self.bd_pipe is not None:
            os.close(self.bd_pipe)
        if os.path.exists(BIRDSEYE_PIPE):
            os.remove(BIRDSEYE_PIPE)
        os.mkfifo(BIRDSEYE_PIPE, mode=0o600)
        reader = os.open(BIRDSEYE_PIPE, os.O_RDONLY | os.O_NONBLOCK)
        try:
            self.bd_pipe = os.open(BIRDSEYE_PIPE, os.O_WRONLY | os.O_NONBLOCK)
        finally:
            os.close(reader)
        self.reading_birdseye = False

    def _write_frame(self, frame: bytes) -> None:
        descriptor = self.bd_pipe
        if descriptor is None:
            raise OSError("Birdseye restream pipe is unavailable")
        view = memoryview(frame)
        written = 0
        while written < len(view) and not self.stop_event.is_set():
            try:
                count = os.write(descriptor, view[written:])
            except BlockingIOError:
                select.select([], [descriptor], [], 0.1)
                continue
            except BrokenPipeError:
                if self.reading_birdseye:
                    self.recreate_birdseye_pipe()
                return
            if count <= 0:
                raise OSError("Birdseye restream pipe did not accept frame data")
            self.reading_birdseye = True
            written += count

    def run(self) -> None:
        last_frame = None
        try:
            while not self.stop_event.is_set():
                deadline = time.monotonic() + 0.1
                while True:
                    try:
                        last_frame = self.input_queue.get_nowait()
                    except queue.Empty:
                        break
                if last_frame is not None:
                    self._write_frame(last_frame)
                self.stop_event.wait(max(0, deadline - time.monotonic()))
        except OSError:
            logger.exception("Birdseye restream output failed")
        finally:
            if self.bd_pipe is not None:
                os.close(self.bd_pipe)
                self.bd_pipe = None


class BirdsEyeFrameManager:
    def __init__(
        self,
        config: FrigateConfig,
        stop_event: MpEvent,
    ):
        self.config = config
        width, height = get_canvas_shape(config.birdseye.width, config.birdseye.height)
        self.frame_shape = (height, width)
        self.yuv_shape = (height * 3 // 2, width)
        self.frame: np.ndarray = np.ndarray(self.yuv_shape, dtype=np.uint8)
        self.canvas = Canvas(width, height, config.birdseye.layout.scaling_factor)
        self.stop_event = stop_event
        self.last_refresh_time: float = 0

        # initialize the frame as black and with the Frigate logo
        self.blank_frame = np.zeros(self.yuv_shape, np.uint8)
        self.blank_frame[:] = 128
        self.blank_frame[0 : self.frame_shape[0], 0 : self.frame_shape[1]] = 16

        # find and copy the logo on the blank frame
        birdseye_logo = None

        custom_logo_files = glob.glob(f"{BASE_DIR}/custom.png")

        if len(custom_logo_files) > 0:
            birdseye_logo = cv2.imread(custom_logo_files[0], cv2.IMREAD_UNCHANGED)

        if birdseye_logo is None:
            logo_files = glob.glob(
                os.path.join(INSTALL_DIR, "frigate/images/birdseye.png")
            )

            if len(logo_files) > 0:
                birdseye_logo = cv2.imread(logo_files[0], cv2.IMREAD_UNCHANGED)

        if birdseye_logo is not None:
            if birdseye_logo.ndim == 2:
                # Grayscale image (no channels) — use directly as luminance
                transparent_layer = birdseye_logo
            elif birdseye_logo.shape[2] >= 4:
                # RGBA — use alpha channel as luminance
                transparent_layer = birdseye_logo[:, :, 3]
            else:
                # RGB or other format without alpha — convert to grayscale
                transparent_layer = cv2.cvtColor(birdseye_logo, cv2.COLOR_BGR2GRAY)
            y_offset = height // 2 - transparent_layer.shape[0] // 2
            x_offset = width // 2 - transparent_layer.shape[1] // 2
            self.blank_frame[
                y_offset : y_offset + transparent_layer.shape[0],
                x_offset : x_offset + transparent_layer.shape[1],
            ] = transparent_layer
        else:
            logger.warning("Unable to read Frigate logo")

        self.frame[:] = self.blank_frame

        self.cameras: dict[str, Any] = {}
        for camera in self.config.cameras.keys():
            self.add_camera(camera)

        self.camera_layout: list[Any] = []
        self.active_cameras: set[str] = set()
        self.layout_camera_order: list[str] = []
        self.last_output_time = 0.0

    def add_camera(self, cam: str) -> None:
        """Add a camera to self.cameras with the correct structure."""
        settings = self.config.cameras[cam]
        # precalculate the coordinates for all the channels
        y, u1, u2, v1, v2 = get_yuv_crop(
            settings.frame_shape_yuv,
            (
                0,
                0,
                settings.frame_shape[1],
                settings.frame_shape[0],
            ),
        )
        self.cameras[cam] = {
            "dimensions": [
                settings.detect.width,
                settings.detect.height,
            ],
            "last_active_frame": 0.0,
            "live_active": False,
            "current_frame": 0.0,
            "layout_frame": 0.0,
            "channel_dims": {
                "y": y,
                "u1": u1,
                "u2": u2,
                "v1": v1,
                "v2": v2,
            },
        }

    def remove_camera(self, cam: str) -> None:
        """Remove a camera from self.cameras."""
        if cam in self.cameras:
            del self.cameras[cam]

    def sort_cameras(self, cameras: set[str]) -> list[str]:
        """Sort cameras by birdseye order, falling back to name when tied."""
        return sorted(
            cameras,
            key=lambda camera: (self.config.cameras[camera].birdseye.order, camera),
        )

    def clear_frame(self) -> None:
        logger.debug("Clearing the birdseye frame")
        self.frame[:] = self.blank_frame

    def copy_to_position(
        self,
        position: Any,
        camera: str | None = None,
        frame: np.ndarray | None = None,
    ) -> None:
        if camera is None:
            frame = None
            channel_dims = None
        else:
            if frame is None:
                logger.debug(f"Unable to copy frame {camera} to birdseye.")
                return

            channel_dims = self.cameras[camera]["channel_dims"]

        copy_yuv_to_position(
            self.frame,
            [position[1], position[0]],
            [position[3], position[2]],
            frame,
            channel_dims,
        )

    def camera_threshold_active(
        self,
        modes: list[BirdseyeModeEnum],
        activity: BirdseyeActivity,
    ) -> bool:
        """Return whether activity subject to inactivity_threshold is present."""
        return (BirdseyeModeEnum.motion in modes and activity.has_motion) or (
            BirdseyeModeEnum.all_objects in modes and activity.has_object
        )

    def camera_live_active(
        self,
        modes: list[BirdseyeModeEnum],
        activity: BirdseyeActivity,
    ) -> bool:
        """Return whether activity that ends the moment it stops is present."""
        return (
            BirdseyeModeEnum.continuous in modes
            or (
                BirdseyeModeEnum.alerts in modes
                and activity.severity == SeverityEnum.alert
            )
            or (
                BirdseyeModeEnum.detections in modes
                and activity.severity == SeverityEnum.detection
            )
        )

    def get_camera_coordinates(self) -> dict[str, dict[str, int]]:
        """Return the coordinates of each camera in the current layout."""
        coordinates = {}
        for row in self.camera_layout:
            for position in row:
                camera_name, (x, y, width, height) = position
                coordinates[camera_name] = {
                    "x": x,
                    "y": y,
                    "width": width,
                    "height": height,
                }
        return coordinates

    def update_frame(self, frame: np.ndarray | None = None) -> tuple[bool, bool]:
        """
        Update birdseye, optionally with a new frame.
        Returns (frame_changed, layout_changed) to indicate if the frame or layout changed.
        """

        # determine how many cameras are tracking objects within the last inactivity_threshold seconds
        active_cameras: set[str] = set(
            [
                cam
                for cam, cam_data in self.cameras.items()
                if cam in self.config.cameras
                and self.config.cameras[cam].birdseye.enabled
                and self.config.cameras[cam].enabled_in_config
                and self.config.cameras[cam].enabled
                and (
                    cam_data["live_active"]
                    or (
                        cam_data["last_active_frame"] > 0
                        and cam_data["current_frame_time"]
                        - cam_data["last_active_frame"]
                        < self.config.birdseye.inactivity_threshold
                    )
                )
            ]
        )
        logger.debug(f"Active cameras: {active_cameras}")

        max_cameras = self.config.birdseye.layout.max_cameras
        max_camera_refresh = False
        if max_cameras:
            now = datetime.datetime.now().timestamp()

            if (
                len(self.active_cameras) == max_cameras
                and len(active_cameras) >= max_cameras
                and now - self.last_refresh_time < CAMERA_HOLD_SECONDS
            ):
                active_cameras = self.active_cameras
            else:
                limited_active_cameras = sorted(
                    active_cameras,
                    key=lambda active_camera: (
                        0.0
                        if self.cameras[active_camera]["live_active"]
                        else self.cameras[active_camera]["current_frame_time"]
                        - self.cameras[active_camera]["last_active_frame"]
                    ),
                )
                active_cameras = set(limited_active_cameras[:max_cameras])
                max_camera_refresh = True
                self.last_refresh_time = now

        # Track if the frame or layout changes
        frame_changed = False
        layout_changed = False

        # If no active cameras and layout is already empty, no update needed
        if len(active_cameras) == 0:
            # if the layout is already cleared
            if len(self.camera_layout) == 0:
                return False, False
            # if the layout needs to be cleared
            self.camera_layout = []
            self.active_cameras = set()
            self.layout_camera_order = []
            self.clear_frame()
            frame_changed = True
            layout_changed = True
        else:
            # Determine if layout needs resetting
            if len(self.active_cameras) - len(active_cameras) == 0:
                if (
                    len(self.active_cameras) == 1
                    and self.active_cameras != active_cameras
                ):
                    reset_layout = True
                elif max_camera_refresh:
                    reset_layout = True
                else:
                    reset_layout = False
            else:
                reset_layout = True

            sorted_active_cameras = self.sort_cameras(active_cameras)

            if not reset_layout and sorted_active_cameras != self.layout_camera_order:
                logger.debug("Birdseye camera order changed")
                reset_layout = True

            if reset_layout:
                logger.debug("Resetting Birdseye layout...")
                self.clear_frame()
                self.active_cameras = active_cameras
                self.layout_camera_order = sorted_active_cameras
                layout_changed = True  # Layout is changing due to reset
                # this also converts added_cameras from a set to a list since we need
                # to pop elements in order
                active_cameras_to_add = sorted_active_cameras
                if len(active_cameras) == 1:
                    # show single camera as fullscreen
                    camera = active_cameras_to_add[0]
                    camera_dims = self.cameras[camera]["dimensions"].copy()
                    scaled_width = int(
                        self.canvas.height * camera_dims[0] / camera_dims[1]
                    )

                    # center camera view in canvas and ensure that it fits
                    if scaled_width < self.canvas.width:
                        coefficient: float = 1
                        x_offset = int((self.canvas.width - scaled_width) / 2)
                    else:
                        coefficient = self.canvas.width / scaled_width
                        x_offset = int(
                            (self.canvas.width - (scaled_width * coefficient)) / 2
                        )

                    self.camera_layout = [
                        [
                            (
                                camera,
                                (
                                    x_offset,
                                    0,
                                    int(scaled_width * coefficient),
                                    int(self.canvas.height * coefficient),
                                ),
                            )
                        ]
                    ]
                else:
                    # calculate optimal layout
                    coefficient = self.canvas.get_coefficient(len(active_cameras))
                    calculating = True

                    # decrease scaling coefficient until height of all cameras can fit into the birdseye canvas
                    while calculating:
                        if self.stop_event.is_set():
                            return frame_changed, layout_changed

                        layout_candidate = self.calculate_layout(
                            active_cameras_to_add, coefficient
                        )

                        if not layout_candidate:
                            if coefficient < 10:
                                coefficient += 1
                                continue
                            else:
                                logger.error(
                                    "Error finding appropriate birdseye layout"
                                )
                                return frame_changed, layout_changed
                        calculating = False
                        self.canvas.set_coefficient(len(active_cameras), coefficient)

                    self.camera_layout = layout_candidate or []
                frame_changed = True

            # Draw the layout
            for row in self.camera_layout:
                for position in row:
                    src_frame = self.cameras[position[0]]["current_frame"]
                    if src_frame is None or src_frame.size == 0:
                        logger.debug(f"Skipping invalid frame for {position[0]}")
                        continue
                    self.copy_to_position(position[1], position[0], src_frame)
            if frame is not None:  # Frame presence indicates a potential change
                frame_changed = True

        return frame_changed, layout_changed

    def calculate_layout(
        self,
        cameras_to_add: list[str],
        coefficient: float,
    ) -> list[list[Any]] | None:
        """Calculate the optimal layout for 2+ cameras."""

        def find_available_x(
            current_x: int,
            width: int,
            reserved_ranges: list[tuple[int, int]],
            max_width: int,
        ) -> int | None:
            """Find the first horizontal slot that does not collide with reservations."""
            x = current_x

            for reserved_start, reserved_end in sorted(reserved_ranges):
                if x >= reserved_end:
                    continue

                if x + width <= reserved_start:
                    return x

                x = max(x, reserved_end)

            if x + width <= max_width:
                return x

            return None

        def map_layout(row_height: int) -> tuple[int, int, list[list[Any]] | None]:
            """Lay out cameras row by row while reserving portrait spans for the next row."""
            candidate_layout: list[list[Any]] = []
            reserved_ranges: dict[int, list[tuple[int, int]]] = {}
            current_row: list[Any] = []
            row_index = 0
            row_y = 0
            row_x = 0
            max_width = 0
            max_height = 0

            for camera in cameras_to_add:
                camera_dims = self.cameras[camera]["dimensions"].copy()
                camera_aspect_x, camera_aspect_y = self.canvas.get_camera_aspect(
                    camera, camera_dims[0], camera_dims[1]
                )
                portrait = camera_dims[1] > camera_dims[0]
                scaled_height = row_height * 2 if portrait else row_height
                scaled_width = int(scaled_height * (camera_aspect_x / camera_aspect_y))

                while True:
                    x = find_available_x(
                        row_x,
                        scaled_width,
                        reserved_ranges.get(row_index, []),
                        self.canvas.width,
                    )

                    if x is not None and row_y + scaled_height <= self.canvas.height:
                        current_row.append(
                            (camera, (x, row_y, scaled_width, scaled_height))
                        )
                        row_x = x + scaled_width
                        max_width = max(max_width, row_x)
                        max_height = max(max_height, row_y + scaled_height)

                        if portrait:
                            reserved_ranges.setdefault(row_index + 1, []).append(
                                (x, row_x)
                            )

                        break

                    if current_row:
                        candidate_layout.append(current_row)
                        current_row = []

                    row_index += 1
                    row_y = row_index * row_height
                    row_x = 0

                    if row_y + scaled_height > self.canvas.height:
                        overflow_width = max(max_width, scaled_width)
                        overflow_height = row_y + scaled_height
                        return overflow_width, overflow_height, None

            if current_row:
                candidate_layout.append(current_row)

            return max_width, max_height, candidate_layout

        row_height = max(1, int(self.canvas.height / coefficient))
        total_width, total_height, standard_candidate_layout = map_layout(row_height)

        if not standard_candidate_layout:
            # if standard layout didn't work
            # try reducing row_height by the % overflow
            scale_down_percent = max(
                total_width / self.canvas.width,
                total_height / self.canvas.height,
            )
            row_height = max(1, int(row_height / scale_down_percent))
            total_width, total_height, standard_candidate_layout = map_layout(
                row_height
            )

            if not standard_candidate_layout:
                return None

        # layout can't be optimized more
        if total_width / self.canvas.width >= 0.99:
            return standard_candidate_layout

        scale_up_percent = min(
            1 / (total_width / self.canvas.width),
            1 / (total_height / self.canvas.height),
        )
        row_height = max(1, int(row_height * scale_up_percent))
        _, _, scaled_layout = map_layout(row_height)

        if scaled_layout:
            return scaled_layout
        else:
            return standard_candidate_layout

    def update(
        self,
        camera: str,
        activity: BirdseyeActivity,
        frame_time: float,
        frame: np.ndarray,
    ) -> tuple[bool, bool]:
        """
        Update birdseye for a specific camera with new frame data.
        Returns (frame_changed, layout_changed) to indicate if the frame or layout changed.
        """
        # don't process if camera was removed or birdseye is disabled
        camera_config = self.config.cameras.get(camera)
        if camera_config is None:
            return False, False

        force_update = False
        camera_state = self.cameras.get(camera)

        if camera_state is None:
            return False, False

        # disabling birdseye is a little tricky
        if not camera_config.birdseye.enabled or not camera_config.enabled:
            # if we've rendered a frame (we have activity state) then clear it
            if camera_state["last_active_frame"] > 0 or camera_state["live_active"]:
                camera_state["last_active_frame"] = 0
                camera_state["live_active"] = False
                force_update = True
            else:
                return False, False

        # update the last active frame for the camera
        camera_state["current_frame"] = frame.copy()
        camera_state["current_frame_time"] = frame_time
        modes = camera_config.birdseye.modes

        if self.camera_threshold_active(modes, activity):
            camera_state["last_active_frame"] = frame_time

        camera_state["live_active"] = self.camera_live_active(modes, activity)

        now = datetime.datetime.now().timestamp()

        # limit output to 10 fps
        if not force_update and (now - self.last_output_time) < 1 / 10:
            return False, False

        try:
            frame_changed, layout_changed = self.update_frame(frame)
        except Exception:
            frame_changed, layout_changed = False, False
            self.active_cameras = set()
            self.camera_layout = []
            self.layout_camera_order = []
            print(traceback.format_exc())

        # if the frame was updated or the fps is too low, send frame
        if force_update or frame_changed or (now - self.last_output_time) > 1:
            self.last_output_time = now
            return True, layout_changed

        return False, layout_changed


class Birdseye:
    def __init__(
        self,
        config: FrigateConfig,
        stop_event: MpEvent,
    ) -> None:
        self.config = config
        self.input: queue.Queue[bytes] = queue.Queue(maxsize=10)
        self.writer = (
            BirdseyeRestreamWriter(self.input, stop_event)
            if config.birdseye.restream
            else None
        )
        self.birdseye_manager = BirdsEyeFrameManager(self.config, stop_event)
        self.frame_manager = SharedMemoryFrameManager()
        self.stop_event = stop_event
        self.requestor = InterProcessRequestor()
        self.review_subscriber = ReviewDataSubscriber("")
        self.review_severity: dict[str, str] = {}
        self.idle_fps: float = self.config.birdseye.idle_heartbeat_fps
        self._idle_interval: float | None = (
            (1.0 / self.idle_fps) if self.idle_fps > 0 else None
        )

        if config.birdseye.restream:
            self.birdseye_buffer: Any = self.frame_manager.create(
                "birdseye",
                self.birdseye_manager.yuv_shape[0] * self.birdseye_manager.yuv_shape[1],
            )

        if self.writer is not None:
            self.writer.start()

    def __send_new_frame(self) -> None:
        frame_bytes = self.birdseye_manager.frame.tobytes()

        if self.config.birdseye.restream:
            self.birdseye_buffer[:] = frame_bytes

        if self.writer is not None:
            try:
                self.input.put_nowait(frame_bytes)
            except queue.Full:
                pass

    def all_cameras_disabled(self) -> None:
        self.birdseye_manager.clear_frame()
        self.__send_new_frame()

    def check_review_updates(self) -> None:
        """Drain review updates so each camera's active severity stays current."""
        while True:
            update = self.review_subscriber.check_for_update(timeout=0)

            if update is None:
                break

            camera = update["after"]["camera"]

            if update["type"] == "end":
                self.review_severity.pop(camera, None)
            else:
                self.review_severity[camera] = update["after"]["severity"]

    def add_camera(self, camera: str) -> None:
        """Add a camera to the birdseye manager."""
        self.birdseye_manager.add_camera(camera)
        logger.debug(f"Added camera {camera} to birdseye")

    def remove_camera(self, camera: str) -> None:
        """Remove a camera from the birdseye manager."""
        self.birdseye_manager.remove_camera(camera)
        self.review_severity.pop(camera, None)
        logger.debug(f"Removed camera {camera} from birdseye")

    def write_data(
        self,
        camera: str,
        current_tracked_objects: list[dict[str, Any]],
        motion_boxes: list[list[int]],
        frame_time: float,
        frame: np.ndarray,
    ) -> None:
        activity = BirdseyeActivity(
            has_object=any(
                not tracked_object["false_positive"]
                for tracked_object in current_tracked_objects
            ),
            has_motion=bool(motion_boxes),
            severity=self.review_severity.get(camera),
        )

        frame_changed, frame_layout_changed = self.birdseye_manager.update(
            camera,
            activity,
            frame_time,
            frame,
        )
        if frame_changed:
            self.__send_new_frame()

            if frame_layout_changed:
                coordinates = self.birdseye_manager.get_camera_coordinates()
                self.requestor.send_data(UPDATE_BIRDSEYE_LAYOUT, coordinates)
        if self._idle_interval:
            now = datetime.datetime.now().timestamp()
            is_idle = len(self.birdseye_manager.camera_layout) == 0
            if (
                is_idle
                and (now - self.birdseye_manager.last_output_time)
                >= self._idle_interval
            ):
                self.__send_new_frame()

    def stop(self) -> None:
        self.review_subscriber.stop()
        if self.writer is not None:
            self.writer.join()

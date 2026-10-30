"""Temporary original-detector reference runs for measuring camera-event timing."""

import hashlib
import json
import logging
import math
import os
import secrets
import threading
import time
import urllib.request
from pathlib import Path

from ws4py.client.threadedclient import WebSocketClient

from frigate.motion.record_only import (
    TIMING_CONTROL_PATH,
    TimingCalibrationLease,
    timing_calibration_options,
)

logger = logging.getLogger(__name__)
REPORT_ROOT = Path("/config/horizon-timing-calibration")


def timing_recommendations(reference: list[dict], native: list[dict]) -> dict:
    """Use independent, unambiguous closed alarm episodes, not initialized states."""
    pairs = []
    for alarm in native:
        candidates = [
            item
            for item in reference
            if item["kind"] == "object"
            and item["start"] <= alarm["end"] + 5
            and item["end"] >= alarm["start"] - 5
        ]
        if not candidates:
            continue
        if any(
            sum(
                item["start"] <= other["end"] + 5 and item["end"] >= other["start"] - 5
                for other in native
                if other["channel"] == alarm["channel"]
            )
            != 1
            for item in candidates
        ):
            continue
        pairs.append(
            {
                "channel": alarm["channel"],
                "camera_on_lead_seconds": min(item["start"] for item in candidates)
                - alarm["start"],
                "required_post_hold_seconds": max(
                    0, max(item["end"] for item in candidates) - alarm["end"]
                ),
            }
        )
    channels = {}
    for channel in sorted({pair["channel"] for pair in pairs}):
        values = [pair for pair in pairs if pair["channel"] == channel]
        result: dict[str, object] = {
            "independent_pairs": len(values),
            "recommended_timers": None,
        }
        if len(values) >= 5:
            # Cover the worst observed episode plus a one-second sampling margin.
            result["recommended_timers"] = {
                "pre_roll_seconds": math.ceil(
                    max(0, max(-item["camera_on_lead_seconds"] for item in values)) + 1
                ),
                "post_hold_seconds": math.ceil(
                    max(item["required_post_hold_seconds"] for item in values) + 1
                ),
                "sampling_margin_seconds": 1,
                "basis": "worst_observed_of_at_least_five_independent_episodes",
            }
        channels[channel] = result
    return {
        "status": "recommendations_available"
        if any(item["recommended_timers"] is not None for item in channels.values())
        else "insufficient_data",
        "channels": channels,
        "timers_applied": False,
    }


class _ReferenceSocket(WebSocketClient):
    def __init__(self, owner):
        super().__init__(
            "ws://127.0.0.1:5002/",
            headers=[("Remote-User", "anonymous"), ("Remote-Role", "admin")],
        )
        self.owner = owner
        self.job_id = owner.job_id
        self.sock.settimeout(5)

    def received_message(self, message):
        if self.owner.job_id != self.job_id:
            return
        try:
            envelope = json.loads(message.data)
        except (ValueError, UnicodeDecodeError):
            self.owner.fail("invalid_reference_message")
            return
        if not isinstance(envelope, dict):
            self.owner.fail("invalid_reference_envelope")
            return
        try:
            self.owner.reference_message(envelope, time.time())
        except (ValueError, TypeError):
            self.owner.fail("invalid_reference_payload")

    def closed(self, code, reason=None):
        if self.owner.job_id == self.job_id and not self.owner.stop_event.is_set():
            self.owner.fail("reference_websocket_closed")


class TemporaryTimingTuner:
    """Own one bounded calibration; a separate watchdog enforces decoder expiry."""

    def __init__(self, cameras):
        self.cameras = cameras
        self.lock = threading.RLock()
        self._reset()

    def _reset(self):
        self.stop_event = threading.Event()
        self.thread = None
        self.phase = "idle"
        self.error = None
        self.camera = None
        self.job_id = None
        self.started_at = None
        self.expires_at = None
        self.deadline = None
        self.reference = []
        self.native = []
        self.native_open = {}
        self.object_open = {}
        self.motion_start = None
        self.notifications = set()
        self.topics = set()
        self.reference_ready_at = None
        self.other_cameras_decoded = None
        self.reference_last_sample = None
        self.reference_mode = None
        self.owns_lease = False

    def start(self, camera, duration_seconds):
        """Start only an image-authorized real camera, never a duplicate or all cameras."""
        options = timing_calibration_options()
        if (
            options is None
            or camera != options.camera
            or camera not in self.cameras
            or not self.cameras[camera].enabled
            or not self.cameras[camera].detect.enabled
            or not 30 <= duration_seconds <= options.duration_seconds
        ):
            raise ValueError(
                "This camera or duration is not authorized for calibration"
            )
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                raise RuntimeError("A temporary timing calibration is already running")
            self._reset()
            self.camera = camera
            self.job_id = secrets.token_hex(16)
            self.phase = "preparing"
            self.thread = threading.Thread(
                target=self._run,
                args=(options, duration_seconds),
                name="temporary-timing-tuner",
                daemon=True,
            )
            self.thread.start()
            return self.snapshot()

    def fail(self, code):
        """Fail explicitly and cancel the temporary reference instead of inventing timers."""
        with self.lock:
            self.error = code
        logger.error("Temporary timing calibration failed (%s)", code)
        self.stop_event.set()

    @staticmethod
    def _request(path, body=None):
        headers = {
            "remote-user": "anonymous",
            "remote-role": "admin",
            "X-CSRF-TOKEN": "1",
        }
        data = None if body is None else json.dumps(body).encode()
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            "http://127.0.0.1:5000/api/" + path,
            data=data,
            headers=headers,
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)

    def reference_message(self, envelope, received):
        """Retain only selected-camera timing, never images, boxes, or sublabels."""
        with self.lock:
            if (
                self.phase != "collecting"
                or self.stop_event.is_set()
                or (self.deadline is not None and time.monotonic() >= self.deadline)
            ):
                return
            topic, payload = envelope.get("topic"), envelope.get("payload")
            if topic == f"{self.camera}/motion" and payload in ("ON", "OFF"):
                if payload == "ON" and self.motion_start is None:
                    self.motion_start = received
                elif payload == "OFF" and self.motion_start is not None:
                    self.reference.append(
                        {
                            "kind": "motion",
                            "start": self.motion_start,
                            "end": max(
                                self.motion_start,
                                received
                                - self.cameras[self.camera].motion.mqtt_off_delay,
                            ),
                        }
                    )
                    self.motion_start = None
            elif topic == "events":
                value = json.loads(payload) if isinstance(payload, str) else payload
                if not isinstance(value, dict) or not isinstance(
                    value.get("after"), dict
                ):
                    self.fail("invalid_reference_event")
                    return
                after = value.get("after", {})
                if after.get("camera") != self.camera or after.get("false_positive"):
                    return
                identity = after.get("id")
                if not isinstance(identity, str) or not identity:
                    self.fail("invalid_reference_identity")
                    return
                identity = hashlib.sha256(identity.encode()).hexdigest()[:16]
                started = after.get("start_time")
                ended = after.get("end_time")
                frame_time = after.get("frame_time", received)
                if not all(
                    isinstance(item, (int, float)) and math.isfinite(item)
                    for item in (started, frame_time)
                ):
                    self.fail("invalid_reference_timestamp")
                    return
                if started < self.reference_ready_at:
                    return
                if value.get("type") in ("new", "update"):
                    self.object_open[identity] = started
                elif value.get("type") == "end":
                    if identity in self.object_open:
                        end = ended if isinstance(ended, (int, float)) else frame_time
                        if not math.isfinite(end) or end < started:
                            self.fail("invalid_reference_end")
                            return
                        self.reference.append(
                            {"kind": "object", "start": started, "end": end}
                        )
                        self.object_open.pop(identity)
            if len(self.reference) > 2000 or len(self.object_open) > 200:
                self.fail("reference_capacity_exceeded")

    def observe_native(self, item):
        """Ignore subscription initialization and pair only observed true/false changes."""
        key = (item["topic"], item["received_at"])
        if key in self.notifications:
            return
        self.notifications.add(key)
        self.topics.add(item["topic"])
        if item.get("kind") != "motion" or item.get("operation") != "Changed":
            return
        channel = hashlib.sha256(
            (item["topic"] + item["source_key"]).encode()
        ).hexdigest()[:16]
        if item["active"]:
            self.native_open.setdefault(channel, item["received_at"])
        elif channel in self.native_open:
            self.native.append(
                {
                    "channel": channel,
                    "start": self.native_open.pop(channel),
                    "end": item["received_at"],
                }
            )
        if len(self.notifications) > 5000 or len(self.native) > 2000:
            self.fail("camera_event_capacity_exceeded")

    def snapshot(self):
        """Expose bounded progress and evidence-qualified timing recommendations."""
        with self.lock:
            return {
                "camera": self.camera,
                "job_id": self.job_id,
                "phase": self.phase,
                "error": self.error,
                "started_at": self.started_at,
                "expires_at": self.expires_at,
                "seconds_remaining": max(0, self.deadline - time.monotonic())
                if self.deadline is not None
                and self.phase in ("warming_up", "collecting")
                else 0,
                "notifications_received": len(self.notifications),
                "topics": sorted(self.topics),
                "closed_reference_objects": sum(
                    item["kind"] == "object" for item in self.reference
                ),
                "closed_reference_motion": sum(
                    item["kind"] == "motion" for item in self.reference
                ),
                "closed_camera_motion": len(self.native),
                "truncated_reference_objects": len(self.object_open),
                "open_camera_alarms": len(self.native_open),
                "timing": timing_recommendations(self.reference, self.native)
                if self.error is None
                else {"status": "invalid_run", "channels": {}, "timers_applied": False},
                "clock": "NVR receipt and capture clock",
                "original_motion_off_delay_removed": self.cameras[
                    self.camera
                ].motion.mqtt_off_delay
                if self.camera
                else None,
                "other_cameras_decoded": self.other_cameras_decoded,
                "reference_last_sample": self.reference_last_sample,
                "reference_mode": self.reference_mode,
                "detector_settings_changed": self.reference_mode == "temporary_decoder",
            }

    def _save(self):
        REPORT_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = REPORT_ROOT / f"{self.job_id}.json"
        temporary = target.with_suffix(".part")
        with self.lock:
            data = {
                **self.snapshot(),
                "reference_episodes": self.reference,
                "camera_episodes": self.native,
            }
            temporary.write_text(json.dumps(data, indent=2))
        os.replace(temporary, target)

    def _run(self, options, duration):
        self.reference_mode = options.reference_mode
        endpoint = f"onvif/cameras/{self.camera}/events"
        socket = None
        subscription_started = False
        try:
            state = self._request(endpoint)
            if state["state"] != "stopped":
                raise RuntimeError("An existing camera event subscription is active")
            socket = _ReferenceSocket(self)
            socket.connect()
            socket.sock.settimeout(None)
            self._request(
                endpoint,
                {
                    "enabled": True,
                    "confirm_device_change": True,
                    "integrate_events": False,
                },
            )
            subscription_started = True
            ready_deadline = time.monotonic() + 15
            while self._request(endpoint)["state"] != "subscribed":
                if self.stop_event.wait(0.5) or time.monotonic() >= ready_deadline:
                    raise RuntimeError("Camera event subscription did not become ready")
            started = time.monotonic()
            if self.stop_event.is_set():
                self.phase = "cancelled"
                return
            self.started_at = time.time()
            self.deadline = started + duration
            self.expires_at = self.started_at + duration
            if options.reference_mode == "temporary_decoder":
                lease = TimingCalibrationLease(
                    camera=self.camera,
                    job_id=options.job_id,
                    started_monotonic=started,
                    expires_monotonic=self.deadline,
                )
                temporary = TIMING_CONTROL_PATH.with_suffix(".part")
                temporary.write_text(lease.model_dump_json())
                os.replace(temporary, TIMING_CONTROL_PATH)
                self.owns_lease = True
            self.phase = "warming_up"
            last_save = 0
            last_clock = (time.time(), time.monotonic())
            while not self.stop_event.wait(0.25) and time.monotonic() < self.deadline:
                now, monotonic = time.time(), time.monotonic()
                if abs((now - last_clock[0]) - (monotonic - last_clock[1])) > 0.5:
                    raise RuntimeError("Clock changed during timing calibration")
                last_clock = (now, monotonic)
                state = self._request(endpoint)
                if state["state"] != "subscribed":
                    raise RuntimeError("Camera event subscription was interrupted")
                with self.lock:
                    for item in state["events"]:
                        self.observe_native(item)
                if self.phase == "warming_up" or monotonic - last_save >= 5:
                    stats = self._request("stats")["cameras"]
                    self.other_cameras_decoded = any(
                        camera != self.camera
                        and values.get("ffmpeg_pid") not in (0, None)
                        for camera, values in stats.items()
                    )
                    if (
                        self.other_cameras_decoded
                        and options.reference_mode == "temporary_decoder"
                    ):
                        raise RuntimeError("Unexpected decoder on another camera")
                    reference = stats[self.camera]
                    self.reference_last_sample = {
                        key: reference.get(key)
                        for key in (
                            "camera_fps",
                            "process_fps",
                            "detection_fps",
                            "detection_enabled",
                            "ffmpeg_pid",
                        )
                    }
                    if self.phase == "collecting" and not (
                        reference["camera_fps"] > 1
                        and reference["process_fps"] > 1
                        and reference["detection_enabled"]
                    ):
                        raise RuntimeError(
                            "Original detector reference was interrupted"
                        )
                    if self.phase == "warming_up":
                        if (
                            reference["camera_fps"] > 1
                            and reference["process_fps"] > 1
                            and reference["detection_enabled"]
                        ):
                            self.reference_ready_at = time.time()
                            self.phase = "collecting"
                        elif monotonic - started > 45:
                            raise RuntimeError("Original detector did not become ready")
                    self._save()
                    last_save = monotonic
            self.phase = (
                "failed"
                if self.error
                else "cancelled"
                if self.stop_event.is_set()
                else "complete"
            )
        except (OSError, ValueError, RuntimeError) as error:
            self.fail(type(error).__name__)
            logger.exception("Temporary timing tuner could not complete")
            self.phase = "failed"
        finally:
            if self.phase not in ("complete", "cancelled", "failed"):
                self.phase = "failed"
                self.error = self.error or "calibration_interrupted"
                logger.error("Temporary timing calibration stopped before completion")
            if self.owns_lease:
                TIMING_CONTROL_PATH.unlink(missing_ok=True)
                self.owns_lease = False
            self.stop_event.set()
            if socket is not None:
                socket.close()
            if subscription_started:
                try:
                    self._request(
                        endpoint,
                        {
                            "enabled": False,
                            "confirm_device_change": True,
                            "integrate_events": False,
                        },
                    )
                except OSError:
                    self.error = "camera_subscription_cleanup_failed"
                    logger.exception("Temporary timing subscription cleanup failed")
            self._save()

    def close(self):
        """Cancel calibration; clearing the lease restores decoder-disabled capture."""
        self.stop_event.set()
        if self.thread is not None:
            if self.owns_lease:
                TIMING_CONTROL_PATH.unlink(missing_ok=True)
                self.owns_lease = False
            self.thread.join(timeout=15)
            if self.thread.is_alive():
                logger.error("Temporary timing tuner exceeded its shutdown deadline")

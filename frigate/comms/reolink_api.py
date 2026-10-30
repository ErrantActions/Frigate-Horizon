"""Native Reolink alarm-state ingestion without video decoding or invented confidence."""

import asyncio
import json
import logging
import math
import os
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

import cv2
import httpx
import numpy as np
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)
ACTIVATION_PATH = Path("/opt/frigate/reolink-native.json")
STATE_PATH = Path("/dev/shm/reolink-native-status.json")
LABELS = {"people": "person", "vehicle": "vehicle", "dog_cat": "animal"}


class _PrivateReolinkHttpLog(logging.Filter):
    def filter(self, record):
        if "/cgi-bin/api.cgi" in record.getMessage():
            record.msg = "Reolink HTTP API request details redacted"
            record.args = ()
        return True


logging.getLogger("httpx").addFilter(_PrivateReolinkHttpLog())


class NativeApiActivation(BaseModel):
    """Explicitly selected camera identities; other cameras retain their original detector."""

    model_config = ConfigDict(extra="forbid")
    cameras: set[str] = Field(min_length=1)


@lru_cache(maxsize=1)
def activation() -> NativeApiActivation | None:
    """Never opt unlisted or future cameras into an unverified native API."""
    try:
        raw = ACTIVATION_PATH.read_bytes()
    except FileNotFoundError:
        return None
    return NativeApiActivation.model_validate_json(raw)


def native_api_selected(camera: str | None) -> bool:
    """Separate camera-side detection from ordinary Frigate object inference."""
    settings = activation()
    return settings is not None and camera in settings.cameras


class ReolinkApiError(RuntimeError):
    """Only command names and numeric error codes may enter logs."""

    def __init__(self, command, code):
        super().__init__(f"Reolink {command} failed ({code})")
        self.code = code


def parse_alarm_states(motion: dict, ai: dict) -> dict[str, bool]:
    """Require complete supported boolean states; unknown is never quiet."""
    state = motion.get("state")
    if type(state) is not int or state not in (0, 1):
        raise ValueError("Invalid Reolink motion state")
    if ai.get("channel") != 0:
        raise ValueError("Unexpected Reolink physical channel")
    result = {"motion": bool(state)}
    for key, label in LABELS.items():
        value = ai.get(key)
        if not isinstance(value, dict) or type(value.get("support")) is not int:
            raise ValueError("Missing Reolink AI capability")
        if value["support"] not in (0, 1):
            raise ValueError("Invalid Reolink AI support flag")
        if value["support"] == 0:
            continue
        alarm = value.get("alarm_state")
        if type(alarm) is not int or alarm not in (0, 1):
            raise ValueError("Invalid Reolink AI alarm state")
        result[label] = bool(alarm)
    if "person" not in result or "vehicle" not in result:
        raise ValueError("Reolink person and vehicle states are required")
    return result


class NativeAlarmClient:
    """A token-owned bounded HTTP session with explicit expiry and no credential logging."""

    def __init__(self, connection, client):
        self.connection = connection
        self.client = client
        parsed = urlsplit(
            connection.host if "://" in connection.host else "//" + connection.host
        )
        host = parsed.hostname
        if not host:
            raise ValueError("Reolink camera host is unavailable")
        host = f"[{host}]" if ":" in host else host
        self.endpoint = f"{parsed.scheme or 'http'}://{host}/cgi-bin/api.cgi"
        self.token = None
        self.expires = 0.0

    async def command(self, command, param=None):
        params = {"cmd": command, "token": self.token or "null"}
        payload = {"cmd": command, "action": 0}
        if param is not None:
            payload["param"] = param
        response = await self.client.post(self.endpoint, params=params, json=[payload])
        if response.status_code != 200:
            raise ReolinkApiError(command, response.status_code)
        if len(response.content) > 1024 * 1024:
            raise ValueError("Reolink response exceeds its limit")
        data = response.json()
        if (
            not isinstance(data, list)
            or len(data) != 1
            or not isinstance(data[0], dict)
            or data[0].get("cmd") != command
        ):
            raise ValueError("Invalid Reolink response envelope")
        item = data[0]
        if item.get("code") != 0:
            raise ReolinkApiError(command, item.get("error", {}).get("rspCode"))
        value = item.get("value", {})
        if not isinstance(value, dict):
            raise ValueError("Invalid Reolink response value")
        return value

    async def login(self):
        """Authenticate once and keep the token private to this camera session."""
        value = await self.command(
            "Login",
            {
                "User": {
                    "userName": self.connection.username,
                    "password": self.connection.password.get_secret_value(),
                }
            },
        )
        token = value.get("Token", {})
        name, lease = token.get("name"), token.get("leaseTime", token.get("expire"))
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= 256
            or not name.isascii()
            or not name.isalnum()
            or type(lease) not in (int, float)
            or not math.isfinite(lease)
            or lease <= 10
        ):
            raise ValueError("Invalid Reolink login lease")
        self.token = name
        self.expires = time.monotonic() + min(lease * 0.8, 1800)

    async def read(self):
        """Read both motion and AI state without treating command failures as false."""
        for attempt in range(2):
            if self.token is None or time.monotonic() >= self.expires:
                if self.token is not None:
                    await self.logout()
                await self.login()
            try:
                motion = await self.command("GetMdState", {"channel": 0})
                ai = await self.command("GetAiState", {"channel": 0})
            except ReolinkApiError as error:
                if error.code != -6 or attempt:
                    raise
                self.token = None
                continue
            return parse_alarm_states(motion, ai)
        raise RuntimeError("Reolink authentication could not be renewed")

    async def logout(self):
        """Release the owned session; report logout failures without leaking tokens."""
        if self.token is None:
            return
        try:
            await self.command("Logout")
        finally:
            self.token = None
            self.expires = 0

    async def snapshot(self):
        """Fetch a camera-encoded still only at alarm onset, never decode its video stream."""
        if self.token is None:
            raise RuntimeError("Reolink snapshot session is not authenticated")
        response = await self.client.get(
            self.endpoint,
            params={
                "cmd": "Snap",
                "channel": 0,
                "token": self.token,
                "width": 640,
                "height": 360,
            },
        )
        if response.status_code != 200 or not response.content.startswith(b"\xff\xd8"):
            raise ValueError("Camera did not return a JPEG snapshot")
        if len(response.content) > 8 * 1024 * 1024:
            raise ValueError("Camera snapshot exceeds its limit")
        return response.content


def save_alarm_snapshot(camera, identities, jpeg):
    """Create event thumbnails from one camera JPEG, not decoded video frames."""
    from frigate.const import CLIPS_DIR, THUMB_DIR

    image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.shape[0] > 4320 or image.shape[1] > 7680:
        raise ValueError("Invalid native alarm snapshot")
    height = image.shape[0]
    thumbnail = cv2.resize(
        image,
        (max(1, round(image.shape[1] * 175 / height)), 175),
        interpolation=cv2.INTER_AREA,
    )
    success, encoded = cv2.imencode(".webp", thumbnail)
    if not success:
        raise ValueError("Unable to encode native alarm thumbnail")
    clean, full = cv2.imencode(".webp", image)
    if not clean:
        raise ValueError("Unable to encode native alarm image")
    folder = Path(THUMB_DIR) / camera
    folder.mkdir(parents=True, exist_ok=True)
    for identity in identities.values():
        for target, content in (
            (folder / f"{identity}.webp", encoded.tobytes()),
            (Path(CLIPS_DIR) / f"{camera}-{identity}-clean.webp", full.tobytes()),
        ):
            temporary = target.with_suffix(".part")
            temporary.write_bytes(content)
            os.replace(temporary, target)


@dataclass
class AlarmEvent:
    event_id: str
    started_at: float
    last_seen: float
    refreshed: float


class NativeAlarmBridge:
    """Camera-level alarm episodes, not boxes, tracks, confidence, or Frigate verification."""

    def __init__(self, camera, publish, *, reference=False):
        self.camera = camera
        self.publish = publish
        self.reference = reference
        self.active: dict[str, AlarmEvent] = {}
        self.previous = None
        self.history: deque[dict[str, object]] = deque(maxlen=300)
        self.starts = 0
        self.ends = 0

    def observe(self, states, now, identities=None, snapshot_available=False):
        """Publish native labels and refresh a separate consumer-side crash expiry."""
        for label, active in states.items():
            if self.previous is not None and self.previous.get(label) != active:
                self.history.append(
                    {"label": label, "active": active, "received_at": now}
                )
        self.previous = states.copy()
        if self.reference:
            return
        # Generic motion remains motion, never a made-up object classification.
        for label in ("person", "vehicle", "animal"):
            current = self.active.get(label)
            if states.get(label) is True:
                if current is None:
                    identity = (
                        identities[label]
                        if identities is not None and label in identities
                        else f"{now:.6f}-{secrets.token_hex(3)}"
                    )
                    self.publish(
                        {
                            "action": "start",
                            "camera": self.camera,
                            "label": label,
                            "event_id": identity,
                            "timestamp": now,
                            "expiry_seconds": 15,
                            "confidence": None,
                            "source": "reolink",
                            "snapshot_available": label in snapshot_available
                            if isinstance(snapshot_available, set)
                            else snapshot_available,
                        },
                        "reolink_event",
                    )
                    self.active[label] = AlarmEvent(identity, now, now, now)
                    self.starts += 1
                else:
                    current.last_seen = now
                    if now - current.refreshed >= 3:
                        self.publish(
                            {
                                "action": "refresh",
                                "camera": self.camera,
                                "event_id": current.event_id,
                                "timestamp": now,
                                "expiry_seconds": 15,
                            },
                            "reolink_event",
                        )
                        current.refreshed = now
            elif current is not None and now - current.last_seen >= 1:
                self._end(label, now)

    def _end(self, label, now):
        current = self.active[label]
        self.publish(
            {
                "action": "end",
                "camera": self.camera,
                "event_id": current.event_id,
                "timestamp": max(current.started_at, now),
            },
            "reolink_event",
        )
        self.active.pop(label)
        self.ends += 1

    def close(self, now):
        """Bound event lifetime on disconnect; transport failure is not quiet."""
        for label in list(self.active):
            self._end(label, now)
        self.previous = None


class ReolinkEventManager:
    """Independent native camera sessions; no video processing or silent decode fallback."""

    def __init__(self, config_provider):
        from frigate.motion.native_person import NativePersonController

        self.config_provider = config_provider
        self.person_analysis = NativePersonController(config_provider)
        self.stop_event = threading.Event()
        self.thread = None
        self.lock = threading.Lock()
        self.statuses = {}
        self.bridges = {}
        self.publisher = None
        self.reference_tuner = None

    def start(self):
        """Start only explicitly activated native API cameras."""
        self.person_analysis.initialize()
        if activation() is None:
            return
        self.thread = threading.Thread(
            target=lambda: asyncio.run(self._run()),
            name="reolink-native-api",
            daemon=True,
        )
        self.thread.start()

    def snapshot(self, camera):
        """Expose health and bounded alarm timing without credentials or raw vendor payloads."""
        with self.lock:
            state = dict(self.statuses.get(camera, {"state": "starting"}))
            bridge = self.bridges.get(camera)
            state["history"] = list(bridge.history) if bridge else []
            state["active_labels"] = sorted(bridge.active) if bridge else []
            state["events_started"] = bridge.starts if bridge else 0
            state["events_ended"] = bridge.ends if bridge else 0
        updated = state.get("updated_at")
        if state["state"] == "running" and (
            not isinstance(updated, (int, float)) or time.time() - updated > 5
        ):
            state["state"] = "stale"
        result = {
            "camera": camera,
            **state,
            "source": "reolink_http_api",
            "confidence_available": False,
            "video_decoded": False,
            "poll_interval_seconds": 0.5,
            "person_analysis": self.person_analysis.status(camera),
            "short_events_can_be_missed": True,
        }
        if self.reference_tuner is not None and camera == self.reference_tuner.camera:
            result["timing_reference"] = self.reference_tuner.snapshot()
        return result

    async def _reference(self, options):
        from frigate.motion.record_only import TIMING_CONTROL_PATH
        from frigate.motion.timing_calibration import (
            TemporaryTimingTuner,
            _ReferenceSocket,
        )

        tuner = TemporaryTimingTuner(self.config_provider().cameras)
        self.reference_tuner = tuner
        tuner.camera = options.camera
        tuner.job_id = secrets.token_hex(16)
        tuner.reference_mode = "temporary_decoder"
        tuner.started_at = time.time()
        tuner.expires_at = tuner.started_at + options.duration_seconds
        tuner.deadline = time.monotonic() + options.duration_seconds
        tuner.phase = "warming_up"
        socket = None
        try:
            socket = _ReferenceSocket(tuner)
            await asyncio.to_thread(socket.connect)
            socket.sock.settimeout(None)
            async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                while (
                    not self.stop_event.is_set() and time.monotonic() < tuner.deadline
                ):
                    response = await client.get(
                        "http://127.0.0.1:5000/api/stats",
                        headers={"remote-user": "anonymous", "remote-role": "admin"},
                    )
                    if response.status_code != 200:
                        raise RuntimeError("Timing reference stats unavailable")
                    stats = response.json()["cameras"]
                    ref = stats[options.camera]
                    tuner.reference_last_sample = {
                        key: ref.get(key)
                        for key in (
                            "camera_fps",
                            "process_fps",
                            "detection_fps",
                            "ffmpeg_pid",
                        )
                    }
                    if (
                        tuner.phase == "warming_up"
                        and ref["camera_fps"] >= 3
                        and ref["process_fps"] >= 3
                    ):
                        tuner.phase = "collecting"
                        tuner.reference_ready_at = time.time()
                    if (
                        tuner.phase == "warming_up"
                        and time.time() - tuner.started_at > 45
                    ):
                        raise RuntimeError(
                            "Original timing detector did not become ready"
                        )
                    if tuner.stop_event.is_set():
                        raise RuntimeError(
                            "Original reference websocket was interrupted"
                        )
                    with self.lock:
                        bridge = self.bridges.get(options.camera)
                        state = self.statuses.get(options.camera, {})
                        if state.get("state") == "error":
                            raise RuntimeError(
                                "Native HTTP timing observations interrupted"
                            )
                        history = list(bridge.history) if bridge else []
                    with tuner.lock:
                        for item in history:
                            if item["label"] == "motion":
                                tuner.observe_native(
                                    {
                                        "topic": "reolink/http/motion",
                                        "source_key": "channel0",
                                        "operation": "Changed",
                                        "kind": "motion",
                                        "active": item["active"],
                                        "received_at": item["received_at"],
                                    }
                                )
                    await asyncio.to_thread(tuner._save)
                    await asyncio.sleep(2)
            tuner.phase = "cancelled" if self.stop_event.is_set() else "complete"
        except (OSError, ValueError, RuntimeError, httpx.HTTPError):
            tuner.fail("http_reference_interrupted")
            tuner.phase = "failed"
            logger.exception("Temporary native HTTP timing comparison failed")
        finally:
            # This reference owns the only exception to normal decoder-disabled capture.
            TIMING_CONTROL_PATH.unlink(missing_ok=True)
            tuner.stop_event.set()
            if socket is not None:
                socket.close()
            await asyncio.to_thread(tuner._save)

    def _publish(self, payload, topic):
        import zmq

        if self.publisher is None:
            raise RuntimeError("Native event publisher is not available")
        if topic == "reolink_event" and self.person_analysis.consume(payload):
            return
        try:
            self.publisher.publish(payload, topic)
        except zmq.ZMQError:
            raise RuntimeError("Native Reolink event signal bus failed") from None

    async def _camera(self, name):
        from frigate.ptz.device_manager import connection_for

        failures = 0
        async with httpx.AsyncClient(
            timeout=3,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            while not self.stop_event.is_set():
                config = self.config_provider()
                camera = config.cameras.get(name)
                if (
                    camera is None
                    or not camera.enabled
                    or (
                        not camera.detect.enabled and not camera.snapshots.native_person
                    )
                ):
                    with self.lock:
                        self.statuses[name] = {"state": "disabled"}
                    await asyncio.sleep(0.5)
                    continue
                try:
                    connection = connection_for(config, name)
                except ValueError:
                    with self.lock:
                        self.statuses[name] = {
                            "state": "error",
                            "error": "saved_camera_connection_unavailable",
                        }
                    logger.error(
                        "Saved native Reolink API connection unavailable for %s", name
                    )
                    await asyncio.sleep(5)
                    continue
                api = NativeAlarmClient(connection, client)
                reference = self._reference_active(name)
                bridge = self.bridges.setdefault(
                    name, NativeAlarmBridge(name, self._publish, reference=reference)
                )
                with self.lock:
                    self.statuses[name] = {"state": "connecting", "failures": failures}
                try:
                    # Let the local IPC subscription settle before the initial true state.
                    await asyncio.sleep(0.25)
                    while not self.stop_event.is_set():
                        current_config = self.config_provider()
                        current = current_config.cameras.get(name)
                        if (
                            current is None
                            or not current.enabled
                            or (
                                not current.detect.enabled
                                and not current.snapshots.native_person
                            )
                            or connection_for(current_config, name) != connection
                        ):
                            break
                        states = await api.read()
                        reference = self._reference_active(name)
                        now = time.time()
                        identities = (
                            {
                                label: f"{now:.6f}-{secrets.token_hex(3)}"
                                for label in ("person", "vehicle", "animal")
                                if states.get(label) is True
                                and label not in bridge.active
                            }
                            if not reference
                            else {}
                        )
                        snapshot_available = False
                        snapshot_identities = {
                            label: identity
                            for label, identity in identities.items()
                            if label != "person"
                            or self.person_analysis.status(name)["state"] != "ready"
                        }
                        if snapshot_identities:
                            try:
                                jpeg = await api.snapshot()
                                await asyncio.to_thread(
                                    save_alarm_snapshot, name, snapshot_identities, jpeg
                                )
                                snapshot_available = set(snapshot_identities)
                            except (httpx.HTTPError, ValueError, OSError):
                                logger.warning(
                                    "Reolink alarm snapshot unavailable for %s; event has no snapshot",
                                    name,
                                )
                        with self.lock:
                            if bridge.reference != reference:
                                bridge.close(now)
                                bridge.reference = reference
                            bridge.observe(states, now, identities, snapshot_available)
                            self.statuses[name] = {
                                "state": "running",
                                "updated_at": now,
                                "failures": failures,
                                "states": states,
                                "timing_reference": reference,
                            }
                        failures = 0
                        await asyncio.sleep(0.5)
                except (httpx.HTTPError, ValueError, RuntimeError) as error:
                    failures += 1
                    with self.lock:
                        self.statuses[name] = {
                            "state": "error",
                            "failures": failures,
                            "error": type(error).__name__,
                        }
                    logger.error(
                        "Reolink API unavailable for %s (%s); no automatic decoder fallback",
                        name,
                        type(error).__name__,
                    )
                finally:
                    with self.lock:
                        bridge.close(time.time())
                    try:
                        await api.logout()
                    except (httpx.HTTPError, ValueError, RuntimeError):
                        logger.warning(
                            "Unable to close Reolink API session for %s", name
                        )
                await asyncio.sleep(min(5, failures or 0.25))

    @staticmethod
    def _reference_active(name):
        from frigate.motion.record_only import timing_calibration_active

        return timing_calibration_active(name)

    async def _run(self):
        import zmq

        from frigate.comms.event_metadata_updater import EventMetadataPublisher
        from frigate.motion.record_only import (
            TIMING_CONTROL_PATH,
            TimingCalibrationLease,
            timing_calibration_options,
        )

        options = timing_calibration_options()
        if options is not None and options.reference_mode == "temporary_decoder":
            started = time.monotonic()
            lease = TimingCalibrationLease(
                camera=options.camera,
                job_id=options.job_id,
                started_monotonic=started,
                expires_monotonic=started + options.duration_seconds,
            )
            temporary = TIMING_CONTROL_PATH.with_suffix(".part")
            temporary.write_text(lease.model_dump_json())
            os.replace(temporary, TIMING_CONTROL_PATH)
        self.publisher = EventMetadataPublisher()
        tasks = []
        try:
            settings = activation()
            if settings is None:
                return
            tasks = [
                asyncio.create_task(self._camera(name))
                for name in sorted(settings.cameras)
            ]
            reference_task = (
                asyncio.create_task(self._reference(options))
                if options is not None and options.reference_mode == "temporary_decoder"
                else None
            )
            while not self.stop_event.is_set():
                self.person_analysis.sync()
                failed = [task for task in tasks if task.done()]
                if failed:
                    for task in failed:
                        task.result()
                    raise RuntimeError("Native Reolink worker stopped unexpectedly")
                payload = {
                    name: self.snapshot(name) for name in sorted(settings.cameras)
                }
                temporary = STATE_PATH.with_suffix(".part")
                temporary.write_text(json.dumps(payload))
                os.replace(temporary, STATE_PATH)
                await asyncio.sleep(0.5)
        except (OSError, RuntimeError, ValueError, zmq.ZMQError):
            logger.exception("Native Reolink event manager failed")
            self.stop_event.set()
        finally:
            self.person_analysis.close()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if "reference_task" in locals() and reference_task is not None:
                await reference_task
            self.publisher.stop()
            self.publisher = None
            if options is not None and options.reference_mode == "temporary_decoder":
                TIMING_CONTROL_PATH.unlink(missing_ok=True)

    def close(self):
        """Stop polling and close all camera-reported event episodes."""
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=12)
            if self.thread.is_alive():
                raise RuntimeError("Native Reolink event manager did not stop")

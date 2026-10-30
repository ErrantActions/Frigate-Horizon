"""Opt-in weather polling and serialized per-camera NVR profile application."""

import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from math import isfinite
from multiprocessing.synchronize import Event as MpEvent
from pathlib import Path

import httpx
import zmq

from frigate.comms.environment import GlareSignalReceiver
from frigate.config import FrigateConfig
from frigate.config.profile_manager import ProfileManager
from frigate.const import CACHE_DIR, REPLAY_CAMERA_PREFIX
from frigate.environment.messages import GlareSignal, glare_configuration_hash
from frigate.environment.profiles import ProfilePlan, ProfileSelector
from frigate.environment.weather import (
    OpenMeteoClient,
    WeatherDataError,
    WeatherLocationError,
    WeatherProviderError,
    WeatherSnapshot,
)
from frigate.motion.adaptive_runtime import atomic_json

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GlareSensorStatus:
    enabled: bool
    state: str
    last_frame_time: float | None = None
    last_event_time: float | None = None
    zones: tuple[str, ...] = ()
    error_code: str | None = None
    lighting_transition: bool | None = None


@dataclass(frozen=True)
class CameraEnvironmentStatus:
    camera: str
    phase: str
    desired: ProfilePlan
    applied_profiles: tuple[str, ...] = ()
    blocked_fields: tuple[str, ...] = ()
    error_code: str | None = None
    updated_at: datetime | None = None
    glare: GlareSensorStatus = field(
        default_factory=lambda: GlareSensorStatus(False, "disabled")
    )


@dataclass(frozen=True)
class WeatherStatus:
    enabled: bool
    state: str
    data: WeatherSnapshot | None
    error_code: str | None
    next_refresh_seconds: float
    service_running: bool


class EnvironmentManager(threading.Thread):
    """Select NVR plans and delegate opted-in physical plans to the device owner."""

    def __init__(
        self,
        config: FrigateConfig,
        profile_manager: ProfileManager,
        stop_event: MpEvent,
        http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        super().__init__(name="environment_manager")
        self.config = config
        self.profile_manager = profile_manager
        self.stop_event = stop_event
        self._client_factory = http_client_factory or httpx.AsyncClient
        self._lock = threading.RLock()
        self._selectors = {
            name: ProfileSelector(camera.environment)
            for name, camera in config.cameras.items()
            if not name.startswith(REPLAY_CAMERA_PREFIX)
        }
        self._states: dict[str, CameraEnvironmentStatus] = {}
        self._glare_until: dict[str, float] = {}
        self._glare_sensor_states: dict[str, str] = {}
        self._glare_last_frame: dict[str, float] = {}
        self._glare_last_event: dict[str, float] = {}
        self._glare_last_zones: dict[str, tuple[str, ...]] = {}
        self._glare_errors: dict[str, str | None] = {}
        self._weather: WeatherSnapshot | None = None
        self._adaptive_weather_signature = ""
        self._weather_error: str | None = None
        self._next_refresh = 0.0
        self._weather_failures = 0
        self._generation = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._async_stop: asyncio.Event | None = None
        self._weather_wake: asyncio.Event | None = None
        self._profiles_wake: asyncio.Event | None = None
        self._weather_task: asyncio.Task[None] | None = None
        self._signals_task: asyncio.Task[None] | None = None
        self._stop_requested = threading.Event()
        self._service_started = False
        self.device_manager = None
        self._device_tasks: dict[str, asyncio.Task] = {}

    def update_config(self, config: FrigateConfig) -> None:
        """Invalidate obsolete work without blocking config saves on network I/O."""
        with self._lock:
            old = self.config
            self.config = config
            self._generation += 1
            selectors: dict[str, ProfileSelector] = {}
            for name, camera in tuple(config.cameras.items()):
                if name.startswith(REPLAY_CAMERA_PREFIX):
                    continue
                previous = old.cameras.get(name)
                if previous is not None and previous.environment == camera.environment:
                    selectors[name] = self._selectors.get(
                        name, ProfileSelector(camera.environment)
                    )
                else:
                    selectors[name] = ProfileSelector(camera.environment)
                    if (
                        previous is None
                        or not camera.environment.enabled
                        or previous.environment.glare != camera.environment.glare
                    ):
                        self._glare_until.pop(name, None)
                        self._glare_sensor_states.pop(name, None)
                        self._glare_last_frame.pop(name, None)
                        self._glare_errors.pop(name, None)
            self._selectors = selectors
            self._states = {
                name: state
                for name, state in self._states.items()
                if name in config.cameras
            }
            self._glare_until = {
                name: until
                for name, until in self._glare_until.items()
                if name in config.cameras
            }
            for state in (
                self._glare_sensor_states,
                self._glare_last_frame,
                self._glare_last_event,
                self._glare_last_zones,
                self._glare_errors,
            ):
                for name in list(state):
                    if name not in config.cameras:
                        state.pop(name)
            if (
                old.weather.enabled,
                old.weather.zip_code,
                old.weather.latitude,
                old.weather.longitude,
            ) != (
                config.weather.enabled,
                config.weather.zip_code,
                config.weather.latitude,
                config.weather.longitude,
            ):
                self._weather = None
                self._weather_error = None
                self._weather_failures = 0
            self._next_refresh = 0
        self._wake()

    def _wake(self) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        for event in (self._weather_wake, self._profiles_wake):
            if event is not None:
                loop.call_soon_threadsafe(event.set)

    async def refresh_weather(
        self,
        client: OpenMeteoClient,
        now: datetime,
        monotonic_now: float,
    ) -> None:
        """Fetch only when enabled/due, rejecting responses for superseded locations."""
        with self._lock:
            settings = self.config.weather
            fingerprint = settings.model_dump()
            if not settings.enabled:
                self._weather = None
                self._weather_error = None
                self._next_refresh = monotonic_now + 60
                return
            if monotonic_now < self._next_refresh:
                return
        if settings.zip_code is None and (
            settings.latitude is None or settings.longitude is None
        ):
            logger.error("Weather polling requires an explicitly configured location")
            with self._lock:
                self._weather_error = "weather_configuration_invalid"
                self._next_refresh = monotonic_now + 60
            return
        try:
            if settings.zip_code is not None:
                snapshot = await client.fetch_zip_code(settings.zip_code, now=now)
            else:
                assert settings.latitude is not None and settings.longitude is not None
                snapshot = await client.fetch(
                    settings.latitude, settings.longitude, now=now
                )
        except WeatherProviderError as error:
            with self._lock:
                if self.config.weather.model_dump() != fingerprint:
                    return
                self._weather_failures += 1
                self._weather_error = (
                    "weather_location_not_found"
                    if isinstance(error, WeatherLocationError)
                    else "weather_data_invalid"
                    if isinstance(error, WeatherDataError)
                    else "weather_rate_limited"
                    if error.status_code == 429
                    else "weather_provider_unavailable"
                )
                delay = max(
                    settings.refresh_seconds,
                    min(
                        settings.refresh_seconds
                        * (2 ** min(self._weather_failures, 5)),
                        3600,
                    ),
                )
                if error.status_code == 429:
                    delay = max(delay, 3600)
                self._next_refresh = monotonic_now + delay
            logger.warning("Modeled weather refresh failed (%s)", type(error).__name__)
            return
        with self._lock:
            if self.config.weather.model_dump() != fingerprint:
                return
            self._weather = snapshot
            self._weather_error = None
            self._weather_failures = 0
            self._next_refresh = monotonic_now + settings.refresh_seconds
        if self._profiles_wake is not None:
            self._profiles_wake.set()

    async def tick(self, now: datetime, monotonic_now: float) -> None:
        """Apply current desired layers per camera without waiting for weather requests."""
        with self._lock:
            config = self.config
            generation = self._generation
            selectors = dict(self._selectors)
            weather = self._weather
            leases = dict(self._glare_until)
            previous_states = dict(self._states)
            cameras = dict(config.cameras)
        if any(camera.motion.adaptive.enabled for camera in cameras.values()):
            fresh = (
                config.weather.enabled
                and weather is not None
                and not weather.is_stale(now, config.weather.stale_after_seconds)
            )
            context = {
                "state": "modeled" if fresh else "unknown",
                "observed_at": weather.model_time.timestamp() if weather else None,
                "gust_mps": weather.wind_gusts_mps if fresh else None,
                "direction_degrees": weather.wind_direction_degrees if fresh else None,
                "source": weather.provider if weather else None,
                "valid_until": (
                    weather.model_time.timestamp() + config.weather.stale_after_seconds
                    if fresh
                    else 0
                ),
                "learning_paused": bool(
                    fresh
                    and (
                        (
                            weather.visibility_m is not None
                            and weather.visibility_m < 500
                        )
                        or weather.weather_code in {95, 96, 99}
                    )
                ),
            }
            signature = json.dumps(context, sort_keys=True)
            if signature != getattr(self, "_adaptive_weather_signature", ""):
                try:
                    await asyncio.to_thread(
                        atomic_json,
                        Path(CACHE_DIR) / "adaptive-motion/weather.json",
                        context,
                    )
                except OSError:
                    logger.exception(
                        "Unable to publish modeled weather to adaptive motion"
                    )
                else:
                    self._adaptive_weather_signature = signature
        for name, camera in cameras.items():
            if name.startswith(REPLAY_CAMERA_PREFIX) or name not in selectors:
                continue
            if self.stop_event.is_set() or self._stop_requested.is_set():
                return
            plan = selectors[name].decide(
                now,
                monotonic_now,
                weather,
                config.weather.stale_after_seconds,
                manual_profile=camera.environment.manual_profile,
                glare_until=leases.get(name),
            )
            previous = previous_states.get(name)
            needs_clear = previous is not None and bool(previous.applied_profiles)
            if (
                not camera.environment.enabled
                and not needs_clear
                and not self.profile_manager.has_camera_profiles(name)
            ):
                state = CameraEnvironmentStatus(name, "disabled", plan, updated_at=now)
            else:
                error = await asyncio.to_thread(
                    self.profile_manager.apply_camera_profiles,
                    name,
                    plan.profiles,
                    expected_config=config,
                )
                if self.profile_manager.config is not config:
                    return
                applied = self.profile_manager.get_camera_profile_info(name)
                state = CameraEnvironmentStatus(
                    camera=name,
                    phase="error"
                    if error
                    else "applied"
                    if camera.environment.enabled
                    else "disabled",
                    desired=plan,
                    applied_profiles=tuple(applied["profiles"]),
                    blocked_fields=tuple(applied["blocked_fields"]),
                    error_code="profile_application_failed" if error else None,
                    updated_at=now,
                )
            with self._lock:
                if self._generation != generation:
                    return
                self._states[name] = state
            if self.device_manager is not None and state.error_code is None:
                previous_task = self._device_tasks.get(name)
                if previous_task is None or previous_task.done():
                    self._device_tasks[name] = asyncio.create_task(
                        self._apply_physical(name, plan.profiles, config)
                    )

    async def _apply_physical(self, name, profiles, config) -> None:
        try:
            await self.device_manager.automatic_image(name, profiles, config)
        except Exception:
            # A disconnected camera must not stop weather/NVR automation.
            # SDK exception text can contain credentials, so do not log it.
            logger.warning("Physical image automation unavailable for camera %s", name)

    def trigger_glare(self, camera_name: str, monotonic_now: float) -> bool:
        """Lease the configured glare profile for an enabled camera's transient event."""
        if not isfinite(monotonic_now) or monotonic_now < 0:
            logger.warning("Ignoring invalid glare event timestamp")
            return False
        with self._lock:
            camera = self.config.cameras.get(camera_name)
            if (
                camera_name.startswith(REPLAY_CAMERA_PREFIX)
                or camera is None
                or not camera.enabled
                or not camera.environment.enabled
                or not camera.environment.glare.enabled
            ):
                return False
            self._glare_until[camera_name] = max(
                self._glare_until.get(camera_name, 0),
                monotonic_now + camera.environment.glare.restore_after_seconds,
            )
        self._wake()
        return True

    def _lighting_transition(self, camera_name: str, now: datetime) -> bool | None:
        camera = self.config.cameras[camera_name]
        minutes = camera.environment.glare.transition_exclusion_minutes
        if minutes == 0:
            return False
        if self._weather is None:
            return None
        times = [
            value
            for day in self._weather.solar_days
            for value in (day.sunrise, day.sunset)
            if value is not None
        ]
        if not times or not any(
            abs((value - now).total_seconds()) < 86400 for value in times
        ):
            return None
        return any(abs(value - now) <= timedelta(minutes=minutes) for value in times)

    def receive_glare_signal(
        self, signal: GlareSignal, now: datetime, monotonic_now: float
    ) -> bool:
        """Accept only fresh signals matching the current camera's configured geometry."""
        with self._lock:
            camera = self.config.cameras.get(signal.camera)
            if (
                signal.camera.startswith(REPLAY_CAMERA_PREFIX)
                or camera is None
                or not camera.environment.enabled
                or not camera.environment.glare.enabled
            ):
                return False
            if signal.configuration_hash != glare_configuration_hash(camera):
                return False
            age = now.timestamp() - signal.frame_time
            if age > 15 or age < -5:
                logger.warning(
                    "Ignoring stale or future glare signal for camera %s", signal.camera
                )
                return False
            if any(zone not in camera.environment.glare.zones for zone in signal.zones):
                logger.warning(
                    "Ignoring glare signal with unknown zones for camera %s",
                    signal.camera,
                )
                return False
            self._glare_last_frame[signal.camera] = signal.frame_time
            self._glare_sensor_states[signal.camera] = (
                "unavailable" if signal.kind == "unavailable" else "ready"
            )
            self._glare_errors[signal.camera] = signal.error_code
            if (
                signal.kind != "glare"
                or self._lighting_transition(signal.camera, now) is True
            ):
                return False
            if self.trigger_glare(signal.camera, monotonic_now):
                self._glare_last_event[signal.camera] = signal.frame_time
                self._glare_last_zones[signal.camera] = signal.zones
                return True
            return False

    def camera_status(
        self, camera_name: str, now: datetime | None = None
    ) -> CameraEnvironmentStatus:
        """Return desired/applied state without exposing provider coordinates."""
        with self._lock:
            camera = self.config.cameras[camera_name]
            current = datetime.now(UTC) if now is None else now
            state = self._states.get(
                camera_name,
                CameraEnvironmentStatus(
                    camera_name,
                    "initializing" if camera.environment.enabled else "disabled",
                    ProfilePlan(
                        (), "initializing" if camera.environment.enabled else "disabled"
                    ),
                ),
            )
            enabled = camera.environment.enabled and camera.environment.glare.enabled
            sensor = self._glare_sensor_states.get(camera_name, "initializing")
            frame_time = self._glare_last_frame.get(camera_name)
            if not enabled:
                sensor = "disabled"
            elif not camera.enabled:
                sensor = "paused_camera"
            elif (
                sensor == "ready"
                and frame_time is not None
                and current.timestamp() - frame_time > 15
            ):
                sensor = "stale"
            if (
                self._service_started
                and not self.is_alive()
                and camera.environment.enabled
            ):
                state = replace(
                    state, phase="error", error_code="environment_service_stopped"
                )
            return replace(
                state,
                glare=GlareSensorStatus(
                    enabled=enabled,
                    state=sensor,
                    last_frame_time=frame_time,
                    last_event_time=self._glare_last_event.get(camera_name),
                    zones=self._glare_last_zones.get(camera_name, ()),
                    error_code=self._glare_errors.get(camera_name),
                    lighting_transition=self._lighting_transition(camera_name, current),
                ),
            )

    def camera_names(self) -> tuple[str, ...]:
        """Return configured camera identities for authorization-aware API filtering."""
        with self._lock:
            return tuple(
                name
                for name in tuple(self.config.cameras)
                if not name.startswith(REPLAY_CAMERA_PREFIX)
            )

    def all_camera_statuses(self) -> dict[str, CameraEnvironmentStatus]:
        """Snapshot configured camera states atomically across config replacement."""
        with self._lock:
            return {name: self.camera_status(name) for name in self.camera_names()}

    def weather_status(self, now: datetime, monotonic_now: float) -> WeatherStatus:
        """Report unavailable/stale/cached modeled data explicitly, never fabricated weather."""
        with self._lock:
            settings = self.config.weather
            snapshot = self._weather
            state = (
                "disabled"
                if not settings.enabled
                else "unavailable"
                if snapshot is None and self._weather_error
                else "initializing"
                if snapshot is None
                else "stale"
                if snapshot.is_stale(now, settings.stale_after_seconds)
                else "cached"
                if self._weather_error
                else "fresh"
            )
            return WeatherStatus(
                settings.enabled,
                state,
                snapshot,
                self._weather_error,
                max(0.0, self._next_refresh - monotonic_now),
                self.is_alive(),
            )

    async def _weather_loop(self, client: OpenMeteoClient) -> None:
        assert self._async_stop is not None and self._weather_wake is not None
        while not self._async_stop.is_set() and not self.stop_event.is_set():
            self._weather_wake.clear()
            await self.refresh_weather(client, datetime.now(UTC), time.monotonic())
            with self._lock:
                delay = max(1.0, self._next_refresh - time.monotonic())
            try:
                await asyncio.wait_for(self._weather_wake.wait(), delay)
            except TimeoutError:
                pass

    async def _profiles_loop(self) -> None:
        assert self._async_stop is not None and self._profiles_wake is not None
        try:
            while not self._async_stop.is_set() and not self.stop_event.is_set():
                self._profiles_wake.clear()
                await self.tick(datetime.now(UTC), time.monotonic())
                try:
                    await asyncio.wait_for(self._profiles_wake.wait(), 1)
                except TimeoutError:
                    pass
        finally:
            self._async_stop.set()
            if self._weather_task is not None:
                self._weather_task.cancel()
            if self._signals_task is not None:
                self._signals_task.cancel()

    async def _signals_loop(self) -> None:
        assert self._async_stop is not None
        receiver: GlareSignalReceiver | None = None
        try:
            while not self._async_stop.is_set() and not self.stop_event.is_set():
                with self._lock:
                    enabled = any(
                        camera.environment.enabled and camera.environment.glare.enabled
                        for name, camera in tuple(self.config.cameras.items())
                        if not name.startswith(REPLAY_CAMERA_PREFIX)
                    )
                if not enabled:
                    await asyncio.sleep(0.5)
                    continue
                if receiver is None:
                    try:
                        receiver = GlareSignalReceiver()
                    except zmq.ZMQError:
                        logger.exception("Local glare signal transport is unavailable")
                        with self._lock:
                            for name, camera in tuple(self.config.cameras.items()):
                                if camera.environment.glare.enabled:
                                    self._glare_sensor_states[name] = "unavailable"
                                    self._glare_errors[name] = (
                                        "glare_transport_unavailable"
                                    )
                        await asyncio.sleep(1)
                        continue
                signal = await receiver.receive()
                if signal is not None:
                    self.receive_glare_signal(
                        signal, datetime.now(UTC), time.monotonic()
                    )
        finally:
            if receiver is not None:
                receiver.stop()

    async def _serve(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._async_stop = asyncio.Event()
        if self._stop_requested.is_set():
            self._async_stop.set()
        self._weather_wake = asyncio.Event()
        self._profiles_wake = asyncio.Event()
        async with self._client_factory() as client:
            async with asyncio.TaskGroup() as tasks:
                self._weather_task = tasks.create_task(
                    self._weather_loop(OpenMeteoClient(client))
                )
                self._signals_task = tasks.create_task(self._signals_loop())
                tasks.create_task(self._profiles_loop())

    def run(self) -> None:
        """Own asynchronous polling and profile ticks in one background service thread."""
        completed = False
        self._service_started = True
        try:
            asyncio.run(self._serve())
            completed = True
        finally:
            if not completed:
                logger.error("Environmental automation stopped unexpectedly")
            self._loop = None

    def stop(self) -> None:
        """Wake both loops and cancel an in-flight weather request during shutdown."""
        self._stop_requested.set()
        loop = self._loop
        if loop is None or loop.is_closed():
            return

        def stop_in_loop() -> None:
            if self._async_stop is not None:
                self._async_stop.set()
            for event in (self._weather_wake, self._profiles_wake):
                if event is not None:
                    event.set()
            if self._weather_task is not None:
                self._weather_task.cancel()
            if self._signals_task is not None:
                self._signals_task.cancel()

        loop.call_soon_threadsafe(stop_in_loop)

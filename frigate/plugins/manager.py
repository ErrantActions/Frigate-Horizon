"""Persistent plugin lifecycle, private media sources and camera-event routing."""

import asyncio
import hashlib
import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from pydantic import ValidationError

from frigate.plugins.inference import EventInferenceConfig, EventInferenceController
from frigate.plugins.models import (
    PluginDevice,
    PluginEntry,
    PluginManifest,
    PluginSettings,
    PluginSnapshot,
)
from frigate.plugins.packages import (
    PluginError,
    download_package,
    install_archive,
    write_private_json,
)
from frigate.plugins.worker import PluginWorker

logger = logging.getLogger(__name__)
BUNDLED = Path(__file__).parent / "ring"


class PluginManager:
    """Supervise opted-in plugins without making their secrets API responses."""

    def __init__(self, root: Path, get_config, publish_event) -> None:
        self.root = root
        self.get_config = get_config
        self.publish_event = publish_event
        self.settings = PluginSettings()
        self.workers: dict[str, PluginWorker] = {}
        self.devices: dict[str, dict[str, PluginDevice]] = {}
        self.states: dict[str, dict[str, Any]] = {}
        self.registered: dict[str, str] = {}
        self.event_keys: dict[str, float] = {}
        self.lock = asyncio.Lock()
        self.task: asyncio.Task | None = None
        self.poll_tasks: dict[str, asyncio.Task] = {}
        self.action_locks: dict[str, asyncio.Lock] = {}
        self.client: aiohttp.ClientSession | None = None
        self.last_media_scan = 0.0
        self.suspended: set[str] = set()
        self.service_error: str | None = None
        self.inference = EventInferenceController(root)
        self.recording_only_cameras: dict[str, bool] | None = None

    def _save(self) -> None:
        write_private_json(
            self.root / "settings.json", self.settings.model_dump(mode="json")
        )

    def _entry(self, plugin_id: str) -> PluginEntry:
        entry = self.settings.plugins.get(plugin_id)
        if entry is None:
            raise PluginError("plugin_not_installed")
        return entry

    def _package(self, plugin_id: str) -> Path:
        entry = self._entry(plugin_id)
        return self.root / "packages" / plugin_id / entry.digest

    def _claims(self) -> None:
        configured = self.get_config().cameras
        cameras = {
            camera: True
            for entry in self.settings.plugins.values()
            for camera in entry.bindings.values()
            if camera not in configured or not configured[camera].detect.enabled
        }
        if cameras == self.recording_only_cameras:
            return
        write_private_json(self.root / "recording-only.json", cameras)
        self.recording_only_cameras = cameras

    async def start(self) -> None:
        """Restore lifecycle intent, but never authenticate or enable implicitly."""
        path = self.root / "settings.json"
        if path.exists():
            self.settings = PluginSettings.model_validate_json(path.read_bytes())
        events = self.root / "events.json"
        if events.exists():
            self.event_keys = json.loads(events.read_bytes())
        await asyncio.to_thread(self.inference.initialize)
        self.client = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10), trust_env=False
        )
        self.task = asyncio.create_task(self._run(), name="horizon_plugins")
        self.task.add_done_callback(self._supervisor_finished)

    def _supervisor_finished(self, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            self.service_error = "plugin_supervisor_failed"
            logger.error(
                "Plugin supervisor stopped (%s)", type(task.exception()).__name__
            )

    async def close(self) -> None:
        """Stop plugin work before closing its local media connection."""
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        async with self.lock:
            for plugin_id in tuple(self.workers):
                try:
                    await self._stop(plugin_id)
                except (
                    PluginError,
                    OSError,
                    aiohttp.ClientError,
                    TimeoutError,
                ) as error:
                    logger.error(
                        "Plugin %s shutdown failed (%s)",
                        plugin_id,
                        type(error).__name__,
                    )
        if self.client:
            await self.client.close()
            self.client = None

    def catalog(self) -> dict:
        """Expose package descriptions and safe, bounded operational state."""
        manifest = PluginManifest.model_validate_json(
            (BUNDLED / "plugin.json").read_bytes()
        )
        installed = []
        for plugin_id, entry in self.settings.plugins.items():
            state = self.states.get(plugin_id, {"state": "disabled"})
            installed.append(
                {
                    **entry.model_dump(exclude={"bindings"}),
                    "bindings": {
                        device: camera
                        for device, camera in entry.bindings.items()
                        if camera in self.get_config().cameras
                    },
                    **state,
                    "devices": [
                        {
                            "id": device.id,
                            "name": device.name,
                            "two_way_audio": device.two_way_audio,
                        }
                        for device in self.devices.get(plugin_id, {}).values()
                    ],
                    "event_inference": {
                        camera: self.inference.status(camera)
                        for camera in entry.bindings.values()
                    },
                }
            )
        return {
            "developer_mode": self.settings.developer_mode,
            "catalog": [manifest.model_dump()],
            "installed": installed,
        }

    async def developer_mode(self, enabled: bool, acknowledged: bool) -> None:
        """Require deliberate admin acknowledgement of in-container code access."""
        if enabled and not acknowledged:
            raise PluginError("developer_acknowledgement_required")
        async with self.lock:
            self.settings.developer_mode = enabled
            await asyncio.to_thread(self._save)

    async def configure_inference(
        self, plugin_id: str, camera: str, settings: EventInferenceConfig
    ) -> None:
        """Only an enabled camera owned by this plugin can receive AI permission."""
        entry = self._entry(plugin_id)
        config = self.get_config().cameras.get(camera)
        if camera not in entry.bindings.values() or config is None:
            raise PluginError("camera_not_bound")
        if settings.enabled and (not entry.enabled or not config.enabled):
            raise PluginError("plugin_or_camera_disabled")
        if settings.enabled and "person" in settings.triggers:
            raise PluginError("person_not_supported")
        if settings.enabled and "motion" not in entry.manifest.capabilities:
            raise PluginError("motion_not_supported")
        await asyncio.to_thread(self.inference.configure, camera, settings)

    async def install(
        self, bundled: bool, url: str | None, digest: str | None, acknowledged: bool
    ) -> None:
        """Install immutably without executing downloaded code or setup scripts."""
        if bundled:
            payload = (BUNDLED / "plugin.py").read_bytes()
            manifest = PluginManifest.model_validate_json(
                (BUNDLED / "plugin.json").read_bytes()
            )
            checksum = hashlib.sha256(
                payload + manifest.model_dump_json().encode()
            ).hexdigest()
        else:
            if not self.settings.developer_mode or not acknowledged:
                raise PluginError("developer_acknowledgement_required")
            if not url or not digest:
                raise PluginError("package_url_and_checksum_required")
            payload = await download_package(url)
            checksum = digest
            manifest = None
        async with self.lock:
            if bundled:
                if manifest.id in self.settings.plugins:
                    raise PluginError("plugin_already_installed")
                target = self.root / "packages" / manifest.id / checksum
                await asyncio.to_thread(
                    shutil.copytree,
                    BUNDLED,
                    target,
                    ignore=shutil.ignore_patterns("__pycache__"),
                )
            else:
                if not self.settings.developer_mode:
                    raise PluginError("developer_mode_disabled")
                manifest = await asyncio.to_thread(
                    install_archive, payload, checksum, self.root / "packages"
                )
                if manifest.id in self.settings.plugins:
                    await asyncio.to_thread(
                        shutil.rmtree,
                        self.root / "packages" / manifest.id / checksum,
                    )
                    raise PluginError("plugin_already_installed")
            self.settings.plugins[manifest.id] = PluginEntry(
                manifest=manifest, developer=not bundled, digest=checksum
            )
            await asyncio.to_thread(self._save)

    async def enable(self, plugin_id: str, enabled: bool) -> None:
        """Persist intent and start/stop only the selected worker."""
        async with self.lock:
            entry = self._entry(plugin_id)
            entry.enabled = enabled
            await asyncio.to_thread(self._save)
            if enabled:
                await self._start(plugin_id)
            else:
                await self._stop(plugin_id)

    async def uninstall(self, plugin_id: str) -> None:
        """Refuse to strand configured cameras or delete any recording."""
        async with self.lock:
            entry = self._entry(plugin_id)
            cameras = self.get_config().cameras
            if any(camera in cameras for camera in entry.bindings.values()):
                raise PluginError("remove_plugin_cameras_first")
            await self._stop(plugin_id)
            package = self._package(plugin_id)
            del self.settings.plugins[plugin_id]
            await asyncio.to_thread(self._save)
            await asyncio.to_thread(self._claims)
            await asyncio.to_thread(shutil.rmtree, package)
            data = self.root / "data" / plugin_id
            if data.exists():
                await asyncio.to_thread(shutil.rmtree, data)
            self.states.pop(plugin_id, None)
            self.devices.pop(plugin_id, None)

    async def action(self, plugin_id: str, method: str, params: dict) -> dict:
        """Invoke an advertised action without returning provider tokens."""
        async with self.lock:
            entry = self._entry(plugin_id)
            if method not in entry.manifest.actions:
                raise PluginError("unknown_plugin_action")
            if not entry.enabled:
                raise PluginError("plugin_disabled")
            worker = self.workers.get(plugin_id)
            if worker is None:
                raise PluginError("worker_not_running")
            async with self.action_locks.setdefault(plugin_id, asyncio.Lock()):
                result = await worker.call(method, params)
                snapshot = PluginSnapshot.model_validate(result)
                await self._apply(plugin_id, snapshot)
            return {"state": snapshot.state}

    async def bind(self, plugin_id: str, device_id: str, camera: str) -> str:
        """Reserve a newly created camera and keep its source URL private."""
        entry = self._entry(plugin_id)
        if "camera" not in entry.manifest.capabilities:
            raise PluginError("camera_capability_required")
        if device_id not in self.devices.get(plugin_id, {}):
            raise PluginError("device_not_available")
        if camera in self.get_config().cameras:
            raise PluginError("camera_already_exists")
        if any(
            camera in item.bindings.values() for item in self.settings.plugins.values()
        ):
            raise PluginError("camera_already_bound")
        if (
            device_id in entry.bindings
            and entry.bindings[device_id] in self.get_config().cameras
        ):
            raise PluginError("device_already_bound")
        entry.bindings[device_id] = camera
        try:
            await asyncio.to_thread(self._save)
            await asyncio.to_thread(self._claims)
            await self._register(plugin_id, device_id)
        except (OSError, PluginError, aiohttp.ClientError, TimeoutError):
            entry.bindings.pop(device_id, None)
            await asyncio.to_thread(self._save)
            await asyncio.to_thread(self._claims)
            raise
        return self.stream_name(plugin_id, device_id)

    async def unbind(self, plugin_id: str, device_id: str) -> None:
        """Roll back a failed camera creation without touching other cameras."""
        self._entry(plugin_id).bindings.pop(device_id, None)
        await asyncio.to_thread(self._save)
        await asyncio.to_thread(self._claims)

    @staticmethod
    def stream_name(plugin_id: str, device_id: str) -> str:
        """Keep generated stream names independent of untrusted device labels."""
        key = hashlib.sha256(device_id.encode()).hexdigest()[:16]
        return f"horizon_plugin_{plugin_id}_{key}"

    async def _start(self, plugin_id: str) -> None:
        if plugin_id in self.workers:
            return
        package = self._package(plugin_id)
        manifest = PluginManifest.model_validate_json(
            (package / "plugin.json").read_bytes()
        )
        if manifest != self._entry(plugin_id).manifest:
            raise PluginError("installed_manifest_changed")
        worker = PluginWorker(package / "plugin.py", self.root / "data" / plugin_id)
        await worker.start()
        self.workers[plugin_id] = worker
        self.states[plugin_id] = {"state": "starting"}

    async def _stop(self, plugin_id: str) -> None:
        poll_task = self.poll_tasks.pop(plugin_id, None)
        if poll_task:
            poll_task.cancel()
            await asyncio.gather(poll_task, return_exceptions=True)
        worker = self.workers.pop(plugin_id, None)
        if worker:
            await worker.close()
        await self._remove_sources(plugin_id)
        self.states[plugin_id] = {"state": "disabled"}

    async def _remove_sources(self, plugin_id: str) -> None:
        for device_id in self._entry(plugin_id).bindings:
            await asyncio.to_thread(
                self.inference.revoke, self._entry(plugin_id).bindings[device_id]
            )
            name = self.stream_name(plugin_id, device_id)
            if self.client is not None:
                async with self.client.delete(
                    "http://127.0.0.1:1984/api/streams", params={"src": name}
                ) as response:
                    if response.status not in (200, 404):
                        raise PluginError("stream_removal_failed")
            self.registered.pop(name, None)
        self.suspended.add(plugin_id)

    async def _register(self, plugin_id: str, device_id: str) -> None:
        device = self.devices.get(plugin_id, {}).get(device_id)
        if device is None or self.client is None:
            raise PluginError("device_not_available")
        if urlsplit(device.source).scheme not in (
            "ring",
            "rtsp",
            "rtsps",
            "http",
            "https",
        ):
            raise PluginError("unsupported_stream_source")
        name = self.stream_name(plugin_id, device_id)
        if self.registered.get(name) == device.source:
            return
        async with self.client.put(
            "http://127.0.0.1:1984/api/streams",
            params={"name": name, "src": device.source},
        ) as response:
            if response.status != 200:
                raise PluginError("stream_registration_failed")
        self.registered[name] = device.source
        self.suspended.discard(plugin_id)

    async def _apply(self, plugin_id: str, snapshot: PluginSnapshot) -> None:
        entry = self._entry(plugin_id)
        self.devices[plugin_id] = {device.id: device for device in snapshot.devices}
        self.states[plugin_id] = {"state": snapshot.state, "updated_at": time.time()}
        if snapshot.state != "running":
            if plugin_id not in self.suspended:
                await self._remove_sources(plugin_id)
            return
        for device_id, camera in entry.bindings.items():
            config = self.get_config().cameras.get(camera)
            if config is not None and config.enabled:
                await self._register(plugin_id, device_id)
        now = time.time()
        self.event_keys = {
            key: expiry for key, expiry in self.event_keys.items() if expiry > now
        }
        changed = False
        for event in snapshot.events:
            camera = entry.bindings.get(event.device_id)
            config = self.get_config().cameras.get(camera)
            key = f"{plugin_id}:{event.id}:{event.kind}"
            if (
                config is None
                or not config.enabled
                or event.kind not in entry.manifest.capabilities
                or key in self.event_keys
                or abs(event.timestamp - now) > 120
            ):
                continue
            event_id = (
                f"{event.timestamp}-{hashlib.sha256(key.encode()).hexdigest()[:12]}"
            )
            self.publish_event(
                (
                    event.timestamp,
                    camera,
                    event.kind,
                    event_id,
                    True,
                    None,
                    None,
                    15,
                    "plugin",
                    {},
                    0,
                )
            )
            await asyncio.to_thread(self.inference.trigger, camera, event.kind)
            self.event_keys[key] = now + 3600
            changed = True
        if changed:
            await asyncio.to_thread(
                write_private_json, self.root / "events.json", self.event_keys
            )

    async def _poll(self, plugin_id: str) -> None:
        async with self.action_locks.setdefault(plugin_id, asyncio.Lock()):
            entry = self.settings.plugins.get(plugin_id)
            if entry is None or not entry.enabled:
                return
            try:
                await self._start(plugin_id)
                result = await self.workers[plugin_id].call("poll")
                await self._apply(plugin_id, PluginSnapshot.model_validate(result))
            except (
                PluginError,
                OSError,
                ValidationError,
                aiohttp.ClientError,
                TimeoutError,
            ) as error:
                logger.error("Plugin %s failed (%s)", plugin_id, type(error).__name__)
                worker = self.workers.pop(plugin_id, None)
                if worker:
                    await worker.close()
                self.states[plugin_id] = {
                    "state": "error",
                    "error": str(error)
                    if isinstance(error, PluginError)
                    else "plugin_failed",
                    "retry_at": time.time() + 30,
                }

    async def _run(self) -> None:
        try:
            while True:
                async with self.lock:
                    changed = False
                    for entry in self.settings.plugins.values():
                        current = {
                            device: camera
                            for device, camera in entry.bindings.items()
                            if camera in self.get_config().cameras
                        }
                        if current != entry.bindings:
                            entry.bindings = current
                            changed = True
                    if changed:
                        await asyncio.to_thread(self._save)
                    await asyncio.to_thread(self._claims)
                    if (
                        self.client
                        and any(item.enabled for item in self.settings.plugins.values())
                        and time.monotonic() - self.last_media_scan >= 15
                    ):
                        self.last_media_scan = time.monotonic()
                        try:
                            async with self.client.get(
                                "http://127.0.0.1:1984/api/streams"
                            ) as response:
                                response.raise_for_status()
                                media = await response.json()
                            self.registered = {
                                name: source
                                for name, source in self.registered.items()
                                if name in media
                            }
                        except (aiohttp.ClientError, TimeoutError, ValueError):
                            self.registered.clear()
                            if any(
                                item.enabled for item in self.settings.plugins.values()
                            ):
                                logger.warning("Plugin media registry is unavailable")
                    for plugin_id, entry in tuple(self.settings.plugins.items()):
                        pending = self.poll_tasks.get(plugin_id)
                        if pending is not None and pending.done():
                            pending.result()
                        if (
                            entry.enabled
                            and (pending is None or pending.done())
                            and self.states.get(plugin_id, {}).get("retry_at", 0)
                            <= time.time()
                        ):
                            self.poll_tasks[plugin_id] = asyncio.create_task(
                                self._poll(plugin_id)
                            )
                await asyncio.sleep(2)
        finally:
            for task in self.poll_tasks.values():
                task.cancel()
            await asyncio.gather(*self.poll_tasks.values(), return_exceptions=True)

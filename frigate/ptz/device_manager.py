"""One physical-device owner for API controls and environmental image plans."""

import asyncio
import hashlib
import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from frigate.api.defs.request.onvif_body import OnvifConnectionBody
from frigate.ptz.connection import camera_cleanup_task, drain_camera_cleanup
from frigate.ptz.imaging import (
    ImagingReadbackMismatch,
    ImagingWriteUnverified,
    read_imaging,
    write_imaging,
)
from frigate.util.onvif_device import MANUAL_IMAGING_MODES, OnvifSettingsConflict


@dataclass
class ImageLease:
    baseline: dict = field(default_factory=dict)
    owned: dict = field(default_factory=dict)
    profile_token: str | None = None
    signature: str | None = None
    manual_until: float = 0
    blocked: bool = False
    phase: str = "idle"
    connection_fingerprint: str | None = None


def connection_for(config, camera_name, profile_token=None):
    """Resolve credentials only from the saved server configuration."""
    camera = config.cameras.get(camera_name)
    if camera is None or not camera.enabled or not camera.onvif.host:
        raise ValueError("An enabled camera with a saved ONVIF connection is required")
    onvif = camera.onvif
    return OnvifConnectionBody(
        host=onvif.host,
        port=onvif.port,
        username=onvif.user or "",
        password=onvif.password or "",
        tls_insecure=onvif.tls_insecure,
        ignore_time_mismatch=onvif.ignore_time_mismatch,
        profile_token=profile_token,
    )


def image_values(response):
    """Return reported image values, not options or assumed defaults."""
    if response.imaging is None:
        raise ValueError("Choose the intended physical image source")
    return {control.key: control.value for control in response.imaging.controls}


class OnvifDeviceManager(threading.Thread):
    """Serialize mutations across event loops without holding the NVR config lock."""

    def __init__(
        self,
        config_provider: Callable,
        journal_path: Path | None = None,
        publisher_factory=None,
    ):
        super().__init__(name="onvif_device_owner", daemon=True)
        self.config_provider = config_provider
        self._ready = threading.Event()
        self._loop = None
        self._locks = {}
        self._leases: dict[str, ImageLease] = {}
        self._events = {}
        self._closing = False
        self._journal_path = journal_path
        self._journal_lock = None
        self._journal_unavailable = False
        self._metadata_publisher = None
        self._publisher_factory = publisher_factory

    def run(self):
        """Run the device-owner loop; starting it does not contact any camera."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        if self._journal_path is not None and self._journal_path.exists():
            try:
                data = json.loads(self._journal_path.read_text())
                allowed = {item.name for item in fields(ImageLease)}
                for name, value in data.items():
                    lease = ImageLease(
                        **{key: value[key] for key in allowed if key in value}
                    )
                    lease.manual_until = time.monotonic() + max(
                        0, value.get("manual_expires_at", 0) - time.time()
                    )
                    self._leases[name] = lease
            except (OSError, ValueError, TypeError, AttributeError):
                self._journal_unavailable = True
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            pending = {
                task
                for task in asyncio.all_tasks(loop)
                if not camera_cleanup_task(task)
            }
            for task in pending:
                task.cancel()
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(drain_camera_cleanup())
            if self._metadata_publisher is not None:
                self._metadata_publisher.stop()
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(loop.shutdown_default_executor())
            loop.close()
            self._loop = None

    def native_event_bridge(
        self, camera_name, config, expiry_seconds, source_tokens, allow_unscoped
    ):
        """Create the manual-event publisher only after an explicit runtime opt-in."""
        import zmq

        from frigate.comms.event_metadata_updater import EventMetadataPublisher
        from frigate.ptz.native_event_bridge import NativeEventBridge

        if self._metadata_publisher is None:
            factory = self._publisher_factory or EventMetadataPublisher
            try:
                self._metadata_publisher = factory()
            except zmq.ZMQError:
                raise RuntimeError("Native event signal bus is unavailable") from None

        def publish(payload, topic):
            try:
                self._metadata_publisher.publish(payload, topic)
            except zmq.ZMQError:
                raise RuntimeError("Native event signal bus is unavailable") from None

        def allowed():
            current = self.config_provider()
            camera = current.cameras.get(camera_name)
            return (
                not self._closing
                and current is config
                and camera is not None
                and camera.enabled
            )

        return NativeEventBridge(
            camera_name,
            publish,
            allowed,
            expiry_seconds=expiry_seconds,
            source_tokens=source_tokens,
            allow_unscoped=allow_unscoped,
        )

    async def _persist(self):
        if self._journal_unavailable:
            raise RuntimeError("Physical image ownership journal is unavailable")
        if self._journal_path is None:
            return
        if self._journal_lock is None:
            self._journal_lock = asyncio.Lock()
        async with self._journal_lock:
            data = {}
            for name, lease in self._leases.items():
                data[name] = {
                    **asdict(lease),
                    "manual_expires_at": time.time()
                    + max(0, lease.manual_until - time.monotonic()),
                }
            path = self._journal_path

            def save():
                pending = path.with_suffix(".pending")
                descriptor = os.open(
                    pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
                )
                with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                    json.dump(data, output, allow_nan=False)
                    output.flush()
                    os.fsync(output.fileno())
                pending.replace(path)
                if hasattr(os, "O_DIRECTORY"):
                    directory = os.open(path.parent, os.O_DIRECTORY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)

            try:
                await asyncio.to_thread(save)
            except OSError:
                self._journal_unavailable = True
                for lease in self._leases.values():
                    lease.blocked = True
                    lease.phase = "journal_unavailable"
                raise

    async def execute(self, camera_name, operation):
        """Dispatch a camera operation to its single serialized owner."""
        if self._closing or not self._ready.is_set() or self._loop is None:
            raise RuntimeError("ONVIF device service is unavailable")

        async def serialized():
            lock = self._locks.setdefault(camera_name, asyncio.Lock())
            async with lock:
                if self._closing:
                    raise RuntimeError("ONVIF device service is stopping")
                config = self.config_provider()

                def guard():
                    if self._closing or self.config_provider() is not config:
                        raise OnvifSettingsConflict(
                            "Configuration changed; refresh before applying"
                        )

                return await operation(config, guard)

        future = asyncio.run_coroutine_threadsafe(serialized(), self._loop)
        # A disconnected HTTP client must not release ownership while a write is in flight.
        wrapped = asyncio.wrap_future(future)
        # Consume detached failures without logging SDK text, which may contain
        # credentials. An attached caller still receives the original exception.
        wrapped.add_done_callback(
            lambda completed: None if completed.cancelled() else completed.exception()
        )
        return await asyncio.shield(wrapped)

    def guard_manual_focus(self, camera_name, profile_token):
        """Never move a lens while automatic or uncertain image ownership is active."""
        lease = self._leases.get(camera_name)
        if lease is not None and (
            lease.blocked
            or (
                lease.owned
                and (
                    lease.profile_token != profile_token
                    or lease.manual_until <= time.monotonic()
                )
            )
        ):
            raise OnvifSettingsConflict(
                "Pause or reconcile physical image automation before moving focus"
            )

    def guard_administration(self, camera_name):
        """Disallow generic device changes while physical image ownership is active."""
        lease = self._leases.get(camera_name)
        if lease is not None and (lease.blocked or lease.owned):
            raise OnvifSettingsConflict(
                "Release physical image ownership before device administration"
            )

    async def manual_image(
        self,
        camera_name,
        profile_token,
        changes,
        expected,
        *,
        persistence=False,
        override_seconds=900,
    ):
        """A verified manual edit owns an explicit temporary automation override."""

        async def operation(config, guard):
            lease = self._leases.setdefault(camera_name, ImageLease())
            if lease.owned and lease.profile_token != profile_token:
                raise OnvifSettingsConflict(
                    "Release the active image source before selecting another"
                )

            async def intent():
                guard()
                lease.blocked = True
                lease.phase = "in_flight"
                await self._persist()
                guard()

            try:
                result = await write_imaging(
                    connection_for(config, camera_name, profile_token),
                    changes,
                    expected,
                    confirm_device_change=True,
                    force_persistence=persistence,
                    before_write=intent,
                )
                guard()
            except (
                ImagingWriteUnverified,
                ImagingReadbackMismatch,
                OnvifSettingsConflict,
            ) as error:
                lease.blocked = True
                lease.phase = (
                    "conflict"
                    if isinstance(error, OnvifSettingsConflict)
                    else "unverified"
                )
                await self._persist()
                raise
            # Only manually changed fields become a new baseline. Preserve the
            # restoration baseline for untouched fields of an automatic plan.
            for key in changes:
                lease.baseline.pop(key, None)
                lease.owned.pop(key, None)
            lease.signature = None
            lease.profile_token = profile_token
            lease.manual_until = time.monotonic() + override_seconds
            lease.blocked = False
            lease.phase = "manual_override"
            await self._persist()
            return result

        return await self.execute(camera_name, operation)

    async def automatic_image(self, camera_name, profiles, expected_config):
        """Merge selected physical layers and safely restore values we still own."""

        async def apply(config, guard):
            if config is not expected_config:
                return
            camera = config.cameras.get(camera_name)
            lease = self._leases.setdefault(camera_name, ImageLease())
            if lease.blocked or time.monotonic() < lease.manual_until:
                return
            if self._journal_unavailable:
                lease.phase = "journal_unavailable"
                return
            settings = camera.environment if camera else None
            if camera is None:
                return
            fingerprint = hashlib.sha256(
                repr(
                    (
                        camera.onvif.host,
                        camera.onvif.port,
                        camera.onvif.user,
                        camera.onvif.password,
                        camera.onvif.tls_insecure,
                    )
                ).encode()
            ).hexdigest()
            if (
                lease.connection_fingerprint is not None
                and lease.connection_fingerprint != fingerprint
                and lease.owned
            ):
                lease.phase = "connection_changed"
                lease.blocked = True
                await self._persist()
                return
            lease.connection_fingerprint = fingerprint
            selected = (
                [
                    settings.image_profiles[name]
                    for name in profiles
                    if settings and name in settings.image_profiles
                ]
                if settings and settings.enabled and settings.allow_device_changes
                else []
            )
            desired = {}
            sources = {profile.profile_token for profile in selected}
            if len(sources) > 1:
                lease.phase = "source_conflict"
                lease.blocked = True
                await self._persist()
                return
            token = next(iter(sources), lease.profile_token)
            for profile in selected:
                desired.update(profile.settings)
            signature = repr((token, sorted(desired.items())))
            if signature == lease.signature:
                return
            if not desired and not lease.owned:
                lease.phase = "idle"
                lease.signature = signature
                return
            if lease.owned and token != lease.profile_token:
                lease.phase = "source_conflict"
                lease.blocked = True
                await self._persist()
                return
            connection = connection_for(config, camera_name, token)
            observed = await read_imaging(connection)
            values = image_values(observed)
            if any(values.get(key) != value for key, value in lease.owned.items()):
                lease.phase = "external_change"
                lease.blocked = True
                await self._persist()
                return
            changes = dict(desired)
            for key in lease.owned:
                if key not in desired:
                    changes[key] = lease.baseline[key]
            for key, mode_key in MANUAL_IMAGING_MODES.items():
                if key not in desired and changes.get(mode_key) == "AUTO":
                    changes.pop(key, None)
            if any(values.get(key) is None for key in changes):
                raise ValueError(
                    "The camera did not report a safe restoration baseline"
                )
            baseline = dict(lease.baseline)
            for key in desired:
                baseline.setdefault(key, values[key])
            changes = {
                key: value for key, value in changes.items() if values.get(key) != value
            }
            if changes:

                async def intent():
                    guard()
                    lease.baseline = baseline
                    lease.profile_token = token
                    lease.blocked = True
                    lease.phase = "in_flight"
                    await self._persist()
                    guard()

                try:
                    await write_imaging(
                        connection,
                        changes,
                        {key: values[key] for key in changes},
                        confirm_device_change=True,
                        before_write=intent,
                    )
                except (ImagingWriteUnverified, ImagingReadbackMismatch):
                    lease.phase = "unverified"
                    lease.blocked = True
                    await self._persist()
                    raise
            else:
                # Even an unchanged plan must not claim ownership after a config race.
                guard()
            guard()
            lease.baseline = {key: baseline[key] for key in desired}
            lease.owned = dict(desired)
            lease.profile_token = token
            lease.signature = signature
            lease.phase = "automatic" if desired else "restored"
            lease.blocked = False
            await self._persist()

        async def operation(config, guard):
            try:
                await apply(config, guard)
            except (OnvifSettingsConflict, ValueError):
                # Update conflict state while still holding the same owner lock.
                lease = self._leases.setdefault(camera_name, ImageLease())
                lease.phase = "conflict"
                lease.blocked = True
                await self._persist()

        await self.execute(camera_name, operation)

    async def image_status(self, camera_name):
        """Expose ownership state without any camera connectivity probe."""

        async def operation(config, guard):
            lease = self._leases.setdefault(camera_name, ImageLease())
            return {
                "phase": lease.phase,
                "blocked": lease.blocked,
                "manual_remaining_seconds": max(
                    0, lease.manual_until - time.monotonic()
                ),
                "owned_fields": sorted(lease.owned),
                "profile_token": lease.profile_token,
            }

        return await self.execute(camera_name, operation)

    async def resume(self, camera_name):
        """Accept the current camera as baseline before resuming automatic plans."""

        async def operation(config, guard):
            old = self._leases.get(camera_name)
            if old and old.owned and not old.blocked:
                old.manual_until = 0
                old.signature = None
                old.phase = "automatic"
            else:
                self._leases[camera_name] = ImageLease()
            await self._persist()
            return {"success": True}

        return await self.execute(camera_name, operation)

    def stop(self):
        """Cancel subscriptions and in-flight work; do not guess at uncertain writes."""
        self._closing = True
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)

"""Authorized ONVIF operations using saved connections and a shared device owner."""

import asyncio
from datetime import UTC, datetime
from typing import Literal

import httpx
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response
from fastapi.routing import APIRoute
from lxml.etree import Error as XmlError
from onvif import ONVIFError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from zeep.exceptions import Error as ZeepError

from frigate.api.auth import require_camera_access, require_role
from frigate.api.defs.request.onvif_body import (
    DeviceValue,
    FiniteNumber,
    OnvifAudioChanges,
    OnvifEncoderChanges,
)
from frigate.api.defs.tags import Tags
from frigate.ptz import device_services
from frigate.ptz.administration import (
    administration_catalog,
    download_system_backup,
    invoke_operation,
    restore_system_backup,
)
from frigate.ptz.audio_encoder import read_audio_encoder, write_audio_encoder
from frigate.ptz.device_manager import connection_for
from frigate.ptz.encoder import read_encoder, write_encoder
from frigate.ptz.firmware import FIRMWARE_METHODS, upgrade_firmware
from frigate.ptz.focus import read_focus, write_focus
from frigate.ptz.imaging import (
    ImagingReadbackMismatch,
    ImagingWriteUnverified,
    read_imaging,
)
from frigate.ptz.native_events import NativeEventMonitor
from frigate.ptz.recording_access import RecordingSourceDenied, recording_binding
from frigate.ptz.replay import MAX_REPLAY_SECONDS, ReplayCapacityError
from frigate.ptz.replay_transport import ReplayUnsupported
from frigate.util.onvif_backup import MAX_BACKUP_BYTES
from frigate.util.onvif_device import OnvifSettingsConflict


class OnvifRoute(APIRoute):
    """Do not reflect rejected credential fields or vendor tokens in validation errors."""

    def get_route_handler(self):
        """Keep this camera-control router's validation responses secret safe."""
        original = super().get_route_handler()

        async def handler(request):
            try:
                return await original(request)
            except RequestValidationError:
                raise HTTPException(422, "Invalid ONVIF operation parameters") from None

        return handler


router = APIRouter(tags=[Tags.camera], route_class=OnvifRoute)
ADMIN = [Depends(require_role(["admin"])), Depends(require_camera_access)]
READ = [Depends(require_camera_access)]
DEVICE_ERRORS = (
    ONVIFError,
    ZeepError,
    XmlError,
    httpx.HTTPError,
    TimeoutError,
    OSError,
)


class ConfirmedControl(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    confirm_device_change: Literal[True]

    @field_validator("confirm_device_change", mode="before")
    @classmethod
    def confirmation(cls, value):
        """Reject coerced truthy values for physical-device consent."""
        if value is not True:
            raise ValueError("Explicit device-change confirmation is required")
        return value


class ImageUpdate(ConfirmedControl):
    profile_token: str = Field(min_length=1, max_length=256)
    changes: dict[str, DeviceValue] = Field(min_length=1, max_length=40)
    expected_values: dict[str, DeviceValue | None] = Field(max_length=40)
    force_persistence: bool = Field(default=False, strict=True)
    override_seconds: int = Field(default=900, ge=60, le=86400, strict=True)


class EncoderUpdate(ConfirmedControl):
    profile_token: str = Field(min_length=1, max_length=256)
    changes: OnvifEncoderChanges
    expected_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    force_persistence: bool = Field(default=False, strict=True)


class AudioEncoderUpdate(ConfirmedControl):
    profile_token: str = Field(min_length=1, max_length=256)
    changes: OnvifAudioChanges
    expected_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    force_persistence: bool = Field(default=False, strict=True)
    service: Literal["media", "media2"] = "media"


class FocusUpdate(ConfirmedControl):
    profile_token: str = Field(min_length=1, max_length=256)
    position: FiniteNumber
    speed: FiniteNumber | None = None
    expected_revision: str = Field(pattern=r"^[a-f0-9]{64}$")


class AdministrationOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    catalog_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    parameters: dict = Field(default_factory=dict, max_length=128, repr=False)
    confirm_device_change: bool = Field(default=False, strict=True)
    typed_confirmation: str = Field(default="", max_length=128)


class BackupDownload(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    catalog_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirm_sensitive_download: Literal[True]

    @field_validator("confirm_sensitive_download", mode="before")
    @classmethod
    def confirmation(cls, value):
        """Require explicit consent to download credential-bearing camera data."""
        if value is not True:
            raise ValueError("Explicit sensitive-download confirmation is required")
        return value


class PresetUpdate(ConfirmedControl):
    profile_token: str = Field(min_length=1, max_length=256)
    action: Literal["set", "goto", "remove"]
    token: str | None = Field(default=None, min_length=1, max_length=256)
    name: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_action(self):
        """Preset creation needs a name; moves/removals need a reported token."""
        if self.action == "set" and self.name is None:
            raise ValueError("A preset name is required")
        if self.action != "set" and self.token is None:
            raise ValueError("A preset token is required")
        return self


class EventControl(ConfirmedControl):
    enabled: bool = Field(strict=True)
    integrate_events: bool = Field(default=False, strict=True)
    event_expiry_seconds: int = Field(default=30, ge=1, le=300, strict=True)
    source_profile_token: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def require_integration_source(self):
        """Physical event sources are explicit and separate from PTZ selection."""
        if self.enabled and self.integrate_events and self.source_profile_token is None:
            raise ValueError(
                "Choose a physical source profile for NVR event integration"
            )
        return self


class ReplayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    recording_token: str = Field(min_length=1, max_length=256)
    start: datetime
    end: datetime
    audio: bool = Field(default=True, strict=True)

    @model_validator(mode="after")
    def validate_range(self):
        """Never turn a caller URL or unbounded range into an FFmpeg input."""
        if (
            self.start.tzinfo is None
            or self.end.tzinfo is None
            or not 0 < (self.end - self.start).total_seconds() <= MAX_REPLAY_SECONDS
        ):
            raise ValueError(
                "Use a timezone-aware replay range of at most five minutes"
            )
        return self


async def _operation(request, camera_name, operation, *, mutation=False):
    if camera_name not in request.app.frigate_config.cameras:
        raise HTTPException(404, "Camera is not configured")
    manager = getattr(request.app, "onvif_devices", None)
    if manager is None:
        raise HTTPException(503, "ONVIF device controls are unavailable")
    try:
        return await manager.execute(camera_name, operation)
    except OnvifSettingsConflict:
        raise HTTPException(
            409, "Camera or configuration changed; refresh before applying"
        ) from None
    except (ImagingWriteUnverified, ImagingReadbackMismatch):
        raise HTTPException(
            409,
            "The camera may have applied all or part of the change. Refresh and reconcile; do not retry automatically",
        ) from None
    except RecordingSourceDenied:
        raise HTTPException(
            403,
            "Camera recording source attribution is missing, ambiguous, or does not match this camera",
        ) from None
    except DEVICE_ERRORS:
        message = (
            "The device change could not be verified. Refresh before another action"
            if mutation
            else "The camera service is unavailable or rejected authentication"
        )
        raise HTTPException(502, message) from None
    except ValueError:
        raise HTTPException(
            422,
            "The camera did not report support for these values, source, or operation",
        ) from None
    except RuntimeError:
        raise HTTPException(503, "ONVIF device controls are unavailable") from None


@router.post("/onvif/discover", dependencies=[Depends(require_role(["admin"]))])
async def discover(body: ConfirmedControl):
    """Perform explicit LAN discovery only; never scan or probe automatically."""
    try:
        return await device_services.discover_devices()
    except OSError:
        raise HTTPException(
            502, "LAN discovery is unavailable on this network"
        ) from None


@router.get("/onvif/cameras/{camera_name}/status", dependencies=READ)
async def status(camera_name: str, request: Request):
    """Read actual advertised ONVIF services for an authorized saved camera."""

    async def operation(config, guard):
        return await device_services.device_status(connection_for(config, camera_name))

    return await _operation(request, camera_name, operation)


@router.get("/onvif/cameras/{camera_name}/imaging", dependencies=READ)
async def imaging(
    camera_name: str,
    request: Request,
    profile_token: str | None = Query(None, max_length=256),
):
    """Read real options; ambiguous physical sources require explicit selection."""

    async def operation(config, guard):
        return await read_imaging(connection_for(config, camera_name, profile_token))

    return await _operation(request, camera_name, operation)


@router.put("/onvif/cameras/{camera_name}/imaging", dependencies=ADMIN)
async def update_imaging(camera_name: str, body: ImageUpdate, request: Request):
    """Apply one optimistic, confirmed update through the shared image owner."""
    if camera_name not in request.app.frigate_config.cameras:
        raise HTTPException(404, "Camera is not configured")
    manager = getattr(request.app, "onvif_devices", None)
    if manager is None:
        raise HTTPException(503, "ONVIF device controls are unavailable")
    # manual_image serializes itself, so do not recursively acquire its camera lock.
    try:
        return await manager.manual_image(
            camera_name,
            body.profile_token,
            body.changes,
            body.expected_values,
            persistence=body.force_persistence,
            override_seconds=body.override_seconds,
        )
    except OnvifSettingsConflict:
        raise HTTPException(
            409, "Camera settings changed; refresh before applying"
        ) from None
    except (ImagingWriteUnverified, ImagingReadbackMismatch):
        raise HTTPException(
            409,
            "The camera may have applied part of this change. Refresh and reconcile before another write",
        ) from None
    except DEVICE_ERRORS:
        raise HTTPException(502, "The camera imaging service is unavailable") from None
    except ValueError:
        raise HTTPException(
            422, "Unsupported settings, stale source, or out-of-range imaging values"
        ) from None
    except RuntimeError:
        raise HTTPException(503, "ONVIF device controls are unavailable") from None


@router.get("/onvif/cameras/{camera_name}/ownership", dependencies=READ)
async def ownership(camera_name: str, request: Request):
    """Read runtime manual/automatic ownership without contacting a camera."""
    if camera_name not in request.app.frigate_config.cameras:
        raise HTTPException(404, "Camera is not configured")
    manager = getattr(request.app, "onvif_devices", None)
    if manager is None:
        raise HTTPException(503, "ONVIF device controls are unavailable")
    try:
        return await manager.image_status(camera_name)
    except RuntimeError:
        raise HTTPException(503, "ONVIF device controls are unavailable") from None


@router.post("/onvif/cameras/{camera_name}/resume", dependencies=ADMIN)
async def resume(camera_name: str, body: ConfirmedControl, request: Request):
    """Explicitly reconcile uncertain/external changes as a new automation baseline."""
    if camera_name not in request.app.frigate_config.cameras:
        raise HTTPException(404, "Camera is not configured")
    manager = getattr(request.app, "onvif_devices", None)
    if manager is None:
        raise HTTPException(503, "ONVIF device controls are unavailable")
    try:
        return await manager.resume(camera_name)
    except OnvifSettingsConflict:
        raise HTTPException(
            409, "An automatic image plan still owns settings"
        ) from None
    except (RuntimeError, OSError):
        raise HTTPException(
            503, "Physical image ownership journal is unavailable"
        ) from None


@router.get("/onvif/cameras/{camera_name}/encoder/{service}", dependencies=READ)
async def encoder(
    camera_name: str,
    service: Literal["media", "media2"],
    request: Request,
    profile_token: str | None = Query(None, max_length=256),
):
    """Read Media1 or explicitly selected Media2; never substitute a different binding."""

    async def operation(config, guard):
        return await read_encoder(
            connection_for(config, camera_name, profile_token), service
        )

    return await _operation(request, camera_name, operation)


@router.put("/onvif/cameras/{camera_name}/encoder/{service}", dependencies=ADMIN)
async def update_encoder(
    camera_name: str,
    service: Literal["media", "media2"],
    body: EncoderUpdate,
    request: Request,
):
    """Apply bounded encoder values after revision validation and explicit consent."""

    async def operation(config, guard):
        return await write_encoder(
            connection_for(config, camera_name, body.profile_token),
            service,
            body.changes,
            body.expected_revision,
            force_persistence=body.force_persistence,
            before_write=guard,
        )

    return await _operation(request, camera_name, operation, mutation=True)


@router.get("/onvif/cameras/{camera_name}/audio-encoder", dependencies=READ)
async def audio_encoder(
    camera_name: str,
    request: Request,
    profile_token: str | None = Query(None, max_length=256),
    service: Literal["media", "media2"] = "media",
):
    """Read Media1 audio options without inferring a microphone or sample rate."""

    async def operation(config, guard):
        return await read_audio_encoder(
            connection_for(config, camera_name, profile_token),
            kind=service,
        )

    return await _operation(request, camera_name, operation)


@router.put("/onvif/cameras/{camera_name}/audio-encoder", dependencies=ADMIN)
async def update_audio_encoder(
    camera_name: str, body: AudioEncoderUpdate, request: Request
):
    """Serialize one confirmed audio encoder change and verify its readback."""

    async def operation(config, guard):
        return await write_audio_encoder(
            connection_for(config, camera_name, body.profile_token),
            body.changes,
            body.expected_revision,
            force_persistence=body.force_persistence,
            before_write=guard,
            kind=body.service,
        )

    return await _operation(request, camera_name, operation, mutation=True)


@router.get("/onvif/cameras/{camera_name}/profiles/{service}", dependencies=READ)
async def profiles(
    camera_name: str,
    service: Literal["media", "media2"],
    request: Request,
):
    """Read actual media/source bindings independently of PTZ initialization."""

    async def operation(config, guard):
        return await device_services.profile_catalog(
            connection_for(config, camera_name), service
        )

    return await _operation(request, camera_name, operation)


@router.get("/onvif/cameras/{camera_name}/focus", dependencies=READ)
async def focus(
    camera_name: str,
    request: Request,
    profile_token: str | None = Query(None, max_length=256),
):
    """Read reported absolute lens focus support independently of PTZ support."""

    async def operation(config, guard):
        return await read_focus(connection_for(config, camera_name, profile_token))

    return await _operation(request, camera_name, operation)


@router.get("/onvif/cameras/{camera_name}/administration", dependencies=ADMIN)
async def administration(camera_name: str, request: Request):
    """Read the advertised service/WSDL catalog without invoking administration operations."""

    async def operation(config, guard):
        return await administration_catalog(connection_for(config, camera_name))

    return await _operation(request, camera_name, operation)


@router.post(
    "/onvif/cameras/{camera_name}/administration/backup",
    dependencies=ADMIN,
)
async def administration_backup(
    camera_name: str, body: BackupDownload, request: Request
):
    """Download an explicitly requested private backup, never a public media export."""

    async def operation(config, guard):
        return await download_system_backup(
            connection_for(config, camera_name), body.catalog_revision
        )

    data = await _operation(request, camera_name, operation)
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Content-Disposition": 'attachment; filename="camera-backup.zip"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post(
    "/onvif/cameras/{camera_name}/administration/restore",
    dependencies=ADMIN,
)
async def administration_restore(
    camera_name: str,
    request: Request,
    backup: UploadFile = File(...),
    catalog_revision: str = Form(..., pattern=r"^[a-f0-9]{64}$"),
    typed_confirmation: str = Form(...),
    confirm_device_change: str = Form(...),
):
    """Restore one explicitly confirmed private backup without an automatic retry."""
    if typed_confirmation != "RestoreSystem" or confirm_device_change != "true":
        raise HTTPException(422, "Explicit RestoreSystem confirmation is required")
    try:
        payload = await backup.read(MAX_BACKUP_BYTES + 1024 * 1024 + 1)
    finally:
        await backup.close()
    if len(payload) > MAX_BACKUP_BYTES + 1024 * 1024:
        raise HTTPException(413, "Camera backup upload exceeds the limit")

    async def operation(config, guard):
        def before_write():
            guard()
            request.app.onvif_devices.guard_administration(camera_name)

        return await restore_system_backup(
            connection_for(config, camera_name),
            catalog_revision,
            payload,
            before_write=before_write,
        )

    return await _operation(request, camera_name, operation, mutation=True)


@router.post(
    "/onvif/cameras/{camera_name}/administration/firmware",
    dependencies=ADMIN,
)
async def administration_firmware(
    camera_name: str,
    request: Request,
    firmware: UploadFile = File(...),
    catalog_revision: str = Form(..., pattern=r"^[a-f0-9]{64}$"),
    method: str = Form(...),
    typed_confirmation: str = Form(...),
    confirm_device_change: str = Form(...),
):
    """Submit a selected firmware file once, never fetching caller-supplied URLs."""
    if (
        method not in FIRMWARE_METHODS
        or typed_confirmation != method
        or confirm_device_change != "true"
    ):
        raise HTTPException(422, "Explicit firmware operation confirmation is required")
    try:
        payload = await firmware.read(MAX_BACKUP_BYTES + 1)
    finally:
        await firmware.close()
    if len(payload) > MAX_BACKUP_BYTES:
        raise HTTPException(413, "Camera firmware upload exceeds 32 MiB")
    if not payload:
        raise HTTPException(422, "Camera firmware upload is empty")

    async def operation(config, guard):
        def before_write():
            guard()
            request.app.onvif_devices.guard_administration(camera_name)

        return await upgrade_firmware(
            connection_for(config, camera_name),
            catalog_revision,
            method,
            payload,
            before_write=before_write,
        )

    return await _operation(request, camera_name, operation, mutation=True)


@router.post(
    "/onvif/cameras/{camera_name}/administration/{service}/{operation_name}",
    dependencies=ADMIN,
)
async def administration_operation(
    camera_name: str,
    service: str,
    operation_name: str,
    body: AdministrationOperation,
    request: Request,
):
    """Admin-only single operation with pinned endpoint, typed consent and no write retry."""

    async def operation(config, guard):
        def before_write():
            if not body.confirm_device_change:
                raise ValueError("Explicit device-change confirmation is required")
            guard()
            request.app.onvif_devices.guard_administration(camera_name)
            if (
                service == "ptz"
                and config.cameras[camera_name].onvif.autotracking.enabled
            ):
                raise OnvifSettingsConflict(
                    "Stop autotracking before PTZ administration"
                )

        return await invoke_operation(
            connection_for(config, camera_name),
            service,
            operation_name,
            body.parameters,
            body.catalog_revision,
            body.typed_confirmation,
            before_write=before_write,
        )

    return await _operation(request, camera_name, operation, mutation=True)


@router.put("/onvif/cameras/{camera_name}/focus", dependencies=ADMIN)
async def update_focus(camera_name: str, body: FocusUpdate, request: Request):
    """Move once, preserving physical image ownership and verifying camera position."""

    async def operation(config, guard):
        def before_write(profile_token):
            guard()
            request.app.onvif_devices.guard_manual_focus(camera_name, profile_token)

        return await write_focus(
            connection_for(config, camera_name, body.profile_token),
            body.position,
            body.speed,
            body.expected_revision,
            before_write=before_write,
        )

    return await _operation(request, camera_name, operation, mutation=True)


@router.get("/onvif/cameras/{camera_name}/presets", dependencies=READ)
async def presets(
    camera_name: str,
    request: Request,
    profile_token: str | None = Query(None, max_length=256),
):
    """Read presets only for an explicitly selected PTZ-capable media profile."""

    async def operation(config, guard):
        return await device_services.presets(
            connection_for(config, camera_name, profile_token)
        )

    return await _operation(request, camera_name, operation)


@router.post("/onvif/cameras/{camera_name}/presets", dependencies=ADMIN)
async def update_preset(camera_name: str, body: PresetUpdate, request: Request):
    """Create, replace, move to, or remove a confirmed camera-native preset."""

    async def operation(config, guard):
        if config.cameras[camera_name].onvif.autotracking.enabled:
            raise HTTPException(
                409, "Stop camera autotracking before a manual preset action"
            )
        return await device_services.presets(
            connection_for(config, camera_name, body.profile_token),
            body.action,
            body.token,
            body.name,
            guard,
        )

    return await _operation(request, camera_name, operation, mutation=True)


@router.get("/onvif/cameras/{camera_name}/events", dependencies=READ)
async def events(camera_name: str, request: Request):
    """Read bounded native-event history; reading does not create subscriptions."""

    async def operation(config, guard):
        monitor = request.app.onvif_devices._events.get(camera_name)
        return (
            monitor.snapshot()
            if monitor
            else {
                "state": "stopped",
                "events": [],
                "failures": 0,
                "integration": "status_history_only",
            }
        )

    return await _operation(request, camera_name, operation)


@router.post("/onvif/cameras/{camera_name}/events", dependencies=ADMIN)
async def control_events(camera_name: str, body: EventControl, request: Request):
    """Explicitly start or stop a runtime subscription with renewal and reconnect."""

    async def operation(config, guard):
        manager = request.app.onvif_devices
        monitor = manager._events.pop(camera_name, None)
        if monitor and monitor.task:
            monitor.task.cancel()
            await asyncio.gather(monitor.task, return_exceptions=True)
        if not body.enabled:
            return {"state": "stopped", "events": []}
        connection = connection_for(config, camera_name)
        capabilities = await device_services.device_status(connection)
        if not capabilities["services"]["events"]:
            raise ValueError("The camera did not advertise native event support")
        guard()
        source_tokens = None
        allow_unscoped = False
        if body.integrate_events:
            profiles = await device_services.native_event_sources(connection)
            selected = next(
                (
                    profile
                    for profile in profiles
                    if profile["token"] == body.source_profile_token
                ),
                None,
            )
            if selected is None:
                raise ValueError("The camera did not report the selected event source")
            source_tokens = {
                "VideoSourceToken": {selected["video_source_token"]},
                "VideoSourceConfigurationToken": {
                    profile["configuration_token"]
                    for profile in profiles
                    if profile["video_source_token"] == selected["video_source_token"]
                    and profile["configuration_token"]
                },
            }
            source_tokens = {
                key: value for key, value in source_tokens.items() if value
            }
            allow_unscoped = (
                len({profile["video_source_token"] for profile in profiles}) == 1
            )
        guard()
        monitor = NativeEventMonitor(
            connection,
            lambda: (
                not manager._closing
                and manager.config_provider() is config
                and camera_name in config.cameras
                and config.cameras[camera_name].enabled
            ),
            bridge=manager.native_event_bridge(
                camera_name,
                config,
                body.event_expiry_seconds,
                source_tokens,
                allow_unscoped,
            )
            if body.integrate_events
            else None,
        )
        manager._events[camera_name] = monitor
        monitor.task = asyncio.create_task(monitor.run())
        return monitor.snapshot()

    return await _operation(request, camera_name, operation)


@router.get("/onvif/cameras/{camera_name}/events/sources", dependencies=READ)
async def event_sources(camera_name: str, request: Request):
    """Explicitly read source choices for camera-native NVR integration."""

    async def operation(config, guard):
        return {
            "profiles": await device_services.native_event_sources(
                connection_for(config, camera_name)
            )
        }

    return await _operation(request, camera_name, operation)


@router.get("/onvif/cameras/{camera_name}/recordings", dependencies=READ)
async def recordings(
    camera_name: str, request: Request, start: datetime, end: datetime
):
    """Search at most 100 camera-native recordings overlapping an explicit interval."""
    if (
        start.tzinfo is None
        or end.tzinfo is None
        or start >= end
        or (end - start).total_seconds() > 7 * 86400
    ):
        raise HTTPException(
            422, "Use a timezone-aware interval of no more than seven days"
        )

    async def operation(config, guard):
        return await device_services.search_recordings(
            connection_for(config, camera_name),
            start.astimezone(UTC),
            end.astimezone(UTC),
            recording_binding(
                config, camera_name, getattr(request.app, "onvif_source_key", None)
            ),
        )

    return await _operation(request, camera_name, operation)


@router.get("/onvif/cameras/{camera_name}/replay", dependencies=READ)
async def replay(camera_name: str, request: Request):
    """Report browser replay limits, never disclose a device replay URI."""
    if camera_name not in request.app.frigate_config.cameras:
        raise HTTPException(404, "Camera is not configured")
    return {
        "state": "browser_sessions",
        "max_duration_seconds": MAX_REPLAY_SECONDS,
        "audio": "when_present",
        "audio_codec": "aac_lc",
        "audio_sample_rate": 48000,
        "seek": "new_fixed_range",
    }


def _replay_manager(request):
    manager = getattr(request.app, "onvif_replay", None)
    if manager is None or manager.closing:
        raise HTTPException(503, "Browser replay is unavailable")
    return manager


@router.post("/onvif/cameras/{camera_name}/replay/sessions", dependencies=READ)
async def create_replay(camera_name: str, body: ReplayRequest, request: Request):
    """Resolve a camera recording server-side and reserve a bounded browser session."""
    manager = _replay_manager(request)
    configuration = request.app.frigate_config
    if camera_name not in configuration.cameras:
        raise HTTPException(404, "Camera is not configured")
    try:
        binding = recording_binding(
            configuration, camera_name, getattr(request.app, "onvif_source_key", None)
        )
    except RecordingSourceDenied:
        raise HTTPException(
            403, "Save an unambiguous camera recording source binding before replay"
        ) from None
    try:
        manager.reserve(camera_name)
    except ReplayCapacityError:
        raise HTTPException(
            429, "Replay capacity is in use; close an existing replay first"
        ) from None
    try:
        # Replay discovery is read-only and cancellable, unlike uncertain physical
        # mutations. The reserved replay pool bounds these camera/DNS operations.
        async with asyncio.timeout(30):
            source = await device_services.replay_source(
                connection_for(configuration, camera_name),
                body.recording_token,
                body.start,
                body.end,
                binding,
            )
        if await request.is_disconnected():
            raise HTTPException(499, "Replay request disconnected")
        return manager.create(
            camera_name,
            source,
            body.start,
            body.end,
            configuration.cameras[camera_name].ffmpeg.ffmpeg_path,
            configuration,
            audio=body.audio,
        )
    except RecordingSourceDenied:
        raise HTTPException(
            403, "The recording source does not match this authorized camera"
        ) from None
    except DEVICE_ERRORS:
        raise HTTPException(502, "Camera replay discovery is unavailable") from None
    except ValueError:
        raise HTTPException(
            422,
            "The camera did not report a supported recording range or replay source",
        ) from None
    finally:
        manager.finish_resolution(camera_name)


@router.get("/onvif/cameras/{camera_name}/recording_binding", dependencies=READ)
async def recording_binding_status(camera_name: str, request: Request):
    """Report saved scope only, without any camera connectivity probe."""
    try:
        binding = recording_binding(
            request.app.frigate_config,
            camera_name,
            getattr(request.app, "onvif_source_key", None),
        )
    except RecordingSourceDenied:
        return {"configured": False}
    return {
        "configured": True,
        "profile_token": binding.profile_token,
        "video_source_token": binding.video_source_token,
        "source_ref": binding.source_ref,
    }


@router.get("/onvif/cameras/{camera_name}/recording_sources", dependencies=ADMIN)
async def recording_sources(camera_name: str, request: Request):
    """Only administrators may discover device-wide recording source identities."""

    async def operation(config, guard):
        return await device_services.discover_recording_sources(
            connection_for(config, camera_name),
            getattr(request.app, "onvif_source_key", None),
        )

    return await _operation(request, camera_name, operation)


@router.get(
    "/onvif/cameras/{camera_name}/replay/sessions/{session_id}", dependencies=READ
)
async def replay_session_status(camera_name: str, session_id: str, request: Request):
    """Read only an authorized camera's opaque playback session."""
    try:
        return _replay_manager(request).status(camera_name, session_id)
    except KeyError:
        raise HTTPException(404, "Replay session is not available") from None


@router.get(
    "/onvif/cameras/{camera_name}/replay/sessions/{session_id}/hls/{resource}",
    dependencies=READ,
)
async def replay_hls(
    camera_name: str, session_id: str, resource: str, request: Request
):
    """Authorize every HLS object from one bounded camera-native replay."""
    manager = _replay_manager(request)
    try:
        content = await manager.resource(camera_name, session_id, resource)
    except (KeyError, FileNotFoundError):
        raise HTTPException(404, "Replay session is not available") from None
    except ReplayUnsupported:
        raise HTTPException(
            422, "The camera does not provide supported bounded browser replay"
        ) from None
    except (OSError, TimeoutError):
        raise HTTPException(502, "Camera replay video is unavailable") from None

    return Response(
        content,
        media_type="application/vnd.apple.mpegurl"
        if resource.endswith(".m3u8")
        else "video/mp4",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )


@router.delete(
    "/onvif/cameras/{camera_name}/replay/sessions/{session_id}", dependencies=READ
)
async def close_replay(camera_name: str, session_id: str, request: Request):
    """Cancel the camera/FFmpeg session without exposing its upstream address."""
    manager = _replay_manager(request)
    try:
        session = manager.get(camera_name, session_id)
    except KeyError:
        return {"state": "closed"}
    await manager.close(session)
    return {"state": "closed"}

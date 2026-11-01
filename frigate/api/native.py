"""Strict native controls using existing camera services and owned restreams."""

import asyncio
import hashlib
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from onvif import ONVIFError
from peewee import DatabaseError
from zeep.exceptions import Fault, TransportError

from frigate.api.auth import allow_any_authenticated
from frigate.api.defs.response.live_webrtc_response import LiveWebRTCDiscovery
from frigate.api.defs.response.recorded_webrtc_response import RecordedWebRTCDiscovery
from frigate.api.notification import native_image_user
from frigate.comms.native_controls import (
    NativeControlConflict,
    NativeControlLeases,
    NativeControlUnavailable,
    NativePTZBody,
    PTZCommand,
)
from frigate.config import FrigateConfig
from frigate.models import NativeControlTombstone, User

logger = logging.getLogger(__name__)


class NativeRoute(APIRoute):
    """Keep rejected control payloads out of validation response bodies."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await original(request)
            except RequestValidationError as error:
                return JSONResponse(
                    {
                        "detail": [
                            {
                                "loc": item["loc"],
                                "msg": item["msg"],
                                "type": item["type"],
                            }
                            for item in error.errors()
                        ]
                    },
                    status_code=422,
                    headers={"Cache-Control": "private, no-store"},
                )

        return handler


router = APIRouter(
    prefix="/native",
    tags=["Native"],
    route_class=NativeRoute,
    dependencies=[Depends(allow_any_authenticated())],
)


def allowed_native_camera(
    config: FrigateConfig, user: dict, camera: str, *, control=False
):
    """Use the verified session's current database role, not a forwarded role claim."""
    if user["role"] not in config.auth.roles:
        raise HTTPException(403, "Camera account role is no longer authorized")
    if control and user["role"] != "admin":
        raise HTTPException(403, "Camera controls require the current admin role")
    allowed = User.get_allowed_cameras(
        user["role"], config.auth.roles, set(config.cameras)
    )
    if camera not in allowed:
        raise HTTPException(403, "Camera is not allowed for this account")
    selected = config.cameras.get(camera)
    if selected is None or not selected.enabled:
        raise HTTPException(404, "Camera is not available")
    return selected


@router.get("/features", openapi_extra={"x-required-role": "any"})
async def native_features(
    request: Request, user: dict = Depends(native_image_user)
) -> dict[str, bool | LiveWebRTCDiscovery | RecordedWebRTCDiscovery]:
    """Advertise implemented APIs, not unsupported per-camera hardware."""
    return {
        "controls": getattr(request.app.state, "native_controls", None) is not None,
        "export_protection": getattr(
            request.app.state, "native_export_protection", False
        ),
        "live_webrtc": LiveWebRTCDiscovery(
            enabled=request.app.frigate_config.live_webrtc.enabled,
            available=(
                getattr(request.app.state, "live_webrtc_manager", None) is not None
                and request.app.state.live_webrtc_manager.available()
            ),
        ),
        "recorded_webrtc": RecordedWebRTCDiscovery(
            enabled=request.app.frigate_config.live_webrtc.enabled,
            available=(
                getattr(request.app.state, "recorded_webrtc_manager", None) is not None
                and request.app.state.recorded_webrtc_manager.available()
            ),
        ),
    }


@router.get("/cameras/{camera}/controls", openapi_extra={"x-required-role": "camera"})
async def native_camera_controls(
    request: Request, camera: str, user: dict = Depends(native_image_user)
):
    """Use cached capabilities or bounded read-only discovery for a configured controller."""
    selected = allowed_native_camera(request.app.frigate_config, user, camera)
    controller = request.app.onvif
    cam = controller.cams.get(camera, {}) if controller is not None else {}
    if (
        user["role"] == "admin"
        and controller is not None
        and selected.onvif.host
        and not cam.get("init")
    ):
        try:
            future = asyncio.run_coroutine_threadsafe(
                controller.get_camera_info(camera), controller.loop
            )
            await asyncio.wait_for(asyncio.wrap_future(future), timeout=5)
        except (
            OSError,
            TimeoutError,
            ONVIFError,
            Fault,
            TransportError,
            httpx.HTTPError,
        ) as error:
            logger.warning(
                "Native ONVIF capability discovery unavailable (%s)",
                type(error).__name__,
            )
        cam = controller.cams.get(camera, {})
    usable = (
        user["role"] == "admin"
        and cam.get("init")
        and not selected.onvif.autotracking.enabled
    )
    features = cam.get("features", []) if usable else []
    presets = cam.get("presets", {}) if usable else {}
    return {
        "camera": camera,
        "ptz": {
            "supported": bool("pt" in features or "zoom" in features or presets),
            "pan_tilt": "pt" in features,
            "zoom": "zoom" in features,
            "presets": [{"id": token, "name": name} for name, token in presets.items()],
        },
        "talkback": {"supported": False},
    }


@router.post("/cameras/{camera}/ptz", openapi_extra={"x-required-role": "admin"})
async def native_ptz(
    request: Request,
    camera: str,
    body: NativePTZBody,
    user: dict = Depends(native_image_user),
):
    """Dispatch only through a short, current-account, server-owned control lease."""
    allowed_native_camera(request.app.frigate_config, user, camera, control=True)
    try:
        return await request.app.state.native_controls.command(
            camera, user["username"], body
        )
    except NativeControlConflict:
        raise HTTPException(
            409, "Camera control lease is stale or conflicting"
        ) from None
    except NativeControlUnavailable:
        raise HTTPException(409, "Camera controls are not available") from None
    except (
        OSError,
        TimeoutError,
        ONVIFError,
        Fault,
        TransportError,
        httpx.HTTPError,
    ) as error:
        logger.error(
            "Native PTZ transport failed for %s (%s)", camera, type(error).__name__
        )
        raise HTTPException(503, "Camera control transport is unavailable") from None


def initialize_native_controls(app) -> None:
    """Create no hardware sessions or commands at service startup."""

    async def transport(
        camera: str,
        command: PTZCommand,
        preset: str | None,
        lease_id: str,
        duration: int,
        owner: str,
    ) -> None:
        if command != "STOP":
            account = await asyncio.to_thread(User.get_or_none, User.username == owner)
            if account is None:
                raise NativeControlUnavailable("Camera account no longer exists")
            allowed_native_camera(
                app.frigate_config,
                {"username": owner, "role": account.role},
                camera,
                control=True,
            )
        if app.onvif is None:
            raise NativeControlUnavailable("Camera controller is unavailable")
        future = asyncio.run_coroutine_threadsafe(
            app.onvif.native_command(camera, command, preset, lease_id, duration),
            app.onvif.loop,
        )
        await asyncio.wrap_future(future)

    def reserve(camera: str, owner: str, lease_id: str, closed_at: float) -> bool:
        identity = hashlib.sha256(f"{camera}\0{owner}\0{lease_id}".encode()).hexdigest()
        try:
            if (
                NativeControlTombstone.get_or_none(
                    NativeControlTombstone.id == identity
                )
                is not None
            ):
                return False
            if NativeControlTombstone.select().count() >= 250_000:
                raise NativeControlUnavailable("Persistent PTZ replay capacity is full")
            _, created = NativeControlTombstone.get_or_create(
                id=identity,
                defaults={
                    "camera": camera,
                    "owner": owner,
                    "lease_id": lease_id,
                    "closed_at": closed_at,
                },
            )
            return created
        except DatabaseError as error:
            logger.error(
                "Native PTZ replay storage unavailable (%s)", type(error).__name__
            )
            raise NativeControlUnavailable(
                "Persistent PTZ replay protection is unavailable"
            ) from error

    app.state.native_controls = NativeControlLeases(
        transport, reserve_tombstone=reserve
    )

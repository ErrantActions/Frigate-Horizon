"""Strict native controls using existing camera services and owned restreams."""

import asyncio
import hashlib
import logging
import time
from typing import Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import OctKey
from onvif import ONVIFError
from peewee import DatabaseError
from pydantic import BaseModel, ConfigDict, Field, field_validator
from zeep.exceptions import Fault, TransportError

from frigate.api.auth import allow_any_authenticated
from frigate.api.notification import native_image_user
from frigate.comms.native_controls import (
    NativeControlConflict,
    NativeControlLeases,
    NativeControlUnavailable,
    NativePTZBody,
    PTZCommand,
)
from frigate.comms.native_talkback import (
    NativeTalkbackConflict,
    NativeTalkbackLeases,
    NativeTalkbackUnavailable,
    OwnedTalkbackTransport,
    TalkbackIdentity,
)
from frigate.config import FrigateConfig
from frigate.models import NativeControlTombstone, User

logger = logging.getLogger(__name__)


class NativeRoute(APIRoute):
    """Keep rejected SDP/ICE credentials out of validation response bodies."""

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


def audio_only_sdp(value: str) -> str:
    """Permit one encrypted WebRTC audio section, never video or data channels."""
    if "\0" in value or not value.startswith("v=0"):
        raise ValueError("Invalid audio SDP")
    media = [line.split() for line in value.splitlines() if line.startswith("m=")]
    if (
        len(media) != 1
        or len(media[0]) < 4
        or media[0][0] != "m=audio"
        or media[0][1] == "0"
        or media[0][2] not in ("UDP/TLS/RTP/SAVPF", "TCP/TLS/RTP/SAVPF")
        or not any(line.startswith("a=fingerprint:") for line in value.splitlines())
    ):
        raise ValueError("An audio-only encrypted WebRTC offer is required")
    return value


class NativeTalkbackBody(BaseModel):
    """Only session descriptions are accepted, never destinations or credentials."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["offer"]
    sdp: str = Field(min_length=10, max_length=131072, repr=False)

    @field_validator("sdp")
    @classmethod
    def audio_offer(cls, value: str) -> str:
        """Validate media before any go2rtc transport request."""
        return audio_only_sdp(value)


async def _backchannel_stream(request: Request, camera: str) -> str | None:
    config = request.app.frigate_config.cameras[camera]
    streams = set(config.live.streams.values())
    try:
        async with httpx.AsyncClient(
            timeout=2, follow_redirects=False, trust_env=False
        ) as client:
            response = await client.get("http://127.0.0.1:1984/api/streams")
            response.raise_for_status()
            if len(response.content) > 1024 * 1024:
                raise ValueError("Restream metadata exceeds its bound")
            values = response.json()
    except (httpx.HTTPError, ValueError) as error:
        logger.warning(
            "Native talkback capabilities unavailable (%s)", type(error).__name__
        )
        return None
    if not isinstance(values, dict):
        logger.warning("Native talkback restream metadata is invalid")
        return None
    for name in sorted(streams):
        stream = values.get(name, {})
        if not isinstance(stream, dict):
            continue
        audio_send = audio_receive = False
        for producer in stream.get("producers") or []:
            if not isinstance(producer, dict):
                continue
            for media in producer.get("medias") or []:
                if isinstance(media, str):
                    parts = [part.strip().lower() for part in media.split(",")]
                    if len(parts) >= 2 and parts[0] == "audio":
                        audio_send |= parts[1] in ("sendonly", "sendrecv")
                        audio_receive |= parts[1] in ("recvonly", "sendrecv")
        if audio_send and audio_receive:
            return name
    return None


@router.get("/features", openapi_extra={"x-required-role": "any"})
async def native_features(request: Request, user: dict = Depends(native_image_user)):
    """Advertise implemented APIs, not unsupported per-camera hardware."""
    return {
        "controls": getattr(request.app.state, "native_controls", None) is not None,
        "export_protection": getattr(
            request.app.state, "native_export_protection", False
        ),
    }


@router.get(
    "/cameras/{camera}/controls", openapi_extra={"x-required-role": "camera"}
)
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
        "talkback": {
            "supported": user["role"] == "admin"
            and await request.app.state.native_talkback.transport.available()
            and await _backchannel_stream(request, camera) is not None
        },
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


@router.post("/cameras/{camera}/talkback", openapi_extra={"x-required-role": "admin"})
async def native_talkback(
    request: Request,
    camera: str,
    body: NativeTalkbackBody,
    user: dict = Depends(native_image_user),
):
    """Exchange audio SDP only through this camera's verified configured backchannel."""
    allowed_native_camera(request.app.frigate_config, user, camera, control=True)
    stream = await _backchannel_stream(request, camera)
    if stream is None:
        raise HTTPException(409, "This camera has no verified talkback backchannel")
    identity = _talkback_identity(request, user)
    manager = request.app.state.native_talkback
    try:
        answer = await manager.create(camera, identity, stream, body.sdp)
        try:
            audio_only_sdp(answer["sdp"])
            if await request.is_disconnected():
                raise NativeTalkbackConflict("Camera audio request disconnected")
        except (ValueError, TypeError):
            await manager.release(answer["lease_id"], camera, identity)
            raise
    except NativeTalkbackConflict:
        raise HTTPException(409, "Camera audio is busy or its lease expired") from None
    except PermissionError:
        raise HTTPException(403, "Camera audio access is no longer allowed") from None
    except (httpx.HTTPError, ValueError) as error:
        logger.error(
            "Native talkback exchange failed for %s (%s)", camera, type(error).__name__
        )
        raise HTTPException(503, "Camera talkback transport is unavailable") from None
    return JSONResponse(
        {"type": "answer", "sdp": answer["sdp"]},
        headers={
            "Cache-Control": "private, no-store",
            "X-Horizon-Talkback-Lease": answer["lease_id"],
            "X-Horizon-Talkback-Expires": str(answer["expires_at"]),
            "X-Horizon-Talkback-Renew-After-Ms": "5000",
        },
    )


def _signing_generation(app) -> str:
    key = app.jwt_token
    if isinstance(key, OctKey):
        key = key.raw_value
    elif isinstance(key, str):
        key = key.encode()
    if not isinstance(key, (bytes, bytearray)) or not key:
        raise ValueError("Camera audio signing key is unavailable")
    return hashlib.sha256(key).hexdigest()


def _talkback_identity(request: Request, user: dict) -> TalkbackIdentity:
    header = request.headers.get("authorization", "")
    encoded = (
        header.removeprefix("Bearer ")
        if header.startswith("Bearer ")
        else request.cookies.get(request.app.frigate_config.auth.cookie_name)
    )
    try:
        if not isinstance(encoded, str):
            raise ValueError("Missing session")
        claims = jwt.decode(encoded, request.app.jwt_token, algorithms=["HS256"]).claims
        if claims.get("sub") != user["username"]:
            raise ValueError("Session changed")
        return TalkbackIdentity(
            user["username"],
            hashlib.sha256(encoded.encode()).hexdigest(),
            float(claims["iat"]),
            float(claims["exp"]),
            _signing_generation(request.app),
        )
    except (ValueError, TypeError, KeyError, JoseError):
        raise HTTPException(
            401, "A valid current camera audio session is required"
        ) from None


@router.post(
    "/cameras/{camera}/talkback/{lease_id}/renew",
    openapi_extra={"x-required-role": "admin"},
)
async def renew_native_talkback(
    request: Request,
    camera: str,
    lease_id: UUID,
    user: dict = Depends(native_image_user),
):
    """Reauthorize current admin and camera rights before renewing an owned lease."""
    identity = _talkback_identity(request, user)
    try:
        return await request.app.state.native_talkback.renew(
            str(lease_id), camera, identity
        )
    except NativeTalkbackConflict:
        raise HTTPException(409, "Camera audio lease expired or changed") from None
    except PermissionError:
        raise HTTPException(403, "Camera audio access was revoked") from None
    except (httpx.HTTPError, ValueError) as error:
        logger.warning("Camera audio renewal unavailable (%s)", type(error).__name__)
        raise HTTPException(503, "Camera audio renewal is unavailable") from None


@router.delete(
    "/cameras/{camera}/talkback/{lease_id}",
    openapi_extra={"x-required-role": "any"},
)
async def close_native_talkback(
    request: Request,
    camera: str,
    lease_id: UUID,
    user: dict = Depends(native_image_user),
):
    """Release only an owned lease without requiring ongoing control permission."""
    try:
        await request.app.state.native_talkback.release(
            str(lease_id), camera, _talkback_identity(request, user)
        )
    except NativeTalkbackConflict:
        raise HTTPException(404, "Camera audio lease is unavailable") from None
    except (httpx.HTTPError, ValueError) as error:
        logger.warning("Camera audio cleanup pending (%s)", type(error).__name__)
        raise HTTPException(
            503, "Camera audio cleanup awaits its bounded expiry"
        ) from None
    return {"success": True}


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

    async def authorize_audio(identity: TalkbackIdentity, camera: str) -> None:
        if (
            identity.expires_at <= time.time()
            or identity.signing_generation != _signing_generation(app)
        ):
            raise PermissionError("Camera audio session expired")
        try:
            account = await asyncio.to_thread(
                User.get_or_none, User.username == identity.username
            )
        except DatabaseError as error:
            raise NativeTalkbackUnavailable(
                "Camera audio account verification is unavailable"
            ) from error
        if account is None or (
            account.password_changed_at is not None
            and identity.issued_at < account.password_changed_at.timestamp()
        ):
            raise PermissionError("Camera audio account changed")
        try:
            allowed_native_camera(
                app.frigate_config,
                {"username": identity.username, "role": account.role},
                camera,
                control=True,
            )
        except HTTPException as error:
            raise PermissionError("Camera audio permissions changed") from error

    app.state.native_talkback = NativeTalkbackLeases(
        OwnedTalkbackTransport(), authorize_audio
    )

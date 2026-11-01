"""Current-account authenticated recorded WebRTC, without source or HLS endpoints."""

import asyncio
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from joserfc import jwt

from frigate.api.auth import allow_any_authenticated, require_camera_access
from frigate.api.defs.request.recorded_webrtc_body import (
    RecordedAnswer,
    RecordedCandidates,
    RecordedControl,
    RecordedCreate,
    RecordedQuality,
)
from frigate.api.defs.response.recorded_webrtc_response import (
    RecordedWebRTCSession,
    RecordedWebRTCStatus,
)
from frigate.api.live_webrtc import HEADERS, public_error
from frigate.api.native import NativeRoute
from frigate.api.notification import native_image_user
from frigate.comms.live_webrtc import DETAIL_BITRATES, DETAIL_HEIGHTS, LiveWebRTCError
from frigate.comms.recorded_webrtc import RecordedWebRTCManager
from frigate.models import User


class RecordedRoute(NativeRoute):
    """Apply private no-store headers to dependency and camera-access failures too."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def private_handler(request: Request):
            try:
                return await handler(request)
            except HTTPException as error:
                error.headers = {**(error.headers or {}), **HEADERS}
                raise

        return private_handler


router = APIRouter(
    prefix="/{camera_name}/recordings/webrtc",
    tags=["Media"],
    route_class=RecordedRoute,
    dependencies=[
        Depends(allow_any_authenticated()),
        Depends(require_camera_access),
    ],
)


def manager_for(request: Request) -> RecordedWebRTCManager:
    """Only use the lifespan-owned manager and its shared admission pool."""
    manager = getattr(request.app.state, "recorded_webrtc_manager", None)
    if manager is None:
        raise HTTPException(503, "recorded_webrtc_not_started", headers=HEADERS)
    return manager


def check_camera(request: Request, camera_name: str, user: dict) -> None:
    """Authorize historical media independently of current capture/live availability."""
    config = request.app.frigate_config
    if user["role"] not in config.auth.roles:
        raise HTTPException(403, "Camera account role is no longer authorized")
    allowed = User.get_allowed_cameras(
        user["role"], config.auth.roles, set(config.cameras)
    )
    if camera_name not in allowed:
        raise HTTPException(403, "Camera is not allowed for this account")
    if camera_name not in config.cameras:
        raise HTTPException(404, "Camera is not available")


@router.get(
    "/status",
    response_model=RecordedWebRTCStatus,
    openapi_extra={"x-required-role": "camera"},
)
async def recorded_webrtc_status(
    request: Request,
    camera_name: str,
    response: Response,
    user: dict = Depends(native_image_user),
):
    """Discover finite, on-demand recording windows without opening originals."""
    check_camera(request, camera_name, user)
    settings = request.app.frigate_config.live_webrtc
    available = manager_for(request).available()
    response.headers.update(HEADERS)
    return {
        "enabled": settings.enabled,
        "available": available,
        "session_url": f"{quote(camera_name, safe='')}/recordings/webrtc/sessions",
        "relay_available": available and settings.turn.enabled,
        "renditions": [
            {"height": height, "bitrate": DETAIL_BITRATES[height]}
            for height in DETAIL_HEIGHTS
            if height <= settings.max_height
        ],
        "reason": None if available else "recorded_webrtc_disabled_or_restart_required",
    }


@router.post(
    "/sessions",
    status_code=201,
    response_model=RecordedWebRTCSession,
    openapi_extra={"x-required-role": "camera"},
)
async def create_recorded_webrtc_session(
    request: Request,
    camera_name: str,
    body: RecordedCreate,
    response: Response,
    user: dict = Depends(native_image_user),
):
    """Create one owned viewer; token expiry is a nonrenewable hard deadline."""
    check_camera(request, camera_name, user)
    config = request.app.frigate_config
    encoded = request.headers.get("authorization", "")
    encoded = (
        encoded.removeprefix("Bearer ")
        if encoded.startswith("Bearer ")
        else request.cookies.get(config.auth.cookie_name)
    )
    # native_image_user has verified this exact token and its current account.
    claims = jwt.decode(encoded, request.app.jwt_token, algorithms=["HS256"]).claims
    manager = manager_for(request)
    try:
        result = await manager.create(
            camera_name,
            user["username"],
            body,
            claims["exp"],
            auth_issued=claims["iat"],
        )
    except LiveWebRTCError as error:
        raise public_error(error) from None
    try:
        disconnected = await request.is_disconnected()
    except asyncio.CancelledError:
        await manager.delete(camera_name, user["username"], result["session_id"])
        raise
    if disconnected:
        await manager.delete(camera_name, user["username"], result["session_id"])
        raise HTTPException(499, "recorded_webrtc_viewer_disconnected", headers=HEADERS)
    response.headers.update(HEADERS)
    return result


@router.post(
    "/sessions/{session_id}/answer",
    openapi_extra={"x-required-role": "camera"},
)
async def answer_recorded_webrtc_session(
    request: Request,
    camera_name: str,
    session_id: UUID,
    body: RecordedAnswer,
    user: dict = Depends(native_image_user),
):
    """Answer only the current generation, never a peer replaced by a seek."""
    check_camera(request, camera_name, user)
    try:
        await manager_for(request).answer(
            camera_name, user["username"], str(session_id), body
        )
    except LiveWebRTCError as error:
        raise public_error(error) from None
    return Response(status_code=204, headers=HEADERS)


@router.post(
    "/sessions/{session_id}/candidates",
    openapi_extra={"x-required-role": "camera"},
)
async def candidates_recorded_webrtc_session(
    request: Request,
    camera_name: str,
    session_id: UUID,
    body: RecordedCandidates,
    user: dict = Depends(native_image_user),
):
    """Forward bounded ICE on this account's current generation only."""
    check_camera(request, camera_name, user)
    try:
        await manager_for(request).candidates(
            camera_name, user["username"], str(session_id), body
        )
    except LiveWebRTCError as error:
        raise public_error(error) from None
    return Response(status_code=204, headers=HEADERS)


@router.patch(
    "/sessions/{session_id}/quality",
    openapi_extra={"x-required-role": "camera"},
)
async def quality_recorded_webrtc_session(
    request: Request,
    camera_name: str,
    session_id: UUID,
    body: RecordedQuality,
    user: dict = Depends(native_image_user),
):
    """Select automatic or bounded manual quality without replacing the peer."""
    check_camera(request, camera_name, user)
    try:
        await manager_for(request).quality(
            camera_name, user["username"], str(session_id), body
        )
    except LiveWebRTCError as error:
        raise public_error(error) from None
    return Response(status_code=204, headers=HEADERS)


@router.post(
    "/sessions/{session_id}/control",
    response_model=RecordedWebRTCSession,
    openapi_extra={"x-required-role": "camera"},
)
async def control_recorded_webrtc_session(
    request: Request,
    camera_name: str,
    session_id: UUID,
    body: RecordedControl,
    response: Response,
    user: dict = Depends(native_image_user),
):
    """Activate decoded warmup on its peer; resume/seek return a new warming peer."""
    check_camera(request, camera_name, user)
    manager = manager_for(request)
    try:
        result = await manager.control(
            camera_name, user["username"], str(session_id), body
        )
    except LiveWebRTCError as error:
        raise public_error(error) from None
    try:
        disconnected = await request.is_disconnected()
    except asyncio.CancelledError:
        await manager.delete(camera_name, user["username"], str(session_id))
        raise
    if disconnected:
        await manager.delete(camera_name, user["username"], str(session_id))
        raise HTTPException(499, "recorded_webrtc_viewer_disconnected", headers=HEADERS)
    response.headers.update(HEADERS)
    return result


@router.patch(
    "/sessions/{session_id}",
    response_model=RecordedWebRTCSession,
    openapi_extra={"x-required-role": "camera"},
)
async def renew_recorded_webrtc_session(
    request: Request,
    camera_name: str,
    session_id: UUID,
    response: Response,
    user: dict = Depends(native_image_user),
):
    """Renew the lease, returning a source-read cursor or explicit unknown position."""
    check_camera(request, camera_name, user)
    try:
        result = await manager_for(request).renew(
            camera_name, user["username"], str(session_id)
        )
    except LiveWebRTCError as error:
        raise public_error(error) from None
    response.headers.update(HEADERS)
    return result


@router.delete(
    "/sessions/{session_id}",
    openapi_extra={"x-required-role": "camera"},
)
async def close_recorded_webrtc_session(
    request: Request,
    camera_name: str,
    session_id: UUID,
    user: dict = Depends(native_image_user),
):
    """Close the account-owned viewer, retaining uncertain native cleanup reservations."""
    check_camera(request, camera_name, user)
    try:
        await manager_for(request).delete(
            camera_name, user["username"], str(session_id)
        )
    except LiveWebRTCError as error:
        raise public_error(error) from None
    return Response(status_code=204, headers=HEADERS)

"""Authenticated, camera-scoped WebRTC viewing over Frigate's HTTPS API."""

import logging
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from joserfc import jwt

from frigate.api.auth import allow_any_authenticated, require_camera_access
from frigate.api.defs.response.live_webrtc_response import (
    LiveWebRTCLease,
    LiveWebRTCSession,
    LiveWebRTCStatus,
)
from frigate.api.native import NativeRoute, allowed_native_camera
from frigate.api.notification import native_image_user
from frigate.comms.live_webrtc import (
    DETAIL_BITRATES,
    DETAIL_HEIGHTS,
    AnswerBody,
    CandidateBody,
    LiveWebRTCError,
    LiveWebRTCManager,
    QualityBody,
    ViewerBody,
)

logger = logging.getLogger(__name__)
HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
router = APIRouter(
    prefix="/{camera_name}/live/webrtc",
    tags=["Media"],
    route_class=NativeRoute,
    dependencies=[
        Depends(allow_any_authenticated()),
        Depends(require_camera_access),
    ],
)


def manager_for(request: Request) -> LiveWebRTCManager:
    """Require the managed lifecycle instance, never start media from an API request."""
    manager = getattr(request.app.state, "live_webrtc_manager", None)
    if manager is None:
        raise HTTPException(503, "live_webrtc_not_started", headers=HEADERS)
    return manager


def check_camera(request: Request, camera_name: str, user: dict) -> None:
    """Recheck the current account and camera configuration on every operation."""
    allowed_native_camera(request.app.frigate_config, user, camera_name)


def public_error(error: LiveWebRTCError) -> HTTPException:
    """Only predefined nonsecret codes cross the viewing API boundary."""
    return HTTPException(error.status, error.code, headers=HEADERS)


@router.get(
    "/status",
    response_model=LiveWebRTCStatus,
    openapi_extra={"x-required-role": "camera"},
)
async def live_webrtc_status(
    request: Request,
    camera_name: str,
    response: Response,
    user: dict = Depends(native_image_user),
):
    """Advertise configured on-demand viewing without starting any camera pull."""
    check_camera(request, camera_name, user)
    response.headers.update(HEADERS)
    settings = request.app.frigate_config.live_webrtc
    available = settings.enabled and manager_for(request).available()
    return {
        "version": 1,
        "protocol": "webrtc",
        "enabled": settings.enabled,
        "available": available,
        "session_url": f"{quote(camera_name, safe='')}/live/webrtc/sessions",
        "grid_height": settings.grid_height,
        "grid_rendition": {"height": settings.grid_height, "bitrate": 500},
        "relay_available": available and settings.turn.enabled,
        "renditions": [
            {"height": height, "bitrate": DETAIL_BITRATES.get(height, 500)}
            for height in sorted(
                {
                    settings.grid_height,
                    *[
                        height
                        for height in DETAIL_HEIGHTS
                        if height <= settings.max_height
                    ],
                }
            )
        ],
        "reason": None if available else "live_webrtc_disabled_or_restart_required",
    }


@router.post(
    "/sessions",
    status_code=201,
    response_model=LiveWebRTCSession,
    openapi_extra={"x-required-role": "camera"},
)
async def create_live_webrtc_session(
    request: Request,
    camera_name: str,
    body: ViewerBody,
    response: Response,
    user: dict = Depends(native_image_user),
):
    """Create one owned viewer lease and a shared source pull if needed."""
    check_camera(request, camera_name, user)
    config = request.app.frigate_config
    encoded = request.headers.get("authorization", "")
    encoded = (
        encoded.removeprefix("Bearer ")
        if encoded.startswith("Bearer ")
        else request.cookies.get(config.auth.cookie_name)
    )
    # native_image_user has verified this exact token and its current account.
    expires = jwt.decode(encoded, request.app.jwt_token, algorithms=["HS256"]).claims[
        "exp"
    ]
    try:
        result = await manager_for(request).create(
            camera_name, user["username"], body, expires
        )
    except LiveWebRTCError as error:
        raise public_error(error) from None
    if await request.is_disconnected():
        await manager_for(request).delete(
            camera_name, user["username"], result["session_id"]
        )
        raise HTTPException(499, "live_webrtc_viewer_disconnected", headers=HEADERS)
    response.headers.update(HEADERS)
    return result


@router.post(
    "/sessions/{session_id}/answer", openapi_extra={"x-required-role": "camera"}
)
async def answer_live_webrtc_session(
    request: Request,
    camera_name: str,
    session_id: UUID,
    body: AnswerBody,
    user: dict = Depends(native_image_user),
):
    """Answer only the peer offered to this account and camera."""
    check_camera(request, camera_name, user)
    try:
        await manager_for(request).answer(
            camera_name, user["username"], str(session_id), body
        )
    except LiveWebRTCError as error:
        raise public_error(error) from None
    return Response(status_code=204, headers=HEADERS)


@router.post(
    "/sessions/{session_id}/candidates", openapi_extra={"x-required-role": "camera"}
)
async def candidate_live_webrtc_session(
    request: Request,
    camera_name: str,
    session_id: UUID,
    body: CandidateBody,
    user: dict = Depends(native_image_user),
):
    """Forward bounded client ICE candidates through the existing authenticated API."""
    check_camera(request, camera_name, user)
    try:
        await manager_for(request).candidates(
            camera_name, user["username"], str(session_id), body
        )
    except LiveWebRTCError as error:
        raise public_error(error) from None
    return Response(status_code=204, headers=HEADERS)


@router.patch(
    "/sessions/{session_id}",
    response_model=LiveWebRTCLease,
    openapi_extra={"x-required-role": "camera"},
)
async def renew_live_webrtc_session(
    request: Request,
    camera_name: str,
    session_id: UUID,
    response: Response,
    user: dict = Depends(native_image_user),
):
    """Renew the viewer lease only while its current account still has camera access."""
    check_camera(request, camera_name, user)
    try:
        result = await manager_for(request).renew(
            camera_name, user["username"], str(session_id)
        )
    except LiveWebRTCError as error:
        raise public_error(error) from None
    response.headers.update(HEADERS)
    return result


@router.patch(
    "/sessions/{session_id}/quality", openapi_extra={"x-required-role": "camera"}
)
async def quality_live_webrtc_session(
    request: Request,
    camera_name: str,
    session_id: UUID,
    body: QualityBody,
    user: dict = Depends(native_image_user),
):
    """Change an advertised rendition on the existing peer rather than reconnecting."""
    check_camera(request, camera_name, user)
    try:
        await manager_for(request).quality(
            camera_name, user["username"], str(session_id), body
        )
    except LiveWebRTCError as error:
        raise public_error(error) from None
    return Response(status_code=204, headers=HEADERS)


@router.delete("/sessions/{session_id}", openapi_extra={"x-required-role": "camera"})
async def close_live_webrtc_session(
    request: Request,
    camera_name: str,
    session_id: UUID,
    user: dict = Depends(native_image_user),
):
    """Stop the owned viewer and release source encoding after the last viewer leaves."""
    check_camera(request, camera_name, user)
    try:
        await manager_for(request).delete(
            camera_name, user["username"], str(session_id)
        )
    except LiveWebRTCError as error:
        raise public_error(error) from None
    return Response(status_code=204, headers=HEADERS)

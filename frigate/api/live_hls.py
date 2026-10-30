"""Authenticated live HLS masters and camera-scoped private media resources."""

from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from frigate.api.auth import allow_any_authenticated, require_camera_access
from frigate.api.defs.response.live_hls_response import (
    LiveHlsRenditionInfo,
    LiveHlsStatus,
)
from frigate.api.defs.tags import Tags
from frigate.comms.live_hls import LiveHlsError, LiveHlsManager
from frigate.util.live_hls import (
    RESOURCE,
    LiveHlsRendition,
    camera_renditions,
)

HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
router = APIRouter(
    tags=[Tags.media],
    prefix="/{camera_name}/live/hls",
    dependencies=[
        Depends(allow_any_authenticated()),
        Depends(require_camera_access),
    ],
)


def plan_for(request: Request, camera_name: str) -> tuple[LiveHlsRendition, ...]:
    """Recheck the current config on every playlist and media request."""
    config = request.app.frigate_config
    if not config.ui.live_hls:
        raise HTTPException(503, "live_hls_disabled")
    if camera_name != "birdseye" and camera_name not in config.cameras:
        raise HTTPException(404, "camera_not_found")
    plan = camera_renditions(config, camera_name)
    if not plan:
        raise HTTPException(404, "live_hls_ladder_unavailable")
    return plan


def manager_for(request: Request) -> LiveHlsManager:
    """Require initialized lifecycle integration rather than an ad hoc client."""
    manager = getattr(request.app.state, "live_hls_manager", None)
    if manager is None:
        raise HTTPException(503, "live_hls_not_started")
    return manager


@router.get("/status", response_model=LiveHlsStatus)
async def live_hls_status(
    request: Request, camera_name: str, response: Response
) -> LiveHlsStatus:
    """Return whether this authorized camera has a configured live HLS ladder."""
    config = request.app.frigate_config
    response.headers.update(HEADERS)
    if camera_name != "birdseye" and camera_name not in config.cameras:
        raise HTTPException(404, "camera_not_found")
    plan = camera_renditions(config, camera_name)
    available = config.ui.live_hls and bool(plan)
    if available:
        try:
            await manager_for(request).sync_config()
        except LiveHlsError as error:
            raise HTTPException(error.status, error.code, headers=HEADERS) from None
    return LiveHlsStatus(
        enabled=config.ui.live_hls,
        available=available,
        playlist=f"{quote(camera_name, safe='')}/live/hls/master.m3u8"
        if available
        else None,
        renditions=[
            LiveHlsRenditionInfo(height=value.height, bitrate=value.bitrate)
            for value in plan
        ]
        if available
        else [],
    )


@router.get(
    "/master.m3u8",
    response_class=Response,
    responses={
        200: {
            "content": {"application/vnd.apple.mpegurl": {"schema": {"type": "string"}}}
        }
    },
)
async def live_hls_master(
    request: Request,
    camera_name: str,
    audio: bool = Query(True),
    height: int | None = Query(None, ge=144, le=2160),
    max_height: int | None = Query(None, ge=144, le=2160),
) -> Response:
    """Advertise a low-first bitrate ladder without exposing private upstream URLs."""
    plan = plan_for(request, camera_name)
    if height is not None:
        plan = tuple(value for value in plan if value.height == height)
    if max_height is not None:
        plan = tuple(value for value in plan if value.height <= max_height)
    if not plan:
        raise HTTPException(404, "live_hls_rendition_not_found")
    try:
        text = await manager_for(request).master(camera_name, plan, audio=audio)
    except LiveHlsError as error:
        raise HTTPException(error.status, error.code) from None
    return Response(
        text,
        media_type="application/vnd.apple.mpegurl",
        headers=HEADERS,
    )


@router.get(
    "/{height}p/{resource:path}",
    response_class=Response,
    responses={
        200: {
            "content": {
                "application/vnd.apple.mpegurl": {"schema": {"type": "string"}},
                "video/mp4": {"schema": {"type": "string", "format": "binary"}},
            }
        }
    },
)
async def live_hls_resource(
    request: Request,
    camera_name: str,
    height: int,
    resource: str,
    msn: int | None = Query(None, ge=0, le=2**63 - 1, alias="_HLS_msn"),
    part: int | None = Query(None, ge=0, le=1_000_000, alias="_HLS_part"),
    skip: Literal["YES", "v2"] | None = Query(None, alias="_HLS_skip"),
) -> Response:
    """Proxy only this camera's advertised variants and bounded flat resources."""
    plan = plan_for(request, camera_name)
    if height not in {value.height for value in plan}:
        raise HTTPException(404, "live_hls_rendition_not_found")
    if not RESOURCE.fullmatch(resource) or ".." in resource:
        raise HTTPException(404, "live_hls_resource_not_found")
    if part is not None and msn is None:
        raise HTTPException(400, "live_hls_part_requires_sequence")
    params = {}
    if msn is not None:
        params["_HLS_msn"] = str(msn)
    if part is not None:
        params["_HLS_part"] = str(part)
    if skip is not None:
        params["_HLS_skip"] = skip
    try:
        data = await manager_for(request).fetch(camera_name, height, resource, params)
    except LiveHlsError as error:
        raise HTTPException(error.status, error.code) from None
    return Response(
        data,
        media_type="application/vnd.apple.mpegurl"
        if resource.endswith(".m3u8")
        else "video/mp4",
        headers=HEADERS,
    )

"""Authenticated, camera-scoped compatibility playback of stored recordings."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from frigate.api.auth import allow_any_authenticated, require_camera_access
from frigate.api.defs.request.playback_body import PlaybackCreateBody
from frigate.api.defs.response.playback_response import PlaybackStatus
from frigate.api.defs.tags import Tags
from frigate.record.playback import (
    RESOURCE,
    PlaybackError,
    PlaybackManager,
    PlaybackSession,
)


async def private_response(response: Response) -> None:
    """Prevent shared caches from storing any session metadata."""
    response.headers["Cache-Control"] = "private, no-store"


router = APIRouter(
    tags=[Tags.media],
    prefix="/{camera_name}/recordings/playback-compatibility",
    dependencies=[
        Depends(allow_any_authenticated()),
        Depends(require_camera_access),
        Depends(private_response),
    ],
)


def manager_for(request: Request) -> PlaybackManager:
    """Fail explicitly if lifecycle integration has not started the manager."""
    manager = getattr(request.app, "playback_manager", None)
    if manager is None or manager.root is None:
        raise HTTPException(503, "playback_not_started")
    return manager


def owner_for(request: Request) -> str:
    """Use the trusted auth identity, not a caller-supplied session credential."""
    return request.headers.get("remote-user", "anonymous")


def session_for(
    request: Request, camera_name: str, session_id: str, *, cleanup: bool = False
) -> tuple[PlaybackManager, PlaybackSession]:
    """Recheck ownership after route dependencies recheck current camera access."""
    manager = manager_for(request)
    try:
        if not cleanup:
            manager.check_enabled()
        return manager, manager.get(session_id, camera_name, owner_for(request))
    except PlaybackError as error:
        raise HTTPException(error.status, error.code) from None


@router.post("", response_model=PlaybackStatus, status_code=202)
async def create_playback(
    request: Request, camera_name: str, body: PlaybackCreateBody
) -> PlaybackStatus:
    """Reserve and asynchronously normalize one authorized recording window."""
    manager = manager_for(request)
    creator = asyncio.create_task(manager.create(camera_name, owner_for(request), body))
    try:
        session = await asyncio.shield(creator)
    except asyncio.CancelledError:
        # Creation can be inside a thread-backed DB query. Do not leave an
        # unclaimed reservation or encoder when the requesting client leaves.
        result = await asyncio.gather(creator, return_exceptions=True)
        if not isinstance(result[0], BaseException):
            await manager.cancel(result[0])
        raise
    except PlaybackError as error:
        raise HTTPException(error.status, error.code) from None
    if await request.is_disconnected():
        await manager.cancel(session)
        raise HTTPException(499, "playback_client_disconnected")
    return manager.status(session)


@router.get("/{session_id}", response_model=PlaybackStatus)
async def playback_status(
    request: Request, camera_name: str, session_id: str
) -> PlaybackStatus:
    """Return terminal failures or measured progress without exposing source paths."""
    manager, session = session_for(request, camera_name, session_id)
    return manager.status(session)


@router.post("/{session_id}/keepalive", response_model=PlaybackStatus)
async def playback_keepalive(
    request: Request, camera_name: str, session_id: str
) -> PlaybackStatus:
    """Renew the idle lease, including while the player is paused."""
    manager, session = session_for(request, camera_name, session_id)
    return manager.status(session)


@router.delete("/{session_id}", status_code=204)
async def cancel_playback(request: Request, camera_name: str, session_id: str):
    """Cancel a superseded window and release its reservation."""
    manager, session = session_for(request, camera_name, session_id, cleanup=True)
    try:
        await manager.cancel(session)
    except PlaybackError as error:
        raise HTTPException(error.status, error.code) from None
    return Response(status_code=204)


@router.get("/{session_id}/media/{resource:path}")
async def playback_resource(
    request: Request, camera_name: str, session_id: str, resource: str
):
    """Serve only published resources, never a caller-selected filesystem path."""
    manager, session = session_for(request, camera_name, session_id)
    if not RESOURCE.fullmatch(resource):
        raise HTTPException(404, "playback_resource_not_found")
    if session.state == "failed":
        raise HTTPException(410, session.error)
    if resource == "index.m3u8" and session.state == "preparing":
        raise HTTPException(425, "playback_not_ready")
    if resource not in session.resources:
        raise HTTPException(404, "playback_resource_not_found")

    async def chunks():
        if session.deleting:
            return
        session.readers += 1
        try:
            file = await asyncio.to_thread((session.directory / resource).open, "rb")
            try:
                while chunk := await asyncio.to_thread(file.read, 64 * 1024):
                    yield chunk
            finally:
                await asyncio.to_thread(file.close)
        finally:
            session.readers -= 1
            if session.deleting and not session.readers:
                await manager.cancel(session)

    return StreamingResponse(
        chunks(),
        media_type=(
            "application/vnd.apple.mpegurl"
            if resource.endswith(".m3u8")
            else "video/mp2t"
        ),
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )

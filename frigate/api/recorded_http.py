"""Authenticated finite MP4 windows with owned single-range HTTP delivery."""

import asyncio
import logging
from email.utils import formatdate
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from joserfc import jwt

from frigate.api.auth import allow_any_authenticated, require_camera_access
from frigate.api.defs.request.recorded_http_body import (
    RecordedHTTPCreate,
    RecordedHTTPResolve,
)
from frigate.api.defs.response.recorded_http_response import (
    RecordedHTTPDiscovery,
    RecordedHTTPStatus,
    RecordedHTTPTarget,
)
from frigate.api.live_webrtc import HEADERS, public_error
from frigate.api.notification import native_image_user
from frigate.api.recorded_webrtc import RecordedRoute, check_camera
from frigate.comms.live_webrtc import LiveWebRTCError
from frigate.comms.recorded_webrtc import finish_cleanup
from frigate.record.http_range import UnsatisfiableRange, media_range

logger = logging.getLogger(__name__)


def read_slice(path, offset, length):
    """Own each file handle entirely in the joined reader thread."""
    with path.open("rb") as source:
        source.seek(offset)
        return source.read(length)


def read_owned_slice(manager, session, offset, length):
    """Validate original identity on the same joined worker that reads its bytes."""
    return read_slice(manager.media_path(session), offset, length)


router = APIRouter(
    prefix="/{camera_name}/recordings/http",
    tags=["Media"],
    route_class=RecordedRoute,
    dependencies=[Depends(allow_any_authenticated()), Depends(require_camera_access)],
)


def manager_for(request: Request):
    """Use only the startup-owned shared-admission recording manager."""
    manager = getattr(request.app.state, "recorded_http_manager", None)
    if manager is None:
        raise HTTPException(503, "recorded_http_not_started", headers=HEADERS)
    return manager


@router.get(
    "/status",
    response_model=RecordedHTTPDiscovery,
    openapi_extra={"x-required-role": "camera"},
)
async def discovery(
    request: Request,
    camera_name: str,
    response: Response,
    user: dict = Depends(native_image_user),
):
    """Discover recording delivery without opening or probing original files."""
    check_camera(request, camera_name, user)
    manager = manager_for(request)
    available = manager.available()
    response.headers.update(HEADERS)
    return RecordedHTTPDiscovery(
        enabled=request.app.frigate_config.ui.recorded_http,
        available=available,
        session_url=f"{quote(camera_name, safe='')}/recordings/http/sessions",
        resolve_url=f"{quote(camera_name, safe='')}/recordings/http/resolve",
        qualities=manager.qualities() if available else [],
        reason=None if available else "recorded_http_disabled_or_restart_required",
    )


@router.post(
    "/resolve",
    response_model=RecordedHTTPTarget,
    openapi_extra={"x-required-role": "camera"},
)
async def resolve_target(
    request: Request,
    camera_name: str,
    body: RecordedHTTPResolve,
    response: Response,
    user: dict = Depends(native_image_user),
):
    """Find a real source-frame target only within explicit caller search bounds."""
    check_camera(request, camera_name, user)
    config = request.app.frigate_config
    authorization = request.headers.get("authorization", "")
    encoded = (
        authorization.removeprefix("Bearer ")
        if authorization.startswith("Bearer ")
        else request.cookies.get(config.auth.cookie_name)
    )
    claims = jwt.decode(encoded, request.app.jwt_token, algorithms=["HS256"]).claims
    try:
        result = await manager_for(request).resolve(
            camera_name,
            user["username"],
            body,
            claims["exp"],
            auth_issued=claims["iat"],
        )
    except LiveWebRTCError as error:
        raise public_error(error) from None
    response.headers.update(HEADERS)
    return result


@router.post(
    "/sessions",
    status_code=202,
    response_model=RecordedHTTPStatus,
    openapi_extra={"x-required-role": "camera"},
)
async def create(
    request: Request,
    camera_name: str,
    body: RecordedHTTPCreate,
    response: Response,
    user: dict = Depends(native_image_user),
):
    """Return preparing state; media exists only after successful finite completion."""
    check_camera(request, camera_name, user)
    config = request.app.frigate_config
    authorization = request.headers.get("authorization", "")
    encoded = (
        authorization.removeprefix("Bearer ")
        if authorization.startswith("Bearer ")
        else request.cookies.get(config.auth.cookie_name)
    )
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
        if await request.is_disconnected():
            await manager.delete(camera_name, user["username"], result["session_id"])
            raise HTTPException(
                499, "recorded_http_viewer_disconnected", headers=HEADERS
            )
    except asyncio.CancelledError:
        if "result" in locals():
            await manager.delete(camera_name, user["username"], result["session_id"])
        raise
    except LiveWebRTCError as error:
        raise public_error(error) from None
    response.headers.update(HEADERS)
    response.headers["Location"] = result["status_url"]
    return result


@router.get(
    "/sessions/{session_id}",
    response_model=RecordedHTTPStatus,
    openapi_extra={"x-required-role": "camera"},
)
async def status(
    request: Request,
    camera_name: str,
    session_id: UUID,
    response: Response,
    user: dict = Depends(native_image_user),
):
    """Read status without renewing the nonrenewable hard expiry."""
    check_camera(request, camera_name, user)
    manager = manager_for(request)
    try:
        session = await manager.owned(camera_name, user["username"], str(session_id))
    except LiveWebRTCError as error:
        raise public_error(error) from None
    response.headers.update(HEADERS)
    return manager.metadata(session)


@router.delete(
    "/sessions/{session_id}",
    status_code=204,
    openapi_extra={"x-required-role": "camera"},
)
async def close(
    request: Request,
    camera_name: str,
    session_id: UUID,
    user: dict = Depends(native_image_user),
):
    """Drain original-reader processes and response readers before releasing quota."""
    check_camera(request, camera_name, user)
    manager = manager_for(request)
    try:
        await manager.delete(camera_name, user["username"], str(session_id))
    except LiveWebRTCError as error:
        raise public_error(error) from None
    return Response(status_code=204, headers=HEADERS)


class OwnedMP4Response(Response):
    """A cancellable bounded reader, never Starlette's unowned path-send shortcut."""

    def __init__(self, manager, session, first, last, *, head, status_code, headers):
        super().__init__(status_code=status_code, headers=headers)
        self.manager, self.session = manager, session
        self.first, self.last, self.head = first, last, head

    async def __call__(self, scope, receive, send):
        try:
            reader = await self.manager.acquire_reader(self.session)
        except LiveWebRTCError as error:
            raise public_error(error) from None
        disconnected = None
        body_finished = False

        async def watch_disconnect():
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    if not body_finished:
                        reader.cancel()
                    return

        try:
            disconnected = asyncio.create_task(watch_disconnect())
            await send(
                {
                    "type": "http.response.start",
                    "status": self.status_code,
                    "headers": self.raw_headers,
                }
            )
            remaining = 0 if self.head else self.last - self.first + 1
            while remaining:
                await self.manager.owned(
                    self.session.camera, self.session.owner, self.session.identifier
                )
                chunk = await finish_cleanup(
                    asyncio.to_thread(
                        read_owned_slice,
                        self.manager,
                        self.session,
                        self.last - remaining + 1,
                        min(256 * 1024, remaining),
                    )
                )
                if not chunk:
                    raise RuntimeError("Completed recording output changed")
                remaining -= len(chunk)
                await send(
                    {"type": "http.response.body", "body": chunk, "more_body": True}
                )
            body_finished = True
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        except (LiveWebRTCError, OSError, RuntimeError):
            logger.exception("Recorded HTTP response reader failed")
            raise
        finally:

            async def cleanup():
                if disconnected is not None:
                    disconnected.cancel()
                    await asyncio.gather(disconnected, return_exceptions=True)
                self.session.readers.discard(reader)

            await finish_cleanup(cleanup())


@router.get(
    "/sessions/{session_id}/media.mp4",
    response_class=Response,
    openapi_extra={"x-required-role": "camera"},
)
@router.head(
    "/sessions/{session_id}/media.mp4",
    response_class=Response,
    openapi_extra={"x-required-role": "camera"},
)
async def media(
    request: Request,
    camera_name: str,
    session_id: UUID,
    user: dict = Depends(native_image_user),
):
    """Serve exact completed-file lengths and single suffix, open or explicit ranges."""
    check_camera(request, camera_name, user)
    manager = manager_for(request)
    try:
        session = await manager.owned(camera_name, user["username"], str(session_id))
    except LiveWebRTCError as error:
        raise public_error(error) from None
    if session.state != "ready":
        raise HTTPException(
            409, session.error or "recorded_http_not_ready", headers=HEADERS
        )
    headers = {
        **HEADERS,
        "Content-Type": "video/mp4",
        "Accept-Ranges": "bytes",
        "ETag": session.etag,
        "Last-Modified": formatdate(session.modified, usegmt=True),
        "Content-Disposition": 'inline; filename="recording.mp4"',
    }
    try:
        code, first, last = media_range(
            session.size,
            request.headers.get("range"),
            request.headers.get("if-range"),
            session.etag,
            session.modified,
        )
    except UnsatisfiableRange:
        return Response(
            status_code=416,
            headers={
                **headers,
                "Content-Range": f"bytes */{session.size}",
                "Content-Length": "0",
            },
        )
    headers["Content-Length"] = str(last - first + 1)
    if code == 206:
        headers["Content-Range"] = f"bytes {first}-{last}/{session.size}"
    return OwnedMP4Response(
        manager,
        session,
        first,
        last,
        head=request.method == "HEAD",
        status_code=code,
        headers=headers,
    )

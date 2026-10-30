"""Notification apis."""

import asyncio
import ipaddress
import logging
import os
import time
from datetime import datetime
from math import isfinite
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import UUID

from cryptography.hazmat.primitives import serialization
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from joserfc import jwt
from joserfc.errors import JoseError
from peewee import DatabaseError
from py_vapid import Vapid01, utils

from frigate.api.auth import allow_any_authenticated, get_current_user
from frigate.api.defs.request.native_push_body import (
    NativePushRegistrationBody,
    NativePushTestBody,
)
from frigate.api.defs.response.generic_response import GenericResponse
from frigate.api.defs.response.native_push_response import (
    NativeNotificationResolution,
    NativePushDeviceMetadata,
    NativePushRegistrationResponse,
    NativePushStatus,
)
from frigate.api.defs.tags import Tags
from frigate.comms.apns import (
    APNsMessage,
    APNsProvider,
    APNsProviderError,
    APNsTransport,
)
from frigate.comms.apns_registry import APNsRegistry, NativePushDevice
from frigate.comms.apns_thumbnails import (
    NativeThumbnailUnavailable,
    render_native_thumbnail,
    resolve_native_thumbnail,
)
from frigate.comms.native_health import NativeHealthResponse
from frigate.comms.native_preferences import (
    DevicePreferences,
    DevicePreferenceUpdate,
    NativeSnoozeBody,
)
from frigate.const import CONFIG_DIR
from frigate.models import Event, ReviewSegment, User

logger = logging.getLogger(__name__)

router = APIRouter(tags=[Tags.notifications])


def _native_notification_reference(
    kind: Literal["event", "review", "camera"], identifier: str
) -> tuple[str, float | None]:
    """Resolve routing metadata without reading thumbnails or opening media files."""
    if kind == "camera":
        return identifier, None
    model = Event if kind == "event" else ReviewSegment
    row = (
        model.select(model.camera, model.start_time)
        .where(model.id == identifier)
        .first()
    )
    if row is None:
        raise HTTPException(404, "Notification reference is unavailable")
    value = row.start_time
    if isinstance(value, datetime):
        timestamp = value.timestamp()
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        timestamp = float(value)
    else:
        logger.error("Native notification reference has invalid timestamp metadata")
        raise HTTPException(503, "Notification metadata is unavailable")
    if not isfinite(timestamp):
        logger.error("Native notification reference has non-finite timestamp metadata")
        raise HTTPException(503, "Notification metadata is unavailable")
    return row.camera, timestamp


# Push endpoints are opaque URLs but stay well under this in practice
MAX_ENDPOINT_LENGTH = 2048

# Suffixes that only ever resolve on the local network
INTERNAL_HOST_SUFFIXES = (".local", ".localdomain", ".internal", ".home.arpa")


def _validate_push_endpoint(endpoint: Any) -> str | None:
    """Return a reason the endpoint is unusable, or None when it is valid.

    Subscriptions are issued by the browser vendor's push service, so a valid
    endpoint is always a public https URL. Anything else is either a broken
    registration or an attempt to aim the notification sender somewhere it
    should not reach.
    """
    if not isinstance(endpoint, str) or not endpoint:
        return "endpoint must be a url"

    if len(endpoint) > MAX_ENDPOINT_LENGTH:
        return "endpoint is too long"

    try:
        parsed = urlparse(endpoint)
        port = parsed.port
    except ValueError:
        return "endpoint is not a valid url"

    if parsed.scheme != "https":
        return "endpoint must use https"

    if parsed.username or parsed.password:
        return "endpoint must not include credentials"

    if port is not None and port != 443:
        return "endpoint must use the default https port"

    hostname = parsed.hostname

    if not hostname:
        return "endpoint must include a hostname"

    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None

    if address is not None:
        # A push service is never reachable at an address only this network can
        # route, so anything non-global is a misconfiguration at best
        if not address.is_global:
            return "endpoint must not use a private address"
    elif hostname == "localhost" or "." not in hostname:
        return "endpoint must use a fully qualified hostname"
    elif hostname.endswith(INTERNAL_HOST_SUFFIXES):
        return "endpoint must not use an internal hostname"

    # The subscription token lives in the path, and webpush.py assumes there is
    # a separator after the host when it builds the VAPID audience
    if len(parsed.path) <= 1:
        return "endpoint must include a subscription path"

    return None


def _validate_subscription(sub: Any) -> str | None:
    """Return a reason the subscription is unusable, or None when it is valid."""
    if not isinstance(sub, dict):
        return "subscription must be an object"

    reason = _validate_push_endpoint(sub.get("endpoint"))

    if reason:
        return reason

    keys = sub.get("keys")

    if not isinstance(keys, dict):
        return "subscription must include keys"

    # WebPusher raises on a missing key, which would break every send for the
    # user rather than just this registration
    for name in ("p256dh", "auth"):
        value = keys.get(name)

        if not isinstance(value, str) or not value:
            return f"subscription keys must include {name}"

    return None


@router.get(
    "/notifications/pubkey",
    dependencies=[Depends(allow_any_authenticated())],
    summary="Get VAPID public key",
    description="""Gets the VAPID public key for the notifications.
    Returns the public key or an error if notifications are not enabled.
    """,
)
def get_vapid_pub_key(request: Request):
    config = request.app.frigate_config
    notifications_enabled = config.notifications.enabled
    camera_notifications_enabled = [
        c for c in config.cameras.values() if c.enabled and c.notifications.enabled
    ]
    if not (notifications_enabled or camera_notifications_enabled):
        return JSONResponse(
            content=({"success": False, "message": "Notifications are not enabled."}),
            status_code=400,
        )

    key = Vapid01.from_file(os.path.join(CONFIG_DIR, "notifications.pem"))
    raw_pub = key.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return JSONResponse(content=utils.b64urlencode(raw_pub), status_code=200)


@router.post(
    "/notifications/register",
    dependencies=[Depends(allow_any_authenticated())],
    summary="Register notifications",
    description="""Registers a notifications subscription.
    Returns a success message or an error if the subscription is not provided.
    """,
)
def register_notifications(request: Request, body: dict = None):
    if request.app.frigate_config.auth.enabled:
        # FIXME: For FastAPI the remote-user is not being populated
        username = request.headers.get("remote-user") or "admin"
    else:
        username = "admin"

    json: dict[str, Any] = body or {}
    sub = json.get("sub")

    if not sub:
        return JSONResponse(
            content={"success": False, "message": "Subscription must be provided."},
            status_code=400,
        )

    reason = _validate_subscription(sub)

    if reason:
        logger.warning(
            "Rejected notification registration for %s: %s", username, reason
        )
        return JSONResponse(
            content={"success": False, "message": f"Invalid subscription: {reason}"},
            status_code=400,
        )

    updated = (
        User.update(notification_tokens=User.notification_tokens.append(sub))
        .where(User.username == username)
        .execute()
    )

    if not updated:
        logger.warning(
            "Rejected notification registration for unknown user %s", username
        )
        return JSONResponse(
            content=({"success": False, "message": "Could not find user."}),
            status_code=404,
        )

    return JSONResponse(
        content=({"success": True, "message": "Successfully saved token."}),
        status_code=200,
    )


def _native_username(request: Request, user: dict | JSONResponse) -> str:
    """Never use the legacy browser-registration admin fallback for native devices."""
    if not request.app.frigate_config.auth.enabled or isinstance(user, JSONResponse):
        raise HTTPException(403, "Native push requires an authenticated user account")
    username = user.get("username")
    if not isinstance(username, str) or not username or username == "anonymous":
        raise HTTPException(403, "Native push requires an authenticated user account")
    return username


def _native_registry() -> APNsRegistry:
    return APNsRegistry(User._meta.database.execute_sql)


async def native_push_user(
    request: Request, user: dict = Depends(get_current_user)
) -> dict:
    """Resolve the current database account and role, not a stale token role."""
    username = _native_username(request, user)
    account = await asyncio.to_thread(User.get_or_none, User.username == username)
    if account is None:
        raise HTTPException(404, "User account no longer exists")
    return {"username": username, "role": account.role}


@router.get(
    "/notifications/apns",
    response_model=NativePushStatus,
    dependencies=[Depends(allow_any_authenticated())],
    summary="Get native Apple push capabilities",
)
def native_push_status(
    request: Request, user: dict = Depends(native_push_user)
) -> NativePushStatus:
    """Expose readiness metadata, never provider keys, tokens, or filesystem paths."""
    _native_username(request, user)
    settings = request.app.frigate_config.apns
    return NativePushStatus(
        enabled=settings.enabled,
        server_id=settings.server_id if settings.enabled else None,
        topic=settings.topic if settings.enabled else None,
        image_notifications=settings.enabled and settings.image_notifications,
        device_preferences=True,
        action_notifications=True,
        supports_device_features=True,
        health_notifications=getattr(request.app.state, "native_health", None)
        is not None,
    )


_PRIVATE_IMAGE_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
}


async def _native_image_account(request: Request, username: str) -> dict:
    """Validate the actual session and current account rather than cached proxy roles."""
    config = request.app.frigate_config
    authorization = request.headers.get("authorization", "")
    encoded = (
        authorization.removeprefix("Bearer ")
        if authorization.startswith("Bearer ")
        else request.cookies.get(config.auth.cookie_name)
    )
    try:
        if not encoded or not config.auth.enabled:
            raise ValueError("No native session")
        claims = jwt.decode(encoded, request.app.jwt_token, algorithms=["HS256"]).claims
        now = time.time()
        if (
            claims.get("sub") != username
            or claims.get("role") not in config.auth.roles
            or any(
                not isinstance(claims.get(key), (int, float))
                or isinstance(claims.get(key), bool)
                or not isfinite(claims[key])
                for key in ("iat", "exp")
            )
            or claims["exp"] <= now
            or claims["iat"] > now
        ):
            raise ValueError("Invalid native session")
    except (ValueError, TypeError, JoseError):
        raise HTTPException(
            401, "A valid native session is required", headers=_PRIVATE_IMAGE_HEADERS
        ) from None
    account = await asyncio.to_thread(User.get_or_none, User.username == username)
    if account is None or (
        account.password_changed_at is not None
        and claims["iat"] < account.password_changed_at.timestamp()
    ):
        raise HTTPException(
            401, "The native session is no longer valid", headers=_PRIVATE_IMAGE_HEADERS
        )
    if account.role not in config.auth.roles:
        raise HTTPException(
            403, "Camera access is no longer allowed", headers=_PRIVATE_IMAGE_HEADERS
        )
    return {"username": username, "role": account.role}


async def native_image_user(
    request: Request, user: dict = Depends(get_current_user)
) -> dict:
    """Require a real authenticated cookie or bearer session, not a forwarded header alone."""
    username = _native_username(request, user)
    return await _native_image_account(request, username)


async def _authorize_native_image(
    request: Request, username: str, installation_id: UUID, camera: str
) -> None:
    config = request.app.frigate_config
    if not config.apns.enabled or not config.apns.image_notifications:
        raise HTTPException(
            503,
            "Native image notifications are not enabled",
            headers=_PRIVATE_IMAGE_HEADERS,
        )
    devices = await asyncio.to_thread(
        _native_registry().authorized_devices,
        username,
        camera,
        config.auth.roles,
        set(config.cameras),
    )
    device = next(
        (
            device
            for device in devices
            if device.installation_id == str(installation_id)
        ),
        None,
    )
    if device is None or (
        device.camera_ids is not None and camera not in device.camera_ids
    ):
        raise HTTPException(
            403,
            "This installation cannot access the camera image",
            headers=_PRIVATE_IMAGE_HEADERS,
        )


@router.get(
    "/notifications/apns/thumbnail/{installation_id}/{kind}/{identifier}",
    dependencies=[Depends(allow_any_authenticated())],
    summary="Get an installation-scoped private native notification thumbnail",
    response_class=Response,
)
async def native_push_thumbnail(
    request: Request,
    installation_id: UUID,
    kind: Literal["review", "event", "camera"],
    identifier: str,
    height: int = Query(default=360, ge=1, le=1024),
    user: dict = Depends(native_image_user),
) -> Response:
    """Resolve media by ID, checking session and installation scope before and after rendering."""
    settings = request.app.frigate_config.apns
    if not settings.enabled or not settings.image_notifications:
        raise HTTPException(
            503,
            "Native image notifications are not enabled",
            headers=_PRIVATE_IMAGE_HEADERS,
        )
    if (
        not identifier
        or len(identifier) > 256
        or identifier in (".", "..")
        or any(
            char in "/\\%:" or ord(char) < 32 or 127 <= ord(char) <= 159
            for char in identifier
        )
    ):
        raise HTTPException(
            404,
            "Notification image reference is unavailable",
            headers=_PRIVATE_IMAGE_HEADERS,
        )
    try:
        reference = await asyncio.to_thread(resolve_native_thumbnail, kind, identifier)
        await _authorize_native_image(
            request, user["username"], installation_id, reference.camera
        )
        image = await asyncio.to_thread(
            render_native_thumbnail,
            reference,
            getattr(request.app, "detected_frames_processor", None),
            height,
        )
        current_reference = await asyncio.to_thread(
            resolve_native_thumbnail, kind, identifier
        )
        if current_reference != reference:
            raise NativeThumbnailUnavailable("Notification image reference changed")
    except NativeThumbnailUnavailable:
        logger.debug("Native notification thumbnail is unavailable")
        raise HTTPException(
            404, "Notification image is unavailable", headers=_PRIVATE_IMAGE_HEADERS
        ) from None
    await _native_image_account(request, user["username"])
    await _authorize_native_image(
        request, user["username"], installation_id, reference.camera
    )
    return Response(
        image,
        media_type="image/jpeg",
        headers={**_PRIVATE_IMAGE_HEADERS, "X-Frigate-Camera": reference.camera},
    )


@router.get(
    "/notifications/apns/devices",
    response_model=list[NativePushDeviceMetadata],
    dependencies=[Depends(allow_any_authenticated())],
    summary="List your native push installations",
)
async def native_push_devices(
    request: Request, user: dict = Depends(native_push_user)
) -> list[NativePushDeviceMetadata]:
    """Only the current account's nonsecret registration metadata is returned."""
    username = _native_username(request, user)
    result = await asyncio.to_thread(_native_registry().devices, username)
    if result is None:
        raise HTTPException(404, "User account no longer exists")
    return [
        NativePushDeviceMetadata(
            installation_id=UUID(device.installation_id),
            environment=device.environment,
            camera_ids=device.camera_ids,
            registered_at=device.registered_at,
        )
        for device in result[1]
    ]


@router.post(
    "/notifications/apns/register",
    response_model=NativePushRegistrationResponse,
    dependencies=[Depends(allow_any_authenticated())],
    summary="Register this installation for native Apple push",
)
async def register_native_push(
    request: Request,
    body: NativePushRegistrationBody,
    user: dict = Depends(native_image_user),
) -> NativePushRegistrationResponse:
    """Bind the device to the authenticated user and enforce current camera scope."""
    username = _native_username(request, user)
    config = request.app.frigate_config
    if user["role"] not in config.auth.roles:
        raise HTTPException(403, "User role is no longer authorized")
    if not config.apns.enabled:
        raise HTTPException(503, "Native Apple push is not enabled on this server")
    server_id = config.apns.server_id
    if server_id is None:
        raise HTTPException(
            503, "Native Apple push provider configuration is incomplete"
        )
    allowed = set(
        User.get_allowed_cameras(user["role"], config.auth.roles, set(config.cameras))
    )
    if body.camera_ids is not None and not set(body.camera_ids).issubset(allowed):
        raise HTTPException(403, "Native push camera scope exceeds your permissions")
    device = NativePushDevice(
        str(body.installation_id),
        body.token,
        body.environment,
        body.camera_ids,
        time.time(),
        features=body.features,
    )
    try:
        updated = await asyncio.to_thread(_native_registry().register, username, device)
    except DatabaseError as error:
        logger.error(
            "Native push registration storage failed (%s)", type(error).__name__
        )
        raise HTTPException(
            503, "Native push registration storage is unavailable"
        ) from error
    if not updated:
        raise HTTPException(404, "User account no longer exists")
    return NativePushRegistrationResponse(
        success=True, message="Native push registration saved", server_id=server_id
    )


async def _owned_native_device(
    request: Request, user: dict, installation_id: UUID
) -> tuple[NativePushDevice, set[str]]:
    try:
        result = await asyncio.to_thread(_native_registry().devices, user["username"])
    except (DatabaseError, ValueError) as error:
        logger.error("Native installation storage failed (%s)", type(error).__name__)
        raise HTTPException(503, "Native installation storage is unavailable") from None
    current = await _native_image_account(request, user["username"])
    if result is None:
        raise HTTPException(401, "Native account no longer exists")
    role, devices = result
    if (
        role != current["role"]
        or role != user["role"]
        or role not in request.app.frigate_config.auth.roles
    ):
        raise HTTPException(403, "Native account permissions changed")
    device = next(
        (item for item in devices if item.installation_id == str(installation_id)), None
    )
    if device is None:
        raise HTTPException(
            404, "Native installation is not registered to this account"
        )
    config = request.app.frigate_config
    allowed = set(
        User.get_allowed_cameras(role, config.auth.roles, set(config.cameras))
    )
    if device.camera_ids is not None:
        allowed.intersection_update(device.camera_ids)
    return device, allowed


async def _save_native_preferences(
    request: Request,
    user: dict,
    installation_id: UUID,
    expected_revision: int,
    updated: DevicePreferences,
) -> bool:
    device, allowed = await _owned_native_device(request, user, installation_id)
    if device.preferences.revision != expected_revision:
        return False
    if not set(updated.camera_snooze_until).issubset(allowed):
        raise HTTPException(
            403, "Camera snooze exceeds this installation's current scope"
        )
    if updated.health_alerts and "health-v1" not in device.features:
        raise HTTPException(
            409, "This installation has not enabled the health-v1 feature"
        )
    try:
        return await asyncio.to_thread(
            _native_registry().set_preferences,
            user["username"],
            user["role"],
            str(installation_id),
            expected_revision,
            updated,
            device.registered_at,
        )
    except DatabaseError as error:
        logger.error("Native preference storage failed (%s)", type(error).__name__)
        raise HTTPException(503, "Native preference storage is unavailable") from None


def _visible_preferences(
    device: NativePushDevice, allowed: set[str]
) -> DevicePreferences:
    return DevicePreferences.model_validate(
        {
            **device.preferences.model_dump(),
            "camera_snooze_until": {
                camera: until
                for camera, until in device.preferences.camera_snooze_until.items()
                if camera in allowed and until > time.time()
            },
        }
    )


@router.get(
    "/notifications/apns/devices/{installation_id}/preferences",
    response_model=DevicePreferences,
    dependencies=[Depends(allow_any_authenticated())],
)
async def native_device_preferences(
    request: Request,
    response: Response,
    installation_id: UUID,
    user: dict = Depends(native_image_user),
) -> DevicePreferences:
    """Return only the current account's installation-local alert preferences."""
    response.headers.update(_PRIVATE_IMAGE_HEADERS)
    device, allowed = await _owned_native_device(request, user, installation_id)
    return _visible_preferences(device, allowed)


@router.get(
    "/notifications/apns/health",
    response_model=NativeHealthResponse,
    dependencies=[Depends(allow_any_authenticated())],
)
async def native_notification_health(
    request: Request, response: Response, user: dict = Depends(native_image_user)
) -> NativeHealthResponse:
    """Return sanitized scoped observations and explicitly incomplete telemetry."""
    response.headers.update(_PRIVATE_IMAGE_HEADERS)
    monitor = getattr(request.app.state, "native_health", None)
    if monitor is None:
        raise HTTPException(503, "Native health observations are unavailable")
    if (
        not -5 <= time.time() - monitor.latest.sampled_at <= 15
        or not monitor.latest.checks_complete
    ):
        await monitor.collect()
    current = await _native_image_account(request, user["username"])
    return monitor.scoped(current)


@router.get(
    "/notifications/apns/resolve/{kind}/{identifier}",
    response_model=NativeNotificationResolution,
    dependencies=[Depends(allow_any_authenticated())],
)
async def resolve_native_notification(
    request: Request,
    response: Response,
    kind: Literal["event", "review", "camera"],
    identifier: str,
    user: dict = Depends(native_image_user),
) -> NativeNotificationResolution:
    """Authorize a queued native destination against the current account and metadata."""
    response.headers.update(_PRIVATE_IMAGE_HEADERS)
    if (
        not identifier
        or len(identifier) > 256
        or identifier in (".", "..")
        or any(
            char in "/\\%:" or ord(char) < 32 or ord(char) == 127 for char in identifier
        )
    ):
        raise HTTPException(404, "Notification reference is unavailable")
    server_id = request.app.frigate_config.apns.server_id
    if not server_id:
        raise HTTPException(503, "Native notification routing is not configured")
    reference = await asyncio.to_thread(
        _native_notification_reference, kind, identifier
    )
    current = await _native_image_account(request, user["username"])
    config = request.app.frigate_config
    allowed = set(
        User.get_allowed_cameras(
            current["role"], config.auth.roles, set(config.cameras)
        )
    )
    camera, timestamp = reference
    if camera not in allowed:
        raise HTTPException(403, "Notification camera is no longer allowed")
    if kind != "camera":
        latest = await asyncio.to_thread(
            _native_notification_reference, kind, identifier
        )
        if latest != reference:
            raise HTTPException(409, "Notification reference changed; retry resolution")
        current = await _native_image_account(request, user["username"])
        config = request.app.frigate_config
        if camera not in User.get_allowed_cameras(
            current["role"], config.auth.roles, set(config.cameras)
        ):
            raise HTTPException(403, "Notification camera is no longer allowed")
    return NativeNotificationResolution(
        server_id=server_id,
        kind=kind,
        identifier=identifier,
        camera=camera,
        timestamp=timestamp,
    )


@router.post(
    "/notifications/apns/devices/{installation_id}/preferences",
    response_model=DevicePreferences,
    dependencies=[Depends(allow_any_authenticated())],
)
async def update_native_device_preferences(
    request: Request,
    response: Response,
    installation_id: UUID,
    body: DevicePreferenceUpdate,
    user: dict = Depends(native_image_user),
) -> DevicePreferences:
    """CAS preferences without touching browser registrations or server alert rules."""
    response.headers.update(_PRIVATE_IMAGE_HEADERS)
    device, allowed = await _owned_native_device(request, user, installation_id)
    if body.revision != device.preferences.revision:
        raise HTTPException(409, "Device preferences changed; reload before saving")
    if not set(body.camera_snooze_until).issubset(allowed):
        raise HTTPException(
            403, "Camera snooze exceeds this installation's current scope"
        )
    now = time.time()
    if any(
        until != device.preferences.camera_snooze_until.get(camera)
        and until > now + 3600
        for camera, until in body.camera_snooze_until.items()
    ):
        raise HTTPException(422, "Camera snooze cannot exceed one hour")
    if body.health_alerts and "health-v1" not in device.features:
        raise HTTPException(
            409, "This installation has not enabled the health-v1 feature"
        )
    updated = DevicePreferences.model_validate(
        {
            **body.model_dump(),
            "revision": body.revision + 1,
            "camera_snooze_until": {
                camera: until
                for camera, until in body.camera_snooze_until.items()
                if until > now
            },
        }
    )
    saved = await _save_native_preferences(
        request,
        user,
        installation_id,
        body.revision,
        updated,
    )
    if not saved:
        raise HTTPException(409, "Device preferences changed; reload before saving")
    return updated


@router.post(
    "/notifications/apns/devices/{installation_id}/snooze",
    response_model=DevicePreferences,
    dependencies=[Depends(allow_any_authenticated())],
)
async def snooze_native_camera(
    request: Request,
    response: Response,
    installation_id: UUID,
    body: NativeSnoozeBody,
    user: dict = Depends(native_image_user),
) -> DevicePreferences:
    """Pause only this installation's allowed camera, using bounded atomic retries."""
    response.headers.update(_PRIVATE_IMAGE_HEADERS)
    for _attempt in range(5):
        device, allowed = await _owned_native_device(request, user, installation_id)
        if body.camera_id not in allowed:
            raise HTTPException(403, "Camera is not allowed for this installation")
        now = time.time()
        snoozes = {
            camera: until
            for camera, until in device.preferences.camera_snooze_until.items()
            if camera in allowed and until > now
        }
        if body.seconds:
            snoozes[body.camera_id] = now + body.seconds
        else:
            snoozes.pop(body.camera_id, None)
        updated = DevicePreferences.model_validate(
            {
                **device.preferences.model_dump(),
                "revision": device.preferences.revision + 1,
                "camera_snooze_until": snoozes,
            }
        )
        if await _save_native_preferences(
            request,
            user,
            installation_id,
            device.preferences.revision,
            updated,
        ):
            return updated
    raise HTTPException(409, "Device preferences changed repeatedly; retry the action")


@router.delete(
    "/notifications/apns/register/{installation_id}",
    response_model=GenericResponse,
    dependencies=[Depends(allow_any_authenticated())],
    summary="Remove your native push installation",
)
async def unregister_native_push(
    installation_id: UUID, request: Request, user: dict = Depends(native_push_user)
) -> GenericResponse:
    """Revocation works even after the operator disables the APNs provider."""
    username = _native_username(request, user)
    updated = await asyncio.to_thread(
        _native_registry().unregister, username, str(installation_id)
    )
    if not updated:
        raise HTTPException(404, "User account no longer exists")
    return GenericResponse(success=True, message="Native push registration removed")


@router.post(
    "/notifications/apns/test",
    response_model=GenericResponse,
    dependencies=[Depends(allow_any_authenticated())],
    summary="Send a native push test to your installation",
)
async def test_native_push(
    request: Request, body: NativePushTestBody, user: dict = Depends(native_push_user)
) -> GenericResponse:
    """Report Apple's acceptance separately from actual display on the device."""
    username = _native_username(request, user)
    settings = request.app.frigate_config.apns
    if not settings.enabled:
        raise HTTPException(503, "Native Apple push is not enabled on this server")
    if user["role"] not in request.app.frigate_config.auth.roles:
        raise HTTPException(403, "User role is no longer authorized")
    registry = _native_registry()
    result = await asyncio.to_thread(registry.devices, username)
    devices = (
        [
            device
            for device in result[1]
            if device.installation_id == str(body.installation_id)
        ]
        if result is not None
        else []
    )
    if not devices:
        raise HTTPException(404, "This installation is not registered for your account")
    transport = APNsTransport(APNsProvider.from_settings(settings))
    try:
        deliveries = await transport.send(
            APNsMessage(username, {}, "Frigate Horizon", "Test notification", "test"),
            devices,
        )
    except APNsProviderError as error:
        logger.error("APNs test provider setup failed for user %s", username)
        raise HTTPException(
            503, "APNs provider credentials are invalid or unreadable"
        ) from error
    finally:
        await transport.close()
    for delivery in deliveries:
        if delivery.invalidated_at is not None:
            matching = next(
                device
                for device in devices
                if device.installation_id == delivery.installation_id
            )
            await asyncio.to_thread(
                registry.invalidate, username, matching, delivery.invalidated_at
            )
    if not all(delivery.accepted for delivery in deliveries):
        raise HTTPException(
            502,
            "Apple did not accept the test push; inspect the server notification logs",
        )
    return GenericResponse(
        success=True,
        message="Apple accepted the test push; delivery and display are best-effort",
    )

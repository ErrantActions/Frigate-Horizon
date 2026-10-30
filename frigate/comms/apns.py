"""HTTP/2 APNs delivery with server-held ES256 credentials and private payloads."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import ECKey

from frigate.comms.apns_registry import NativePushDevice

if TYPE_CHECKING:
    from frigate.config.apns import APNsConfig

logger = logging.getLogger(__name__)

APPLE_HOSTS = {
    "production": "https://api.push.apple.com",
    "sandbox": "https://api.sandbox.push.apple.com",
}
KNOWN_REASONS = frozenset(
    {
        "BadDeviceToken",
        "DeviceTokenNotForTopic",
        "Unregistered",
        "BadTopic",
        "ExpiredProviderToken",
        "InvalidProviderToken",
        "TooManyRequests",
        "TooManyProviderTokenUpdates",
        "InternalServerError",
        "ServiceUnavailable",
        "Shutdown",
        "PayloadEmpty",
        "PayloadTooLarge",
        "BadPriority",
        "BadExpirationDate",
        "MissingTopic",
        "Forbidden",
    }
)


class _RedactDeviceURL(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "authorization" in message.lower():
            record.msg = "Sensitive HTTP authorization diagnostics omitted"
            record.args = ()
            return True
        if "/3/device/" in message:
            record.msg = re.sub(r"(/3/device/)[0-9a-fA-F]+", r"\1[redacted]", message)
            record.args = ()
        return True


for _name in ("httpx", "httpcore.http2", "httpcore.http11", "httpcore.connection"):
    _http_logger = logging.getLogger(_name)
    if not any(isinstance(item, _RedactDeviceURL) for item in _http_logger.filters):
        _http_logger.addFilter(_RedactDeviceURL())


class _NoHeaderCompressionDiagnostics(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # HPACK logs both header values and their reversible encoded bytes.
        return record.levelno > logging.DEBUG


logging.getLogger("hpack.hpack").addFilter(_NoHeaderCompressionDiagnostics())


class APNsProviderError(ValueError):
    """A safe-to-display provider setup failure without credential contents."""


@dataclass(frozen=True)
class APNsProvider:
    """Provider metadata; the private key stays in an external server file."""

    server_id: str
    topic: str
    team_id: str
    key_id: str
    private_key_file: str
    include_details: bool = False
    image_notifications: bool = False

    @classmethod
    def from_settings(cls, settings: APNsConfig) -> APNsProvider:
        """Construct credentials without casting optional configuration values."""
        if (
            settings.server_id is None
            or settings.topic is None
            or settings.team_id is None
            or settings.key_id is None
            or settings.private_key_file is None
        ):
            raise APNsProviderError("APNs provider configuration is incomplete")
        return cls(
            settings.server_id,
            settings.topic,
            settings.team_id,
            settings.key_id,
            settings.private_key_file,
            settings.include_details,
            settings.image_notifications,
        )


@dataclass(frozen=True)
class APNsMessage:
    """An already-authorized notification from the existing alert pipeline."""

    user: str
    payload: dict[str, Any]
    title: str
    message: str
    notification_type: str = "alert"
    ttl: int = 0

    @property
    def camera(self) -> str | None:
        after = self.payload.get("after")
        value = after.get("camera") if isinstance(after, dict) else None
        if not isinstance(value, str):
            value = self.payload.get("camera")
        return value if isinstance(value, str) else None


@dataclass(frozen=True)
class APNsDelivery:
    """Apple acceptance is not a guarantee of display on the phone."""

    installation_id: str
    accepted: bool
    reason: str | None = None
    invalidated_at: float | None = None


class APNsTransport:
    """Reuse an HTTP/2 client and refresh provider tokens before their expiry."""

    def __init__(
        self,
        provider: APNsProvider,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.provider = provider
        self.client = client or httpx.AsyncClient(
            http2=True, timeout=10.0, follow_redirects=False
        )
        self.owns_client = client is None
        self.clock = clock
        self.key: ECKey | None = None
        self.token: str | None = None
        self.issued_at = 0
        self.token_lock = asyncio.Lock()
        self.parallel = asyncio.Semaphore(8)

    async def close(self) -> None:
        """Close only the HTTP client owned by this transport."""
        if self.owns_client:
            await self.client.aclose()

    async def _provider_token(self, refresh: bool = False) -> str:
        async with self.token_lock:
            now = int(self.clock())
            if not refresh and self.token and 0 <= now - self.issued_at < 1200:
                return self.token
            if self.key is None:
                try:
                    pem = await asyncio.to_thread(
                        Path(self.provider.private_key_file).read_bytes
                    )
                    private = serialization.load_pem_private_key(pem, password=None)
                    if not isinstance(
                        private, ec.EllipticCurvePrivateKey
                    ) or not isinstance(private.curve, ec.SECP256R1):
                        raise APNsProviderError("APNs requires a P-256 private key")
                    self.key = ECKey.import_key(pem)
                except (
                    OSError,
                    ValueError,
                    TypeError,
                    JoseError,
                    UnsupportedAlgorithm,
                ) as error:
                    raise APNsProviderError(
                        "APNs credentials are invalid or unreadable"
                    ) from error
            self.token = jwt.encode(
                {"alg": "ES256", "kid": self.provider.key_id},
                {"iss": self.provider.team_id, "iat": now},
                self.key,
            )
            self.issued_at = now
            return self.token

    def payload(self, notification: APNsMessage) -> dict[str, Any]:
        """Send generic alert text by default, plus identifiers for authenticated routing."""
        after = notification.payload.get("after")
        after = after if isinstance(after, dict) else {}
        camera = notification.camera
        kind = "test"
        identifier: str | None = None
        health = notification.notification_type == "health"
        if health:
            kind, identifier = "health", str(notification.payload.get("identifier", ""))
            if not identifier:
                raise APNsProviderError(
                    "Health notification requires an issue identifier"
                )
        elif after.get("id") and notification.payload.get("type") in ("end", "genai"):
            kind, identifier = "review", str(after["id"])
        elif notification.payload.get("event_id"):
            kind, identifier = "event", str(notification.payload["event_id"])
        elif camera:
            kind, identifier = "camera", camera
        title = "Frigate Horizon"
        body = (
            "This is a test notification."
            if notification.notification_type == "test"
            else "NVR health information is available."
            if health
            else "New camera activity is available."
        )
        if self.provider.include_details and not health:
            title = notification.title[:120]
            body = notification.message[:300]
        result: dict[str, Any] = {
            "aps": {"alert": {"title": title, "body": body}, "sound": "default"},
            "server_id": self.provider.server_id,
            "kind": kind,
        }
        if identifier is not None:
            if (
                len(identifier) > 256
                or identifier in (".", "..")
                or any(char in identifier for char in "/\\%:")
                or any(ord(char) < 32 or ord(char) == 127 for char in identifier)
            ):
                raise APNsProviderError(
                    "Native notification routing identifier is invalid"
                )
            result["identifier"] = identifier
        if kind in ("review", "event", "camera"):
            result["aps"]["category"] = "HORIZON_ACTIVITY"
        elif health:
            result["aps"]["category"] = "HORIZON_HEALTH"
        if camera is not None:
            result["aps"]["thread-id"] = hashlib.sha256(
                f"{self.provider.server_id}:{camera}".encode()
            ).hexdigest()
        if (
            self.provider.image_notifications
            and kind not in ("test", "health")
            and notification.notification_type != "test"
        ):
            result["aps"]["mutable-content"] = 1
        return result

    async def send(
        self, notification: APNsMessage, devices: list[NativePushDevice]
    ) -> list[APNsDelivery]:
        """Deliver only to the caller's authorized and camera-filtered devices."""
        if (
            notification.notification_type not in ("test", "health")
            and not notification.camera
        ):
            raise APNsProviderError("Camera notification has no routing camera")
        selected = [
            device
            for device in devices
            if (
                notification.camera is None
                or device.camera_ids is None
                or notification.camera in device.camera_ids
            )
            and (
                notification.notification_type != "health"
                or ("health-v1" in device.features and device.preferences.health_alerts)
            )
            and (
                notification.notification_type == "test"
                or device.preferences.allows(notification.camera, self.clock())
            )
        ]
        if not selected:
            return []
        payload = self.payload(notification)
        encoded = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode()
        if len(encoded) > 4096:
            raise APNsProviderError("APNs notification exceeds Apple's payload limit")
        return list(
            await asyncio.gather(
                *(
                    self._send_device(notification, device, encoded)
                    for device in selected
                )
            )
        )

    async def _send_device(
        self, notification: APNsMessage, device: NativePushDevice, encoded: bytes
    ) -> APNsDelivery:
        async with self.parallel:
            refreshed = False
            for attempt in range(3):
                token = await self._provider_token()
                headers = {
                    "authorization": f"bearer {token}",
                    "apns-topic": self.provider.topic,
                    "apns-push-type": "alert",
                    "apns-priority": "10",
                    "apns-expiration": str(
                        int(self.clock()) + notification.ttl
                        if notification.ttl > 0
                        else 0
                    ),
                    "content-type": "application/json",
                }
                if notification.notification_type != "test":
                    route = self.payload(notification)
                    collapse = f"{self.provider.server_id}:{route['kind']}:{route.get('identifier', '')}"
                    headers["apns-collapse-id"] = hashlib.sha256(
                        collapse.encode()
                    ).hexdigest()
                try:
                    response = await self.client.post(
                        f"{APPLE_HOSTS[device.environment]}/3/device/{device.token}",
                        content=encoded,
                        headers=headers,
                    )
                except httpx.RequestError as error:
                    logger.warning(
                        "APNs transport failed for user %s (%s)",
                        notification.user,
                        type(error).__name__,
                    )
                    return APNsDelivery(device.installation_id, False, "TransportError")
                if response.status_code == 200 and response.http_version == "HTTP/2":
                    return APNsDelivery(device.installation_id, True)
                try:
                    failure = response.json()
                except ValueError:
                    failure = {}
                raw_reason = (
                    failure.get("reason") if isinstance(failure, dict) else None
                )
                reason = (
                    raw_reason
                    if isinstance(raw_reason, str) and raw_reason in KNOWN_REASONS
                    else "InvalidResponse"
                )
                if reason == "ExpiredProviderToken" and not refreshed and attempt < 2:
                    await self._provider_token(refresh=True)
                    refreshed = True
                    continue
                if response.status_code in (429, 500, 503) and attempt < 2:
                    retry_after = response.headers.get("retry-after")
                    try:
                        delay = (
                            float(retry_after)
                            if retry_after is not None
                            else 0.5 * (2**attempt)
                        )
                    except ValueError:
                        delay = 3.0
                    if isfinite(delay) and 0 <= delay <= 2:
                        await asyncio.sleep(delay)
                        continue
                timestamp = (
                    failure.get("timestamp") if isinstance(failure, dict) else None
                )
                invalidated_at = None
                if (
                    response.status_code == 410
                    and reason == "Unregistered"
                    and isinstance(timestamp, (int, float))
                    and not isinstance(timestamp, bool)
                    and isfinite(timestamp)
                    and timestamp >= 0
                ):
                    invalidated_at = timestamp / 1000
                logger.warning(
                    "APNs rejected notification for user %s: HTTP %s, %s",
                    notification.user,
                    response.status_code,
                    reason,
                )
                return APNsDelivery(
                    device.installation_id, False, reason, invalidated_at
                )
        raise APNsProviderError("APNs provider retry did not complete")

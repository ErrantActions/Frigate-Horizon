"""Explicit, pinned ONVIF firmware transfers without automatic write retries."""

import asyncio
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

import httpx
from lxml.etree import Error as XmlError
from onvif import ONVIFError
from zeep.exceptions import Error as ZeepError
from zeep.exceptions import Fault, ValidationError

from frigate.ptz.administration import _catalog, _protected_soap
from frigate.ptz.connection import camera_session, create_camera_service
from frigate.ptz.focus import unsupported_operation
from frigate.ptz.imaging import ImagingWriteUnverified
from frigate.util.onvif_backup import MAX_BACKUP_BYTES
from frigate.util.onvif_device import OnvifSettingsConflict, device_value

FIRMWARE_METHODS = frozenset(("UpgradeSystemFirmware", "StartFirmwareUpgrade"))


def pinned_firmware_upload(address: str, saved_host: str, soap_endpoint: str) -> str:
    """Keep server-reported upload references on the saved camera, never redirects."""
    parsed = urlsplit(address)
    saved = urlsplit(saved_host if "://" in saved_host else f"http://{saved_host}")
    endpoint = urlsplit(soap_endpoint)
    if (
        parsed.scheme not in ("http", "https")
        or parsed.hostname != saved.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not parsed.path
        or (
            (saved.scheme == "https" or endpoint.scheme == "https")
            and parsed.scheme != "https"
        )
    ):
        raise ValueError("Firmware upload endpoint is outside the saved camera policy")
    if parsed.port is not None and not 0 < parsed.port <= 65535:
        raise ValueError("Firmware upload port is invalid")
    return address


async def upgrade_firmware(connection, revision, method, payload, *, before_write):
    """Submit one confirmed firmware action; a transport acknowledgement is not installation proof."""
    if method not in FIRMWARE_METHODS:
        raise ValueError("Choose a supported firmware workflow")
    if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_BACKUP_BYTES:
        raise ValueError("Firmware must be non-empty and at most 32 MiB")
    async with camera_session(connection) as camera:
        catalog = await _catalog(camera, connection)
        if catalog["revision"] != revision:
            raise OnvifSettingsConflict("The service catalog changed; read it again")
        row = next(
            (item for item in catalog["services"] if item["service"] == "devicemgmt"),
            None,
        )
        if (
            row is None
            or row["binding"] != "available"
            or not any(
                item["name"] == method
                and item["readiness"] != "service_reference_required"
                for item in row["operations"]
            )
        ):
            raise ValueError("The device-management firmware binding is unavailable")
        service = await create_camera_service(camera, "devicemgmt", connection)
        service.transport.client.timeout = httpx.Timeout(180, connect=10)
        started = False

        def guard():
            nonlocal started
            before_write()
            started = True

        try:
            if method == "UpgradeSystemFirmware":
                identifier = "firmware-" + secrets.token_hex(16)
                values = {
                    "Firmware": {
                        "Include": {"href": f"cid:{identifier}"},
                        "contentType": "application/octet-stream",
                    }
                }
                binding = service.zeep_client.wsdl.bindings[service.binding_name]
                body = binding.get(method).input.body
                request = body.type(**values)
                body.type.validate(request, required=True)
                await _protected_soap(
                    service, method, values, {identifier: payload}, guard
                )
            else:
                guard()
                result = await service.ws_client[method]()
                address = device_value(result, "UploadUri")
                delay = device_value(result, "UploadDelay")
                if (
                    not isinstance(address, str)
                    or not isinstance(delay, timedelta)
                    or not 0 <= delay.total_seconds() <= 30
                ):
                    raise ValueError(
                        "The camera returned an unsupported firmware upload response"
                    )
                address = pinned_firmware_upload(
                    address, connection.host, service.xaddr
                )
                await asyncio.sleep(delay.total_seconds())
                before_write()
                response = await service.transport.client.post(
                    address,
                    content=payload,
                    headers={"Content-Type": "application/octet-stream"},
                    follow_redirects=False,
                )
                if not 200 <= response.status_code < 300:
                    raise ValueError("The firmware upload was not acknowledged")
        except Fault as error:
            if unsupported_operation(error):
                return {"state": "unsupported", "result": None}
            if started:
                raise ImagingWriteUnverified(
                    "Firmware outcome is unverified"
                ) from error
            raise
        except (
            ONVIFError,
            ZeepError,
            XmlError,
            httpx.HTTPError,
            TimeoutError,
            OSError,
            TypeError,
            ValueError,
            ValidationError,
        ) as error:
            if started:
                raise ImagingWriteUnverified(
                    "Firmware outcome is unverified"
                ) from error
            raise
        return {"state": "acknowledged_unverified", "result": None}

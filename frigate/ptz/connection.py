"""Shared, scoped ONVIF connections for camera setup and device operations."""

import asyncio
import logging
import re
import threading
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
from onvif import ONVIFCamera, ONVIFError, ONVIFService
from onvif.definition import SERVICES
from onvif.exceptions import ONVIFAuthError
from zeep.exceptions import Fault

from frigate.api.defs.request.onvif_body import OnvifConnectionBody
from frigate.util.onvif_device import device_value
from frigate.util.onvif_schema import pinned_service_url
from frigate.util.onvif_transport import PrivateSoapTransport

logger = logging.getLogger(__name__)
_cleanup_tasks: set[asyncio.Task] = set()
_cleanup_lock = threading.Lock()


def security_rejection(error: Exception) -> bool:
    """Retry only authentication rejections, not arbitrary SOAP or network faults."""
    if isinstance(error, ONVIFAuthError):
        return True
    if isinstance(error, ONVIFError):
        nested = error.__cause__ or (error.args[0] if error.args else None)
        return isinstance(nested, Fault) and security_rejection(nested)
    if not isinstance(error, Fault):
        return False
    codes = [str(error.code or ""), *(str(code) for code in error.subcodes or [])]
    if any(
        re.search(
            r"(?:NotAuthorized|FailedAuthentication|InvalidSecurityToken|InvalidSecurity|FailedCheck)$",
            code,
        )
        for code in codes
    ):
        return True
    return bool(
        re.search(
            r"\b(?:not\s*authorized|failed\s*authentication|invalid\s*security(?:\s*token)?)\b",
            error.message or "",
            re.IGNORECASE,
        )
    )


async def _close_camera_impl(camera) -> None:
    try:
        await asyncio.wait_for(camera.close(), 10)
        return
    except Exception:
        logger.debug("ONVIF client cleanup did not complete normally")
    services = getattr(camera, "services", None)
    if isinstance(services, dict):

        async def close_service(service):
            try:
                await asyncio.wait_for(service.close(), 5)
            except Exception:
                logger.debug("ONVIF service cleanup did not complete normally")

        await asyncio.gather(*(close_service(service) for service in services.values()))


async def _close_camera(camera) -> None:
    task = asyncio.create_task(_close_camera_impl(camera))
    with _cleanup_lock:
        _cleanup_tasks.add(task)

    def completed(item):
        with _cleanup_lock:
            _cleanup_tasks.discard(item)
        if not item.cancelled():
            item.exception()

    task.add_done_callback(completed)
    await asyncio.shield(task)


def camera_cleanup_task(task) -> bool:
    """Identify protected socket cleanup when stopping a device-owner loop."""
    with _cleanup_lock:
        return task in _cleanup_tasks


async def drain_camera_cleanup() -> None:
    """Wait for bounded client teardown owned by the current ASGI/device loop."""
    loop = asyncio.get_running_loop()
    with _cleanup_lock:
        pending = [task for task in _cleanup_tasks if task.get_loop() is loop]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


class CameraTransport(PrivateSoapTransport):
    """Close both HTTP clients owned by an ONVIF service transport."""

    async def aclose(self) -> None:
        """Release asynchronous RPC and synchronous WSDL-loading resources."""
        try:
            await super().aclose()
        finally:
            await asyncio.to_thread(self.wsdl_client.close)


async def create_camera_service(
    camera: ONVIFCamera,
    name: str,
    connection: OnvifConnectionBody,
    port_type: str | None = None,
) -> ONVIFService:
    """Configure authentication before the first RPC, including service discovery."""
    if name == "media2":
        from onvif.client import UsernameDigestTokenDtDiff

        from frigate.ptz.wsdl_service import (
            MEDIA2_NAMESPACE as NAMESPACE,
        )
        from frigate.ptz.wsdl_service import (
            SchemaService,
            media2_service,
        )

        existing = camera.services.get(("media2", None))
        if isinstance(existing, SchemaService):
            return existing

        endpoint = camera.xaddrs.get(NAMESPACE)
        if not endpoint:
            device = await create_camera_service(camera, "devicemgmt", connection)
            advertised = await device.GetServices({"IncludeCapability": False})
            endpoint = next(
                (
                    device_value(item, "XAddr")
                    for item in advertised or []
                    if device_value(item, "Namespace") == NAMESPACE
                ),
                None,
            )
            if endpoint:
                endpoint = camera.rewrite_url(endpoint)
                camera.xaddrs[NAMESPACE] = endpoint
        if not endpoint:
            raise ValueError("The camera did not advertise an ONVIF Media2 service")
        pinned_service_url(endpoint, connection.host)
        service = await media2_service(
            endpoint,
            connection,
            UsernameDigestTokenDtDiff(
                connection.username,
                connection.password.get_secret_value(),
                use_digest=camera.encrypt,
                dt_diff=camera.dt_diff,
            ),
        )
        camera.services[("media2", None)] = service
        return service
    namespace = SERVICES.get(name, {}).get("ns")
    if (
        name != "devicemgmt"
        and port_type is None
        and namespace
        and not camera.xaddrs.get(namespace)
    ):
        device = await create_camera_service(camera, "devicemgmt", connection)
        advertised = await device.GetServices({"IncludeCapability": False})
        for item in advertised or []:
            reported_namespace = device_value(item, "Namespace")
            endpoint = device_value(item, "XAddr")
            if (
                isinstance(reported_namespace, str)
                and isinstance(endpoint, str)
                and endpoint
            ):
                camera.xaddrs[reported_namespace] = camera.rewrite_url(endpoint)
    service = await camera.create_onvif_service(name, port_type=port_type)
    address = getattr(service, "xaddr", None)
    if isinstance(address, str):
        pinned_service_url(
            address,
            connection.host,
            allow_query=name in ("pullpoint", "subscription")
            and port_type == "PullPointSubscription",
        )
    if isinstance(service.transport, CameraTransport):
        return service
    if service.zeep_client is None:
        raise RuntimeError("ONVIF service binding is not initialized")

    auth = (
        httpx.DigestAuth(connection.username, connection.password.get_secret_value())
        if connection.auth_type == "digest"
        else None
    )
    transport = CameraTransport(
        client=httpx.AsyncClient(
            auth=auth,
            verify=not connection.tls_insecure,
            timeout=10,
        ),
        wsdl_client=httpx.Client(
            verify=not connection.tls_insecure,
            timeout=10,
        ),
        operation_timeout=10,
    )
    previous = service.transport
    service.transport = transport
    service.zeep_client.transport = transport
    authless = getattr(service, "zeep_client_authless", None)
    if authless is not None:
        authless.transport = transport
    await previous.aclose()
    return service


@asynccontextmanager
async def camera_session(
    connection: OnvifConnectionBody,
    *,
    wsdl_dir: str | None = None,
) -> AsyncGenerator[ONVIFCamera]:
    """Connect with WS-Security negotiation and close every successful/failed client."""
    first_fault: Exception | None = None
    # Passing None overrides the SDK's bundled WSDL directory default.
    wsdl_options = {"wsdl_dir": wsdl_dir} if wsdl_dir is not None else {}
    for password_digest in (True, False):
        async with AsyncExitStack() as resources:
            camera = ONVIFCamera(
                connection.host,
                connection.port,
                connection.username,
                connection.password.get_secret_value(),
                encrypt=password_digest,
                adjust_time=connection.ignore_time_mismatch,
                no_cache=True,
                **wsdl_options,
            )
            resources.push_async_callback(_close_camera, camera)
            await create_camera_service(camera, "devicemgmt", connection)
            try:
                await camera.update_xaddrs()
            except (Fault, ONVIFError) as error:
                if not security_rejection(error):
                    raise
                if first_fault is None:
                    first_fault = error
                logger.debug(
                    "ONVIF WS-Security %s was rejected",
                    "PasswordDigest" if password_digest else "PasswordText",
                )
                continue
            yield camera
            return
    if first_fault is None:
        raise RuntimeError("ONVIF connection failed without a reported fault")
    raise first_fault

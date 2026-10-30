"""Shared SDK handshake policy with mock cameras and no camera/network contacts."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from onvif.definition import SERVICES
from onvif.exceptions import ONVIFAuthError, ONVIFTimeoutError
from zeep.exceptions import Fault, TransportError

from frigate.api.defs.request.onvif_body import OnvifConnectionBody
from frigate.ptz.connection import (
    camera_session,
    create_camera_service,
    drain_camera_cleanup,
)


def camera(error=None):
    return SimpleNamespace(
        update_xaddrs=AsyncMock(side_effect=error),
        close=AsyncMock(),
        services={},
    )


class TestScopedOnvifConnection(unittest.IsolatedAsyncioTestCase):
    async def test_query_endpoints_are_only_allowed_for_subscription_bindings(self):
        connection = OnvifConnectionBody(host="camera.invalid")
        for name, port_type, allowed in (
            ("pullpoint", "PullPointSubscription", True),
            ("subscription", "PullPointSubscription", True),
            ("pullpoint", None, False),
            ("events", "PullPointSubscription", False),
            ("devicemgmt", None, False),
        ):
            with self.subTest(name=name, port_type=port_type):
                service = SimpleNamespace(
                    xaddr="http://camera.invalid:8000/onvif/Subscription?Idx=12",
                    transport=SimpleNamespace(aclose=AsyncMock()),
                    zeep_client=SimpleNamespace(),
                )
                device = SimpleNamespace(
                    create_onvif_service=AsyncMock(return_value=service),
                    xaddrs={
                        definition["ns"]: "http://camera.invalid/onvif/service"
                        for definition in SERVICES.values()
                        if definition.get("ns")
                    },
                )
                if not allowed:
                    with self.assertRaises(ValueError):
                        await create_camera_service(device, name, connection, port_type)
                else:
                    result = await create_camera_service(
                        device, name, connection, port_type
                    )
                    await result.transport.aclose()

    async def asyncTearDown(self):
        await drain_camera_cleanup()

    async def test_transport_is_configured_before_any_discovery_rpc(self):
        device = camera()
        configured = False

        async def setup(*args, **kwargs):
            nonlocal configured
            configured = True

        async def discover():
            self.assertTrue(configured)

        device.update_xaddrs.side_effect = discover
        connection = OnvifConnectionBody(
            host="camera.invalid",
            auth_type="digest",
            tls_insecure=True,
            ignore_time_mismatch=True,
        )
        with (
            patch(
                "frigate.ptz.connection.ONVIFCamera", return_value=device
            ) as constructor,
            patch("frigate.ptz.connection.create_camera_service", side_effect=setup),
        ):
            async with camera_session(connection) as result:
                self.assertIs(result, device)
                device.close.assert_not_awaited()
        device.close.assert_awaited_once()
        self.assertTrue(constructor.call_args.kwargs["adjust_time"])
        self.assertTrue(constructor.call_args.kwargs["encrypt"])
        self.assertTrue(constructor.call_args.kwargs["no_cache"])
        self.assertNotIn("wsdl_dir", constructor.call_args.kwargs)

    async def test_explicit_wsdl_directory_is_preserved(self):
        device = camera()
        with (
            patch(
                "frigate.ptz.connection.ONVIFCamera", return_value=device
            ) as constructor,
            patch("frigate.ptz.connection.create_camera_service", AsyncMock()),
        ):
            async with camera_session(
                OnvifConnectionBody(host="camera.invalid"), wsdl_dir="custom-wsdl"
            ):
                pass
        self.assertEqual(constructor.call_args.kwargs["wsdl_dir"], "custom-wsdl")
        device.close.assert_awaited_once()

    async def test_failed_auth_candidate_is_closed_before_password_text_fallback(self):
        first = camera(Fault("NotAuthorized", code="ter:NotAuthorized"))
        second = camera()

        def construct(*args, **kwargs):
            if kwargs["encrypt"]:
                return first
            first.close.assert_awaited_once()
            return second

        with (
            patch(
                "frigate.ptz.connection.ONVIFCamera", side_effect=construct
            ) as constructor,
            patch("frigate.ptz.connection.create_camera_service", AsyncMock()),
        ):
            async with camera_session(
                OnvifConnectionBody(host="camera.invalid")
            ) as result:
                self.assertIs(result, second)
        self.assertEqual(constructor.call_count, 2)
        second.close.assert_awaited_once()

    async def test_sdk_auth_errors_negotiate_but_arbitrary_faults_and_timeouts_do_not(
        self,
    ):
        for error, retry in (
            (ONVIFAuthError("synthetic rejected token"), True),
            (Fault("Invalid argument", code="ter:InvalidArgVal"), False),
            (TransportError("synthetic connection failure"), False),
            (ONVIFTimeoutError("synthetic timeout"), False),
        ):
            with self.subTest(error=type(error).__name__):
                first = camera(error)
                second = camera()
                with (
                    patch(
                        "frigate.ptz.connection.ONVIFCamera",
                        side_effect=[first, second],
                    ) as constructor,
                    patch("frigate.ptz.connection.create_camera_service", AsyncMock()),
                ):
                    if retry:
                        async with camera_session(
                            OnvifConnectionBody(host="camera.invalid")
                        ):
                            pass
                    else:
                        with self.assertRaises(type(error)):
                            async with camera_session(
                                OnvifConnectionBody(host="camera.invalid")
                            ):
                                pass
                self.assertEqual(constructor.call_count, 2 if retry else 1)
                first.close.assert_awaited_once()

    async def test_cleanup_failure_cannot_leak_secret_logs_or_skip_other_services(self):
        device = camera()
        device.close.side_effect = RuntimeError(
            "rtsp://private-password@camera.invalid/?private-token"
        )
        service = SimpleNamespace(close=AsyncMock())
        device.services = {"media": service}
        with (
            patch("frigate.ptz.connection.ONVIFCamera", return_value=device),
            patch("frigate.ptz.connection.create_camera_service", AsyncMock()),
            self.assertLogs("frigate.ptz.connection", level="DEBUG") as messages,
        ):
            async with camera_session(OnvifConnectionBody(host="camera.invalid")):
                pass
        service.close.assert_awaited_once()
        self.assertNotIn("private-password", " ".join(messages.output))
        self.assertNotIn("private-token", " ".join(messages.output))

    async def test_http_digest_tls_and_both_zeep_clients_share_owned_transport(self):
        previous = SimpleNamespace(aclose=AsyncMock())
        service = SimpleNamespace(
            transport=previous,
            zeep_client=SimpleNamespace(),
            zeep_client_authless=SimpleNamespace(),
        )
        device = SimpleNamespace(create_onvif_service=AsyncMock(return_value=service))
        connection = OnvifConnectionBody(
            host="camera.invalid",
            username="user",
            password="synthetic-password",
            auth_type="digest",
            tls_insecure=True,
        )
        real_async, real_sync = httpx.AsyncClient, httpx.Client
        with (
            patch(
                "frigate.ptz.connection.httpx.AsyncClient", wraps=real_async
            ) as async_client,
            patch(
                "frigate.ptz.connection.httpx.Client", wraps=real_sync
            ) as sync_client,
        ):
            result = await create_camera_service(device, "devicemgmt", connection)
        try:
            self.assertIsInstance(
                async_client.call_args.kwargs["auth"], httpx.DigestAuth
            )
            self.assertFalse(async_client.call_args.kwargs["verify"])
            self.assertFalse(sync_client.call_args.kwargs["verify"])
            self.assertIs(result.transport, result.zeep_client.transport)
            self.assertIs(result.transport, result.zeep_client_authless.transport)
            previous.aclose.assert_awaited_once()
        finally:
            await result.transport.aclose()
        self.assertTrue(result.transport.client.is_closed)
        self.assertTrue(result.transport.wsdl_client.is_closed)

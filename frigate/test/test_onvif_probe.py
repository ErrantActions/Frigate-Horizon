"""Wizard probe contract and secret-safe scoped lifecycle, using bounded mocks."""

import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from onvif import ONVIFError

from frigate.api.camera import _connect_onvif_camera, onvif_probe_body
from frigate.api.defs.request.onvif_body import OnvifProbeBody


class TestOnvifWizardProbe(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.request = SimpleNamespace(
            app=SimpleNamespace(
                frigate_config=SimpleNamespace(ffmpeg=object()),
            )
        )
        self.body = OnvifProbeBody(
            host="camera.invalid",
            username="user",
            password="synthetic-private-password",
            auth_type="digest",
            tls_insecure=True,
            ignore_time_mismatch=True,
        )
        self.closed = False
        self.camera = object()

        @asynccontextmanager
        async def connect(*args, **kwargs):
            try:
                yield self.camera
            finally:
                self.closed = True

        self.connect = connect

    async def test_adapter_delegates_transport_time_and_cleanup_policy(self):
        entered = []

        @asynccontextmanager
        async def session(connection, **kwargs):
            entered.append((connection, kwargs))
            try:
                yield self.camera
            finally:
                self.closed = True

        with patch("frigate.api.camera.camera_session", session):
            async with _connect_onvif_camera(
                "camera.invalid",
                80,
                "user",
                "synthetic-private-password",
                None,
                "digest",
                tls_insecure=True,
                ignore_time_mismatch=True,
            ) as camera:
                self.assertIs(camera, self.camera)
                self.assertFalse(self.closed)
        self.assertTrue(self.closed)
        connection = entered[0][0]
        self.assertEqual(connection.auth_type, "digest")
        self.assertTrue(connection.tls_insecure)
        self.assertTrue(connection.ignore_time_mismatch)

    async def test_response_contract_is_preserved_but_logs_never_include_private_uris(
        self,
    ):
        private_uri = "rtsp://user:synthetic-private-password@camera.invalid/live?token=synthetic-private-token"
        device = SimpleNamespace(
            GetDeviceInformation=AsyncMock(
                return_value={"Manufacturer": "Example", "Model": "Mock"}
            )
        )
        media = SimpleNamespace(
            GetProfiles=AsyncMock(return_value=[{"token": "main"}]),
            GetStreamUri=AsyncMock(return_value={"Uri": private_uri}),
        )

        async def service(camera, name, connection):
            self.assertEqual(connection.auth_type, "digest")
            self.assertTrue(connection.tls_insecure)
            if name == "ptz":
                raise ONVIFError(private_uri)
            return {"devicemgmt": device, "media": media}[name]

        with (
            patch("frigate.api.camera._connect_onvif_camera", self.connect),
            patch("frigate.api.camera.create_camera_service", side_effect=service),
            self.assertLogs("frigate.api.camera", level="DEBUG") as messages,
        ):
            response = await onvif_probe_body(self.request, self.body)
        result = json.loads(response.body)
        self.assertTrue(result["success"])
        self.assertEqual(result["profiles_count"], 1)
        self.assertEqual(result["rtsp_candidates"][0]["uri"], private_uri)
        self.assertFalse(result["ptz_supported"])
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertTrue(self.closed)
        self.assertNotIn("synthetic-private-password", " ".join(messages.output))
        self.assertNotIn("synthetic-private-token", " ".join(messages.output))

    async def test_probe_timeout_closes_the_owned_context(self):
        async def wait():
            await asyncio.Event().wait()

        device = SimpleNamespace(GetDeviceInformation=wait)
        timeout = asyncio.timeout
        with (
            patch("frigate.api.camera._connect_onvif_camera", self.connect),
            patch(
                "frigate.api.camera.create_camera_service",
                AsyncMock(return_value=device),
            ),
            patch(
                "frigate.api.camera.asyncio.timeout",
                side_effect=lambda _: timeout(0.01),
            ),
        ):
            response = await onvif_probe_body(self.request, self.body)
        self.assertEqual(response.status_code, 503)
        self.assertTrue(self.closed)

    async def test_cancelled_probe_closes_the_owned_context(self):
        entered = asyncio.Event()

        async def wait():
            entered.set()
            await asyncio.Event().wait()

        device = SimpleNamespace(GetDeviceInformation=wait)
        with (
            patch("frigate.api.camera._connect_onvif_camera", self.connect),
            patch(
                "frigate.api.camera.create_camera_service",
                AsyncMock(return_value=device),
            ),
        ):
            task = asyncio.create_task(onvif_probe_body(self.request, self.body))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(self.closed)

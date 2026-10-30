"""Installed-SDK catalog integration with only mocked device RPCs, never camera writes."""

import unittest
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from onvif import ONVIFCamera
from onvif.definition import SERVICES

from frigate.api.defs.request.onvif_body import OnvifConnectionBody
from frigate.ptz.administration import (
    administration_catalog,
    download_system_backup,
    invoke_operation,
    restore_system_backup,
)
from frigate.ptz.connection import create_camera_service
from frigate.ptz.firmware import pinned_firmware_upload, upgrade_firmware
from frigate.ptz.imaging import ImagingWriteUnverified
from frigate.ptz.wsdl_service import MEDIA2_NAMESPACE
from frigate.test.test_onvif_mtom import envelope
from frigate.util.onvif_backup import backup_download
from frigate.util.onvif_device import OnvifSettingsConflict
from frigate.util.onvif_mtom import encode_mtom


class Proxy:
    def __init__(self, operations):
        self.operations = operations

    def __getitem__(self, name):
        return self.operations[name]


class TestInstalledAdministrationCatalog(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.connection = OnvifConnectionBody(host="camera.invalid")
        self.camera = ONVIFCamera("camera.invalid", 80, "", "", no_cache=True)
        self.addAsyncCleanup(self.camera.close)
        namespace = SERVICES["devicemgmt"]["ns"]
        self.reported = [
            {
                "Namespace": namespace,
                "XAddr": "http://camera.invalid/onvif/device_service",
                "Version": {"Major": 1, "Minor": 0},
            },
            {
                "Namespace": MEDIA2_NAMESPACE,
                "XAddr": "http://camera.invalid/onvif/media2",
                "Version": {"Major": 2, "Minor": 0},
            },
            {
                "Namespace": "urn:unknown:camera-service",
                "XAddr": "http://camera.invalid/onvif/unknown",
            },
            {
                "Namespace": "urn:foreign:endpoint",
                "XAddr": "http://other.invalid/onvif/service",
            },
        ]
        self.camera.xaddrs[namespace] = self.reported[0]["XAddr"]
        self.device = await create_camera_service(
            self.camera, "devicemgmt", self.connection
        )
        self.device.GetServices = AsyncMock(side_effect=lambda _: self.reported)
        self.read = AsyncMock(return_value=SimpleNamespace(Name="Fixture"))
        self.write = AsyncMock()
        self.device.ws_client = Proxy(
            {"GetHostname": self.read, "SystemReboot": self.write}
        )

        @asynccontextmanager
        async def session(connection):
            yield self.camera

        self.session_patch = patch("frigate.ptz.administration.camera_session", session)
        self.session_patch.start()
        self.addCleanup(self.session_patch.stop)
        self.firmware_session_patch = patch(
            "frigate.ptz.firmware.camera_session", session
        )
        self.firmware_session_patch.start()
        self.addCleanup(self.firmware_session_patch.stop)

    async def test_real_sdk_and_official_media2_binding_are_cataloged_without_operation_probes(
        self,
    ):
        catalog = await administration_catalog(self.connection)
        rows = {row["namespace"]: row for row in catalog["services"]}
        self.assertEqual(rows[MEDIA2_NAMESPACE]["binding"], "available")
        self.assertEqual(len(rows[MEDIA2_NAMESPACE]["operations"]), 59)
        self.assertEqual(rows["urn:unknown:camera-service"]["binding"], "unavailable")
        self.assertEqual(rows["urn:foreign:endpoint"]["binding"], "endpoint_rejected")
        self.assertFalse(catalog["complete"])
        self.read.assert_not_awaited()
        self.write.assert_not_awaited()

    async def test_catalog_changed_endpoint_conflicts_before_any_write(self):
        catalog = await administration_catalog(self.connection)
        self.reported[1]["XAddr"] = "http://camera.invalid/onvif/replaced-media2"
        with self.assertRaises(OnvifSettingsConflict):
            await invoke_operation(
                self.connection,
                "devicemgmt",
                "SystemReboot",
                {},
                catalog["revision"],
                "SystemReboot",
                before_write=Mock(),
            )
        self.write.assert_not_awaited()

    async def test_unavailable_and_unclassified_operations_do_not_bypass_binding_validation(
        self,
    ):
        catalog = await administration_catalog(self.connection)
        for service, operation in (
            ("other", "SystemReboot"),
            ("devicemgmt", "UnknownOperation"),
        ):
            with self.assertRaises(ValueError):
                await invoke_operation(
                    self.connection,
                    service,
                    operation,
                    {},
                    catalog["revision"],
                    operation,
                    before_write=Mock(),
                )
        self.write.assert_not_awaited()

    async def test_lost_ack_is_not_retried_through_the_sdk(self):
        catalog = await administration_catalog(self.connection)
        self.write.side_effect = httpx.ReadTimeout("synthetic lost acknowledgement")
        guard = Mock()
        with self.assertRaises(httpx.ReadTimeout):
            await invoke_operation(
                self.connection,
                "devicemgmt",
                "SystemReboot",
                {},
                catalog["revision"],
                "SystemReboot",
                before_write=guard,
            )
        self.write.assert_awaited_once()
        guard.assert_called_once()

    async def test_backup_download_preserves_private_bytes_without_mutating_device(
        self,
    ):
        import io
        import zipfile

        catalog = await administration_catalog(self.connection)
        content, content_type = encode_mtom(
            envelope(
                "GetSystemBackup",
                "<tds:BackupFiles><tt:Name>device-config</tt:Name><tt:Data>"
                '<xop:Include href="cid:backup-one"/></tt:Data></tds:BackupFiles>',
            ),
            {"backup-one": b"fixture-backup"},
        )
        backup = AsyncMock(
            return_value=httpx.Response(
                200,
                content=content,
                headers={"Content-Type": content_type},
                request=httpx.Request("POST", "http://camera.invalid/onvif"),
            )
        )
        self.device.transport.post = backup
        result = await download_system_backup(self.connection, catalog["revision"])
        with zipfile.ZipFile(io.BytesIO(result)) as archive:
            self.assertEqual(archive.read("files/000.bin"), b"fixture-backup")
        backup.assert_awaited_once()
        self.write.assert_not_awaited()

    async def test_backup_catalog_conflict_is_rejected_before_reading_private_data(
        self,
    ):
        backup = AsyncMock()
        self.device.transport.post = backup
        with self.assertRaises(OnvifSettingsConflict):
            await download_system_backup(self.connection, "0" * 64)
        backup.assert_not_awaited()

    async def test_restore_uses_installed_schema_and_never_retries_lost_ack(self):
        catalog = await administration_catalog(self.connection)
        restore = AsyncMock(
            return_value=httpx.Response(
                200,
                content=envelope("RestoreSystem"),
                headers={"Content-Type": "application/soap+xml"},
                request=httpx.Request("POST", "http://camera.invalid/onvif"),
            )
        )
        self.device.transport.post = restore
        payload = backup_download(
            [{"Name": "device-config", "Data": {"Data": b"fixture-backup"}}]
        )
        guard = Mock()
        response = await restore_system_backup(
            self.connection, catalog["revision"], payload, before_write=guard
        )
        self.assertEqual(response["state"], "acknowledged_unverified")
        restore.assert_awaited_once()
        guard.assert_called_once()
        restore.reset_mock()
        restore.side_effect = httpx.ReadTimeout("synthetic acknowledgement lost")
        with self.assertRaises(httpx.ReadTimeout):
            await restore_system_backup(
                self.connection, catalog["revision"], payload, before_write=guard
            )
        restore.assert_awaited_once()

    async def test_firmware_mtom_uses_actual_sdk_schema_and_sends_once(self):
        catalog = await administration_catalog(self.connection)
        guard = Mock()

        async def submit(address, message, headers):
            guard.assert_called_once()
            self.assertIn(b"fixture-firmware\x00\xff", message)
            self.assertIn(b"cid:firmware-", message)
            self.assertIn("multipart/related", headers["Content-Type"])
            return httpx.Response(
                200,
                content=envelope(
                    "UpgradeSystemFirmware", "<tds:Message>Accepted</tds:Message>"
                ),
                headers={"Content-Type": "application/soap+xml"},
                request=httpx.Request("POST", address),
            )

        sender = AsyncMock(side_effect=submit)
        self.device.transport.post = sender
        result = await upgrade_firmware(
            self.connection,
            catalog["revision"],
            "UpgradeSystemFirmware",
            b"fixture-firmware\x00\xff",
            before_write=guard,
        )
        self.assertEqual(result["state"], "acknowledged_unverified")
        sender.assert_awaited_once()

    async def test_firmware_lost_ack_is_unverified_and_never_retried(self):
        catalog = await administration_catalog(self.connection)
        sender = AsyncMock(
            side_effect=httpx.ReadTimeout("synthetic acknowledgement lost")
        )
        self.device.transport.post = sender
        with self.assertRaises(ImagingWriteUnverified):
            await upgrade_firmware(
                self.connection,
                catalog["revision"],
                "UpgradeSystemFirmware",
                b"fixture-firmware",
                before_write=Mock(),
            )
        sender.assert_awaited_once()

    async def test_uri_firmware_upload_is_pinned_and_never_follows_redirects(self):
        catalog = await administration_catalog(self.connection)
        start = AsyncMock(
            return_value=SimpleNamespace(
                UploadUri="http://camera.invalid/firmware?ticket=fixture",
                UploadDelay=timedelta(0),
            )
        )
        self.device.ws_client = Proxy({"StartFirmwareUpgrade": start})
        sender = AsyncMock(return_value=httpx.Response(200))
        self.device.transport.client.post = sender
        guard = Mock()
        result = await upgrade_firmware(
            self.connection,
            catalog["revision"],
            "StartFirmwareUpgrade",
            b"fixture-firmware",
            before_write=guard,
        )
        self.assertEqual(result["state"], "acknowledged_unverified")
        self.assertEqual(guard.call_count, 2)
        start.assert_awaited_once()
        sender.assert_awaited_once_with(
            "http://camera.invalid/firmware?ticket=fixture",
            content=b"fixture-firmware",
            headers={"Content-Type": "application/octet-stream"},
            follow_redirects=False,
        )

    async def test_foreign_firmware_upload_is_blocked_after_start_without_retry(self):
        catalog = await administration_catalog(self.connection)
        start = AsyncMock(
            return_value=SimpleNamespace(
                UploadUri="http://other.invalid/firmware",
                UploadDelay=timedelta(0),
            )
        )
        self.device.ws_client = Proxy({"StartFirmwareUpgrade": start})
        sender = AsyncMock()
        self.device.transport.client.post = sender
        with self.assertRaises(ImagingWriteUnverified):
            await upgrade_firmware(
                self.connection,
                catalog["revision"],
                "StartFirmwareUpgrade",
                b"fixture-firmware",
                before_write=Mock(),
            )
        start.assert_awaited_once()
        sender.assert_not_awaited()

    async def test_firmware_catalog_conflict_does_not_write(self):
        sender = AsyncMock()
        self.device.transport.post = sender
        guard = Mock()
        with self.assertRaises(OnvifSettingsConflict):
            await upgrade_firmware(
                self.connection,
                "0" * 64,
                "UpgradeSystemFirmware",
                b"fixture-firmware",
                before_write=guard,
            )
        sender.assert_not_awaited()
        guard.assert_not_called()

    def test_firmware_upload_policy_rejects_foreign_credentials_and_tls_downgrade(self):
        for address in (
            "http://other.invalid/upload",
            "http://fixture@camera.invalid/upload",
            "file:///firmware",
            "http://camera.invalid/upload#fragment",
            "http://camera.invalid/upload",
        ):
            with self.assertRaises(ValueError):
                pinned_firmware_upload(
                    address, "camera.invalid", "https://camera.invalid/onvif"
                )

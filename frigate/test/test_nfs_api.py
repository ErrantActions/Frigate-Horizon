"""NFS helpers require admin access and report bounded, truthful probe results."""

import socket
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from frigate.api.nfs import router


class TestNfsApi(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(router)
        self.app.frigate_config = SimpleNamespace(
            auth=SimpleNamespace(roles={"admin": [], "viewer": []}),
            proxy=SimpleNamespace(separator=","),
            record=SimpleNamespace(enabled=False),
            storage=SimpleNamespace(
                archive_path="/media/frigate/archive",
                mount_id="archive-id",
                local_budget_gib=64,
            ),
        )
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.admin = {"remote-role": "admin"}

    def test_missing_and_viewer_roles_cannot_discover_or_probe_storage(self):
        for headers in ({}, {"remote-role": "viewer"}):
            with (
                self.subTest(headers=headers),
                patch(
                    "frigate.api.nfs.asyncio.open_connection", new_callable=AsyncMock
                ) as connect,
            ):
                self.assertEqual(
                    self.client.get("/storage/nfs", headers=headers).status_code, 403
                )
                self.assertEqual(
                    self.client.post(
                        "/storage/nfs/probe",
                        headers=headers,
                        json={"server": "nas.example"},
                    ).status_code,
                    403,
                )
                self.assertEqual(
                    self.client.post(
                        "/storage/nfs/plan", headers=headers, json={"shares": []}
                    ).status_code,
                    403,
                )
                connect.assert_not_called()

    def test_status_uses_metadata_only_and_does_not_claim_verified_export_access(self):
        with patch("frigate.util.nfs.psutil.disk_partitions", return_value=[]):
            response = self.client.get("/storage/nfs", headers=self.admin)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["automatic_remounts"])
        self.assertFalse(response.json()["mounts"]["archive"]["access_verified"])
        self.assertFalse(response.json()["recording_enabled"])

    def test_plan_is_admin_only_and_does_not_change_live_settings(self):
        response = self.client.post(
            "/storage/nfs/plan",
            headers=self.admin,
            json={"shares": [{"server": "nas.example", "export": "/mnt/nvr"}]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["enables_recording"])
        self.assertFalse(self.app.frigate_config.record.enabled)

    def test_probe_closes_socket_and_does_not_pretend_to_validate_exports(self):
        writer = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
        with (
            patch(
                "asyncio.BaseEventLoop.getaddrinfo",
                new=AsyncMock(
                    return_value=[
                        (
                            socket.AF_INET,
                            socket.SOCK_STREAM,
                            6,
                            "",
                            ("10.20.30.40", 2049),
                        )
                    ]
                ),
            ),
            patch(
                "frigate.api.nfs.asyncio.open_connection",
                new=AsyncMock(return_value=(None, writer)),
            ) as connect,
        ):
            response = self.client.post(
                "/storage/nfs/probe", headers=self.admin, json={"server": "nas.example"}
            )
        self.assertTrue(response.json()["reachable"])
        self.assertFalse(response.json()["export_verified"])
        connect.assert_awaited_once_with("10.20.30.40", 2049, family=socket.AF_INET)
        writer.close.assert_called_once()
        writer.wait_closed.assert_awaited_once()

    def test_dns_resolution_to_loopback_is_rejected_without_connecting(self):
        with (
            patch(
                "asyncio.BaseEventLoop.getaddrinfo",
                new=AsyncMock(
                    return_value=[
                        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 2049))
                    ]
                ),
            ),
            patch(
                "frigate.api.nfs.asyncio.open_connection", new_callable=AsyncMock
            ) as connect,
            self.assertLogs("frigate.api.nfs", level="WARNING"),
        ):
            response = self.client.post(
                "/storage/nfs/probe", headers=self.admin, json={"server": "nas.example"}
            )
        self.assertFalse(response.json()["reachable"])
        self.assertEqual(response.json()["error"], "address_not_remote")
        connect.assert_not_called()

    def test_timeout_and_dns_errors_are_explicit_and_logged(self):
        for error, code in (
            (TimeoutError(), "timeout"),
            (socket.gaierror("synthetic"), "dns_failed"),
        ):
            with (
                self.subTest(code=code),
                patch(
                    "asyncio.BaseEventLoop.getaddrinfo",
                    new=AsyncMock(side_effect=error),
                ),
                self.assertLogs("frigate.api.nfs", level="WARNING"),
            ):
                response = self.client.post(
                    "/storage/nfs/probe",
                    headers=self.admin,
                    json={"server": "nas.example"},
                )
            self.assertFalse(response.json()["reachable"])
            self.assertEqual(response.json()["error"], code)


if __name__ == "__main__":
    unittest.main()

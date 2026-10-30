"""ONVIF authorization and secret-safe failures behind the real default-admin gate."""

import unittest
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from onvif import ONVIFError

from frigate.api.auth import require_admin_by_default
from frigate.api.onvif import router
from frigate.config import FrigateConfig
from frigate.ptz.imaging import ImagingWriteUnverified
from frigate.ptz.recording_access import source_reference
from frigate.ptz.replay_transport import ReplayUnsupported
from frigate.test.test_onvif_recording_access import SOURCE_KEY
from frigate.util.onvif_device import OnvifSettingsConflict


class TestOnvifHttp(unittest.TestCase):
    def setUp(self):
        camera = {
            "ffmpeg": {
                "inputs": [{"path": "rtsp://camera.invalid/test", "roles": ["detect"]}]
            },
            "detect": {"height": 720, "width": 1280, "fps": 5},
            "onvif": {
                "host": "camera.invalid",
                "user": "fixture",
                "password": "fixture-secret",
                "recording_profile": "main",
                "recording_video_source_token": "sensor",
                "recording_source_ref": source_reference(
                    {"Source": {"SourceId": "urn:camera:physical-lens-a"}}, SOURCE_KEY
                ),
            },
        }
        self.config = FrigateConfig(
            mqtt={"enabled": False},
            auth={"roles": {"front_only": ["front"]}},
            cameras={"front": camera, "back": camera},
        )
        self.app = FastAPI(dependencies=[Depends(require_admin_by_default())])
        self.app.frigate_config = self.config
        self.app.onvif_source_key = SOURCE_KEY

        async def execute(name, operation):
            return await operation(self.config, lambda: None)

        self.owner = SimpleNamespace(
            execute=AsyncMock(side_effect=execute),
            manual_image=AsyncMock(return_value={"state": "ready"}),
            image_status=AsyncMock(return_value={"phase": "idle"}),
            resume=AsyncMock(return_value={"success": True}),
            _events={},
            native_event_bridge=MagicMock(),
        )
        self.app.onvif_devices = self.owner
        self.app.onvif_replay = SimpleNamespace(
            closing=False,
            get=MagicMock(),
            status=MagicMock(),
            reserve=MagicMock(),
            resolving=set(),
            finish_resolution=MagicMock(),
        )
        self.app.include_router(router)

        @self.app.get("/onvif/cameras/{camera_name}/admin-only")
        def admin_only(camera_name: str):
            return {"camera": camera_name}

        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    @staticmethod
    def headers(role):
        return {"remote-user": "fixture", "remote-role": role}

    def image_body(self, **kwargs):
        return {
            "profile_token": "main",
            "changes": {"brightness": 60},
            "expected_values": {"brightness": 50},
            "confirm_device_change": True,
            **kwargs,
        }

    def test_private_backup_requires_admin_and_explicit_consent(self):
        with patch(
            "frigate.api.onvif.download_system_backup",
            AsyncMock(return_value=b"synthetic-private-backup"),
        ) as backup:
            path = "/onvif/cameras/front/administration/backup"
            body = {"catalog_revision": "a" * 64, "confirm_sensitive_download": True}
            denied = self.client.post(
                path, json=body, headers=self.headers("front_only")
            )
            missing = self.client.post(
                path,
                json={"catalog_revision": "a" * 64},
                headers=self.headers("admin"),
            )
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(missing.status_code, 422)
            backup.assert_not_awaited()
            allowed = self.client.post(path, json=body, headers=self.headers("admin"))
            self.assertEqual(allowed.status_code, 200)
            self.assertEqual(allowed.content, b"synthetic-private-backup")
            self.assertEqual(allowed.headers["cache-control"], "no-store")
            self.assertEqual(allowed.headers["content-type"], "application/zip")
            self.assertIn("attachment", allowed.headers["content-disposition"])
            backup.assert_awaited_once()

    def test_firmware_upload_requires_admin_and_exact_confirmation(self):
        path = "/onvif/cameras/front/administration/firmware"
        data = {
            "catalog_revision": "a" * 64,
            "method": "UpgradeSystemFirmware",
            "typed_confirmation": "UpgradeSystemFirmware",
            "confirm_device_change": "true",
        }
        self.owner.guard_administration = MagicMock()

        async def confirmed(connection, revision, method, payload, *, before_write):
            before_write()
            self.assertEqual(payload, b"synthetic-firmware")
            return {"state": "acknowledged_unverified", "result": None}

        with patch(
            "frigate.api.onvif.upgrade_firmware", AsyncMock(side_effect=confirmed)
        ) as upgrade:
            files = {
                "firmware": (
                    "fixture.bin",
                    b"synthetic-firmware",
                    "application/octet-stream",
                )
            }
            denied = self.client.post(
                path, data=data, files=files, headers=self.headers("front_only")
            )
            wrong = self.client.post(
                path,
                data={**data, "typed_confirmation": "wrong"},
                files=files,
                headers=self.headers("admin"),
            )
            unconfirmed = self.client.post(
                path,
                data={**data, "confirm_device_change": "false"},
                files=files,
                headers=self.headers("admin"),
            )
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(wrong.status_code, 422)
            self.assertEqual(unconfirmed.status_code, 422)
            upgrade.assert_not_awaited()
            accepted = self.client.post(
                path, data=data, files=files, headers=self.headers("admin")
            )
            self.assertEqual(accepted.status_code, 200)
            self.assertEqual(accepted.json()["state"], "acknowledged_unverified")
            upgrade.assert_awaited_once()
            self.owner.guard_administration.assert_called_once_with("front")

    def test_empty_or_oversized_firmware_never_reaches_the_camera(self):
        data = {
            "catalog_revision": "a" * 64,
            "method": "UpgradeSystemFirmware",
            "typed_confirmation": "UpgradeSystemFirmware",
            "confirm_device_change": "true",
        }
        with (
            patch("frigate.api.onvif.MAX_BACKUP_BYTES", 4),
            patch("frigate.api.onvif.upgrade_firmware", AsyncMock()) as upgrade,
        ):
            for payload, status in ((b"", 422), (b"12345", 413)):
                result = self.client.post(
                    "/onvif/cameras/front/administration/firmware",
                    data=data,
                    files={"firmware": ("fixture.bin", payload)},
                    headers=self.headers("admin"),
                )
                self.assertEqual(result.status_code, status)
            upgrade.assert_not_awaited()

    def test_restore_requires_admin_and_typed_confirmation_before_device_call(self):
        self.owner.guard_administration = MagicMock()
        with patch(
            "frigate.api.onvif.restore_system_backup",
            AsyncMock(
                return_value={"state": "acknowledged_unverified", "result": None}
            ),
        ) as restore:
            path = "/onvif/cameras/front/administration/restore"
            fields = {
                "catalog_revision": "a" * 64,
                "typed_confirmation": "RestoreSystem",
                "confirm_device_change": "true",
            }
            denied = self.client.post(
                path,
                data=fields,
                files={"backup": ("backup.zip", b"fixture", "application/zip")},
                headers=self.headers("front_only"),
            )
            self.assertEqual(denied.status_code, 403)
            missing = self.client.post(
                path,
                data={**fields, "typed_confirmation": ""},
                files={"backup": ("backup.zip", b"fixture", "application/zip")},
                headers=self.headers("admin"),
            )
            self.assertEqual(missing.status_code, 422)
            restore.assert_not_awaited()
            allowed = self.client.post(
                path,
                data=fields,
                files={"backup": ("backup.zip", b"fixture", "application/zip")},
                headers=self.headers("admin"),
            )
            self.assertEqual(allowed.status_code, 200)
            restore.assert_awaited_once()

    def test_camera_read_is_filtered_before_any_camera_connection(self):
        with patch(
            "frigate.api.onvif.device_services.device_status",
            AsyncMock(return_value={"connected": True}),
        ):
            allowed = self.client.get(
                "/onvif/cameras/front/status", headers=self.headers("front_only")
            )
            denied = self.client.get(
                "/onvif/cameras/back/status", headers=self.headers("front_only")
            )
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(denied.status_code, 403)
        self.owner.execute.assert_awaited_once()

    def test_administration_catalog_is_admin_only_before_camera_contact(self):
        with patch(
            "frigate.api.onvif.administration_catalog",
            AsyncMock(return_value={"services": [], "complete": False}),
        ) as catalog:
            for role in ("viewer", "front_only"):
                response = self.client.get(
                    "/onvif/cameras/front/administration", headers=self.headers(role)
                )
                self.assertEqual(response.status_code, 403)
            catalog.assert_not_awaited()
            response = self.client.get(
                "/onvif/cameras/front/administration", headers=self.headers("admin")
            )
        self.assertEqual(response.status_code, 200)
        catalog.assert_awaited_once()

    def test_administration_write_preserves_ownership_guard_and_secret_safe_conflicts(
        self,
    ):
        self.owner.guard_administration = MagicMock(
            side_effect=OnvifSettingsConflict("private-key-sentinel")
        )

        async def invoke(*args, before_write):
            before_write()

        with patch("frigate.api.onvif.invoke_operation", side_effect=invoke):
            response = self.client.post(
                "/onvif/cameras/front/administration/devicemgmt/SystemReboot",
                headers=self.headers("admin"),
                json={
                    "catalog_revision": "a" * 64,
                    "parameters": {},
                    "confirm_device_change": True,
                    "typed_confirmation": "SystemReboot",
                },
            )
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("private-key-sentinel", response.text)
        self.owner.guard_administration.assert_called_once_with("front")

    def test_administration_rejects_coerced_confirmation_and_caller_connection(self):
        with patch("frigate.api.onvif.invoke_operation", AsyncMock()) as invoke:
            for extra in (
                {"confirm_device_change": "true"},
                {"host": "other.invalid"},
                {"password": "write-only-sentinel"},
            ):
                response = self.client.post(
                    "/onvif/cameras/front/administration/devicemgmt/SystemReboot",
                    headers=self.headers("admin"),
                    json={"catalog_revision": "a" * 64, "parameters": {}, **extra},
                )
                self.assertEqual(response.status_code, 422)
                self.assertNotIn("write-only-sentinel", response.text)
            invoke.assert_not_awaited()

    def test_viewers_and_anonymous_users_never_write_devices(self):
        for role in ("front_only", "viewer"):
            response = self.client.put(
                "/onvif/cameras/front/imaging",
                json=self.image_body(),
                headers=self.headers(role),
            )
            self.assertEqual(response.status_code, 403)
        self.assertEqual(
            self.client.put(
                "/onvif/cameras/front/imaging",
                json=self.image_body(),
            ).status_code,
            403,
        )
        self.owner.manual_image.assert_not_awaited()

    def test_missing_confirmation_and_truthy_coercions_are_rejected(self):
        for value in (False, 1, "true", None):
            response = self.client.put(
                "/onvif/cameras/front/imaging",
                json=self.image_body(confirm_device_change=value),
                headers=self.headers("admin"),
            )
            self.assertEqual(response.status_code, 422)
            self.owner.manual_image.assert_not_awaited()

    def test_connection_credentials_are_not_accepted_in_camera_mutations(self):
        response = self.client.put(
            "/onvif/cameras/front/imaging",
            json=self.image_body(password="must-remain-server-side"),
            headers=self.headers("admin"),
        )
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("must-remain-server-side", response.text)
        self.owner.manual_image.assert_not_awaited()

    def test_uncertain_write_and_sdk_errors_do_not_disclose_secrets(self):
        for error, code in (
            (ImagingWriteUnverified("fixture-secret"), 409),
            (ONVIFError("http://fixture:fixture-secret@camera.invalid"), 502),
        ):
            self.owner.manual_image.side_effect = error
            response = self.client.put(
                "/onvif/cameras/front/imaging",
                json=self.image_body(),
                headers=self.headers("admin"),
            )
            self.assertEqual(response.status_code, code)
            self.assertNotIn("fixture-secret", response.text)

    def test_replay_is_camera_authorized_before_token_lookup(self):
        with patch(
            "frigate.api.onvif.device_services.replay_source", AsyncMock()
        ) as replay:
            response = self.client.post(
                "/onvif/cameras/back/replay/sessions",
                json={
                    "recording_token": "private",
                    "start": "2026-10-18T00:00:00Z",
                    "end": "2026-10-18T00:01:00Z",
                },
                headers=self.headers("front_only"),
            )
        self.assertEqual(response.status_code, 403)
        replay.assert_not_awaited()

    def test_every_replay_status_stream_and_delete_request_requires_camera_access(self):
        for method, suffix in (
            ("GET", "/session"),
            ("GET", "/session/hls/index.m3u8"),
            ("GET", "/session/hls/init.mp4"),
            ("GET", "/session/hls/segment_00000.m4s"),
            ("DELETE", "/session"),
        ):
            response = self.client.request(
                method,
                "/onvif/cameras/back/replay/sessions" + suffix,
                headers=self.headers("front_only"),
            )
            self.assertEqual(response.status_code, 403)
        self.app.onvif_replay.get.assert_not_called()

    def test_replay_rejects_caller_urls_and_unbounded_ranges_before_any_camera_contact(
        self,
    ):
        for body in (
            {
                "recording_token": "recording",
                "start": "2026-10-18T00:00:00Z",
                "end": "2026-10-18T00:06:00Z",
            },
            {
                "recording_token": "recording",
                "start": "2026-10-18T00:00:00Z",
                "end": "2026-10-18T00:01:00Z",
                "url": "rtsp://private-secret@elsewhere",
            },
        ):
            response = self.client.post(
                "/onvif/cameras/front/replay/sessions",
                json=body,
                headers=self.headers("admin"),
            )
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("private-secret", response.text)
        self.owner.execute.assert_not_awaited()

    def test_replay_capability_response_never_returns_a_camera_uri(self):
        response = self.client.get(
            "/onvif/cameras/front/replay", headers=self.headers("front_only")
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "browser_sessions")
        self.assertNotIn("uri", response.json())

    def test_unsupported_replay_never_reflects_the_private_camera_address(self):
        with patch(
            "frigate.api.onvif.device_services.replay_source",
            AsyncMock(
                side_effect=ReplayUnsupported(
                    "rtsp://private-user:private-password@camera.invalid/replay?token=private-token"
                )
            ),
        ):
            response = self.client.post(
                "/onvif/cameras/front/replay/sessions",
                json={
                    "recording_token": "recording",
                    "start": "2026-10-18T00:00:00Z",
                    "end": "2026-10-18T00:01:00Z",
                },
                headers=self.headers("front_only"),
            )
        self.assertEqual(response.status_code, 422)
        for secret in ("private-password", "private-token", "rtsp://"):
            self.assertNotIn(secret, response.text)

    def test_hls_resources_are_private_and_do_not_release_the_shared_session(self):
        self.app.onvif_replay.resource = AsyncMock(return_value=b"#EXTM3U\n")
        self.app.onvif_replay.close = AsyncMock()
        response = self.client.get(
            "/onvif/cameras/front/replay/sessions/opaque/hls/index.m3u8",
            headers=self.headers("front_only"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"#EXTM3U\n")
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertEqual(
            response.headers["content-type"], "application/vnd.apple.mpegurl"
        )
        self.app.onvif_replay.close.assert_not_awaited()

    def test_hls_range_requests_return_the_complete_bounded_object(self):
        self.app.onvif_replay.resource = AsyncMock(return_value=b"init")
        response = self.client.get(
            "/onvif/cameras/front/replay/sessions/opaque/hls/init.mp4",
            headers={**self.headers("front_only"), "Range": "bytes=500-1000"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"init")
        self.app.onvif_replay.resource.assert_awaited_once_with(
            "front", "opaque", "init.mp4"
        )
        self.owner.execute.assert_not_awaited()

    def test_native_events_are_explicitly_status_history_only(self):
        response = self.client.get(
            "/onvif/cameras/front/events",
            headers=self.headers("front_only"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["integration"], "status_history_only")

    def test_native_integration_needs_explicit_opt_in_and_a_separate_source_selection(
        self,
    ):
        response = self.client.post(
            "/onvif/cameras/front/events",
            json={
                "enabled": True,
                "integrate_events": True,
                "confirm_device_change": True,
            },
            headers=self.headers("admin"),
        )
        self.assertEqual(response.status_code, 422)
        self.owner.execute.assert_not_awaited()

    def test_allowed_lens_a_cannot_replay_a_guessed_lens_b_token_on_the_same_host(self):
        self.config.cameras["back"].onvif.recording_profile = "lens-b"
        self.config.cameras["back"].onvif.recording_video_source_token = "sensor-b"
        self.config.cameras["back"].onvif.recording_source_ref = source_reference(
            {"Source": {"SourceId": "urn:camera:physical-lens-b"}},
            SOURCE_KEY,
        )
        now = datetime(2026, 10, 18, tzinfo=UTC)
        media = SimpleNamespace(
            GetProfiles=AsyncMock(
                return_value=[
                    {
                        "token": "main",
                        "VideoSourceConfiguration": {"SourceToken": "sensor"},
                    },
                    {
                        "token": "lens-b",
                        "VideoSourceConfiguration": {"SourceToken": "sensor-b"},
                    },
                ]
            )
        )
        search = SimpleNamespace(
            GetRecordingInformation=AsyncMock(
                return_value={
                    "RecordingToken": "guessed-lens-b",
                    "Source": {"SourceId": "urn:camera:physical-lens-b"},
                    "EarliestRecording": now,
                    "LatestRecording": now + timedelta(hours=1),
                }
            )
        )
        replay = SimpleNamespace(GetReplayUri=AsyncMock())

        @asynccontextmanager
        async def session(connection):
            yield object()

        async def service(camera, name, connection):
            return {"media": media, "search": search, "replay": replay}[name]

        with (
            patch("frigate.ptz.device_services.camera_session", session),
            patch(
                "frigate.ptz.device_services.create_camera_service", side_effect=service
            ),
        ):
            response = self.client.post(
                "/onvif/cameras/front/replay/sessions",
                json={
                    "recording_token": "guessed-lens-b",
                    "start": now.isoformat(),
                    "end": (now + timedelta(minutes=1)).isoformat(),
                },
                headers=self.headers("front_only"),
            )
        self.assertEqual(response.status_code, 403)
        replay.GetReplayUri.assert_not_awaited()
        self.assertNotIn("physical-lens-b", response.text)

    def test_viewer_cannot_discover_all_sources_or_override_saved_recording_binding(
        self,
    ):
        with patch(
            "frigate.api.onvif.device_services.discover_recording_sources", AsyncMock()
        ) as discover:
            response = self.client.get(
                "/onvif/cameras/front/recording_sources",
                headers=self.headers("front_only"),
            )
        self.assertEqual(response.status_code, 403)
        discover.assert_not_awaited()
        response = self.client.post(
            "/onvif/cameras/front/replay/sessions",
            json={
                "recording_token": "guessed-lens-b",
                "start": "2026-10-18T00:00:00Z",
                "end": "2026-10-18T00:01:00Z",
                "source_ref": source_reference(
                    {"Source": {"SourceId": "urn:camera:physical-lens-b"}}, SOURCE_KEY
                ),
            },
            headers=self.headers("front_only"),
        )
        self.assertEqual(response.status_code, 422)

    def test_default_native_subscription_never_creates_an_nvr_publisher(self):
        monitor = SimpleNamespace(
            snapshot=lambda: {
                "state": "subscribed",
                "integration": "status_history_only",
            },
            run=AsyncMock(),
            task=None,
        )
        with (
            patch(
                "frigate.api.onvif.device_services.device_status",
                AsyncMock(return_value={"services": {"events": True}}),
            ),
            patch("frigate.api.onvif.NativeEventMonitor", return_value=monitor),
        ):
            response = self.client.post(
                "/onvif/cameras/front/events",
                json={"enabled": True, "confirm_device_change": True},
                headers=self.headers("admin"),
            )
        self.assertEqual(response.status_code, 200)
        self.owner.native_event_bridge.assert_not_called()

    def test_new_camera_prefix_does_not_exempt_unrelated_admin_routes(self):
        response = self.client.get(
            "/onvif/cameras/front/admin-only",
            headers=self.headers("front_only"),
        )
        self.assertEqual(response.status_code, 403)

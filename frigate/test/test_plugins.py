"""Plugin packages, process boundaries, private state and Ring event contracts."""

import asyncio
import base64
import hashlib
import io
import json
import socket
import stat
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from frigate.plugins.manager import BUNDLED, PluginManager
from frigate.plugins.models import (
    PluginDevice,
    PluginEntry,
    PluginManifest,
    PluginSnapshot,
)
from frigate.plugins.packages import (
    PluginError,
    PublicResolver,
    install_archive,
    validate_download_url,
)
from frigate.plugins.ring.plugin import RingPlugin
from frigate.plugins.worker import PluginWorker

MANIFEST = {
    "api_version": 1,
    "id": "example",
    "name": "Example",
    "version": "1.0.0",
    "description": "Test plugin",
    "capabilities": ["camera", "motion", "doorbell"],
    "actions": [],
}


def package(extra=None, manifest=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("plugin.json", json.dumps(manifest or MANIFEST))
        archive.writestr(
            "plugin.py", "raise RuntimeError('must not execute at installation')"
        )
        for name, content in (extra or {}).items():
            if isinstance(name, str):
                entry = zipfile.ZipInfo(name)
                entry.filename = name
                name = entry
            archive.writestr(name, content)
    payload = output.getvalue()
    return payload, hashlib.sha256(payload).hexdigest()


class TestPluginPackages(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_install_verifies_checksum_and_does_not_execute_code(self):
        payload, digest = package()
        manifest = install_archive(payload, digest, self.root)
        self.assertEqual(manifest.id, "example")
        self.assertTrue((self.root / "example" / digest / "plugin.py").is_file())

    def test_checksum_mismatch_writes_nothing(self):
        payload, _ = package()
        with self.assertRaisesRegex(PluginError, "checksum_mismatch"):
            install_archive(payload, "0" * 64, self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_unsafe_archive_paths(self):
        for name in (
            "../outside",
            "/absolute",
            "nested/../../outside",
            "C:\\outside",
            "nested\\outside",
        ):
            with self.subTest(name=name):
                payload, digest = package({name: "unsafe"})
                with self.assertRaisesRegex(PluginError, "unsafe_archive"):
                    install_archive(payload, digest, self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_symlink_rejected(self):
        item = zipfile.ZipInfo("link")
        item.create_system = 3
        item.external_attr = (stat.S_IFLNK | 0o777) << 16
        payload, digest = package({item: "../../outside"})
        with self.assertRaisesRegex(PluginError, "unsafe_archive"):
            install_archive(payload, digest, self.root)

    def test_bundled_identity_cannot_be_spoofed(self):
        payload, digest = package(manifest={**MANIFEST, "id": "ring"})
        with self.assertRaisesRegex(PluginError, "reserved_plugin_id"):
            install_archive(payload, digest, self.root)

    def test_existing_package_is_not_overwritten(self):
        payload, digest = package()
        install_archive(payload, digest, self.root)
        with self.assertRaisesRegex(PluginError, "package_already_exists"):
            install_archive(payload, digest, self.root)

    def test_public_https_without_credentials_only(self):
        validate_download_url("https://example.com/release.zip")
        for url in (
            "http://example.com/plugin.zip",
            "https://127.0.0.1/file",
            "https://169.254.169.254/file",
            "https://[::1]/file",
            "https://user:secret@example.com/file",
            "file:///config/config.yaml",
            "https://example.com:8443/file",
        ):
            with self.subTest(url=url), self.assertRaises(PluginError):
                validate_download_url(url)


class TestPluginResolver(unittest.IsolatedAsyncioTestCase):
    async def test_dns_resolution_rejects_private_or_mixed_answers(self):
        loop = asyncio.get_running_loop()
        records = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("8.8.8.8", 443),
            ),
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("127.0.0.1", 443),
            ),
        ]
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=records)):
            with self.assertRaisesRegex(PluginError, "download_address_blocked"):
                await PublicResolver().resolve("example.com", 443)


class TestPluginRuntime(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = SimpleNamespace(cameras={})
        self.publish = MagicMock()
        self.manager = PluginManager(self.root, lambda: self.config, self.publish)

    async def test_builtin_install_is_disabled_and_private(self):
        await self.manager.install(True, None, None, False)
        self.assertFalse(self.manager.settings.plugins["ring"].enabled)
        self.assertEqual(self.manager.workers, {})
        saved = json.loads((self.root / "settings.json").read_text())
        self.assertEqual(saved["plugins"]["ring"]["manifest"]["api_version"], 1)

    async def test_external_install_requires_developer_mode_and_acknowledgement(self):
        with self.assertRaisesRegex(PluginError, "acknowledgement"):
            await self.manager.install(
                False, "https://example.com/a.zip", "0" * 64, True
            )
        await self.manager.developer_mode(True, True)
        with self.assertRaisesRegex(PluginError, "acknowledgement"):
            await self.manager.install(
                False, "https://example.com/a.zip", "0" * 64, False
            )

    async def test_external_install_is_developer_tagged_and_not_started(self):
        payload, digest = package()
        await self.manager.developer_mode(True, True)
        with patch(
            "frigate.plugins.manager.download_package", AsyncMock(return_value=payload)
        ):
            await self.manager.install(False, "https://example.com/a.zip", digest, True)
        entry = self.manager.catalog()["installed"][0]
        self.assertTrue(entry["developer"])
        self.assertFalse(entry["enabled"])

    async def test_deduplicated_events_have_no_fabricated_confidence(self):
        self.manager.settings.plugins["example"] = PluginEntry(
            manifest=PluginManifest(**MANIFEST),
            digest="0" * 64,
            bindings={"device": "front"},
        )
        self.config.cameras["front"] = SimpleNamespace(enabled=True)
        snapshot = PluginSnapshot(
            state="running",
            events=[
                {
                    "id": "ding1",
                    "device_id": "device",
                    "kind": "doorbell",
                    "timestamp": time.time(),
                }
            ],
        )
        with patch.object(self.manager, "_register", AsyncMock()):
            await self.manager._apply("example", snapshot)
            await self.manager._apply("example", snapshot)
        self.publish.assert_called_once()
        event = self.publish.call_args.args[0]
        self.assertEqual(event[2], "doorbell")
        self.assertIsNone(event[5])
        self.assertEqual(event[8], "plugin")

    async def test_only_new_motion_refreshes_inference_window(self):
        from frigate.plugins import inference
        from frigate.plugins.inference import EventInferenceConfig

        self.manager.settings.plugins["example"] = PluginEntry(
            manifest=PluginManifest(**MANIFEST),
            digest="0" * 64,
            bindings={"device": "front"},
        )
        self.config.cameras["front"] = SimpleNamespace(enabled=True)
        clock = [100.0]
        self.manager.inference.clock = lambda: clock[0]
        with (
            patch.object(inference, "LEASE_PATH", self.root / "leases.json"),
            patch.object(self.manager, "_register", AsyncMock()),
        ):
            self.manager.inference.initialize()
            self.manager.inference.configure(
                "front", EventInferenceConfig(enabled=True)
            )
            snapshot = PluginSnapshot(
                state="running",
                events=[
                    {
                        "id": "motion1",
                        "device_id": "device",
                        "kind": "motion",
                        "timestamp": time.time(),
                    }
                ],
            )
            await self.manager._apply("example", snapshot)
            self.assertTrue(inference.lease_status("front", 129.999)["active"])
            clock[0] = 120
            await self.manager._apply("example", snapshot)
            self.assertFalse(inference.lease_status("front", 130)["active"])
            await self.manager._apply(
                "example",
                PluginSnapshot(
                    state="running",
                    events=[
                        {
                            "id": "button1",
                            "device_id": "device",
                            "kind": "doorbell",
                            "timestamp": time.time(),
                        }
                    ],
                ),
            )
            self.assertFalse(inference.lease_status("front", 130)["active"])
            await self.manager._apply(
                "example",
                PluginSnapshot(
                    state="running",
                    events=[
                        {
                            "id": "motion2",
                            "device_id": "device",
                            "kind": "motion",
                            "timestamp": time.time(),
                        }
                    ],
                ),
            )
            self.assertTrue(inference.lease_status("front", 149.999)["active"])
            self.assertFalse(inference.lease_status("front", 150)["active"])

    async def test_unbound_stale_and_disabled_camera_events_are_ignored(self):
        self.manager.settings.plugins["example"] = PluginEntry(
            manifest=PluginManifest(**MANIFEST),
            digest="0" * 64,
            bindings={"device": "front"},
        )
        self.config.cameras["front"] = SimpleNamespace(enabled=False)
        await self.manager._apply(
            "example",
            PluginSnapshot(
                state="running",
                events=[
                    {
                        "id": "1",
                        "device_id": "unknown",
                        "kind": "motion",
                        "timestamp": time.time(),
                    },
                    {
                        "id": "2",
                        "device_id": "device",
                        "kind": "motion",
                        "timestamp": time.time() - 300,
                    },
                    {
                        "id": "3",
                        "device_id": "device",
                        "kind": "motion",
                        "timestamp": time.time(),
                    },
                ],
            ),
        )
        self.publish.assert_not_called()

    async def test_sources_never_appear_in_catalog(self):
        await self.manager.install(True, None, None, False)
        self.manager.devices["ring"] = {
            "1": PluginDevice(
                id="1",
                name="Door",
                source="ring:?refresh_token=private-token",
            )
        }
        self.assertNotIn("private-token", json.dumps(self.manager.catalog()))

    async def test_uninstall_refuses_configured_cameras(self):
        await self.manager.install(True, None, None, False)
        self.manager.settings.plugins["ring"].bindings["device"] = "front"
        self.config.cameras["front"] = SimpleNamespace(enabled=True)
        with self.assertRaisesRegex(PluginError, "remove_plugin_cameras_first"):
            await self.manager.uninstall("ring")

    async def test_failed_media_registration_rolls_back_camera_claim(self):
        await self.manager.install(True, None, None, False)
        self.manager.devices["ring"] = {
            "1": PluginDevice(id="1", name="Door", source="ring:?private")
        }
        with patch.object(
            self.manager, "_register", AsyncMock(side_effect=PluginError("unavailable"))
        ):
            with self.assertRaisesRegex(PluginError, "unavailable"):
                await self.manager.bind("ring", "1", "new_camera")
        self.assertEqual(self.manager.settings.plugins["ring"].bindings, {})
        self.assertEqual(
            json.loads((self.root / "recording-only.json").read_text()), {}
        )

    async def test_disconnect_removes_private_media_sources(self):
        await self.manager.install(True, None, None, False)
        self.manager.settings.plugins["ring"].bindings["1"] = "front"
        with patch.object(self.manager, "_remove_sources", AsyncMock()) as remove:
            await self.manager._apply("ring", PluginSnapshot(state="auth_required"))
        remove.assert_awaited_once_with("ring")

    async def test_worker_uses_real_json_lines_and_is_reaped(self):
        script = self.root / "worker.py"
        script.write_text(
            "import sys,json\n"
            "for line in sys.stdin:\n"
            " r=json.loads(line)\n"
            " print(json.dumps({'id':r['id'],'result':{'state':'running'}}),flush=True)\n"
        )
        worker = PluginWorker(script, self.root / "data")
        await worker.start()
        process = worker.process
        try:
            self.assertEqual(await worker.call("poll"), {"state": "running"})
        finally:
            await worker.close()
        self.assertIsNotNone(process.returncode)

    async def test_bundled_worker_handles_initial_auth_required_without_network(self):
        worker = PluginWorker(BUNDLED / "plugin.py", self.root / "data")
        await worker.start()
        try:
            result = await worker.call("poll")
            self.assertEqual(result["state"], "auth_required")
        finally:
            await worker.close()


class TestRingProvider(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.plugin = RingPlugin(Path(self.temporary.name))
        self.plugin.auth = SimpleNamespace(get_hardware_id=lambda: "test-hardware-id")
        self.plugin.token = {"refresh_token": "private-token"}
        self.plugin.ring = SimpleNamespace(
            async_update_devices=AsyncMock(),
            async_update_dings=AsyncMock(),
            devices_data={
                "doorbots": {
                    1: {"id": 1, "device_id": "hardware", "description": "Door"}
                }
            },
            dings_data=[{"id": "42", "kind": "ding", "doorbot_id": 1}],
        )

    async def test_ring_source_contains_all_go2rtc_required_fields(self):
        result = await self.plugin.poll()
        source = result["devices"][0]["source"]
        self.assertIn("camera_id=1", source)
        self.assertIn("device_id=hardware", source)
        query = parse_qs(urlsplit(source).query)
        self.assertEqual(
            json.loads(base64.b64decode(query["refresh_token"][0])),
            {"rt": "private-token", "hid": "test-hardware-id"},
        )
        self.assertTrue(result["devices"][0]["two_way_audio"])

    async def test_ring_motion_and_doorbell_are_not_person_detections(self):
        first = await self.plugin.poll()
        self.assertEqual(first["events"][0]["kind"], "doorbell")
        second = await self.plugin.poll()
        self.assertEqual(second["events"], [])
        self.plugin.ring.dings_data = [{"id": "43", "kind": "motion", "doorbot_id": 1}]
        self.assertEqual((await self.plugin.poll())["events"][0]["kind"], "motion")

    async def test_token_persistence_does_not_save_account_password(self):
        self.plugin.save({"refresh_token": "secret"})
        saved = json.loads((self.plugin.data / "token.json").read_text())
        self.assertEqual(saved, {"refresh_token": "secret"})


@unittest.skipIf(
    sys.platform == "win32", "Full recording and Review pipeline requires Linux"
)
class TestPluginCameraPipeline(unittest.TestCase):
    def test_recording_only_claim_does_not_change_other_cameras(self):
        from frigate.motion import record_only

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording-only.json"
            path.write_text(json.dumps({"ring_front": True}))
            with (
                patch.object(record_only, "PLUGIN_MODES_PATH", path),
                patch.object(record_only, "configuration", return_value=None),
            ):
                self.assertTrue(record_only.recording_only("ring_front"))
                self.assertFalse(record_only.recording_only("existing_camera"))

    def test_ring_query_credentials_are_redacted(self):
        from frigate.util.builtin import clean_camera_user_pass

        cleaned = clean_camera_user_pass(
            "ring:?camera_id=1&refresh_token=private-token&device_id=hardware"
        )
        self.assertNotIn("private-token", cleaned)
        self.assertIn("camera_id=1", cleaned)

    def test_plugin_review_starts_and_ends_without_video_frames(self):
        import threading

        from frigate.comms.detections_updater import DetectionTypeEnum
        from frigate.test.test_review_flow import CAMERA, ReviewFlowTestCase
        from frigate.track.object_processing import ManualEventState

        for label, expected in (("doorbell", "alert"), ("motion", "detection")):
            with self.subTest(label=label):
                fixture = ReviewFlowTestCase()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                maintainer = fixture._make_maintainer(
                    """
    review:
      alerts:
        labels: [doorbell]
      detections:
        labels: [motion]
"""
                )
                maintainer.stop_event = threading.Event()
                Path(fixture.clips_dir.name, "review").mkdir(exist_ok=True)
                queue = [
                    (
                        DetectionTypeEnum.api.value,
                        (
                            CAMERA,
                            timestamp,
                            {
                                "state": state,
                                "event_id": "plugin-event",
                                "label": label,
                                "end_time": None
                                if state == ManualEventState.start
                                else timestamp,
                                "source": "plugin",
                            },
                        ),
                    )
                    for state, timestamp in (
                        (ManualEventState.start, 1000),
                        (ManualEventState.end, 1015),
                    )
                ]

                def update(timeout):
                    if not queue:
                        maintainer.stop_event.set()
                        return None
                    return queue.pop(0)

                maintainer.detection_subscriber.check_for_update.side_effect = update
                with patch(
                    "frigate.review.maintainer.native_api_selected", return_value=False
                ):
                    maintainer.run()
                maintainer.frame_manager.get.assert_not_called()
                reviews = [
                    json.loads(call.args[1])
                    for call in maintainer.requestor.send_data.call_args_list
                    if call.args[0] == "reviews"
                ]
                self.assertEqual(reviews[0]["type"], "new")
                self.assertEqual(reviews[0]["after"]["severity"], expected)
                self.assertEqual(reviews[-1]["type"], "end")
                self.assertFalse(maintainer.indefinite_events.get(CAMERA))

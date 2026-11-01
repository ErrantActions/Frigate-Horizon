"""Private bootstrap, bounded profiles, and recorder-independent startup tests."""

import hashlib
import json
import stat
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from frigate.config import FrigateConfig
from frigate.config.live_webrtc import LiveTurnConfig, LiveWebRTCConfig
from frigate.util.ovenmediaengine_bootstrap import (
    generate_runtime,
    server_xml,
    turn_config,
)


class TestOvenMediaEngineBootstrap(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.runtime = Path(self.directory.name)
        self.settings = LiveWebRTCConfig(enabled=True, advertised_hosts=["10.1.1.4"])

    def test_disabled_startup_removes_stale_markers_and_creates_no_inputs(self):
        for marker in ("enabled", "turn-enabled"):
            (self.runtime / marker).touch()
        generate_runtime(LiveWebRTCConfig(), self.runtime)
        self.assertEqual(list(self.runtime.iterdir()), [])

    def test_control_sockets_are_private_and_media_bind_is_explicit(self):
        tree = ET.fromstring(server_xml(self.settings, "<synthetic-token>"))
        self.assertEqual(tree.findtext("IP"), "127.0.0.1")
        self.assertEqual(tree.findtext("Managers/API/AccessToken"), "<synthetic-token>")
        self.assertEqual(tree.findtext("Bind/Managers/API/Port"), "8081")
        self.assertEqual(
            tree.findtext("Bind/Publishers/WebRTC/Signalling/Port"), "3333"
        )
        ice = tree.find("Bind/Publishers/WebRTC/IceCandidates")
        self.assertEqual(ice.findtext("BindIP"), "0.0.0.0")
        self.assertEqual(ice.findtext("IceCandidate"), "10.1.1.4:8189/udp")
        for forbidden in ("TcpRelay", "Origins", "OriginMapStore", "Url", "Bypass"):
            self.assertEqual(tree.findall(f".//{forbidden}"), [])
        self.assertEqual(tree.findtext("Modules/P2P/Enable"), "false")

    def test_cpu_profiles_are_bounded_and_grid_audio_is_explicitly_separate(self):
        tree = ET.fromstring(server_xml(self.settings, "synthetic-token"))
        apps = tree.findall("VirtualHosts/VirtualHost/Applications/Application")
        self.assertEqual(len(apps), 6)
        for app in apps:
            grid = "grid" in app.findtext("Name")
            for video in app.findall(".//Encodes/Video"):
                self.assertEqual(video.findtext("Codec"), "h264")
                self.assertLessEqual(
                    int(video.findtext("Height")), 360 if grid else 1080
                )
                self.assertEqual(video.findtext("ThreadCount"), "2")
                self.assertEqual(video.findtext("BFrames"), "0")
                self.assertEqual(video.findtext("Profile"), "baseline")
                self.assertEqual(
                    video.findtext("KeyFrameInterval"), video.findtext("Framerate")
                )
            self.assertEqual(
                bool(app.findall(".//Encodes/Audio")),
                app.findtext("Name").endswith("_1"),
            )
            self.assertEqual(app.findtext(".//WebRtcAutoAbr"), "true")
        self.assertEqual(tree.findall(".//Providers/RTMP"), [])

    def test_private_files_and_matching_settings_are_published_before_enable(self):
        generate_runtime(self.settings, self.runtime)
        metadata = json.loads((self.runtime / "runtime.json").read_text())
        self.assertEqual(
            metadata["settings_sha256"],
            hashlib.sha256(self.settings.model_dump_json().encode()).hexdigest(),
        )
        self.assertGreaterEqual(len(metadata["api_token"]), 48)
        self.assertGreaterEqual(len(metadata["turn_secret"]), 48)
        self.assertTrue((self.runtime / "enabled").is_file())
        self.assertFalse((self.runtime / "turn-enabled").exists())
        self.assertEqual(stat.S_IMODE(self.runtime.stat().st_mode), 0o700)
        for path in self.runtime.rglob("*"):
            with self.subTest(path=path.relative_to(self.runtime)):
                self.assertEqual(
                    stat.S_IMODE(path.stat().st_mode),
                    0o700 if path.is_dir() else 0o600,
                )

    def test_existing_schedule_and_media_directories_are_made_private(self):
        directories = []
        for kind in ("recorded-media", "schedules"):
            root = self.runtime / kind
            root.mkdir(mode=0o755)
            directory = root / "frigate_detail_1080_0"
            directory.mkdir(mode=0o755)
            for path in (root, directory):
                path.chmod(0o755)
                directories.append(path)
        generate_runtime(self.settings, self.runtime)
        for path in directories:
            with self.subTest(path=path.relative_to(self.runtime)):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)

    def test_failed_write_never_publishes_success_markers(self):
        with (
            patch(
                "frigate.util.ovenmediaengine_bootstrap.write_private",
                side_effect=OSError("synthetic"),
            ),
            self.assertRaises(OSError),
        ):
            generate_runtime(self.settings, self.runtime)
        self.assertFalse((self.runtime / "enabled").exists())
        self.assertFalse((self.runtime / "turn-enabled").exists())

    def test_relay_uses_authenticated_bounded_media_only_allocations(self):
        self.settings.turn = LiveTurnConfig(
            enabled=True,
            hostname="turn.example.test",
            relay_ip="10.1.1.4",
            external_ip="203.0.113.4",
            tls=False,
        )
        config = turn_config(self.settings, "synthetic-secret")
        for required in (
            "use-auth-secret",
            "static-auth-secret=synthetic-secret",
            "no-tls",
            "no-dtls",
            "no-multicast-peers",
            "user-quota=4",
            "total-quota=128",
            "min-port=49160",
            "max-port=49259",
            "denied-peer-ip=0.0.0.0-255.255.255.255",
            "allowed-peer-ip=10.1.1.4",
            "external-ip=203.0.113.4/10.1.1.4",
        ):
            self.assertIn(required + "\n", config)
        self.assertNotIn("no-auth", config)

    def test_native_auth_cannot_be_disabled_for_web_rtc(self):
        with self.assertRaisesRegex(ValidationError, "requires native authentication"):
            FrigateConfig(
                mqtt={"enabled": False},
                cameras={},
                auth={"enabled": False},
                live_webrtc={"enabled": True, "advertised_hosts": ["10.1.1.4"]},
            )

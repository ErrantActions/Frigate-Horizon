"""Focused tests for qualified HLS payload validation and private lazy startup."""

import copy
import importlib.util
import struct
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("live_hls_verify", HERE / "verify.py")
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


class LiveHLSPackagingTests(unittest.TestCase):
    def config(self) -> dict:
        """Return the existing private manager's relevant configuration fields."""
        return {
            "api": False,
            "metrics": False,
            "pprof": False,
            "rtsp": False,
            "rtmp": False,
            "webrtc": False,
            "srt": False,
            "moq": False,
            "hls": True,
            "hlsAddress": "127.0.0.1:8888",
            "hlsAlwaysRemux": False,
            "paths": {
                "camera_360": {
                    "source": "rtsp://127.0.0.1:8554/trusted_main",
                    "sourceOnDemand": True,
                }
            },
        }

    def test_existing_config_is_accepted_without_changes(self):
        config = self.config()
        before = copy.deepcopy(config)
        verify.validate_config(config)
        self.assertEqual(config, before)

    def test_public_listener_or_always_remux_is_rejected(self):
        for key, value in (
            ("hlsAddress", ":8888"),
            ("hlsAlwaysRemux", True),
            ("api", True),
            ("webrtc", True),
            ("rtsp", True),
        ):
            with self.subTest(key=key):
                config = self.config()
                config[key] = value
                with self.assertRaises(ValueError):
                    verify.validate_config(config)

    def test_nonlazy_or_external_sources_are_rejected(self):
        for settings in (
            {"sourceOnDemand": False},
            {"source": "rtsp://192.0.2.1:554/camera"},
            {"source": "rtsp://secret:private@127.0.0.1:8554/stream"},
        ):
            with self.subTest(settings=list(settings)):
                config = self.config()
                config["paths"]["camera_360"].update(settings)
                with self.assertRaises(ValueError):
                    verify.validate_config(config)

    def test_static_amd64_elf_is_accepted(self):
        binary = bytearray(64)
        binary[:6] = b"\x7fELF\x02\x01"
        struct.pack_into("<H", binary, 18, 62)
        verify.validate_elf(bytes(binary))

    def test_wrong_architecture_and_dynamic_loader_are_rejected(self):
        binary = bytearray(120)
        binary[:6] = b"\x7fELF\x02\x01"
        struct.pack_into("<H", binary, 18, 183)
        with self.assertRaises(ValueError):
            verify.validate_elf(bytes(binary))
        struct.pack_into("<H", binary, 18, 62)
        struct.pack_into("<Q", binary, 32, 64)
        struct.pack_into("<HH", binary, 54, 56, 1)
        struct.pack_into("<I", binary, 64, 3)
        with self.assertRaises(ValueError):
            verify.validate_elf(bytes(binary))

    def test_service_wait_is_bounded_and_runs_nonroot(self):
        service = (
            HERE.parents[2] / "docker/main/rootfs/etc/s6-overlay/s6-rc.d/live-hls/run"
        ).read_text()
        self.assertIn("attempt < 120", service)
        self.assertIn("HORIZON_HLS_SHARED_CLOCK=1", service)
        self.assertIn("s6-svc -O .", service)
        self.assertIn("s6-applyuidgid -u 1000 -g 1000 -G 1000", service)
        self.assertIn("/config/.live-hls/mediamtx.yml", service)


if __name__ == "__main__":
    unittest.main()

"""Focused, dependency-free packaging tests; optional pinned-upstream validation."""

import hashlib
import importlib.util
import io
import os
import re
import shutil
import socket
import struct
import tarfile
import threading
import unittest
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]


def load_module(name: str, path: Path):
    """Load a standalone helper without requiring Frigate's Python dependencies."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load_module("ome_build", HERE / "build.py")
runtime = load_module(
    "ome_qualification", REPO / "tools/test_ovenmediaengine_runtime.py"
)


class PackagingTests(unittest.TestCase):
    def test_source_lock_is_complete_and_immutable(self):
        lock = builder.LOCK
        self.assertEqual(lock["release"], "v0.21.0")
        self.assertEqual(lock["commit"], "a35b09d11a82035f63f1d22e3fec9bbadb8e0aec")
        urls = []
        for entry in lock["sources"]:
            self.assertRegex(entry["sha256"], r"^[a-f0-9]{64}$")
            self.assertTrue(entry["url"].startswith("https://"))
            urls.append(entry["url"])
        self.assertEqual(len(urls), len(set(urls)))
        self.assertIn(lock["commit"], urls[0])

    def test_unlocked_download_and_mismatched_patch_fail_closed(self):
        with self.assertRaises(ValueError):
            builder.source_entry("https://example.invalid/unreviewed.tar.gz")
        for text in ("missing", "same same"):
            with self.assertRaises(ValueError):
                builder.replace_once(text, "same", "replacement")

    def test_downloads_and_cache_are_verified_before_use(self):
        work = HERE / f".download-validation-{os.getpid()}"
        self.assertFalse(work.exists())
        work.mkdir()
        payload = b"reviewed-source-fixture"
        entry = {
            "name": "fixture",
            "url": "https://example.invalid/reviewed",
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        try:
            with (
                patch.object(builder, "CACHE", work / "cache"),
                patch.object(builder, "LOCK", {"sources": [entry]}),
                patch.object(
                    builder.urllib.request, "urlopen", return_value=io.BytesIO(payload)
                ) as download,
            ):
                destination = work / "verified"
                builder.fetch(entry["url"], destination)
                self.assertEqual(destination.read_bytes(), payload)
                builder.fetch(entry["url"], destination)
                self.assertEqual(download.call_count, 1)
                (builder.CACHE / entry["sha256"]).write_bytes(b"corrupt-cache")
                download.return_value = io.BytesIO(b"unexpected-upstream-bytes")
                with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                    builder.fetch(entry["url"], destination)
                self.assertEqual(destination.read_bytes(), payload)
                self.assertFalse(
                    (builder.CACHE / entry["sha256"]).with_suffix(".partial").exists()
                )
        finally:
            shutil.rmtree(work)

    def test_qualification_has_no_startup_sources_or_ome_turn(self):
        config = ET.fromstring(runtime.server_xml("test:private"))
        self.assertEqual(config.findtext("IP"), "127.0.0.1")
        self.assertEqual(config.findtext(".//IceCandidates/BindIP"), "0.0.0.0")
        self.assertEqual(config.findtext(".//IceCandidate"), "127.0.0.1:8189/udp")
        for forbidden in ("Origins", "OriginMapStore", "PersistentStreams", "TcpRelay"):
            self.assertIsNone(config.find(f".//{forbidden}"))
        video = config.find(".//Encodes/Video")
        self.assertEqual(video.findtext("Width"), "160")
        self.assertEqual(video.findtext("Framerate"), "5")

    def test_s6_optional_services_are_nonroot_and_do_not_restart(self):
        services = REPO / "docker/main/rootfs/etc/s6-overlay/s6-rc.d"
        for name, marker in (
            ("ovenmediaengine", "enabled"),
            ("coturn", "turn-enabled"),
        ):
            script = (services / name / "run").read_text()
            self.assertIn("s6-svc -O .", script)
            self.assertIn(f"$state/{marker}", script)
            self.assertIn("s6-applyuidgid -u 1000 -g 1000 -G 1000", script)
            self.assertNotIn("sleep", script)
            self.assertTrue(
                (services / name / "dependencies.d/ovenmediaengine-config").exists()
            )
            self.assertTrue((services / "user/contents.d" / name).exists())
        turn = (services / "coturn/run").read_text()
        self.assertIn("no-auth", turn)
        self.assertIn("use-auth-secret", turn)
        self.assertIn("static-auth-secret=", turn)
        self.assertIn("lt-cred-mech", turn)
        self.assertIn("--no-cli", turn)

    def test_fixture_annexb_access_units(self):
        data = (
            b"\0\0\0\1\x09\x10\0\0\1\x67sps\0\0\1\x68pps"
            b"\0\0\1\x65idr\0\0\0\1\x09\x10\0\0\1\x41slice"
        )
        frames, sps, pps = runtime.annexb_frames(data)
        self.assertEqual(len(frames), 2)
        self.assertEqual(sps, b"\x67sps")
        self.assertEqual(pps, b"\x68pps")

    def test_rtp_fragmentation_preserves_nal_and_end_marker(self):
        handler = runtime.FixtureHandler.__new__(runtime.FixtureHandler)
        handler.sequence = 65535
        handler.timestamp = 9000
        handler.send_lock = threading.Lock()
        sender, receiver = socket.socketpair()
        receiver.settimeout(2)
        handler.request = sender
        nal = b"\x65" + b"x" * 2500
        try:
            handler.send_nal(nal, True)
            data = receiver.recv(8192)
            payloads = []
            markers = []
            while data:
                self.assertEqual(data[:2], b"$\0")
                length = struct.unpack("!H", data[2:4])[0]
                packet = data[4 : 4 + length]
                payloads.append(packet[12:])
                markers.append(bool(packet[1] & 0x80))
                data = data[4 + length :]
            self.assertEqual(len(payloads), 3)
            self.assertEqual(markers, [False, False, True])
            self.assertEqual(payloads[0][1] & 0x80, 0x80)
            self.assertEqual(payloads[-1][1] & 0x40, 0x40)
            self.assertEqual(
                bytes([(payloads[0][0] & 0xE0) | (payloads[0][1] & 31)])
                + b"".join(payload[2:] for payload in payloads),
                nal,
            )
        finally:
            sender.close()
            receiver.close()

    def test_stun_message_integrity_and_challenge_decoding(self):
        sender, receiver = socket.socketpair()
        sender.settimeout(2)
        receiver.settimeout(2)
        errors = []
        key = b"fixture-key"

        def responder():
            try:
                message = receiver.recv(4096)
                self.assertEqual(message[-24:-20], b"\0\x08\0\x14")
                self.assertEqual(
                    message[-20:], runtime.hmac.digest(key, message[:-24], "sha1")
                )
                body = runtime.stun_attribute(9, b"\0\0\x04\x01Unauthorized")
                receiver.sendall(
                    struct.pack("!HHI", 0x0113, len(body), 0x2112A442)
                    + message[8:20]
                    + body
                )
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=responder)
        thread.start()
        try:
            kind, attributes = runtime.stun_exchange(
                sender, runtime.stun_attribute(6, b"user"), key
            )
            self.assertEqual(kind, 0x0113)
            self.assertEqual(attributes[9][2:4], b"\x04\x01")
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
        finally:
            sender.close()
            receiver.close()


@unittest.skipUnless(
    os.environ.get("OME_VERIFY_UPSTREAM") == "1", "opt-in network check"
)
class PinnedUpstreamTests(unittest.TestCase):
    def test_verified_archive_accepts_all_patches_and_dependency_lock(self):
        entry = builder.LOCK["sources"][0]
        with urllib.request.urlopen(entry["url"], timeout=120) as response:
            content = response.read()
        self.assertEqual(hashlib.sha256(content).hexdigest(), entry["sha256"])
        work = HERE / ".upstream-validation"
        self.assertFalse(work.exists(), "Do not overwrite another validation's state")
        work.mkdir()
        try:
            with tarfile.open(fileobj=io.BytesIO(content)) as archive:
                names = archive.getnames()
                root = names[0].split("/")[0]
                paths = [
                    "cmake/InstallPrerequisites.cmake",
                    "cmake/Versions.cmake",
                    "src/config/items/bind/common/webrtc/ice_candidates.h",
                    "src/modules/ice/ice_port.h",
                    "src/modules/ice/ice_port.cpp",
                    "src/modules/ice/ice_port_manager.cpp",
                ]
                for path in paths:
                    destination = work / path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(
                        archive.extractfile(f"{root}/{path}").read()
                    )
            installer = (work / paths[0]).read_text()
            patched = builder.patch_prerequisites(installer, 2)
            self.assertNotIn("/tmp/", patched)
            self.assertNotIn("-j$(nproc)", patched)
            self.assertIn("build.py fetch", patched)
            self.assertIn("build.py licenses", patched)
            builder.patch_ice_bind(work)
            builder.patch_versions(work)
            self.assertIn(
                "set(OME_VER_OPENSSL         3.0.22)",
                (work / "cmake/Versions.cmake").read_text(),
            )
            self.assertIn(
                "bind_ip_list.empty()",
                (work / "src/modules/ice/ice_port.cpp").read_text(),
            )
            # Resolve upstream's CMake URL expressions and reject any unpinned
            # prerequisite, excluding the explicitly disabled GPU/x264 paths.
            variables = dict(
                re.findall(
                    r"set\((OME_VER_\w+)\s+([^\s)]+)\)", (work / paths[1]).read_text()
                )
            )
            for variable, value in tuple(variables.items()):
                prefix = variable.removeprefix("OME_VER_")
                verify, _, reference = value.partition("@")
                variables[prefix + "_SOURCE_REF"] = reference or verify
            for variable, _, override, fallback in re.findall(
                r'ome_select_archive_ref\((\w+)\s+"([^"]+)"\s+'
                r'"([^"]+)"\s+"([^"]+)"\)',
                installer,
            ):
                prefix = variable.removesuffix("_ARCHIVE_REF")
                expression = (
                    override
                    if "@" in variables.get("OME_VER_" + prefix, "")
                    else fallback
                )
                variables[variable] = re.sub(
                    r"\$\{(\w+)\}", lambda match: variables[match[1]], expression
                )
            for variable, expression in re.findall(
                r'set\((\w+_SOURCE_URL)\s+"([^"]+)"\)', installer
            ):
                if variable in ("X264_SOURCE_URL", "NVCC_HDR_SOURCE_URL"):
                    continue
                url = re.sub(
                    r"\$\{(\w+)\}", lambda match: variables[match[1]], expression
                )
                self.assertEqual(builder.source_entry(url)["url"], url)
        finally:
            shutil.rmtree(work)


if __name__ == "__main__":
    unittest.main()

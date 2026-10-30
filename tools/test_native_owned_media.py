"""Exercise the owned media binary on isolated Linux using synthetic audio only."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:1984"
OWNED = "/api/horizon/webrtc"
PROTOCOL = "horizon-owned-webrtc-v1"
COMMIT = "b5948cfb25404cc5cb37b166ecaa2dca20b11d4b"
OFFER = "\r\n".join(
    (
        "v=0",
        "o=- 123456 2 IN IP4 127.0.0.1",
        "s=synthetic-owned-media-validation",
        "t=0 0",
        "a=group:BUNDLE 0",
        "a=msid-semantic: WMS",
        "m=audio 9 UDP/TLS/RTP/SAVPF 0",
        "c=IN IP4 0.0.0.0",
        "a=mid:0",
        "a=ice-ufrag:horizonfixture",
        "a=ice-pwd:horizon-fixture-password-12345678",
        "a=fingerprint:sha-256 " + ":".join(["AB"] * 32),
        "a=setup:actpass",
        "a=recvonly",
        "a=rtcp-mux",
        "a=rtpmap:0 PCMU/8000",
        "a=end-of-candidates",
        "",
    )
)


def request(method: str, path: str, payload: dict | None = None) -> dict:
    """Use only the isolated process's fixed loopback HTTP service."""
    data = None if payload is None else json.dumps(payload).encode()
    value = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
        value, timeout=10
    ) as response:
        result = json.load(response)
    if not isinstance(result, dict):
        raise AssertionError("Owned media returned invalid JSON")
    return result


def consumers() -> set[int]:
    """Observe actual consumer identities rather than inferring teardown from HTTP 200."""
    stream = request("GET", "/api/streams?src=synthetic")
    if not isinstance(stream, dict):
        raise AssertionError("Synthetic stream metadata is unavailable")
    return {value["id"] for value in stream.get("consumers") or []}


def wait_consumers(expected: set[int], timeout: float = 3) -> None:
    """Require the exact surviving consumers, including the legacy shared consumer."""
    deadline = time.monotonic() + timeout
    while True:
        actual = consumers()
        if actual == expected:
            return
        if time.monotonic() >= deadline:
            raise AssertionError(f"Consumer ownership changed: {actual} != {expected}")
        time.sleep(0.1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--ffmpeg", required=True, type=Path)
    args = parser.parse_args()
    if platform.system() != "Linux" or not Path("/.dockerenv").is_file():
        raise SystemExit(
            "This validation requires an isolated, network-disabled Linux container"
        )
    if not __debug__:
        raise SystemExit("Validation requires enabled Python assertions")
    binary = args.binary.resolve(strict=True)
    ffmpeg = args.ffmpeg.resolve(strict=True)
    receipt = json.loads(args.receipt.read_text())
    if (
        receipt.get("source_commit") != COMMIT
        or receipt.get("target") != "linux/amd64"
        or receipt.get("protocol") != PROTOCOL
        or receipt.get("binary_sha256")
        != hashlib.sha256(binary.read_bytes()).hexdigest()
    ):
        raise SystemExit("Owned media binary does not match the pinned build receipt")
    try:
        request("GET", OWNED)
    except urllib.error.HTTPError:
        raise SystemExit(
            "Refusing to test against an existing loopback media service"
        ) from None
    except urllib.error.URLError:
        pass
    else:
        raise SystemExit("Refusing to test against an existing loopback media service")
    with tempfile.TemporaryDirectory(prefix="horizon-owned-media-") as directory:
        root = Path(directory)
        config = root / "go2rtc.yaml"
        command = (
            f"exec:{ffmpeg} -hide_banner -loglevel error -re -f lavfi "
            "-i anullsrc=channel_layout=mono:sample_rate=8000 "
            "-c:a pcm_mulaw -f wav pipe:1"
        )
        config.write_text(
            "api:\n  listen: 127.0.0.1:1984\n"
            "rtsp:\n  listen: ''\n"
            "webrtc:\n  listen: 127.0.0.1:18555\n"
            "  candidates: [127.0.0.1:18555]\n  ice_servers: []\n"
            f"streams:\n  synthetic: {json.dumps(command)}\n"
        )
        executable = root / "go2rtc"
        executable.write_bytes(binary.read_bytes())
        executable.chmod(0o700)
        with (root / "go2rtc.log").open("w+") as log:
            process = subprocess.Popen(
                [str(executable), "-config", str(config)],
                cwd=root,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env={**os.environ, "NVIDIA_VISIBLE_DEVICES": "void"},
            )
            peers: list[str] = []
            try:
                deadline = time.monotonic() + 10
                while True:
                    if process.poll() is not None:
                        raise AssertionError(
                            "Isolated owned media process exited during startup"
                        )
                    try:
                        capability = request("GET", OWNED)
                        break
                    except urllib.error.URLError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.1)
                assert capability == {
                    "protocol": PROTOCOL,
                    "owned_consumers": True,
                    "lease_ttl_seconds": 15,
                    "max_duration_seconds": 120,
                }, capability
                assert consumers() == set()
                offer = {"type": "offer", "sdp": OFFER}
                legacy = request("POST", "/api/webrtc?src=synthetic", offer)
                assert legacy.get("type") == "answer"
                legacy_consumers = consumers()
                assert len(legacy_consumers) == 1
                first = request("POST", OWNED + "?src=synthetic", offer)
                peers.append(first["peer_id"])
                assert first.get("type") == "answer" and "m=audio" in first["sdp"]
                first_consumers = consumers() - legacy_consumers
                assert len(first_consumers) == 1
                second = request("POST", OWNED + "?src=synthetic", offer)
                peers.append(second["peer_id"])
                second_consumers = consumers() - legacy_consumers - first_consumers
                assert len(second_consumers) == 1
                renewed = request("PATCH", OWNED + "?id=" + second["peer_id"])
                assert renewed["peer_id"] == second["peer_id"]
                assert renewed["renew_after_ms"] == 5000
                assert request("DELETE", OWNED + "?id=" + first["peer_id"]) == {
                    "success": True
                }
                wait_consumers(legacy_consumers | second_consumers)
                assert request("DELETE", OWNED + "?id=" + first["peer_id"]) == {
                    "success": True
                }
                assert request("DELETE", OWNED + "?id=" + second["peer_id"]) == {
                    "success": True
                }
                wait_consumers(legacy_consumers)
                expiring = request("POST", OWNED + "?src=synthetic", offer)
                peers.append(expiring["peer_id"])
                assert len(consumers() - legacy_consumers) == 1
                wait_consumers(legacy_consumers, timeout=18)
                try:
                    request("PATCH", OWNED + "?id=" + expiring["peer_id"])
                except urllib.error.HTTPError as error:
                    assert error.code == 404
                else:
                    raise AssertionError("An expired owned consumer was revived")
                print(
                    json.dumps(
                        {
                            "protocol": PROTOCOL,
                            "binary_sha256": receipt["binary_sha256"],
                            "synthetic_sdp": True,
                            "owned_delete_isolated": True,
                            "legacy_consumer_preserved": True,
                            "expiry_cannot_revive": True,
                            "real_camera_microphone_or_push": False,
                        }
                    )
                )
            except (AssertionError, KeyError, OSError, ValueError):
                log.flush()
                log.seek(0)
                print(log.read()[-8192:])
                raise
            finally:
                try:
                    for peer in peers:
                        try:
                            request("DELETE", OWNED + "?id=" + peer)
                        except (urllib.error.URLError, TimeoutError):
                            if process.poll() is None:
                                raise
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)


if __name__ == "__main__":
    main()

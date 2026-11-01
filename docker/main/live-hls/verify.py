"""Verify the qualified static HLS payload and existing private on-demand config."""

import hashlib
import json
import struct
import sys
from pathlib import Path
from urllib.parse import urlsplit

PREFIX = Path("/opt/live-hls")
QUALIFIED_IMAGE = (
    "ghcr.io/errantactions/frigate-horizon-live-hls@sha256:"
    "e2ea553de2de7e4d4f56df53d9e2a41c7f17048044dac9e1ba403586d06e36fd"
)


def validate_elf(header: bytes) -> None:
    """Reject a wrong-architecture binary or one requiring another image's loader."""
    if header[:6] != b"\x7fELF\x02\x01" or len(header) < 64:
        raise ValueError("The qualified HLS payload must be a little-endian ELF64")
    if struct.unpack_from("<H", header, 18)[0] != 62:
        raise ValueError("The qualified HLS payload only supports linux/amd64")
    offset = struct.unpack_from("<Q", header, 32)[0]
    size, count = struct.unpack_from("<HH", header, 54)
    if count and (size < 56 or offset + size * count > len(header)):
        raise ValueError("Invalid HLS ELF program headers")
    for index in range(count):
        if struct.unpack_from("<I", header, offset + index * size)[0] == 3:
            raise ValueError("The HLS payload must not depend on a dynamic ELF loader")


def validate_payload(prefix: Path = PREFIX) -> None:
    """Validate the copied artifact against its original qualified build receipt."""
    receipt = json.loads((prefix / "share/shared-clock.json").read_text())
    if not isinstance(receipt, dict):
        raise ValueError("Invalid qualified HLS receipt")
    expected = {
        "upstream_version": "1.21.3",
        "source_commit": "9540cb6c1d9f1e9351fcc4ded3afc249ffff22bc",
        "target": "linux/amd64",
        "protocol": "horizon-live-hls-shared-clock-v1",
        "opt_in_environment": "HORIZON_HLS_SHARED_CLOCK=1",
        "recording_paths_modified": False,
    }
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise ValueError("The HLS artifact does not match the qualified build receipt")
    binary = (prefix / "bin/mediamtx").read_bytes()
    if hashlib.sha256(binary).hexdigest() != receipt.get("binary_sha256"):
        raise ValueError("The HLS binary does not match its qualified receipt")
    validate_elf(binary)
    license_count = sum(
        path.is_file() for path in (prefix / "share/licenses").rglob("*")
    )
    if not license_count or license_count != receipt.get("license_files"):
        raise ValueError("The HLS artifact's license notices are missing")
    if (prefix / "share/qualified-image.txt").read_text().strip() != QUALIFIED_IMAGE:
        raise ValueError(
            "The HLS artifact provenance does not match the approved image"
        )


def validate_config(config: dict) -> None:
    """Require the existing manager's loopback-only, lazy-pull configuration."""
    if not isinstance(config, dict):
        raise ValueError("Invalid private HLS configuration")
    if (
        config.get("hls") is not True
        or config.get("hlsAddress") != "127.0.0.1:8888"
        or config.get("hlsAlwaysRemux") is not False
    ):
        raise ValueError("HLS must remain private and on demand")
    for protocol in ("api", "metrics", "pprof", "rtsp", "rtmp", "webrtc", "srt", "moq"):
        if config.get(protocol) is not False:
            raise ValueError("Unexpected additional HLS packager listener")
    if config.get("playback", False) is not False:
        raise ValueError("Unexpected additional HLS playback listener")
    paths = config.get("paths")
    if not isinstance(paths, dict):
        raise ValueError("The HLS paths configuration is missing")
    for settings in paths.values():
        if not isinstance(settings, dict) or settings.get("sourceOnDemand") is not True:
            raise ValueError("HLS sources must remain on demand")
        if settings.get("record", False) is not False:
            raise ValueError("The live HLS daemon must never record source media")
        source = urlsplit(settings.get("source", ""))
        if (
            source.scheme != "rtsp"
            or source.hostname != "127.0.0.1"
            or source.port != 8554
            or source.username is not None
            or source.password is not None
        ):
            raise ValueError("HLS sources must use the existing trusted loopback relay")


def main() -> None:
    """Validate payload at build time or private generated JSON at service startup."""
    if sys.argv[1:] == ["--payload"]:
        validate_payload()
        print("Qualified static linux/amd64 HLS payload verified")
    elif len(sys.argv) == 3 and sys.argv[1] == "--config":
        path = Path(sys.argv[2])
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("HLS configuration exceeds its bounded size")
        # LiveHLSManager writes JSON (also valid YAML), including the .yml file.
        validate_config(json.loads(path.read_text()))
    else:
        raise SystemExit("Usage: verify.py --payload | --config PATH")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError):
        # Never include source URLs or private config contents in a failure.
        raise SystemExit(
            "Private HLS packaging/configuration verification failed"
        ) from None

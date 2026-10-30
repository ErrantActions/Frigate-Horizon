"""Build the scoped native-media extension against the pinned go2rtc source."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

SOURCE_COMMIT = "b5948cfb25404cc5cb37b166ecaa2dca20b11d4b"
ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "docker" / "main" / "go2rtc-native"


def run(command: list[str], source: Path, environment: dict[str, str]) -> None:
    subprocess.run(command, cwd=source, env=environment, check=True)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--arch", choices=("amd64", "arm64"), default="amd64")
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=source, text=True
    ).strip()
    if commit != SOURCE_COMMIT:
        raise SystemExit(
            "Use the exact go2rtc v1.9.14 source commit; no unpinned build is allowed."
        )
    changed = set(
        subprocess.check_output(
            ["git", "diff", "--name-only"], cwd=source, text=True
        ).splitlines()
    )
    if changed - {"internal/webrtc/webrtc.go"}:
        raise SystemExit(
            "The source checkout has unrelated tracked changes; use a dedicated build checkout."
        )
    patch = EXTENSION / "enable-owned-webrtc.patch"
    entrypoint = source / "internal" / "webrtc" / "webrtc.go"
    environment = os.environ.copy()
    environment["CGO_ENABLED"] = "0"
    if "\tinitHorizonOwned()" not in entrypoint.read_text():
        run(["git", "apply", "--check", str(patch)], source, environment)
        run(["git", "apply", str(patch)], source, environment)
    else:
        run(["git", "apply", "--reverse", "--check", str(patch)], source, environment)
    for name in ("horizon_owned.go", "horizon_owned_test.go"):
        shutil.copy2(EXTENSION / name, source / "internal" / "webrtc" / name)
    environment.pop("GOOS", None)
    environment.pop("GOARCH", None)
    run(
        ["go", "test", "./internal/webrtc", "-run", "TestHorizon", "-count=1"],
        source,
        environment,
    )
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    binary = output / f"go2rtc-horizon-owned-{args.arch}"
    environment.update(GOOS="linux", GOARCH=args.arch)
    run(
        ["go", "build", "-trimpath", "-ldflags=-s -w", "-o", str(binary), "."],
        source,
        environment,
    )
    license_path = source / "LICENSE"
    if not license_path.is_file():
        raise SystemExit(
            "The upstream license is missing; the artifact is not ready for packaging."
        )
    shutil.copy2(license_path, output / "LICENSE.go2rtc")
    receipt = {
        "upstream_version": "1.9.14",
        "source_commit": commit,
        "target": f"linux/{args.arch}",
        "protocol": "horizon-owned-webrtc-v1",
        "binary_sha256": digest(binary),
        "patch_sha256": digest(patch),
        "extension_sha256": digest(EXTENSION / "horizon_owned.go"),
        "tests": "go test ./internal/webrtc -run TestHorizon -count=1",
        "recording_paths_modified": False,
    }
    (output / f"{binary.name}.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()

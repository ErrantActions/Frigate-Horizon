"""Build the opt-in shared-clock HLS packager from exact upstream source."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

SOURCE_COMMIT = "9540cb6c1d9f1e9351fcc4ded3afc249ffff22bc"
ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "docker" / "live-hls" / "shared-clock"
ENTRYPOINT = "internal/protocols/hls/from_stream.go"
EXTENSIONS = ("horizon_shared_clock.go", "horizon_shared_clock_test.go")


def run(args: list[str], source: Path, environment: dict[str, str]) -> None:
    """Run a bounded build step without ignoring errors."""
    subprocess.run(args, cwd=source, env=environment, check=True, timeout=600)


def digest(path: Path) -> str:
    """Return a content digest for the build receipt."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def collect_licenses(source: Path, output: Path, environment: dict[str, str]) -> int:
    """Preserve upstream, embedded-player, and downloaded Go-module license notices."""
    licenses = output / "licenses"
    licenses.mkdir(exist_ok=True)
    shutil.copy2(source / "LICENSE", licenses / "LICENSE.mediamtx")
    goroot = Path(
        subprocess.check_output(
            ["go", "env", "GOROOT"], env=environment, text=True
        ).strip()
    )
    shutil.copy2(goroot / "LICENSE", licenses / "LICENSE.Go")
    for name in ("LICENSE.hls-js", "LICENSE.Apache-2.0"):
        shutil.copy2(EXTENSION / name, licenses / name)
    remaining = subprocess.check_output(
        ["go", "list", "-m", "-json", "all"], cwd=source, env=environment, text=True
    ).strip()
    decoder = json.JSONDecoder()
    while remaining:
        module, consumed = decoder.raw_decode(remaining)
        remaining = remaining[consumed:].lstrip()
        directory = module.get("Dir")
        if not directory or module.get("Main"):
            continue
        for path in Path(directory).iterdir():
            if (
                path.name.lower().startswith(("license", "copying", "notice"))
                and path.is_file()
            ):
                if path.is_symlink():
                    raise SystemExit("A module license must not be a symlink")
                destination = licenses / module["Path"].replace("/", "_")
                destination.mkdir(exist_ok=True)
                shutil.copy2(path, destination / path.name)
    return sum(path.is_file() for path in licenses.rglob("*"))


def main() -> None:
    """Validate the source boundary, run tests, and build a reproducible binary."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--arch", choices=("amd64",), default="amd64")
    parser.add_argument("--test-only", action="store_true")
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=source, text=True
    ).strip()
    if commit != SOURCE_COMMIT:
        raise SystemExit("Use the exact MediaMTX v1.21.3 source commit")
    changed = set(
        subprocess.check_output(
            ["git", "diff", "--name-only"], cwd=source, text=True
        ).splitlines()
    )
    if changed - {ENTRYPOINT}:
        raise SystemExit("Unrelated upstream tracked changes are not allowed")
    untracked = set(
        subprocess.check_output(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=source,
            text=True,
        ).splitlines()
    )
    if untracked - {f"internal/protocols/hls/{name}" for name in EXTENSIONS}:
        raise SystemExit("Unrelated upstream untracked files are not allowed")
    environment = os.environ.copy()
    for name in ("GOOS", "GOARCH", "HORIZON_HLS_SHARED_CLOCK"):
        environment.pop(name, None)
    environment.update(CGO_ENABLED="0", GOMAXPROCS="2")
    environment["GOFLAGS"] = "-mod=readonly -p=2"
    patch_data = (
        (EXTENSION / "enable-shared-clock.patch").read_bytes().replace(b"\r\n", b"\n")
    )
    with tempfile.TemporaryDirectory() as directory:
        patch = Path(directory) / "clock.patch"
        patch.write_bytes(patch_data)
        if changed:
            actual = subprocess.check_output(
                ["git", "diff", "--binary", "--full-index", "--", ENTRYPOINT],
                cwd=source,
            ).replace(b"\r\n", b"\n")
            if actual != patch_data:
                raise SystemExit("The upstream patch differs from the reviewed patch")
            run(
                ["git", "apply", "--reverse", "--check", str(patch)],
                source,
                environment,
            )
        else:
            run(["git", "apply", "--check", str(patch)], source, environment)
            run(["git", "apply", str(patch)], source, environment)
    for name in EXTENSIONS:
        shutil.copy2(EXTENSION / name, source / "internal" / "protocols" / "hls" / name)
    run(["go", "generate", "./internal/servers/hls"], source, environment)
    tests = [
        "go",
        "test",
        "./internal/protocols/hls",
        "./internal/servers/hls",
        "-count=1",
    ]
    run(tests, source, environment)
    if args.test_only:
        print("Shared-clock packager tests passed; no binary was built")
        return
    run(["go", "generate", "./internal/core"], source, environment)
    version = (source / "internal" / "core" / "VERSION").read_text().strip()
    if not version.startswith("v1.21.3"):
        raise SystemExit("Generated packager version does not match the pinned release")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    binary = output / f"mediamtx-horizon-clock-{args.arch}"
    environment.update(GOOS="linux", GOARCH=args.arch)
    run(
        ["go", "build", "-trimpath", "-ldflags=-s -w", "-o", str(binary), "."],
        source,
        environment,
    )
    license_path = source / "LICENSE"
    if not license_path.is_file():
        raise SystemExit("The upstream license is missing")
    shutil.copy2(license_path, output / "LICENSE.mediamtx")
    license_count = collect_licenses(source, output, environment)
    receipt = {
        "upstream_version": "1.21.3",
        "source_commit": commit,
        "target": f"linux/{args.arch}",
        "protocol": "horizon-live-hls-shared-clock-v1",
        "binary_sha256": digest(binary),
        "patch_sha256": hashlib.sha256(patch_data).hexdigest(),
        "extension_sha256": digest(EXTENSION / EXTENSIONS[0]),
        "tests": " ".join(tests),
        "opt_in_environment": "HORIZON_HLS_SHARED_CLOCK=1",
        "recording_paths_modified": False,
        "license_files": license_count,
    }
    (output / f"{binary.name}.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()

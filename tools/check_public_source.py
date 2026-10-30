"""Reject private runtime material before a source snapshot is published."""

import hashlib
import subprocess
from pathlib import Path, PurePosixPath

FORBIDDEN_SUFFIXES = {".p8", ".pem", ".key", ".db", ".pyc", ".mp4"}
FORBIDDEN_PARTS = {
    ".copilot",
    "__pycache__",
    "node_modules",
    "secrets",
    "%SystemDrive%",
}
SYNTHETIC_HLS_INITIALIZATIONS = {
    "web/e2e/fixtures/media/live-hls/0/init_0.mp4": "57f53d40e2aac98067ca0620a737cf9643cf5f65b5f09fe4f48021f305bb91e8",
    "web/e2e/fixtures/media/live-hls/1/init_1.mp4": "8e73191c50cf640925746dc6d23c2a8b1609692691ec44762e4dd3ce58e168f5",
}


def is_synthetic_hls_initialization(name: str, path: Path) -> bool:
    """Allow only the two exact initialization headers from synthetic test media."""
    expected = SYNTHETIC_HLS_INITIALIZATIONS.get(name)
    return (
        expected is not None
        and path.is_file()
        and not path.is_symlink()
        and hashlib.sha256(path.read_bytes()).hexdigest() == expected
    )


def main() -> None:
    """Inspect tracked paths, never print the content of rejected files."""
    root = Path(__file__).resolve().parents[1]
    entries = (
        subprocess.check_output(["git", "ls-files", "--stage", "-z"], cwd=root)
        .decode()
        .split("\0")
    )
    modes = {}
    for entry in filter(None, entries):
        metadata, name = entry.split("\t", 1)
        mode, _, stage = metadata.split()
        if stage != "0":
            raise SystemExit(f"Unmerged source entry: {name}")
        modes[name] = mode
    names = set(modes)
    required = {
        "frigate/config/config.py",
        "frigate/api/fastapi_app.py",
        "web/src/App.tsx",
        "web/themes/tailwind-base.css",
        "docker/main/Dockerfile",
        "tools/build_native_go2rtc.py",
    }
    if missing := required - names:
        raise SystemExit(
            "Required source inputs are not tracked:\n" + "\n".join(sorted(missing))
        )
    rejected = []
    for name in sorted(names):
        relative = PurePosixPath(name)
        path = root.joinpath(*relative.parts)
        if (
            (
                relative.suffix.lower() in FORBIDDEN_SUFFIXES
                and not is_synthetic_hls_initialization(name, path)
            )
            or any(part in FORBIDDEN_PARTS for part in relative.parts)
            or relative.name == ".env"
            or relative.name.startswith(".env.")
            or name.startswith("config/")
            or relative.name == "RELEASE-HANDOFF.md"
            or path.is_symlink()
            or not path.is_file()
        ):
            rejected.append(name)
    if rejected:
        raise SystemExit(
            "Private/generated source paths are not publishable:\n"
            + "\n".join(rejected)
        )
    not_executable = []
    for name in sorted(names):
        with root.joinpath(*PurePosixPath(name).parts).open("rb") as source:
            if source.read(2) == b"#!" and modes[name] != "100755":
                not_executable.append(name)
    if not_executable:
        raise SystemExit(
            "Linux scripts must retain executable Git modes:\n"
            + "\n".join(not_executable)
        )
    print("Source path boundary check passed")


if __name__ == "__main__":
    main()

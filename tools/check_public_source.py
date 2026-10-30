"""Reject private runtime material before a source snapshot is published."""

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


def main() -> None:
    """Inspect tracked paths, never print the content of rejected files."""
    root = Path(__file__).resolve().parents[1]
    names = (
        subprocess.check_output(["git", "ls-files", "-z"], cwd=root)
        .decode()
        .split("\0")
    )
    required = {
        "frigate/config/config.py",
        "frigate/api/fastapi_app.py",
        "web/src/App.tsx",
        "web/themes/tailwind-base.css",
        "docker/main/Dockerfile",
        "tools/build_native_go2rtc.py",
    }
    if missing := required - set(names):
        raise SystemExit(
            "Required source inputs are not tracked:\n" + "\n".join(sorted(missing))
        )
    rejected = []
    for name in filter(None, names):
        relative = PurePosixPath(name)
        path = root.joinpath(*relative.parts)
        if (
            relative.suffix.lower() in FORBIDDEN_SUFFIXES
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
    print("Source path boundary check passed")


if __name__ == "__main__":
    main()

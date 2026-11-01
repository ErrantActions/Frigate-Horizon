"""Build the checksum-locked upstream CPU runtime without changing Frigate codecs."""

import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PACKAGING = Path(__file__).resolve().parent
LOCK = json.loads((PACKAGING / "sources.lock.json").read_text())
PREFIX = Path("/opt/ovenmediaengine")
SOURCE = Path("/build/source")
CACHE = Path("/build/downloads")
LICENSES = PREFIX / "share/licenses"


def digest(path: Path) -> str:
    """Return a file's SHA-256 without loading an archive into memory."""
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def source_entry(url: str) -> dict:
    """Reject downloads that are not in the reviewed source lock."""
    for entry in LOCK["sources"]:
        if entry["url"] == url:
            return entry
    raise ValueError(f"Unreviewed source URL: {url}")


def fetch(url: str, destination: Path) -> None:
    """Fetch and verify an upstream archive, including cached downloads."""
    entry = source_entry(url)
    CACHE.mkdir(parents=True, exist_ok=True)
    cached = CACHE / entry["sha256"]
    if not cached.exists() or digest(cached) != entry["sha256"]:
        partial = cached.with_suffix(".partial")
        for attempt in range(4):
            try:
                request = urllib.request.Request(
                    url, headers={"User-Agent": "Frigate-OME-source-build"}
                )
                with urllib.request.urlopen(request, timeout=120) as response:
                    with partial.open("wb") as target:
                        shutil.copyfileobj(response, target)
                break
            except (urllib.error.URLError, TimeoutError):
                partial.unlink(missing_ok=True)
                if attempt == 3:
                    raise
                time.sleep(2**attempt)
        if digest(partial) != entry["sha256"]:
            partial.unlink()
            raise ValueError(f"Source checksum mismatch: {entry['name']}")
        partial.replace(cached)
    shutil.copyfile(cached, destination)


def collect_licenses(url: str) -> None:
    """Retain dependency license notices before upstream deletes its build tree."""
    name = source_entry(url)["name"]
    target = LICENSES / name
    for path in Path.cwd().rglob("*"):
        relative = path.relative_to(Path.cwd())
        if (
            len(relative.parts) <= 5
            and path.is_file()
            and path.name.upper().startswith(
                ("LICENSE", "LICENCE", "COPYING", "NOTICE", "COPYRIGHT", "AUTHORS")
            )
        ):
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
    if not target.exists():
        raise ValueError(f"No license notices found for {name}")


def replace_once(text: str, original: str, replacement: str) -> str:
    """Fail closed if an upstream build-script edit no longer matches."""
    if text.count(original) != 1:
        raise ValueError(f"Upstream build script changed: {original!r}")
    return text.replace(original, replacement, 1)


def patch_prerequisites(text: str, jobs: int) -> str:
    """Bound builds, verify every archive, and disable host-specific CPU tuning."""
    text = replace_once(
        text,
        'curl -sSLf --retry 8 -o ome_source_archive \\"$1\\"',
        'python3 /build/packaging/build.py fetch \\"$1\\" ome_source_archive',
    )
    text = replace_once(
        text,
        "    rm -f ome_source_archive",
        '    python3 /build/packaging/build.py licenses \\"$1\\" &&\n'
        "    rm -f ome_source_archive",
    )
    text = replace_once(text, 'set(_J "-j$(nproc)")', f'set(_J "-j{jobs}")')
    text = text.replace("-j$(nproc)", f"-j{jobs}")
    text = replace_once(
        text,
        'set(TEMP_PATH "/tmp/ome_deps_$ENV{USER}")',
        'set(TEMP_PATH "/build/dependencies")',
    )
    text = replace_once(text, "--datadir=/tmp/aac", "--datadir=${PREFIX}/share/fdk-aac")
    return replace_once(
        text,
        '"-DGGML_STATIC=${_GGML_STATIC}"',
        '"-DGGML_STATIC=${_GGML_STATIC}"\n    "-DGGML_NATIVE=OFF"',
    )


def patch_versions(source: Path) -> None:
    """Keep the private TLS dependency on the current compatible 3.0 release."""
    versions = source / "cmake/Versions.cmake"
    versions.write_text(
        replace_once(
            versions.read_text(),
            "set(OME_VER_OPENSSL         3.0.7)",
            "set(OME_VER_OPENSSL         3.0.22)",
        )
    )


def patch_ice_bind(source: Path) -> None:
    """Allow media sockets to bind separately from private HTTP listeners."""
    header = source / "src/config/items/bind/common/webrtc/ice_candidates.h"
    text = replace_once(
        header.read_text(),
        'std::vector<ov::String> _ice_candidate_list{"*:10000/udp"};',
        'std::vector<ov::String> _ice_candidate_list{"*:10000/udp"};\n'
        "\t\t\t\tstd::vector<ov::String> _bind_ip_list;",
    )
    text = replace_once(
        text,
        "CFG_DECLARE_CONST_REF_GETTER_OF(GetIceCandidateList, _ice_candidate_list);",
        "CFG_DECLARE_CONST_REF_GETTER_OF(GetIceCandidateList, _ice_candidate_list);\n"
        "\t\t\tCFG_DECLARE_CONST_REF_GETTER_OF(GetBindIPList, _bind_ip_list);",
    )
    text = replace_once(
        text,
        'Register<Optional>("IceCandidate", &_ice_candidate_list);',
        'Register<Optional>("IceCandidate", &_ice_candidate_list);\n'
        '\t\t\t\t\tRegister<Optional>("BindIP", &_bind_ip_list);',
    )
    header.write_text(text)
    header = source / "src/modules/ice/ice_port.h"
    header.write_text(
        replace_once(
            header.read_text(),
            "int tcp_ice_worker_count = 0);",
            "int tcp_ice_worker_count = 0, "
            "const std::vector<ov::String> &bind_ip_list = {});",
        )
    )
    implementation = source / "src/modules/ice/ice_port.cpp"
    text = replace_once(
        implementation.read_text(),
        "int ice_worker_count, int tcp_ice_worker_count)\n",
        "int ice_worker_count, int tcp_ice_worker_count, "
        "const std::vector<ov::String> &bind_ip_list)\n",
    )
    text = replace_once(
        text,
        "auto &ip_list = server_config.GetIPList();",
        "const auto &ip_list = bind_ip_list.empty() ? "
        "server_config.GetIPList() : bind_ip_list;",
    )
    implementation.write_text(text)
    manager = source / "src/modules/ice/ice_port_manager.cpp"
    manager.write_text(
        replace_once(
            manager.read_text(),
            "ice_candidate_list, ice_worker_count, tcp_ice_worker_count) == false)",
            "ice_candidate_list, ice_worker_count, tcp_ice_worker_count, "
            "ice_candidates_config.GetBindIPList()) == false)",
        )
    )


def build() -> None:
    """Build and stage the isolated Debian 12 runtime and its provenance."""
    jobs = int(os.environ.get("OME_BUILD_JOBS", "2"))
    if not 1 <= jobs <= 32:
        raise ValueError("OME_BUILD_JOBS must be between 1 and 32")
    SOURCE.mkdir(parents=True, exist_ok=True)
    archive = Path("/build/ome-source.tar.gz")
    fetch(LOCK["sources"][0]["url"], archive)
    subprocess.run(
        ["tar", "-xf", str(archive), "--strip-components=1", "-C", str(SOURCE)],
        check=True,
    )
    os.chdir(SOURCE)
    collect_licenses(LOCK["sources"][0]["url"])
    patch_versions(SOURCE)
    installer = SOURCE / "cmake/InstallPrerequisites.cmake"
    installer.write_text(patch_prerequisites(installer.read_text(), jobs))
    patch_ice_bind(SOURCE)
    # x264 + FDK-AAC enables FFmpeg's nonredistributable mode in upstream's
    # default build. OpenH264 supplies CPU H.264 without that combination.
    options = [
        "-DOME_DEP_PREFIX=/opt/ovenmediaengine",
        "-DOME_USE_CLANG=OFF",
        "-DOME_HWACCEL_NVIDIA=OFF",
        "-DOME_HWACCEL_XMA=OFF",
        "-DOME_ENABLE_X264=OFF",
        "-DOME_WHISPER_STATIC=ON",
    ]
    subprocess.run(
        ["cmake", *options, "-DTEMP_PATH=/build/dependencies", "-P", str(installer)],
        check=True,
    )
    subprocess.run(
        [
            "cmake",
            "-S",
            ".",
            "-B",
            "build/Release",
            "-G",
            "Ninja",
            *options,
            "-DCMAKE_BUILD_TYPE=Release",
            "-DOME_SKIP_DEPENDENCY_CHECK=ON",
        ],
        check=True,
    )
    subprocess.run(
        ["cmake", "--build", "build/Release", "--parallel", str(jobs)], check=True
    )
    avutil = ctypes.CDLL(str(PREFIX / "lib/libavutil.so"))
    avutil.avutil_license.restype = ctypes.c_char_p
    ffmpeg_license = avutil.avutil_license().decode()
    if "nonfree" in ffmpeg_license.lower():
        raise ValueError("Refusing to package a nonredistributable FFmpeg build")

    output = Path("/rootfs/opt/ovenmediaengine")
    (output / "bin").mkdir(parents=True)
    binary = output / "bin/OvenMediaEngine"
    shutil.copy2(SOURCE / "build/Release/bin/OvenMediaEngine", binary)
    subprocess.run(["strip", str(binary)], check=True)
    for directory in ("lib", "lib64"):
        if not (PREFIX / directory).exists():
            continue
        for library in (PREFIX / directory).glob("*.so*"):
            target = output / directory / library.name
            target.parent.mkdir(exist_ok=True)
            if library.is_symlink():
                target.symlink_to(os.readlink(library))
            else:
                shutil.copy2(library, target)
    shutil.copytree(LICENSES, output / "share/licenses")
    build_files = output / "share/build"
    shutil.copytree(
        PACKAGING, build_files, ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copy2(SOURCE / "cmake/Versions.cmake", build_files / "Versions.cmake")
    shutil.copy2(
        SOURCE / "misc/conf_examples/Logger.xml", output / "share/Logger.example.xml"
    )
    receipt = {
        "release": LOCK["release"],
        "source_commit": LOCK["commit"],
        "source_sha256": LOCK["sources"][0]["sha256"],
        "source_license": "AGPL-3.0",
        "ffmpeg_license": ffmpeg_license,
        "cpu_h264_encoder": "OpenH264",
        "hardware_acceleration": False,
        "local_patches": [
            "Separate IceCandidates/BindIP from Server/IP for media sockets",
            "Checksum-locked downloads and retained dependency license notices",
            "Bounded build parallelism and portable GGML CPU instructions",
        ],
        "build_distribution": Path("/etc/os-release").read_text(),
        "build_machine": os.uname().machine,
        "binary_sha256": digest(binary),
        "cmake_options": options,
    }
    (output / "share/version.json").write_text(json.dumps(receipt, indent=2) + "\n")
    with (output / "share/manifest.sha256").open("w") as manifest:
        for path in sorted(output.rglob("*")):
            if path.is_file() and not path.is_symlink() and path != Path(manifest.name):
                manifest.write(
                    f"{digest(path)}  {path.relative_to(output).as_posix()}\n"
                )


if __name__ == "__main__":
    match sys.argv[1:]:
        case ["fetch", url, destination]:
            fetch(url, Path(destination))
        case ["licenses", url]:
            collect_licenses(url)
        case ["build"]:
            build()
        case _:
            raise SystemExit("Usage: build.py build | fetch URL PATH | licenses URL")

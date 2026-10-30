"""Bounded HTTPS downloads and non-executing plugin archive installation."""

import asyncio
import hashlib
import io
import ipaddress
import json
import os
import shutil
import socket
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import aiohttp

from frigate.plugins.models import PluginManifest

MAX_DOWNLOAD = 16 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024


class PluginError(Exception):
    """A safe error code suitable for the admin API."""


def write_private_json(path: Path, value: dict) -> None:
    """Atomically persist state without making tokens world-readable."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".plugin-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class PublicResolver(aiohttp.abc.AbstractResolver):
    """Reject private addresses at the connection's DNS resolution boundary."""

    async def resolve(self, host, port=0, family=socket.AF_INET):
        addresses = await asyncio.get_running_loop().getaddrinfo(
            host, port, family=family, type=socket.SOCK_STREAM
        )
        if not addresses or any(
            not ipaddress.ip_address(item[4][0]).is_global for item in addresses
        ):
            raise PluginError("download_address_blocked")
        return [
            {
                "hostname": host,
                "host": address[4][0],
                "port": port,
                "family": address[0],
                "proto": address[2],
                "flags": socket.AI_NUMERICHOST,
            }
            for address in addresses
        ]

    async def close(self):
        """No persistent resolver resources are held."""


def validate_download_url(url: str) -> None:
    """Allow public HTTPS downloads without embedded credentials."""
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.port not in (None, 443)
    ):
        raise PluginError("invalid_download_url")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return
    if not address.is_global:
        raise PluginError("download_address_blocked")


async def download_package(url: str) -> bytes:
    """Bound redirects, wall time and bytes, validating each destination."""
    connector = aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False)
    timeout = aiohttp.ClientTimeout(total=60, connect=10)
    async with aiohttp.ClientSession(
        connector=connector, timeout=timeout, trust_env=False
    ) as client:
        for _ in range(4):
            validate_download_url(url)
            async with client.get(url, allow_redirects=False) as response:
                if response.status in (301, 302, 303, 307, 308):
                    from urllib.parse import urljoin

                    location = response.headers.get("Location")
                    if not location:
                        raise PluginError("download_redirect_invalid")
                    url = urljoin(url, location)
                    continue
                if response.status != 200:
                    raise PluginError("download_failed")
                payload = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    payload.extend(chunk)
                    if len(payload) > MAX_DOWNLOAD:
                        raise PluginError("package_too_large")
                return bytes(payload)
    raise PluginError("too_many_redirects")


def install_archive(payload: bytes, digest: str, destination: Path) -> PluginManifest:
    """Validate the entire ZIP before extracting into a private staging directory."""
    if len(payload) > MAX_DOWNLOAD:
        raise PluginError("package_too_large")
    if hashlib.sha256(payload).hexdigest() != digest:
        raise PluginError("checksum_mismatch")
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as error:
        raise PluginError("invalid_archive") from error
    with archive:
        entries = archive.infolist()
        names = set()
        total = 0
        for entry in entries:
            name = entry.orig_filename
            path = PurePosixPath(name)
            total += entry.file_size
            if (
                len(entries) > 256
                or total > MAX_EXPANDED
                or "\\" in name
                or ":" in name
                or path.is_absolute()
                or any(part in ("..", ".") for part in path.parts)
                or name in names
                or stat.S_ISLNK(entry.external_attr >> 16)
                or entry.flag_bits & 1
                or len(name) > 240
            ):
                raise PluginError("unsafe_archive")
            names.add(name)
        if not {"plugin.json", "plugin.py"}.issubset(names):
            raise PluginError("missing_manifest")
        manifest = PluginManifest.model_validate_json(archive.read("plugin.json"))
        if manifest.id == "ring":
            raise PluginError("reserved_plugin_id")
        target = destination / manifest.id / digest
        if target.exists():
            raise PluginError("package_already_exists")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        stage = Path(tempfile.mkdtemp(dir=target.parent, prefix=".install-"))
        try:
            for entry in entries:
                path = stage.joinpath(*PurePosixPath(entry.filename).parts)
                if entry.is_dir():
                    path.mkdir(parents=True, exist_ok=True, mode=0o700)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    with archive.open(entry) as source, path.open("xb") as output:
                        shutil.copyfileobj(source, output)
                    path.chmod(0o600)
            os.replace(stage, target)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    return manifest

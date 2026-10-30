"""Bounded, explicit ONVIF backup downloads without reflecting private data."""

import hashlib
import io
import json
import zipfile
from collections.abc import Mapping

MAX_BACKUP_BYTES = 32 * 1024 * 1024
MAX_BACKUP_FILES = 128


def backup_download(files) -> bytes:
    """Package actual inline backup bytes with original names in a manifest."""
    if not isinstance(files, (list, tuple)) or not 0 < len(files) <= MAX_BACKUP_FILES:
        raise ValueError("The camera returned an invalid backup file list")
    output = io.BytesIO()
    manifest = []
    total = 0
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for index, item in enumerate(files):
            values = (
                item if isinstance(item, Mapping) else getattr(item, "__values__", {})
            )
            name = values.get("Name")
            data = values.get("Data")
            data = (
                data if isinstance(data, Mapping) else getattr(data, "__values__", {})
            )
            content = data.get("Data")
            if (
                not isinstance(name, str)
                or not name
                or len(name) > 1024
                or any(ord(character) < 32 for character in name)
                or not isinstance(content, bytes)
                or not content
            ):
                raise ValueError(
                    "The camera did not return complete inline backup data"
                )
            total += len(content)
            if total > MAX_BACKUP_BYTES:
                raise ValueError("The camera backup exceeds the download limit")
            member = f"files/{index:03d}.bin"
            archive.writestr(member, content)
            manifest.append(
                {
                    "name": name,
                    "member": member,
                    "bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
        archive.writestr(
            "manifest.json",
            json.dumps(
                {"format": "horizon-onvif-backup", "version": 1, "files": manifest}
            ),
        )
    return output.getvalue()


def restore_files(payload: bytes) -> list[dict]:
    """Validate a Horizon backup fully before constructing a camera restore."""
    if len(payload) > MAX_BACKUP_BYTES + 1024 * 1024:
        raise ValueError("The backup upload exceeds the limit")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = archive.infolist()
            if (
                len(members) > MAX_BACKUP_FILES + 1
                or len({item.filename for item in members}) != len(members)
                or sum(item.file_size for item in members) > MAX_BACKUP_BYTES + 65536
                or any(item.flag_bits & 1 for item in members)
            ):
                raise ValueError("The backup archive is invalid or oversized")
            manifest_info = archive.getinfo("manifest.json")
            if manifest_info.file_size > 65536:
                raise ValueError("The backup manifest exceeds its limit")
            manifest = json.loads(archive.read(manifest_info))
            if (
                not isinstance(manifest, dict)
                or manifest.get("format") != "horizon-onvif-backup"
                or manifest.get("version") != 1
                or not isinstance(manifest.get("files"), list)
                or not 0 < len(manifest["files"]) <= MAX_BACKUP_FILES
            ):
                raise ValueError("The backup manifest is invalid")
            restored = []
            expected = {"manifest.json"}
            for index, item in enumerate(manifest["files"]):
                if not isinstance(item, dict):
                    raise ValueError("The backup file entry is invalid")
                member = f"files/{index:03d}.bin"
                name = item.get("name")
                if (
                    item.get("member") != member
                    or not isinstance(name, str)
                    or not name
                    or len(name) > 1024
                    or any(ord(character) < 32 for character in name)
                    or type(item.get("bytes")) is not int
                    or not 0 < item["bytes"] <= MAX_BACKUP_BYTES
                ):
                    raise ValueError("The backup file entry is invalid")
                info = archive.getinfo(member)
                if info.file_size != item["bytes"]:
                    raise ValueError("The backup file size does not match")
                content = archive.read(info)
                if hashlib.sha256(content).hexdigest() != item.get("sha256"):
                    raise ValueError("The backup file checksum does not match")
                expected.add(member)
                restored.append({"Name": name, "Data": {"Data": content}})
            if expected != {item.filename for item in members}:
                raise ValueError("The backup contains unexpected files")
            return restored
    except (zipfile.BadZipFile, KeyError, UnicodeError, json.JSONDecodeError):
        raise ValueError(
            "The uploaded file is not a complete Horizon camera backup"
        ) from None

"""Synthetic backup packaging tests without cameras, keys or SDK dependencies."""

import io
import json
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

from frigate.util.onvif_backup import backup_download, restore_files


class TestOnvifBackup(unittest.TestCase):
    def test_real_backup_bytes_are_preserved_without_using_vendor_paths(self):
        result = backup_download(
            [
                {"Name": "../../configuration", "Data": {"Data": b"fixture-one"}},
                SimpleNamespace(
                    __values__={
                        "Name": "configuration",
                        "Data": SimpleNamespace(__values__={"Data": b"fixture-two"}),
                    }
                ),
            ]
        )
        with zipfile.ZipFile(io.BytesIO(result)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(
                archive.namelist(), ["files/000.bin", "files/001.bin", "manifest.json"]
            )
            self.assertEqual(archive.read("files/000.bin"), b"fixture-one")
            self.assertEqual(archive.read("files/001.bin"), b"fixture-two")
            self.assertEqual(manifest["files"][0]["name"], "../../configuration")
            self.assertEqual(len(manifest["files"][0]["sha256"]), 64)

    def test_missing_binary_or_reference_only_backup_is_not_success(self):
        for files in (
            [],
            [{"Name": "config", "Data": {"Include": "cid:unresolved"}}],
            [{"Name": "config", "Data": {"Data": "not binary"}}],
            [{"Name": "\ninvalid", "Data": {"Data": b"fixture"}}],
        ):
            with self.subTest(files=files), self.assertRaises(ValueError):
                backup_download(files)

    def test_combined_backup_limit_is_enforced(self):
        with patch("frigate.util.onvif_backup.MAX_BACKUP_BYTES", 6):
            with self.assertRaises(ValueError):
                backup_download(
                    [
                        {"Name": "one", "Data": {"Data": b"1234"}},
                        {"Name": "two", "Data": {"Data": b"5678"}},
                    ]
                )

    def test_export_roundtrips_only_verified_original_bytes(self):
        files = [{"Name": "camera-config", "Data": {"Data": b"fixture-backup"}}]
        self.assertEqual(restore_files(backup_download(files)), files)

    def test_restore_rejects_invalid_or_extra_members(self):
        for payload in (b"not a zip", b""):
            with self.assertRaises(ValueError):
                restore_files(payload)
        output = io.BytesIO(
            backup_download([{"Name": "config", "Data": {"Data": b"one"}}])
        )
        with zipfile.ZipFile(output, "a") as archive:
            archive.writestr("unexpected", b"two")
        with self.assertRaisesRegex(ValueError, "unexpected"):
            restore_files(output.getvalue())

    def test_restore_rejects_wrong_file_checksum(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("files/000.bin", b"tampered")
            archive.writestr(
                "manifest.json",
                json.dumps(
                    {
                        "format": "horizon-onvif-backup",
                        "version": 1,
                        "files": [
                            {
                                "name": "config",
                                "member": "files/000.bin",
                                "bytes": 8,
                                "sha256": "0" * 64,
                            }
                        ],
                    }
                ),
            )
        with self.assertRaisesRegex(ValueError, "checksum"):
            restore_files(output.getvalue())

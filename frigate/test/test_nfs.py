"""NFS setup plans are strict, host-managed, and never activate recording."""

import unittest
from types import SimpleNamespace

import yaml
from pydantic import ValidationError

from frigate.util.nfs import (
    NfsPlan,
    NfsServer,
    NfsShare,
    build_nfs_plan,
    nfs_mount_status,
)


class TestNfsSetup(unittest.TestCase):
    def share(self, **overrides):
        return NfsShare(server="10.20.30.40", export="/mnt/recordings", **overrides)

    def test_server_rejects_urls_options_and_local_addresses(self):
        for server in (
            "http://nas/export",
            "nas,rw",
            "nas;id",
            "user@nas",
            "-nas",
            "127.0.0.1",
            "::1",
            "0.0.0.0",
            "169.254.169.254",
            "localhost",
            "foo.localhost",
            "nas\nother",
            "224.0.0.1",
            "::ffff:127.0.0.1",
            "::ffff:169.254.169.254",
            "2130706433",
            "127.1",
            "0x7f000001",
        ):
            with self.subTest(server=server), self.assertRaises(ValidationError):
                NfsServer(server=server)

    def test_hostname_and_ipv6_are_canonicalized(self):
        self.assertEqual(NfsServer(server="NAS.Example.").server, "nas.example")
        self.assertEqual(NfsServer(server="fd00::0001").server, "fd00::1")

    def test_exports_must_be_dedicated_absolute_paths(self):
        for export in (
            "/",
            "mnt/share",
            "//server/share",
            "/mnt/../etc",
            "/x,soft",
            "/x\n",
        ):
            with self.subTest(export=export), self.assertRaises(ValidationError):
                NfsShare(server="nas.example", export=export)

    def test_connection_count_is_bounded_and_not_a_boolean(self):
        for connections in (0, 17, True, "8", 1.5):
            with (
                self.subTest(connections=connections),
                self.assertRaises(ValidationError),
            ):
                self.share(connections=connections)

    def test_duplicate_roles_destinations_and_server_settings_are_rejected(self):
        cases = [
            [self.share(), self.share()],
            [self.share(), self.share(role="recordings")],
            [
                self.share(),
                NfsShare(
                    server="10.20.30.40",
                    export="/other",
                    role="recordings",
                    connections=4,
                ),
            ],
        ]
        for shares in cases:
            with self.subTest(shares=shares), self.assertRaises(ValidationError):
                NfsPlan(shares=shares, confirm_direct_recordings=True)

    def test_primary_direct_mount_needs_an_acknowledgment(self):
        with self.assertRaises(ValidationError):
            NfsPlan(shares=[self.share(role="recordings")])
        plan = NfsPlan(
            shares=[self.share(role="recordings")], confirm_direct_recordings=True
        )
        output = build_nfs_plan(plan, "/media/frigate/archive", "archive-id", 64)
        self.assertFalse(output["local_first"])
        self.assertIsNone(output["frigate_storage_yaml"])
        mount = yaml.safe_load(output["service_volumes_yaml"])["volumes"][0]
        self.assertEqual(mount["target"], "/media/frigate/recordings")
        self.assertTrue(mount["volume"]["nocopy"])

    def test_both_roles_preserve_separate_mounts_and_versioned_volume_names(self):
        shares = [
            self.share(),
            NfsShare(server="10.20.30.40", export="/mnt/primary", role="recordings"),
        ]
        result = build_nfs_plan(
            NfsPlan(shares=shares, confirm_direct_recordings=True),
            "/media/frigate/archive",
            "archive-id",
            64,
        )
        mounts = yaml.safe_load(result["service_volumes_yaml"])["volumes"]
        self.assertEqual(
            {item["target"] for item in mounts},
            {"/media/frigate/archive", "/media/frigate/recordings"},
        )
        changed = build_nfs_plan(
            NfsPlan(shares=[self.share(connections=4)]),
            "/media/frigate/archive",
            "archive-id",
            64,
        )
        self.assertNotEqual(
            yaml.safe_load(changed["service_volumes_yaml"])["volumes"][0]["source"],
            mounts[0]["source"],
        )

    def test_local_first_plan_never_enables_recording_or_destructive_expiry(self):
        output = build_nfs_plan(
            NfsPlan(shares=[self.share()]), "/media/frigate/archive", "archive-id", 64
        )
        self.assertTrue(output["local_first"])
        self.assertFalse(output["enables_recording"])
        settings = yaml.safe_load(output["frigate_storage_yaml"])["storage"]
        self.assertFalse(settings["enabled"])
        self.assertFalse(settings["emergency_delete"])
        self.assertEqual(settings["local_budget_gib"], 64)
        self.assertTrue(settings["adaptive_transfers"])
        volume = next(
            iter(yaml.safe_load(output["volume_definitions_yaml"])["volumes"].values())
        )
        self.assertEqual(volume["driver_opts"]["device"], ":/mnt/recordings")
        self.assertIn("hard,rw,nconnect=8", volume["driver_opts"]["o"])
        self.assertNotIn("soft", volume["driver_opts"]["o"])
        self.assertEqual(output["archive_marker"]["contents"], "archive-id")

    def test_mount_status_does_not_confuse_a_local_covering_mount_with_nfs(self):
        partitions = [
            SimpleNamespace(
                mountpoint="/media/frigate",
                fstype="nfs4",
                device="nas:/media",
                opts="rw,vers=4.1,nconnect=8",
            ),
            SimpleNamespace(
                mountpoint="/media/frigate/archive",
                fstype="zfs",
                device="pool/local",
                opts="rw",
            ),
        ]
        result = nfs_mount_status("/media/frigate/archive", partitions)
        self.assertFalse(result["mounted"])
        self.assertIsNone(result["source"])
        self.assertFalse(result["access_verified"])

    def test_mount_metadata_reports_requested_pool_not_verified_access_or_sockets(self):
        result = nfs_mount_status(
            "/media/frigate/archive",
            [
                SimpleNamespace(
                    mountpoint="/media/frigate/archive",
                    fstype="nfs4",
                    device="nas:/footage",
                    opts="rw,vers=4.1,nconnect=8",
                )
            ],
        )
        self.assertTrue(result["mounted"])
        self.assertEqual(result["configured_connections"], 8)
        self.assertEqual(result["version"], "4.1")
        self.assertFalse(result["access_verified"])

    def test_unreported_connection_count_is_unknown_not_invented(self):
        result = nfs_mount_status(
            "/media/frigate/archive",
            [
                SimpleNamespace(
                    mountpoint="/media/frigate/archive",
                    fstype="nfs",
                    device="nas:/footage",
                    opts="rw",
                )
            ],
        )
        self.assertIsNone(result["configured_connections"])


if __name__ == "__main__":
    unittest.main()

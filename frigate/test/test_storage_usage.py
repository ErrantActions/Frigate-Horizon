"""Per-camera storage aggregates must match totals without reading media paths."""

import unittest

from peewee import SqliteDatabase

from frigate.models import Recordings
from frigate.util.storage import empty_recording_usage, recording_storage_usage


class TestRecordingStorageUsage(unittest.TestCase):
    def setUp(self):
        self.db = SqliteDatabase(":memory:")
        self.enterContext(self.db.bind_ctx([Recordings]))
        self.addCleanup(self.db.close)
        self.db.create_tables([Recordings])
        self.number = 0

    def insert(self, camera, tier, size):
        self.number += 1
        Recordings.create(
            id=f"recording-{self.number}",
            camera=camera,
            storage_tier=tier,
            path=f"/unavailable-media/{self.number}.mp4",
            start_time=self.number * 10,
            end_time=self.number * 10 + 10,
            duration=10,
            segment_size=size,
            archive_bytes=int(size * (2**20)) if tier == "archive" else None,
        )

    def test_camera_and_tier_totals_are_consistent(self):
        self.insert("front", "local", 1.5)
        self.insert("front", "local", 1.25)
        self.insert("front", "archive", 3)
        self.insert("back", "local", 2)
        self.insert("back", "archive", 4.5)

        totals, cameras = recording_storage_usage()

        self.assertEqual(cameras["front"]["local"]["usage_mib"], 2.75)
        self.assertEqual(cameras["front"]["local"]["recordings"], 2)
        self.assertEqual(cameras["front"]["archive"]["usage_mib"], 3)
        self.assertEqual(totals["local"]["usage_mib"], 4.75)
        self.assertEqual(totals["archive"]["usage_mib"], 7.5)
        for tier, total in totals.items():
            self.assertEqual(
                total["usage_mib"],
                sum(
                    usage.get(tier, empty_recording_usage())["usage_mib"]
                    for usage in cameras.values()
                ),
            )
            self.assertEqual(
                total["recordings"],
                sum(
                    usage.get(tier, empty_recording_usage())["recordings"]
                    for usage in cameras.values()
                ),
            )

    def test_retains_separate_ids_and_historical_cameras(self):
        self.insert("front", "local", 5)
        self.insert("front_old", "archive", 8)
        _, cameras = recording_storage_usage()
        self.assertEqual(set(cameras), {"front", "front_old"})
        self.assertEqual(cameras["front_old"]["archive"]["usage_mib"], 8)

    def test_unknown_sizes_are_not_reported_as_known_empty_recordings(self):
        self.insert("front", "local", 0)
        self.insert("front", "local", -1)
        self.insert("front", "local", 3)
        totals, cameras = recording_storage_usage()
        self.assertEqual(totals["local"]["usage_mib"], 3)
        self.assertEqual(totals["local"]["recordings"], 3)
        self.assertEqual(totals["local"]["unknown_size_recordings"], 2)
        self.assertEqual(cameras["front"]["local"]["unknown_size_recordings"], 2)

    def test_reports_empty_storage_without_camera_entries(self):
        self.assertEqual(recording_storage_usage(), ({}, {}))

    def test_does_not_discard_other_tier_ids(self):
        self.insert("front", "hot_nvme", 2)
        totals, cameras = recording_storage_usage()
        self.assertEqual(totals["hot_nvme"]["usage_mib"], 2)
        self.assertEqual(cameras["front"]["hot_nvme"]["usage_mib"], 2)


if __name__ == "__main__":
    unittest.main()

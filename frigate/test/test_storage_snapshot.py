"""Cached storage snapshots preserve camera identity and consistent totals."""

import unittest
from unittest.mock import MagicMock, patch

from peewee import OperationalError, SqliteDatabase

from frigate.config import FrigateConfig
from frigate.models import Recordings
from frigate.storage import StorageMaintainer


class TestStorageSnapshot(unittest.TestCase):
    def setUp(self):
        self.db = SqliteDatabase(":memory:")
        self.enterContext(self.db.bind_ctx([Recordings]))
        self.addCleanup(self.db.close)
        self.db.create_tables([Recordings])
        self.config = FrigateConfig(
            mqtt={"enabled": False},
            cameras={
                name: {
                    "friendly_name": "Driveway",
                    "ffmpeg": {
                        "inputs": [
                            {
                                "path": "rtsp://camera.invalid/test",
                                "roles": ["detect", "record"],
                            }
                        ]
                    },
                    "detect": {"height": 1080, "width": 1920, "fps": 5},
                }
                for name in ("front", "new_camera")
            },
        )
        self.enterContext(patch("frigate.storage.CameraConfigUpdateSubscriber"))
        self.storage = StorageMaintainer(self.config, MagicMock())

    def insert(self, name="front", size=3):
        Recordings.create(
            id=name,
            camera=name,
            storage_tier="local",
            path=f"/unavailable-media/{name}.mp4",
            start_time=10,
            end_time=20,
            duration=10,
            segment_size=size,
        )

    def test_includes_configured_empty_and_removed_historical_cameras(self):
        self.insert()
        self.insert("removed_camera", 7)
        self.storage.refresh_storage_usage()
        totals, cameras = self.storage.get_storage_usage()
        self.assertEqual(set(cameras), {"front", "new_camera", "removed_camera"})
        self.assertEqual(cameras["front"]["friendly_name"], "Driveway")
        self.assertEqual(cameras["new_camera"]["friendly_name"], "Driveway")
        self.assertEqual(cameras["removed_camera"]["friendly_name"], "removed_camera")
        self.assertEqual(cameras["new_camera"]["tiers"]["local"]["recordings"], 0)
        self.assertEqual(cameras["front"]["tiers"]["archive"]["usage_mib"], 0)
        self.assertEqual(totals["local"]["usage_mib"], 10)

    def test_cache_refreshes_global_and_camera_usage_together(self):
        self.insert()
        with patch("frigate.storage.time.monotonic", return_value=100):
            self.storage.refresh_storage_usage()
            totals, cameras = self.storage.get_storage_usage()
        self.insert("removed_camera", 7)
        with patch("frigate.storage.time.monotonic", return_value=159):
            self.assertEqual(self.storage.get_storage_usage(), (totals, cameras))
            self.assertEqual(self.storage.get_tier_usage(), totals)
        with patch("frigate.storage.time.monotonic", return_value=160):
            self.storage.refresh_storage_usage()
            refreshed, refreshed_cameras = self.storage.get_storage_usage()
        self.assertEqual(refreshed["local"]["usage_mib"], 10)
        self.assertIn("removed_camera", refreshed_cameras)

    def test_callers_cannot_modify_cached_usage(self):
        self.insert()
        self.storage.refresh_storage_usage()
        totals, cameras = self.storage.get_storage_usage()
        totals["local"]["usage_mib"] = 99
        cameras["front"]["tiers"]["local"]["usage_mib"] = 99
        cameras["front"]["tiers"]["archive"]["usage_mib"] = 99
        totals, cameras = self.storage.get_storage_usage()
        self.assertEqual(totals["local"]["usage_mib"], 3)
        self.assertEqual(cameras["front"]["tiers"]["local"]["usage_mib"], 3)
        self.assertEqual(cameras["front"]["tiers"]["archive"]["usage_mib"], 0)

    def test_bandwidth_and_stream_usage_use_ids_not_duplicate_display_names(self):
        self.insert()
        self.storage.refresh_storage_usage()
        self.storage.camera_storage_stats = {
            "front": {"bandwidth": 10},
            "new_camera": {"bandwidth": 20},
        }
        usages = self.storage.calculate_camera_usages()
        self.assertEqual(set(usages), {"front", "new_camera"})
        self.assertEqual(usages["front"]["friendly_name"], "Driveway")
        self.assertEqual(usages["new_camera"]["friendly_name"], "Driveway")
        self.assertEqual(usages["front"]["bandwidth"], 10)
        self.assertEqual(usages["new_camera"]["bandwidth"], 20)

    def test_request_reads_never_query_sqlite_even_when_stale(self):
        self.insert()
        self.storage.refresh_storage_usage()
        with (
            patch.object(
                self.db,
                "execute_sql",
                side_effect=AssertionError("Request queried SQLite"),
            ),
            patch("frigate.storage.time.monotonic", return_value=float("inf")),
        ):
            self.assertEqual(
                self.storage.get_storage_usage()[0]["local"]["usage_mib"], 3
            )
            self.assertTrue(self.storage.get_storage_usage_status()["stale"])

    def test_failed_refresh_retains_snapshot_and_reports_failure(self):
        self.insert()
        self.storage.refresh_storage_usage()
        before = self.storage.get_storage_usage()
        sampled = self.storage.get_storage_usage_status()["sampled_at"]
        with (
            patch(
                "frigate.storage.recording_storage_usage",
                side_effect=OperationalError("synthetic"),
            ),
            self.assertLogs("frigate.storage", level="ERROR"),
        ):
            self.storage.refresh_storage_usage()
        self.assertEqual(self.storage.get_storage_usage(), before)
        self.assertEqual(self.storage.get_storage_usage_status()["sampled_at"], sampled)
        self.assertEqual(
            self.storage.get_storage_usage_status()["error"],
            "storage_usage_unavailable",
        )
        self.storage.refresh_storage_usage()
        self.assertIsNone(self.storage.get_storage_usage_status()["error"])

    def test_slow_refresh_does_not_hold_reader_lock(self):
        def sampling():
            self.assertTrue(self.storage._tier_usage_lock.acquire(blocking=False))
            self.storage._tier_usage_lock.release()
            return {}, {}

        with patch("frigate.storage.recording_storage_usage", side_effect=sampling):
            self.storage.refresh_storage_usage()


if __name__ == "__main__":
    unittest.main()

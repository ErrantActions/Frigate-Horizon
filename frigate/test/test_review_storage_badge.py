"""Review tier badges use indexed metadata, never NAS availability probes."""

import inspect
import unittest
from unittest.mock import patch

from peewee import SqliteDatabase

from frigate.api.review import review, review_storage_column
from frigate.models import Recordings, ReviewSegment


class TestReviewStorageBadge(unittest.TestCase):
    def setUp(self):
        self.db = SqliteDatabase(":memory:")
        self.enterContext(self.db.bind_ctx([Recordings, ReviewSegment]))
        self.db.connect()
        self.addCleanup(self.db.close)
        self.db.create_tables([Recordings, ReviewSegment])
        self.db.execute_sql(
            "CREATE INDEX recordings_camera_stream_type_start_time "
            "ON recordings(camera, stream_type, start_time DESC)"
        )
        ReviewSegment.create(
            id="review",
            camera="synthetic",
            start_time=100,
            end_time=120,
            severity="alert",
            thumb_path="unused",
            data={},
        )

    def recording(self, identifier, tier, **values):
        Recordings.create(
            id=identifier,
            camera=values.pop("camera", "synthetic"),
            path=f"/not-accessed/{identifier}.mp4",
            start_time=values.pop("start_time", 101),
            end_time=110,
            duration=9,
            segment_size=1,
            storage_tier=tier,
            **values,
        )

    def value(self):
        return (
            ReviewSegment.select(ReviewSegment, review_storage_column())
            .dicts()
            .get()["recording_storage"]
        )

    def test_local_archive_and_mixed(self):
        self.assertIsNone(self.value())
        self.recording("local", "local")
        self.assertEqual(self.value(), "local")
        self.recording("archive", "archive")
        self.assertEqual(self.value(), "mixed")
        Recordings.delete().where(Recordings.id == "local").execute()
        self.assertEqual(self.value(), "archive")

    def test_other_cameras_substreams_and_pending_deletions_do_not_count(self):
        self.recording("other", "archive", camera="other")
        self.recording("sub", "archive", stream_type="sub")
        self.recording("deleting", "archive", delete_requested=True)
        self.assertIsNone(self.value())

    def test_badge_does_not_probe_filesystem(self):
        self.recording("archive", "archive")
        with patch("pathlib.Path.stat", side_effect=AssertionError("No NAS probe")):
            self.assertEqual(self.value(), "archive")

    def test_review_list_is_not_executed_on_the_api_event_loop(self):
        self.assertFalse(inspect.iscoroutinefunction(review))

    def test_badge_query_bounds_absent_tier_lookup_on_large_history(self):
        ReviewSegment.update(start_time=200000, end_time=200010).execute()
        for first in range(0, 10000, 250):
            Recordings.insert_many(
                [
                    {
                        "id": f"old-{index}",
                        "camera": "synthetic",
                        "path": f"/not-accessed/{index}.mp4",
                        "start_time": index * 10,
                        "end_time": index * 10 + 10,
                        "duration": 10,
                        "storage_tier": "archive",
                        "stream_type": "main",
                    }
                    for index in range(first, first + 250)
                ]
            ).execute()
        self.recording("current", "archive", start_time=200001)
        Recordings.update(end_time=200009).where(Recordings.id == "current").execute()
        query = ReviewSegment.select(ReviewSegment, review_storage_column())
        sql, params = query.sql()
        plan = [
            row[3] for row in self.db.execute_sql("EXPLAIN QUERY PLAN " + sql, params)
        ]
        searches = [
            line for line in plan if "recordings_camera_stream_type_start_time" in line
        ]
        self.assertTrue(searches)
        self.assertTrue(
            all("start_time>?" in line and "start_time<?" in line for line in searches)
        )
        steps = 0

        def progress():
            nonlocal steps
            steps += 100
            return int(steps > 10000)

        self.db.connection().set_progress_handler(progress, 100)
        try:
            self.assertEqual(query.dicts().get()["recording_storage"], "archive")
        finally:
            self.db.connection().set_progress_handler(None, 0)
        self.assertLessEqual(steps, 10000)

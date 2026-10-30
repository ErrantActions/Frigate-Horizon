"""Keep the live oldest-first archive selection indexed and deletion-safe."""

import time
import unittest
from unittest.mock import patch

from peewee import SqliteDatabase

from frigate.models import Recordings
from frigate.record.archive_limits import plan_archive_batch
from frigate.record.archive_store import archive_candidates


class TestArchiveSelection(unittest.TestCase):
    def setUp(self):
        self.db = SqliteDatabase(":memory:")
        self.enterContext(self.db.bind_ctx([Recordings]))
        self.db.create_tables([Recordings])
        self.addCleanup(self.db.close)
        self.db.execute_sql(
            "CREATE INDEX IF NOT EXISTS recordings_tier_start "
            "ON recordings(storage_tier,start_time)"
        )
        self.db.execute_sql(
            "CREATE INDEX IF NOT EXISTS recordings_tier_expiration "
            "ON recordings(storage_tier,delete_requested,end_time)"
        )
        now = time.time()
        for index in range(64):
            Recordings.create(
                id=str(index),
                camera="synthetic",
                path=f"never-opened-{index}.mp4",
                start_time=now - 200 + index,
                end_time=now - 190 + index,
                duration=10,
                delete_requested=index == 0,
                storage_tier="archive" if index == 1 else "local",
            )

    def test_exact_batch_query_uses_ordered_index_without_temporary_sort(self):
        query = archive_candidates(16)
        sql, args = query.sql()
        plan = " ".join(
            row[3] for row in self.db.execute_sql("EXPLAIN QUERY PLAN " + sql, args)
        )
        self.assertIn("recordings_tier_start", plan)
        self.assertNotIn("TEMP B-TREE", plan)

    def test_oldest_first_skips_deletion_claims_and_archived_rows(self):
        self.assertEqual([row.id for row in archive_candidates(3)], ["2", "3", "4"])

    def test_invalid_batch_bounds_are_explicit_errors(self):
        for value in (0, -1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                archive_candidates(value)

    def test_plan_uses_ordered_index_and_stops_at_required_prefix(self):
        Recordings.update(segment_size=1).execute()
        with patch.object(self.db, "execute_sql", wraps=self.db.execute_sql) as execute:
            self.assertEqual(plan_archive_batch(2**20), (2**20, 1))
        sql, args = execute.call_args.args
        plan = " ".join(
            row[3] for row in self.db.execute_sql("EXPLAIN QUERY PLAN " + sql, args)
        )
        self.assertIn("recordings_tier_start", plan)
        self.assertNotIn("TEMP B-TREE", plan)
        self.assertNotIn("OVER", sql)
        self.assertNotIn("SUM", sql)

    def test_empty_and_invalid_backlog(self):
        with patch.object(
            self.db, "execute_sql", side_effect=AssertionError("Unnecessary scan")
        ):
            self.assertEqual(plan_archive_batch(0), (0, 0))
            for size in (-1, True, 1.5):
                with self.assertRaises(ValueError):
                    plan_archive_batch(size)

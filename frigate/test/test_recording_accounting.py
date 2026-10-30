"""Stored recording sizes retain byte precision, including short camera clips."""

import datetime
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

if os.name != "nt":
    from frigate.record.maintainer import RecordingMaintainer, SegmentInfo


@unittest.skipIf(
    os.name == "nt", "Recording imports require the supported Linux runtime"
)
class TestRecordingAccounting(unittest.IsolatedAsyncioTestCase):
    async def store(self, stored_bytes):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        cache = root / "cache.mp4"
        cache.write_bytes(b"cache is not the final faststart file")
        recordings = root / "recordings"
        recordings.mkdir()
        worker = RecordingMaintainer.__new__(RecordingMaintainer)
        worker.config = SimpleNamespace(ffmpeg=SimpleNamespace(ffmpeg_path="ffmpeg"))
        worker.end_time_cache = {}

        async def convert(*args, **kwargs):
            Path(args[-1]).write_bytes(b"x" * stored_bytes)
            self.assertEqual(args[args.index("-c") + 1], "copy")
            return SimpleNamespace(wait=AsyncMock(), returncode=0)

        start = datetime.datetime(2026, 10, 29, tzinfo=datetime.UTC)
        with (
            patch("frigate.record.maintainer.RECORD_DIR", str(recordings)),
            patch("frigate.record.maintainer.chown_to_runtime"),
            patch(
                "frigate.record.maintainer.asyncio.create_subprocess_exec",
                side_effect=convert,
            ),
        ):
            result = await worker.move_segment(
                "front",
                "main",
                start,
                start + datetime.timedelta(seconds=0.3),
                0.3,
                str(cache),
                SegmentInfo(0, 0, 0, 0),
            )
        self.assertIsNotNone(result)
        self.assertFalse(cache.exists())
        self.assertEqual(Path(result["path"]).stat().st_size, stored_bytes)
        return result

    async def test_short_clip_is_not_rounded_to_unknown_zero_mib(self):
        result = await self.store(4767)
        self.assertEqual(result["segment_size"], 4767 / 2**20)
        self.assertGreater(result["segment_size"], 0)
        self.assertLess(result["segment_size"], 0.005)

    async def test_accounting_measures_final_stored_file_not_cache_estimate(self):
        result = await self.store(2**20 + 17)
        self.assertEqual(result["segment_size"], 1 + 17 / 2**20)

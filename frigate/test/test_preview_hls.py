"""HLS preview mappings remain camera-scoped and never rewrite stored media."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


@unittest.skipIf(os.name == "nt", "Full media API imports require Linux")
class TestPreviewHls(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from frigate.api import media

        self.media = media
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.preview = self.root / "previews" / "front" / "preview.mp4"
        self.preview.parent.mkdir(parents=True)
        self.preview.write_bytes(b"synthetic-preview-mapping-fixture")
        self.row = SimpleNamespace(path=str(self.preview), duration=5.0)
        self.request = SimpleNamespace(
            app=SimpleNamespace(
                frigate_config=SimpleNamespace(cameras={"front": object()})
            )
        )
        self.enterContext(patch.object(media, "CLIPS_DIR", str(self.root)))
        self.lookup = self.enterContext(
            patch.object(media.Previews, "get", return_value=self.row)
        )

    async def test_maps_only_the_existing_preview_without_modifying_it(self):
        import json

        original = self.preview.read_bytes()
        response = await self.media.vod_preview(self.request, "front", 100.0, 105.0)
        data = json.loads(response.body)
        self.assertEqual(data["durations"], [5000])
        self.assertEqual(
            data["sequences"],
            [{"clips": [{"type": "source", "path": str(self.preview.resolve())}]}],
        )
        self.assertEqual(self.preview.read_bytes(), original)
        self.lookup.assert_called_once()

    async def test_unconfigured_camera_and_invalid_ranges_do_not_query_media(self):
        from fastapi import HTTPException

        for camera, after, before, status in (
            ("missing", 100.0, 105.0, 404),
            ("front", 105.0, 100.0, 422),
            ("front", float("nan"), 105.0, 422),
        ):
            with self.subTest(camera=camera, after=after):
                with self.assertRaises(HTTPException) as error:
                    await self.media.vod_preview(self.request, camera, after, before)
                self.assertEqual(error.exception.status_code, status)
        self.lookup.assert_not_called()

    async def test_database_path_cannot_escape_this_camera_preview_directory(self):
        from fastapi import HTTPException

        outside = self.root / "outside.mp4"
        outside.write_bytes(b"not-a-camera-preview")
        self.row.path = str(outside)
        with self.assertRaises(HTTPException) as error:
            await self.media.vod_preview(self.request, "front", 100.0, 105.0)
        self.assertEqual(error.exception.status_code, 404)

    async def test_missing_preview_is_an_explicit_failure(self):
        from fastapi import HTTPException
        from peewee import DoesNotExist

        self.lookup.side_effect = DoesNotExist
        with self.assertRaises(HTTPException) as error:
            await self.media.vod_preview(self.request, "front", 100.0, 105.0)
        self.assertEqual(error.exception.status_code, 404)

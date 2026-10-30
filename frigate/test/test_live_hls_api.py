"""Live HLS routes retain authentication and camera checks on every resource."""

import sys
import unittest
from unittest.mock import AsyncMock

from frigate.test.test_live_hls import configuration


@unittest.skipIf(
    sys.platform == "win32", "Frigate authentication imports require Linux"
)
class TestLiveHlsApi(unittest.TestCase):
    def setUp(self):
        from fastapi import FastAPI, HTTPException
        from fastapi.testclient import TestClient

        from frigate.api.auth import require_camera_access
        from frigate.api.live_hls import router
        from frigate.comms.live_hls import LiveHlsManager

        app = FastAPI()
        self.config = configuration()
        self.config.ui.live_hls = True
        app.frigate_config = self.config
        self.manager = AsyncMock(spec=LiveHlsManager)
        self.manager.master.return_value = "#EXTM3U\n#EXT-X-VERSION:10\n"
        self.manager.fetch.return_value = b"private-part"
        app.state.live_hls_manager = self.manager
        app.include_router(router)
        app.dependency_overrides[router.dependencies[0].dependency] = lambda: None

        async def camera_access(camera_name: str):
            if camera_name != "front":
                raise HTTPException(403, "camera_not_allowed")

        app.dependency_overrides[require_camera_access] = camera_access
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_manual_profile_status_and_master_are_private(self):
        status = self.client.get("/front/live/hls/status")
        self.assertEqual(status.status_code, 200)
        self.assertTrue(status.json()["available"])
        self.assertEqual(status.headers["cache-control"], "private, no-store")
        master = self.client.get("/front/live/hls/master.m3u8")
        self.assertEqual(master.status_code, 200)
        self.assertTrue(master.text.startswith("#EXTM3U"))
        self.assertEqual(master.headers["cache-control"], "private, no-store")
        self.assertEqual(
            master.headers["content-type"], "application/vnd.apple.mpegurl"
        )

    def test_openapi_documents_playlist_and_fragment_response_types(self):
        paths = self.client.get("/openapi.json").json()["paths"]
        master = paths["/{camera_name}/live/hls/master.m3u8"]["get"]
        resource = paths["/{camera_name}/live/hls/{height}p/{resource}"]["get"]
        self.assertEqual(
            set(master["responses"]["200"]["content"]),
            {"application/vnd.apple.mpegurl"},
        )
        self.assertEqual(
            set(resource["responses"]["200"]["content"]),
            {"application/vnd.apple.mpegurl", "video/mp4"},
        )

    def test_all_routes_recheck_camera_access_before_touching_media(self):
        for path in (
            "/other/live/hls/status",
            "/other/live/hls/master.m3u8",
            "/other/live/hls/720p/part0.mp4",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 403)
        self.manager.fetch.assert_not_awaited()
        self.manager.master.assert_not_awaited()

    def test_unadvertised_rungs_and_invalid_resources_never_reach_packager(self):
        for path in (
            "/front/live/hls/360p/part0.mp4",
            "/front/live/hls/720p/a..mp4",
            "/front/live/hls/720p/%2E%2E%2Finit.mp4",
            "/front/live/hls/720p/file.txt",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)
        self.manager.fetch.assert_not_awaited()

    def test_only_bounded_llhls_queries_are_forwarded(self):
        result = self.client.get(
            "/front/live/hls/720p/video1_stream.m3u8"
            "?_HLS_msn=42&_HLS_part=2&_HLS_skip=YES&upstream=https://outside.invalid"
        )
        self.assertEqual(result.status_code, 200)
        self.manager.fetch.assert_awaited_once_with(
            "front",
            720,
            "video1_stream.m3u8",
            {"_HLS_msn": "42", "_HLS_part": "2", "_HLS_skip": "YES"},
        )
        self.assertEqual(result.headers["cache-control"], "private, no-store")

    def test_invalid_reload_sequences_are_explicit_errors(self):
        for query, code in (
            ("_HLS_part=1", 400),
            ("_HLS_msn=-1", 422),
            ("_HLS_msn=1&_HLS_part=1000001", 422),
            ("_HLS_skip=invalid", 422),
        ):
            with self.subTest(query=query):
                result = self.client.get(
                    "/front/live/hls/720p/video1_stream.m3u8?" + query
                )
                self.assertEqual(result.status_code, code)
        self.manager.fetch.assert_not_awaited()

    def test_disabled_feature_and_camera_do_not_serve_old_parts(self):
        self.config.ui.live_hls = False
        status = self.client.get("/front/live/hls/status")
        self.assertFalse(status.json()["available"])
        self.assertEqual(
            self.client.get("/front/live/hls/720p/part0.mp4").status_code, 503
        )
        self.config.ui.live_hls = True
        self.config.cameras["front"].enabled = False
        self.assertEqual(
            self.client.get("/front/live/hls/720p/part0.mp4").status_code, 404
        )
        self.manager.fetch.assert_not_awaited()

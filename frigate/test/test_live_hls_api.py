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
        from fastapi import Depends, FastAPI, HTTPException
        from fastapi.testclient import TestClient

        from frigate.api.auth import require_admin_by_default, require_camera_access
        from frigate.api.live_hls import router
        from frigate.comms.live_hls import LiveHlsManager

        app = FastAPI(dependencies=[Depends(require_admin_by_default())])
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

    def test_video_only_and_pinned_rung_remain_camera_scoped(self):
        response = self.client.get(
            "/front/live/hls/master.m3u8?audio=false&height=1080"
        )
        self.assertEqual(response.status_code, 200)
        args = self.manager.master.await_args
        self.assertEqual(args.args[0], "front")
        self.assertEqual([value.height for value in args.args[1]], [1080])
        self.assertEqual(args.kwargs, {"audio": False})

    def test_viewport_cap_filters_only_advertised_renditions(self):
        response = self.client.get("/front/live/hls/master.m3u8?max_height=720")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [value.height for value in self.manager.master.await_args.args[1]],
            [720],
        )

    def test_unavailable_master_selection_does_not_start_the_packager(self):
        for query in ("height=360", "max_height=200", "audio=invalid"):
            with self.subTest(query=query):
                response = self.client.get("/front/live/hls/master.m3u8?" + query)
                self.assertIn(response.status_code, (404, 422))
        self.manager.master.assert_not_awaited()

    def test_single_rung_remains_available_without_an_alternate_transport(self):
        del self.config.cameras["front"].live.streams["1080p"]
        response = self.client.get("/front/live/hls/status")
        self.assertTrue(response.json()["available"])
        self.assertEqual(
            [value["height"] for value in response.json()["renditions"]], [720]
        )

    def test_birdseye_does_not_bypass_the_existing_camera_access_guard(self):
        self.config.birdseye.enabled = True
        self.config.birdseye.restream = True
        for suffix in ("status", "master.m3u8", "720p/init.mp4"):
            with self.subTest(suffix=suffix):
                response = self.client.get("/birdseye/live/hls/" + suffix)
                self.assertEqual(response.status_code, 403)
        self.manager.master.assert_not_awaited()
        self.manager.fetch.assert_not_awaited()

    def test_birdseye_camera_guard_can_allow_full_scope_without_admin_privileges(self):
        from frigate.api.auth import require_camera_access

        self.config.birdseye.enabled = True
        self.config.birdseye.restream = True
        self.client.app.dependency_overrides[require_camera_access] = lambda: None
        response = self.client.get(
            "/birdseye/live/hls/status",
            headers={"remote-user": "viewer", "remote-role": "viewer"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["available"])

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
            warm_renditions=(),
        )
        self.assertEqual(result.headers["cache-control"], "private, no-store")

    def test_adaptive_warming_is_bounded_by_current_camera_plan(self):
        response = self.client.get(
            "/front/live/hls/720p/video1_stream.m3u8?warm_max_height=1080"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [
                value.height
                for value in self.manager.fetch.await_args.kwargs["warm_renditions"]
            ],
            [720, 1080],
        )
        self.assertEqual(self.manager.fetch.await_args.args[3], {})
        self.manager.fetch.reset_mock()
        for query, height, expected in (
            ("warm_max_height=2161", 720, 422),
            ("warm_max_height=invalid", 720, 422),
            ("warm_max_height=720", 1080, 400),
        ):
            with self.subTest(query=query):
                result = self.client.get(
                    f"/front/live/hls/{height}p/video1_stream.m3u8?{query}"
                )
                self.assertEqual(result.status_code, expected)
        self.manager.fetch.assert_not_awaited()

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

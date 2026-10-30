"""Private HLS proxy limits, lifecycle, errors and credential isolation."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from frigate.comms.live_hls import MAX_RESOURCE_BYTES, LiveHlsError, LiveHlsManager
from frigate.test.test_live_hls import PRIVATE_MASTER, configuration


class TestLiveHlsManager(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = configuration()
        self.config.ui.live_hls = True
        self.manager = LiveHlsManager(
            lambda: self.config, Path(self.directory.name) / "mediamtx.yml"
        )
        self.addAsyncCleanup(self.manager.close)
        await self.manager.start()

    async def transport(self, handler, *, bootstrap=True):
        await self.manager.client.aclose()

        def dispatch(request):
            if bootstrap and request.url.path.endswith("/index.m3u8"):
                return httpx.Response(200, text=PRIVATE_MASTER)
            return handler(request)

        self.manager.client = httpx.AsyncClient(
            transport=httpx.MockTransport(dispatch),
            follow_redirects=False,
            trust_env=False,
        )

    async def test_upstream_is_fixed_local_and_only_explicit_hls_queries_are_sent(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, content=b"test-part")

        await self.transport(handle)
        result = await self.manager.fetch(
            "front", 720, "part1.mp4", {"_HLS_msn": "12", "_HLS_part": "1"}
        )
        self.assertEqual(result, b"test-part")
        request = requests[0]
        self.assertEqual(request.url.host, "127.0.0.1")
        self.assertEqual(request.url.port, 8888)
        self.assertEqual(request.url.params["_HLS_msn"], "12")
        self.assertNotIn("cookie", request.headers)
        self.assertNotIn("authorization", request.headers)
        self.assertEqual(request.url.params["session"], "private-test-1")

    async def test_redirects_cannot_retarget_the_proxy(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(302, headers={"Location": "https://example.invalid/"})

        await self.transport(handle)
        with self.assertRaises(LiveHlsError) as error:
            await self.manager.fetch("front", 720, "init.mp4", {})
        self.assertEqual(error.exception.status, 502)
        self.assertEqual(len(requests), 1)

    async def test_bootstrap_uses_cookieless_sessions_without_following_redirects(self):
        requests = []

        def handle(request):
            requests.append(request)
            if request.url.params.get("cookieCheck") != "1":
                return httpx.Response(
                    302,
                    headers={
                        "Location": str(request.url.copy_add_param("cookieCheck", "1"))
                    },
                )
            return httpx.Response(200, text=PRIVATE_MASTER)

        await self.transport(handle, bootstrap=False)
        master = await self.manager.bootstrap("front", 720)
        self.assertEqual(master.session, "private-test-1")
        self.assertEqual(len(requests), 1)
        self.assertNotIn("cookie", requests[0].headers)
        self.assertFalse(self.manager.client.follow_redirects)

    async def test_bootstrap_still_rejects_redirects_instead_of_following_them(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(302, headers={"Location": "https://example.invalid/"})

        await self.transport(handle, bootstrap=False)
        with self.assertRaises(LiveHlsError) as error:
            await self.manager.bootstrap("front", 720)
        self.assertEqual(error.exception.status, 502)
        self.assertEqual(len(requests), 1)

    async def test_resource_limit_is_enforced_before_returning_bytes(self):
        await self.transport(
            lambda request: httpx.Response(200, content=b"x" * (MAX_RESOURCE_BYTES + 1))
        )
        with self.assertRaises(LiveHlsError) as error:
            await self.manager.fetch("front", 720, "part1.mp4", {})
        self.assertEqual(error.exception.code, "live_hls_resource_too_large")

    async def test_missing_resource_is_not_a_success_shaped_empty_file(self):
        await self.transport(lambda request: httpx.Response(404))
        with self.assertRaises(LiveHlsError) as error:
            await self.manager.fetch("front", 720, "part1.mp4", {})
        self.assertEqual(error.exception.status, 404)

    async def test_connection_and_timeout_failures_are_explicit(self):
        for exception, status in (
            (httpx.ConnectError("unavailable"), 503),
            (httpx.ReadTimeout("timeout"), 504),
        ):

            def handle(request):
                raise exception

            await self.transport(handle)
            with (
                self.subTest(exception=type(exception)),
                self.assertRaises(LiveHlsError) as error,
            ):
                await self.manager.fetch("front", 720, "stream.m3u8", {})
            self.assertEqual(error.exception.status, status)

    async def test_unchanged_plans_do_not_rewrite_the_config_for_every_part(self):
        with patch("frigate.comms.live_hls.write_private_json") as write:
            await self.manager.sync_config()
            await self.manager.sync_config()
        write.assert_not_called()

    async def test_enabled_camera_changes_update_the_packager_paths(self):
        self.config.cameras["front"].enabled = False
        await self.manager.sync_config()
        self.assertEqual(self.manager.last_config["paths"], {})

    async def test_sessions_are_shared_for_concurrent_requests_for_the_same_rung(self):
        import asyncio

        calls = []

        def handle(request):
            calls.append(request)
            if request.url.path.endswith("/index.m3u8"):
                return httpx.Response(200, text=PRIVATE_MASTER)
            return httpx.Response(200, content=b"part")

        await self.transport(handle, bootstrap=False)
        await asyncio.gather(
            self.manager.fetch("front", 720, "part1.mp4", {}),
            self.manager.fetch("front", 720, "part2.mp4", {}),
        )
        self.assertEqual(
            sum(request.url.path.endswith("/index.m3u8") for request in calls), 1
        )

    async def test_expired_upstream_session_is_renewed_once_without_logging_client_out(
        self,
    ):
        calls = []

        def handle(request):
            calls.append(request)
            if request.url.path.endswith("/index.m3u8"):
                session = "renewed" if len(calls) > 1 else "private-test-1"
                return httpx.Response(
                    200, text=PRIVATE_MASTER.replace("private-test-1", session)
                )
            if request.url.params["session"] == "private-test-1":
                return httpx.Response(401)
            return httpx.Response(200, content=b"part")

        await self.transport(handle, bootstrap=False)
        self.assertEqual(
            await self.manager.fetch("front", 720, "part1.mp4", {}), b"part"
        )
        self.assertEqual(len(calls), 4)

    async def test_config_error_does_not_abort_unrelated_recorder_startup(self):
        await self.manager.close()
        other = LiveHlsManager(
            lambda: self.config, Path(self.directory.name) / "unwritable.yml"
        )
        self.addAsyncCleanup(other.close)
        with patch(
            "frigate.comms.live_hls.write_private_json", side_effect=PermissionError
        ):
            await other.start()
        self.assertEqual(other.startup_error.code, "live_hls_configuration_unavailable")
        await other.sync_config()
        self.assertIsNone(other.startup_error)

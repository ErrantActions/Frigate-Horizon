"""HTTP coverage using real role and camera dependencies, no production DB."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import Depends, FastAPI

from frigate.api.auth import require_admin_by_default
from frigate.api.playback import router
from frigate.record.playback import PlaybackManager, SourceClip
from frigate.test.test_playback import PlaybackDirectory, body, write_clip


class TestPlaybackHttp(PlaybackDirectory, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.create_directory()
        self.manager = PlaybackManager("configured-ffmpeg", str(self.directory))
        await self.manager.start()
        self.app = FastAPI(dependencies=[Depends(require_admin_by_default())])
        self.app.frigate_config = SimpleNamespace(
            cameras={"front": SimpleNamespace(), "back": SimpleNamespace()},
            auth=SimpleNamespace(
                roles={"admin": [], "viewer": [], "reviewer": ["front"]}
            ),
        )
        self.app.playback_manager = self.manager
        self.app.include_router(router)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="https://frigate.invalid",
            headers={"remote-user": "alice", "remote-role": "reviewer"},
        )
        self.planner = patch(
            "frigate.record.playback._load_sources",
            return_value=[SourceClip("private-source.mp4", 1000, 2, 0, True)],
        )
        self.planner.start()

        async def encode(session, clip, index):
            write_clip(session.directory, index)

        self.encoder = patch.object(self.manager, "_encode", side_effect=encode)
        self.encoder.start()
        self.base = "/front/recordings/playback-compatibility"

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.manager.close()
        self.encoder.stop()
        self.planner.stop()
        self.remove_directory()

    async def create(self):
        """Return the response and wait for deterministic synthetic output."""
        response = await self.client.post(self.base, json=body().model_dump())
        self.assertEqual(response.status_code, 202, response.text)
        session = self.manager.sessions[response.json()["id"]]
        await session.task
        return response.json(), session

    async def test_custom_role_full_session_and_private_media(self):
        created, _ = await self.create()
        path = f"{self.base}/{created['id']}"
        response = await self.client.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "complete")
        self.assertEqual(response.json()["generation"], 0)
        self.assertEqual(response.json()["client_id"], "review-player")
        self.assertEqual(response.json()["timeline"][0]["measured"], True)
        self.assertEqual(response.json()["timeline"][0]["lead_in_ms"], 0)
        manifest = await self.client.get("/" + created["manifest_path"])
        self.assertEqual(manifest.status_code, 200)
        self.assertIn("private, no-store", manifest.headers["cache-control"])
        self.assertIn("#EXT-X-ENDLIST", manifest.text)
        segment = await self.client.get(path + "/media/p0-000.ts")
        self.assertEqual(segment.content, b"synthetic segment")
        self.assertEqual((await self.client.post(path + "/keepalive")).status_code, 200)
        self.assertEqual((await self.client.delete(path)).status_code, 204)
        self.assertEqual((await self.client.get(path)).status_code, 404)

    async def test_global_review_switch_blocks_creation_and_existing_media_but_allows_cleanup(
        self,
    ):
        created, session = await self.create()
        self.manager.compatibility_enabled = lambda: False
        last_touched = session.touched
        path = f"{self.base}/{created['id']}"
        for method, url, kwargs in (
            ("POST", self.base, {"json": body().model_dump()}),
            ("GET", path, {}),
            ("POST", path + "/keepalive", {}),
            ("GET", path + "/media/p0-000.ts", {}),
        ):
            response = await self.client.request(method, url, **kwargs)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(
                response.json()["detail"], "playback_compatibility_disabled"
            )
        self.assertEqual(session.touched, last_touched)
        self.assertEqual((await self.client.delete(path)).status_code, 204)
        self.assertNotIn(session.id, self.manager.sessions)

    async def test_every_operation_rechecks_camera_access_and_owner(self):
        created, _ = await self.create()
        path = f"{self.base}/{created['id']}"
        operations = (
            ("GET", path),
            ("POST", path + "/keepalive"),
            ("GET", "/" + created["manifest_path"]),
            ("GET", path + "/media/p0-000.ts"),
            ("DELETE", path),
        )
        for method, url in operations:
            with self.subTest(method=method, url=url):
                response = await self.client.request(
                    method, url, headers={"remote-user": "bob"}
                )
                self.assertEqual(response.status_code, 404)
        self.app.frigate_config.auth.roles["reviewer"] = ["back"]
        for method, url in operations:
            with self.subTest(revoked=method, url=url):
                self.assertEqual(
                    (await self.client.request(method, url)).status_code, 403
                )

    async def test_cross_camera_unknown_resources_and_path_traversal(self):
        created, _ = await self.create()
        path = f"{self.base}/{created['id']}"
        forbidden = await self.client.post(
            "/back/recordings/playback-compatibility", json=body().model_dump()
        )
        self.assertEqual(forbidden.status_code, 403)
        cross_camera = await self.client.get(
            f"/back/recordings/playback-compatibility/{created['id']}",
            headers={"remote-role": "admin"},
        )
        self.assertEqual(cross_camera.status_code, 404)
        for resource in (
            "p1-000.ts",
            "p0.m3u8",
            "%2e%2e%2fprivate-source.mp4",
            "https:%2f%2fexample.invalid/source",
            "p0-000.ts%00",
        ):
            with self.subTest(resource=resource):
                self.assertEqual(
                    (await self.client.get(path + "/media/" + resource)).status_code,
                    404,
                )

    async def test_missing_auth_and_invalid_window_and_not_started(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="https://frigate.invalid",
        ) as anonymous:
            response = await anonymous.post(self.base, json=body().model_dump())
            self.assertEqual(response.status_code, 401)
        response = await self.client.post(
            self.base, json={**body().model_dump(), "before": 10000}
        )
        self.assertEqual(response.status_code, 422)
        del self.app.playback_manager
        response = await self.client.post(self.base, json=body().model_dump())
        self.assertEqual(response.status_code, 503)

    async def test_preparing_does_not_serve_playlist_before_requested_start(self):
        started = asyncio.Event()
        finish = asyncio.Event()

        async def encode(session, clip, index):
            started.set()
            await finish.wait()
            write_clip(session.directory, index)

        with patch.object(self.manager, "_encode", side_effect=encode):
            response = await self.client.post(self.base, json=body().model_dump())
            await asyncio.wait_for(started.wait(), 2)
            status = response.json()
            self.assertEqual(status["state"], "preparing")
            self.assertEqual(status["timeline"], [])
            response = await self.client.get("/" + status["manifest_path"])
            self.assertIn(response.status_code, (404, 425))
            finish.set()
            await self.manager.sessions[status["id"]].task

    async def test_stale_create_and_old_delete_cannot_replace_new_generation(self):
        created, _ = await self.create()
        newer = await self.client.post(
            self.base, json=body(generation=1, start_timestamp=1001.5).model_dump()
        )
        self.assertEqual(newer.status_code, 202, newer.text)
        new_id = newer.json()["id"]
        await self.manager.sessions[new_id].task
        stale = await self.client.post(self.base, json=body().model_dump())
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["detail"], "playback_generation_superseded")
        self.assertEqual(
            (await self.client.delete(f"{self.base}/{created['id']}")).status_code,
            204,
        )
        status = await self.client.get(f"{self.base}/{new_id}")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["generation"], 1)
        self.assertEqual(status.json()["state"], "complete")

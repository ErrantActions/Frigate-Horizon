"""Stored-playback lifecycle wiring without cache writes, workers, or cameras."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from frigate.api.fastapi_app import create_fastapi_app
from frigate.config import FrigateConfig


class TestPlaybackSharedLifespan(unittest.IsolatedAsyncioTestCase):
    def create(self, playback):
        config = FrigateConfig(
            mqtt={"enabled": False},
            auth={"enabled": False},
            cameras={
                "front": {
                    "ffmpeg": {
                        "inputs": [
                            {"path": "rtsp://camera.invalid/test", "roles": ["detect"]}
                        ]
                    },
                    "detect": {"width": 640, "height": 480, "fps": 5},
                }
            },
        )
        owner = SimpleNamespace(
            start=MagicMock(),
            stop=MagicMock(),
            join=MagicMock(),
            is_alive=MagicMock(return_value=False),
            _ready=SimpleNamespace(wait=MagicMock(return_value=True)),
        )
        replay = SimpleNamespace(shutdown=AsyncMock())
        audio_repair = SimpleNamespace(start=AsyncMock(), close=AsyncMock())
        compression = SimpleNamespace(start=AsyncMock(), close=AsyncMock())
        with (
            patch(
                "frigate.api.fastapi_app.PlaybackManager", return_value=playback
            ) as constructor,
            patch("frigate.api.fastapi_app.OnvifDeviceManager", return_value=owner),
            patch("frigate.api.fastapi_app.OnvifReplayManager", return_value=replay),
            patch(
                "frigate.api.fastapi_app.AudioRepairManager", return_value=audio_repair
            ),
            patch(
                "frigate.api.fastapi_app.CompressionManager", return_value=compression
            ),
            patch("frigate.api.fastapi_app.GenAIClientManager", MagicMock()),
            patch(
                "frigate.api.fastapi_app.get_jwt_secret",
                return_value="synthetic-instance-key-for-lifecycle-tests-only",
            ),
        ):
            app = create_fastapi_app(
                config,
                MagicMock(),
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                MagicMock(),
            )
        constructor.assert_called_once()
        self.assertIs(app.state.native_export_protection, False)
        self.assertEqual(constructor.call_args.args, (config.ffmpeg.ffmpeg_path,))
        hardware = constructor.call_args.kwargs["get_hwaccel_args"]
        self.assertEqual(hardware("front"), config.cameras["front"].ffmpeg.hwaccel_args)
        config.cameras["front"].ffmpeg.hwaccel_args = "preset-nvidia"
        self.assertEqual(hardware("front"), "preset-nvidia")
        return app, owner, replay

    async def test_existing_single_lifespan_starts_and_closes_stored_playback(self):
        playback = SimpleNamespace(start=AsyncMock(), close=AsyncMock())
        app, owner, replay = self.create(playback)
        self.assertIn(
            "/{camera_name}/recordings/playback-compatibility",
            app.openapi()["paths"],
        )
        playback.start.assert_not_awaited()
        with patch(
            "frigate.api.fastapi_app.debug_replay_auto_stop_watchdog", AsyncMock()
        ):
            async with app.router.lifespan_context(app):
                playback.start.assert_awaited_once()
                app.state.audio_repair_manager.start.assert_awaited_once()
                owner.start.assert_called_once()
                playback.close.assert_not_awaited()
        playback.close.assert_awaited_once()
        app.state.audio_repair_manager.close.assert_awaited_once()
        replay.shutdown.assert_awaited_once()
        owner.stop.assert_called_once()

    async def test_start_failure_still_closes_playback_and_does_not_start_device_thread(
        self,
    ):
        playback = SimpleNamespace(
            start=AsyncMock(side_effect=RuntimeError("synthetic cache failure")),
            close=AsyncMock(),
        )
        app, owner, replay = self.create(playback)
        with self.assertRaises(RuntimeError):
            async with app.router.lifespan_context(app):
                pass
        playback.close.assert_awaited_once()
        app.state.audio_repair_manager.start.assert_not_awaited()
        app.state.audio_repair_manager.close.assert_awaited_once()
        replay.shutdown.assert_awaited_once()
        owner.start.assert_not_called()

    async def test_native_close_failure_does_not_skip_other_services_or_shared_cleanup(
        self,
    ):
        for failing in ("native_health", "native_controls"):
            with self.subTest(failing=failing):
                playback = SimpleNamespace(start=AsyncMock(), close=AsyncMock())
                app, owner, replay = self.create(playback)
                for name in ("native_health", "native_controls"):
                    setattr(
                        app.state,
                        name,
                        SimpleNamespace(
                            start=MagicMock(),
                            close=AsyncMock(
                                side_effect=RuntimeError(
                                    "synthetic native close failure"
                                )
                                if name == failing
                                else None
                            ),
                        ),
                    )
                app.state.plugins = SimpleNamespace(
                    start=AsyncMock(), close=AsyncMock()
                )
                with patch(
                    "frigate.api.fastapi_app.debug_replay_auto_stop_watchdog",
                    AsyncMock(),
                ):
                    with self.assertRaisesRegex(
                        RuntimeError, "synthetic native close failure"
                    ):
                        async with app.router.lifespan_context(app):
                            pass
                for name in (
                    "native_health",
                    "native_controls",
                    "plugins",
                ):
                    getattr(app.state, name).close.assert_awaited_once()
                playback.close.assert_awaited_once()
                app.state.audio_repair_manager.close.assert_awaited_once()
                app.state.compression_manager.close.assert_awaited_once()
                replay.shutdown.assert_awaited_once()
                owner.stop.assert_called_once()

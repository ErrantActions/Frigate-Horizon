"""Keep plugin subprocess startup on the standard asyncio event loop."""

import unittest
from unittest.mock import MagicMock, patch, sentinel

from frigate.app import FrigateApp


class TestPluginApiLoop(unittest.TestCase):
    def test_api_uses_standard_event_loop(self):
        app = MagicMock()
        with (
            patch("frigate.app.set_file_limit"),
            patch("frigate.app.cleanup_replay_cameras"),
            patch("frigate.app.reap_stale_exports"),
            patch("frigate.app.create_fastapi_app", return_value=sentinel.api),
            patch("frigate.app.uvicorn.run") as run,
        ):
            FrigateApp.start(app)

        run.assert_called_once_with(
            sentinel.api,
            host="127.0.0.1",
            port=5001,
            loop="asyncio",
            log_level="error",
        )
        app.stop.assert_called_once()

"""Global compatibility policy applies independently and before live authorization."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError
from starlette.requests import Request

from frigate.api.auth import (
    _is_existing_go2rtc_stream,
    deny_response_for_go2rtc_stream,
)
from frigate.config import FrigateConfig
from frigate.config.ui import UIConfig


class TestCompatibilityControls(unittest.TestCase):
    def test_defaults_and_independent_strict_boolean_controls(self):
        self.assertTrue(UIConfig().live_compatibility)
        self.assertTrue(UIConfig().recording_compatibility)
        live_off = UIConfig(live_compatibility=False)
        self.assertFalse(live_off.live_compatibility)
        self.assertTrue(live_off.recording_compatibility)
        review_off = UIConfig(recording_compatibility=False)
        self.assertTrue(review_off.live_compatibility)
        self.assertFalse(review_off.recording_compatibility)
        for value in ("false", 0, None):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                UIConfig.model_validate({"live_compatibility": value})

    def test_disabled_live_policy_denies_stale_generated_stream_even_for_admin(self):
        config = FrigateConfig(
            mqtt={"enabled": False},
            go2rtc={"streams": {"front": ["rtsp://camera.invalid/main"]}},
            cameras={
                "front": {
                    "ffmpeg": {
                        "inputs": [
                            {"path": "rtsp://camera.invalid/main", "roles": ["detect"]}
                        ]
                    },
                    "detect": {"width": 640, "height": 360, "fps": 5},
                    "live": {
                        "streams": {"Main": "front"},
                        "transcode": {
                            "enabled": True,
                            "qualities": [{"height": 720, "bitrate": 1500}],
                        },
                    },
                }
            },
        )
        request = Request(
            {
                "type": "http",
                "headers": [],
                "app": SimpleNamespace(frigate_config=config),
            }
        )
        self.assertTrue(_is_existing_go2rtc_stream(request, "front_transcode_720p"))
        config.ui.live_compatibility = False
        with patch("frigate.api.auth.requests.get") as remote:
            self.assertFalse(
                _is_existing_go2rtc_stream(request, "front_transcode_720p")
            )
            self.assertEqual(
                deny_response_for_go2rtc_stream(
                    "/live/mse/api/ws?src=front_transcode_720p", "admin", request
                ),
                403,
            )
            self.assertIsNone(
                deny_response_for_go2rtc_stream(
                    "/live/mse/api/ws?src=front", "admin", request
                )
            )
            remote.assert_not_called()

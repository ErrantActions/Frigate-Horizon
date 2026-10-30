"""Camera sample time must precede slow hardware polling and cached REST delivery."""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from frigate.stats.util import stats_snapshot


class TestCameraStatsSampleClock(unittest.TestCase):
    def test_camera_clock_is_captured_before_slow_hardware_collection(self):
        clock = SimpleNamespace(now=1000.0)
        config = MagicMock()
        config.cameras = {}
        config.telemetry.stats.network_bandwidth = False
        hardware = MagicMock()
        hardware.update_stats.side_effect = lambda value: setattr(clock, "now", 1020.0)
        tracking = {
            "camera_metrics": {},
            "detectors": {},
            "started": 0,
            "latest_frigate_version": "unknown",
            "processes": {},
        }
        with (
            patch("frigate.stats.util.time.time", side_effect=lambda: clock.now),
            patch("frigate.stats.util.get_detector_stats", return_value={}),
            patch("frigate.stats.util.embeddings_stats", return_value={}),
            patch(
                "frigate.stats.util.shutil.disk_usage",
                return_value=SimpleNamespace(total=100, used=20, free=80),
            ),
            patch("frigate.stats.util.get_fs_type", return_value="tmpfs"),
            patch("frigate.stats.util.calculate_shm_requirements", return_value={}),
            patch("frigate.stats.util.get_go2rtc_pid", return_value=None),
        ):
            result = stats_snapshot(config, tracking, hardware)
        self.assertEqual(result["camera_sampled_at"], 1000)
        self.assertEqual(result["service"]["last_updated"], 1020)
        hardware.update_stats.assert_called_once()


if __name__ == "__main__":
    unittest.main()

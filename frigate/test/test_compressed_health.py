"""Cutover diagnostics must identify the camera and exact criterion, not a single flowing=False."""

import unittest

from frigate.motion.compressed_health import camera_pipeline_health


def stats():
    return {
        "camera_fps": 5,
        "process_fps": 5,
        "skipped_fps": 0,
        "native_capture": {
            "backend": "experimental_native4k_gated",
            "state": "full",
            "source_width": 3840,
            "source_height": 2160,
            "source_fps": 25,
            "decoded_fps": 5,
            "rendered_frames": 100,
            "sampled_at": 99,
            "last_frame_at": 99.8,
        },
    }


class TestCompressedPipelineHealth(unittest.TestCase):
    def test_healthy_cached_snapshot_does_not_become_a_native_publisher_failure(self):
        health = camera_pipeline_health(
            "camera", stats(), now=119, snapshot_at=100, latest_recording_end=115
        )
        self.assertTrue(health.healthy)
        self.assertEqual(health.snapshot_age_seconds, 19)
        self.assertEqual(health.native_sample_age_seconds, 1)
        self.assertAlmostEqual(health.frame_age_seconds, 0.2)

    def test_publisher_stale_when_collected_is_still_rejected(self):
        value = stats()
        value["native_capture"]["sampled_at"] = 80
        value["native_capture"]["last_frame_at"] = 81
        health = camera_pipeline_health(
            "camera", value, now=119, snapshot_at=100, latest_recording_end=115
        )
        self.assertIn("native_status_stale", health.reasons)
        self.assertIn("native_frames_stale", health.reasons)
        self.assertNotIn("camera_snapshot_stale", health.reasons)

    def test_overall_cached_stats_cannot_remain_successful_forever(self):
        health = camera_pipeline_health(
            "camera", stats(), now=150, snapshot_at=100, latest_recording_end=145
        )
        self.assertIn("camera_snapshot_stale", health.reasons)

    def check(self, value, **kwargs):
        return camera_pipeline_health(
            "camera", value, now=100, latest_recording_end=95, **kwargs
        )

    def test_healthy_native_pipeline_has_exact_rates_without_private_fields(self):
        value = stats()
        value["private_rtsp_url"] = "must not appear"
        health = self.check(value)
        self.assertTrue(health.healthy)
        self.assertEqual(health.reasons, ())
        self.assertEqual(health.source_fps, 25)
        self.assertNotIn("private_rtsp_url", health.to_dict())

    def test_processing_and_capture_failure_are_distinct(self):
        value = stats()
        value["process_fps"] = 0
        health = self.check(value)
        self.assertEqual(health.reasons, ("processing_not_flowing",))
        value["camera_fps"] = 0
        self.assertIn("capture_not_flowing", self.check(value).reasons)

    def test_strict_zero_skip_baseline_is_not_hidden(self):
        value = stats()
        value["skipped_fps"] = 0.1
        health = self.check(value)
        self.assertEqual(health.skipped_limit, 0)
        self.assertEqual(health.reasons, ("capture_skips_exceed_baseline",))
        self.assertTrue(self.check(value, baseline_skipped_fps=0.5).healthy)

    def test_fallback_is_observable_and_does_not_claim_entropy_savings(self):
        value = stats()
        value["native_capture"]["backend"] = "native4k_full_decode_fallback"
        value["native_capture"]["source_fps"] = None
        value["native_capture"]["state"] = "full_fallback"
        self.assertTrue(self.check(value).healthy)
        value["native_capture"]["decoded_fps"] = 3.5
        self.assertEqual(self.check(value).reasons, ("full_decode_fallback_rate_low",))

    def test_source_stall_and_renderer_stall_are_separate_reasons(self):
        value = stats()
        value["native_capture"]["source_fps"] = 0
        value["native_capture"]["last_frame_at"] = 80
        health = self.check(value)
        self.assertIn("entropy_source_rate_low", health.reasons)
        self.assertIn("native_frames_stale", health.reasons)

    def test_missing_or_nonfinite_metrics_cannot_look_healthy(self):
        for value in (None, {}, {"camera_fps": float("nan"), "process_fps": True}):
            with self.subTest(value=value):
                health = self.check(value)
                self.assertFalse(health.healthy)
                self.assertIn("processing_not_flowing", health.reasons)

    def test_legacy_baseline_does_not_require_native_telemetry(self):
        health = self.check(
            {"camera_fps": 5, "process_fps": 5, "skipped_fps": 0},
            require_native=False,
        )
        self.assertTrue(health.healthy)

    def test_recording_freshness_and_timestamp_anomalies_are_explicit(self):
        health = camera_pipeline_health(
            "camera", stats(), now=100, latest_recording_end=0
        )
        self.assertIn("recording_not_fresh", health.reasons)
        value = stats()
        value["native_capture"]["sampled_at"] = 110
        self.assertIn("native_status_stale", self.check(value).reasons)


if __name__ == "__main__":
    unittest.main()

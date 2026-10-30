"""Trial decode decisions preserve unknowns, coded-only changes, native IDR probes and object holds."""

import unittest

from frigate.motion.compressed_policy import NativeDecodePolicy, PixelFeedback
from frigate.motion.compressed_protocol import GRID_CELLS, CompressedEvidence


def evidence(time: float, picture: str = "P", *, coded=0.0, moving=0.0, unknown=None):
    return CompressedEvidence(
        type="frame",
        index=0,
        pts=round(time * 1000),
        relative_seconds=time,
        width=3840,
        height=2160,
        picture_type=picture,
        vector_count=32400,
        moving_vectors=int(moving > 0),
        coverage=1.0,
        vectors_sha256=None,
        uncertainty=unknown,
        time_base=(1, 1000),
        profile_idc=100,
        cabac=1,
        activity=[moving] + [0.0] * (GRID_CELLS - 1),
        coded_activity=[coded] + [0.0] * (GRID_CELLS - 1),
    )


def quiet(time: float):
    return PixelFeedback(time, True, False, False)


class TestNativeDecodePolicy(unittest.TestCase):
    def test_small_vector_and_coded_activity_can_be_filtered_explicitly(self):
        policy = NativeDecodePolicy(startup_seconds=0, activity_threshold=0.01)
        decision = policy.decide(
            evidence(0, coded=0.005, moving=0.005), idr=False, feedback=quiet(0)
        )
        self.assertEqual(decision.action, "quiet")
        larger = policy.decide(
            evidence(0.04, coded=0.03, moving=0), idr=False, feedback=quiet(0.04)
        )
        self.assertEqual(larger.action, "full")

    def test_above_expected_motion_range_is_unknown_not_ignored(self):
        policy = NativeDecodePolicy(startup_seconds=0, activity_threshold=0.1)
        decision = policy.decide(
            evidence(0, unknown="motion_distance_above_expected_range"),
            idr=False,
            feedback=quiet(0),
        )
        self.assertEqual(decision.action, "full")

    def test_compressed_authority_does_not_use_old_pixel_motion_as_wake_signal(self):
        policy = NativeDecodePolicy(startup_seconds=0, use_pixel_motion=False)
        decision = policy.decide(
            evidence(0), idr=False, feedback=PixelFeedback(0, True, True, False)
        )
        self.assertEqual(decision.action, "quiet")
        objects = policy.decide(
            evidence(0.04), idr=False, feedback=PixelFeedback(0.04, True, False, True)
        )
        self.assertEqual(objects.action, "full")

    def test_startup_and_absent_pixel_feedback_require_full_decode(self):
        policy = NativeDecodePolicy()
        self.assertEqual(
            policy.decide(evidence(0), idr=False, feedback=quiet(0)).action, "full"
        )
        policy = NativeDecodePolicy(startup_seconds=0)
        self.assertEqual(
            policy.decide(evidence(0), idr=False, feedback=None).action, "full"
        )

    def test_verified_idr_probes_do_not_force_continuous_decoding(self):
        policy = NativeDecodePolicy(startup_seconds=0)
        first = policy.decide(
            evidence(0, "I", unknown="vectors_unavailable"), idr=True, feedback=quiet(0)
        )
        self.assertEqual(first.action, "idr_probe")
        self.assertEqual(
            policy.decide(evidence(0.04), idr=False, feedback=quiet(0)).action, "quiet"
        )

    def test_coded_only_activity_is_not_misread_as_no_motion(self):
        policy = NativeDecodePolicy(startup_seconds=0)
        decision = policy.decide(evidence(0, coded=0.1), idr=False, feedback=quiet(0))
        self.assertEqual(decision.action, "full")
        self.assertEqual(decision.reason, "vector_or_coded_block_activity")
        self.assertEqual(
            policy.decide(evidence(0.04), idr=False, feedback=quiet(0)).action, "full"
        )

    def test_even_a_single_small_coded_change_prevents_default_sleep(self):
        policy = NativeDecodePolicy(startup_seconds=0)
        decision = policy.decide(
            evidence(0, coded=0.000001), idr=False, feedback=quiet(0)
        )
        self.assertEqual(decision.action, "full")

    def test_unknowns_objects_and_unverified_views_fail_open(self):
        for observed, feedback in (
            (evidence(0, unknown="sparse_vector_coverage"), quiet(0)),
            (evidence(0), PixelFeedback(0, True, False, True)),
            (evidence(0), PixelFeedback(0, False, False, False)),
            (evidence(0), PixelFeedback(0, True, True, False)),
        ):
            with self.subTest(feedback=feedback):
                policy = NativeDecodePolicy(startup_seconds=0)
                self.assertEqual(
                    policy.decide(observed, idr=False, feedback=feedback).action, "full"
                )

    def test_stale_feedback_resets_and_buffer_pressure_prevent_sleep(self):
        policy = NativeDecodePolicy(startup_seconds=0)
        self.assertEqual(
            policy.decide(evidence(10), idr=False, feedback=quiet(0)).action, "full"
        )
        self.assertEqual(
            policy.decide(evidence(0), idr=False, feedback=quiet(0)).reason,
            "source_clock_or_geometry_reset",
        )
        policy = NativeDecodePolicy(startup_seconds=0)
        self.assertEqual(
            policy.decide(
                evidence(0), idr=False, feedback=quiet(0), buffer_pressure=True
            ).reason,
            "compressed_buffer_pressure",
        )


if __name__ == "__main__":
    unittest.main()

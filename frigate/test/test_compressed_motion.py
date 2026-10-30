"""Compressed-feature shadow correctness and fail-open qualification without live side effects."""

import unittest
from dataclasses import replace

import numpy as np

from frigate.motion.compressed import (
    Qualification,
    ShadowDecodeGate,
    ShadowMotionPID,
    vector_evidence,
)

VECTOR_TYPE = np.dtype(
    [
        ("source", "i4"),
        ("w", "i4"),
        ("h", "i4"),
        ("dst_x", "i4"),
        ("dst_y", "i4"),
        ("motion_x", "i4"),
        ("motion_y", "i4"),
        ("motion_scale", "i4"),
    ]
)


def vectors(moving=False):
    """Cover the image with backward-reference blocks, including zero-motion blocks."""
    data = np.zeros(64, dtype=VECTOR_TYPE)
    for index in range(64):
        y, x = divmod(index, 8)
        data[index] = (
            -1,
            16,
            16,
            x * 16 + 8,
            y * 16 + 8,
            4 if moving and index == 0 else 0,
            0,
            4,
        )
    return data


def evidence(now=10.0, moving=False, picture="P", data=None):
    return vector_evidence(
        vectors(moving) if data is None else data,
        timestamp=now,
        width=128,
        height=128,
        picture_type=picture,
        rows=8,
        columns=8,
    )


def hypothetical_qualified():
    """Synthetic proof fixture only, never a claimed production acceptance result."""
    return Qualification(
        extractor_avoids_reconstruction=True,
        matched_labeled_events=50,
        labeled_events=50,
        tested_day_and_night=True,
        tested_small_slow_objects=True,
        measured_resource_ratio=0.25,
        safe_preroll_verified=True,
    )


class TestVectorEvidence(unittest.TestCase):
    def test_real_displacement_uses_motion_scale_and_spatial_destination(self):
        sample = evidence(moving=True)
        self.assertIsNone(sample.uncertainty)
        self.assertEqual(sample.coverage, 1)
        self.assertEqual(sample.activity[0, 0], 1)
        self.assertEqual(np.count_nonzero(sample.activity), 1)
        data = vectors(True)
        data["motion_scale"] = 16
        self.assertEqual(np.count_nonzero(evidence(data=data).activity), 0)

    def test_zero_motion_vectors_are_not_confused_with_absent_data(self):
        sample = evidence()
        self.assertIsNone(sample.uncertainty)
        self.assertEqual(sample.coverage, 1)
        self.assertEqual(np.count_nonzero(sample.activity), 0)
        unknown = vector_evidence(
            None,
            timestamp=10,
            width=128,
            height=128,
            picture_type="P",
            rows=8,
            columns=8,
        )
        self.assertEqual(unknown.uncertainty, "vectors_unavailable")

    def test_i_frames_sparse_and_invalid_vectors_remain_unknown(self):
        self.assertEqual(evidence(picture="I").uncertainty, "intra_or_unknown_frame")
        self.assertEqual(
            evidence(data=vectors()[:1]).uncertainty, "sparse_vector_coverage"
        )
        for field, value in (
            ("motion_scale", 0),
            ("dst_x", 128),
            ("dst_y", -1),
            ("w", 0),
        ):
            data = vectors()
            data[field][0] = value
            with self.subTest(field=field):
                self.assertEqual(evidence(data=data).uncertainty, "invalid_vectors")

    def test_future_only_vectors_are_not_backward_motion_evidence(self):
        data = vectors()
        data["source"] = 1
        self.assertEqual(
            evidence(data=data).uncertainty, "reference_vectors_unavailable"
        )

    def test_global_motion_remains_uncertain(self):
        data = vectors()
        data["motion_x"] = 16
        self.assertEqual(
            evidence(data=data).uncertainty, "global_motion_or_illumination"
        )

    def test_bad_geometry_and_nonfinite_time_are_explicit_errors(self):
        for changes in ({"timestamp": float("nan")}, {"width": 0}, {"rows": 200}):
            args = {
                "timestamp": 1,
                "width": 128,
                "height": 128,
                "picture_type": "P",
                "rows": 8,
                "columns": 8,
            }
            args.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                vector_evidence(vectors(), **args)


class TestIndependentShadowPID(unittest.TestCase):
    def test_no_learning_from_missing_pixel_or_alignment_verification(self):
        pid = ShadowMotionPID(8, 8)
        for now in range(10, 30):
            pid.observe(evidence(float(now)))
        self.assertTrue(np.all(pid.profile.observed == 0))
        self.assertTrue(np.all(pid.profile.integral == 0))

    def test_verified_samples_learn_but_protected_track_cells_do_not(self):
        pid = ShadowMotionPID(8, 8)
        pid.observe(evidence(10), pixel_verified_quiet=True, view_verified=True)
        pid.observe(
            evidence(11),
            pixel_verified_quiet=True,
            view_verified=True,
            tracked_boxes=[(0, 0, 16, 16)],
        )
        self.assertEqual(pid.profile.observed[0, 0], 0)
        self.assertGreater(pid.profile.observed[7, 7], 0)

    def test_gap_does_not_invent_observation_time_or_retain_old_integral(self):
        pid = ShadowMotionPID(8, 8)
        pid.observe(evidence(10), pixel_verified_quiet=True, view_verified=True)
        pid.profile.integral.fill(12)
        pid.observe(evidence(100), pixel_verified_quiet=True, view_verified=True)
        self.assertTrue(np.all(pid.profile.integral == 0))
        self.assertTrue(np.all(pid.profile.observed == 0))
        pid.observe(evidence(9), pixel_verified_quiet=True, view_verified=True)
        self.assertTrue(np.all(pid.profile.observed == 0))

    def test_intra_frames_do_not_earn_quiet_learning_credit(self):
        pid = ShadowMotionPID(8, 8)
        pid.observe(evidence(10), pixel_verified_quiet=True, view_verified=True)
        pid.observe(
            evidence(11, picture="I"), pixel_verified_quiet=True, view_verified=True
        )
        self.assertTrue(np.all(pid.profile.observed == 0))


class TestShadowGateSafety(unittest.TestCase):
    def test_default_and_ffmpeg_backends_never_suppress_decoding(self):
        gate = ShadowDecodeGate()
        for now in range(10, 100):
            self.assertTrue(
                gate.decide(evidence(float(now)), view_verified=True).decode
            )
        qualified_except_full_decode = replace(
            hypothetical_qualified(), extractor_avoids_reconstruction=False
        )
        self.assertTrue(
            ShadowDecodeGate(qualified_except_full_decode)
            .decide(evidence(), view_verified=True)
            .decode
        )

    def test_missing_or_missed_acceptance_evidence_blocks_qualification(self):
        good = hypothetical_qualified()
        self.assertEqual(good.blockers(), ())
        for change in (
            {"matched_labeled_events": 49},
            {"labeled_events": 0},
            {"tested_day_and_night": False},
            {"tested_small_slow_objects": False},
            {"safe_preroll_verified": False},
            {"measured_resource_ratio": None},
            {"measured_resource_ratio": 0.51},
            {"measured_resource_ratio": float("nan")},
        ):
            with self.subTest(change=change):
                self.assertTrue(replace(good, **change).blockers())

    def test_periodic_probe_and_movement_hold_then_quiet_simulation(self):
        gate = ShadowDecodeGate(hypothetical_qualified())
        self.assertEqual(
            gate.decide(evidence(10), view_verified=True).reason,
            "periodic_full_decode_probe",
        )
        self.assertTrue(gate.decide(evidence(11), view_verified=True).decode)
        self.assertFalse(gate.decide(evidence(12), view_verified=True).decode)
        self.assertTrue(
            gate.decide(evidence(13, moving=True), view_verified=True).decode
        )
        for now in range(14, 23):
            self.assertTrue(gate.decide(evidence(now), view_verified=True).decode)
        self.assertFalse(gate.decide(evidence(23), view_verified=True).decode)
        for now in range(24, 40):
            gate.decide(evidence(now), view_verified=True)
        self.assertEqual(
            gate.decide(evidence(40), view_verified=True).reason,
            "periodic_full_decode_probe",
        )

    def test_objects_uncertainty_outage_and_reset_force_decoding(self):
        gate = ShadowDecodeGate(hypothetical_qualified())
        gate.decide(evidence(10), view_verified=True)
        self.assertEqual(
            gate.decide(
                evidence(11), tracked_object_present=True, view_verified=True
            ).reason,
            "tracked_object_hold",
        )
        self.assertTrue(
            gate.decide(evidence(12, picture="I"), view_verified=True).decode
        )
        self.assertTrue(
            gate.decide(evidence(13), extraction_failed=True, view_verified=True).decode
        )
        self.assertTrue(gate.decide(evidence(14), view_verified=False).decode)
        self.assertEqual(
            gate.decide(evidence(100), view_verified=True).reason,
            "timestamp_gap_or_reset",
        )
        self.assertEqual(
            gate.decide(evidence(99), view_verified=True).reason,
            "timestamp_gap_or_reset",
        )


if __name__ == "__main__":
    unittest.main()

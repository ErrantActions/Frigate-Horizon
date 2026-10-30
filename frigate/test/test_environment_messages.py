"""Local glare metadata must not invent identity, clock, or image payload fields."""

import unittest

from frigate.environment.messages import GlareSignal


class TestGlareSignals(unittest.TestCase):
    def payload(self):
        return {
            "version": 1,
            "camera": "front",
            "configuration_hash": "a" * 64,
            "kind": "glare",
            "frame_time": 1792227600,
            "zones": ["driveway"],
            "error_code": None,
        }

    def test_parses_versioned_metadata_without_image_pixels(self):
        signal = GlareSignal.parse(self.payload())
        self.assertEqual(signal.camera, "front")
        self.assertEqual(signal.zones, ("driveway",))
        self.assertEqual(signal.frame_time, 1792227600)

    def test_rejects_unknown_fields_or_missing_fields(self):
        for field in ("camera", "frame_time", "configuration_hash"):
            payload = self.payload()
            del payload[field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                GlareSignal.parse(payload)
        payload = self.payload()
        payload["image"] = "must-not-be-exported"
        with self.assertRaises(ValueError):
            GlareSignal.parse(payload)

    def test_rejects_invalid_versions_clocks_and_identity(self):
        for field, value in (
            ("version", True),
            ("version", 2),
            ("camera", ""),
            ("configuration_hash", "not-a-digest"),
            ("frame_time", True),
            ("frame_time", float("nan")),
            ("kind", "headlight-certain"),
        ):
            payload = self.payload()
            payload[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                GlareSignal.parse(payload)

    def test_glare_requires_unique_zones_and_unavailable_requires_a_reason(self):
        for zones in ([], ["driveway", "driveway"], [None]):
            payload = self.payload()
            payload["zones"] = zones
            with self.subTest(zones=zones), self.assertRaises(ValueError):
                GlareSignal.parse(payload)
        payload = self.payload()
        payload["kind"] = "unavailable"
        with self.assertRaises(ValueError):
            GlareSignal.parse(payload)
        payload["error_code"] = "invalid_glare_frame"
        self.assertEqual(GlareSignal.parse(payload).kind, "unavailable")


if __name__ == "__main__":
    unittest.main()

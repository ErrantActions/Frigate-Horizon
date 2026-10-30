"""Opt-in investigation exports do not leak unselected or runtime data."""

import json
import unittest

from frigate.record.export_metadata import (
    ExportMetadataCategory as Category,
)
from frigate.record.export_metadata import (
    investigation_metadata,
    metadata_json,
    metadata_text,
)


class TestExportMetadata(unittest.TestCase):
    def build(self, categories, **overrides):
        values = {
            "camera": "front",
            "camera_name": "Front Door",
            "after": 100,
            "before": 200,
            "timezone": "UTC",
            "version": "test",
            "timestamp_mode": "none",
            "events": [
                {
                    "id": "event",
                    "start_time": 110,
                    "end_time": 120,
                    "label": "person",
                    "zones": ["porch"],
                    "false_positive": False,
                    "sub_label": "Sensitive Name",
                    "data": {
                        "score": 0.95,
                        "box": [0.1, 0.2, 0.3, 0.4],
                        "recognized_license_plate": "TEST123",
                        "secret": "NEVER_EXPORT",
                    },
                }
            ],
            "timeline": [
                {
                    "timestamp": 112,
                    "source": "tracked_object",
                    "source_id": "event",
                    "class_type": "entered_zone",
                    "data": {
                        "label": "person",
                        "zones": ["porch"],
                        "secret": "NEVER_EXPORT",
                    },
                }
            ],
        }
        values.update(overrides)
        return investigation_metadata(categories, **values)

    def test_unselected_identity_and_geometry_are_excluded(self):
        data = self.build([Category.events])
        serialized = metadata_json(data)
        self.assertNotIn("Sensitive Name", serialized)
        self.assertNotIn("TEST123", serialized)
        self.assertNotIn("NEVER_EXPORT", serialized)
        self.assertNotIn("geometry", data["events"][0])
        self.assertEqual(data["events"][0]["confidence"], 0.95)

    def test_explicit_identity_and_geometry_selections_are_separate(self):
        data = self.build([Category.identities, Category.geometry])
        self.assertEqual(
            data["events"][0]["identity"]["recognized_license_plate"], "TEST123"
        )
        self.assertEqual(data["events"][0]["geometry"]["box"], [0.1, 0.2, 0.3, 0.4])
        self.assertNotIn("label", data["events"][0])
        self.assertNotIn("timeline", data)
        self.assertNotIn("NEVER_EXPORT", metadata_json(data))

    def test_missing_environment_and_notes_are_not_invented(self):
        data = self.build(
            [Category.environment, Category.notes, Category.camera, Category.provenance]
        )
        self.assertIn("environment", data["unavailable"])
        self.assertIn("notes", data["unavailable"])
        self.assertFalse(data["provenance"]["authenticity_verified"])
        self.assertFalse(data["timing"]["camera_clock_verified"])
        self.assertEqual(data["camera"]["display_name"], "Front Door")

    def test_metadata_is_bounded_and_invalid_json_numbers_fail(self):
        with self.assertRaises(ValueError):
            self.build([Category.events], events=[{}] * 10001)
        with self.assertRaises(ValueError):
            metadata_json({"large": "x" * (2 * 2**20)})
        with self.assertRaises(ValueError):
            metadata_json({"score": float("nan")})

    def test_generated_comment_escapes_metadata_syntax(self):
        data = {"note": "one=two;#\\\nnext"}
        text = metadata_text(data)
        self.assertTrue(text.startswith(";FFMETADATA1\ncomment="))
        self.assertEqual(len(text.splitlines()), 2)
        self.assertEqual(json.loads(metadata_json(data)), data)

    def test_capture_to_media_mapping_survives_serialization(self):
        mapping = [
            {
                "capture_start": 100,
                "capture_end": 110,
                "output_start": 0,
                "output_end": 10,
            },
            {
                "capture_start": 200,
                "capture_end": 210,
                "output_start": 10,
                "output_end": 20,
            },
        ]
        data = self.build([Category.provenance], media_timeline=mapping)
        self.assertEqual(json.loads(metadata_json(data))["media_timeline"], mapping)

    def test_notes_are_only_included_when_selected_and_projected(self):
        notes = [
            {
                "id": "note",
                "timestamp": 100,
                "title": "Incident",
                "notes": "Sensitive details",
                "owner": "alice",
                "revision": 2,
                "secret": "NEVER_EXPORT",
            }
        ]
        hidden = self.build([Category.provenance], notes=notes)
        self.assertNotIn("Sensitive details", metadata_json(hidden))
        included = self.build([Category.notes], notes=notes)
        self.assertEqual(included["notes"][0]["notes"], "Sensitive details")
        self.assertNotIn("notes", included["unavailable"])
        self.assertNotIn("NEVER_EXPORT", metadata_json(included))

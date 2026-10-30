"""Nullable custom label paths must preserve the built-in detector labels."""

import unittest
from unittest.mock import patch

from frigate.detectors.detector_config import ModelConfig


class TestModelLabelmap(unittest.TestCase):
    def test_unset_null_and_empty_paths_load_builtin_labels(self):
        for options in ({}, {"labelmap_path": None}, {"labelmap_path": ""}):
            with self.subTest(options=options):
                with patch(
                    "frigate.detectors.detector_config.load_labels",
                    return_value={0: "person"},
                ) as labels:
                    model = ModelConfig(**options)
                labels.assert_called_once_with("/labelmap.txt")
                self.assertEqual(model.merged_labelmap[0], "person")

    def test_explicit_custom_path_and_label_overrides_are_preserved(self):
        with patch(
            "frigate.detectors.detector_config.load_labels",
            return_value={0: "person", 2: "car"},
        ) as labels:
            model = ModelConfig(
                labelmap_path="/config/custom-labels.txt",
                labelmap={0: "custom-person"},
            )
        labels.assert_called_once_with("/config/custom-labels.txt")
        self.assertEqual(model.merged_labelmap, {0: "custom-person", 2: "car"})

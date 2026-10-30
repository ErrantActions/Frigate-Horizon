"""Narrow synthetic HLS exceptions must not admit runtime media or altered files."""

import tempfile
import unittest
from pathlib import Path

from tools.check_public_source import is_synthetic_hls_initialization


class TestSyntheticHlsBoundary(unittest.TestCase):
    def test_exact_synthetic_initialization_is_allowed(self):
        root = Path(__file__).resolve().parents[1]
        name = "web/e2e/fixtures/media/live-hls/0/init_0.mp4"
        self.assertTrue(
            is_synthetic_hls_initialization(name, root.joinpath(*name.split("/")))
        )

    def test_other_path_or_altered_bytes_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.mp4"
            path.write_bytes(b"not-the-reviewed-synthetic-header")
            self.assertFalse(
                is_synthetic_hls_initialization("recordings/runtime.mp4", path)
            )
            self.assertFalse(
                is_synthetic_hls_initialization(
                    "web/e2e/fixtures/media/live-hls/0/init_0.mp4", path
                )
            )

"""Offline CLI evidence shapes and real synthetic-H.264 extraction without live settings."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import av
import numpy as np

from frigate.motion.compressed_shadow import main, run


class TestCompressedShadowReplay(unittest.TestCase):
    def test_live_apply_and_stream_url_options_are_not_supported(self):
        for flag in ("--apply", "--enable-live-gating", "--camera"):
            with (
                self.subTest(flag=flag),
                patch(
                    "sys.argv",
                    [
                        "compressed_shadow",
                        "missing.mp4",
                        "--report",
                        "unused.json",
                        flag,
                    ],
                ),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as stopped,
            ):
                main()
            self.assertEqual(stopped.exception.code, 2)

    def test_replay_limits_are_enforced_before_video_io(self):
        with tempfile.NamedTemporaryFile() as file:
            for seconds, repeats in ((0, 1), (121, 1), (10, 0), (10, 4)):
                with (
                    self.subTest(seconds=seconds, repeats=repeats),
                    self.assertRaises(ValueError),
                ):
                    run(Path(file.name), seconds, repeats)

    def test_actual_h264_replay_report_never_qualifies_the_full_decoder(self):
        if "libx264" not in av.codecs_available:
            self.skipTest(
                "Synthetic H.264 fixture requires the existing libx264 encoder"
            )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic-motion.mp4"
            with av.open(str(path), mode="w") as container:
                stream = container.add_stream("libx264", rate=10)
                stream.width = stream.height = 128
                stream.pix_fmt = "yuv420p"
                stream.options = {"preset": "ultrafast", "crf": "23", "g": "10"}
                for index in range(30):
                    image = np.zeros((128, 128, 3), dtype=np.uint8)
                    image[32:64, index * 2 : index * 2 + 24] = 220
                    frame = av.VideoFrame.from_ndarray(image, format="rgb24")
                    for packet in stream.encode(frame):
                        container.mux(packet)
                for packet in stream.encode():
                    container.mux(packet)
            result = run(path, 2, 1)
            self.assertEqual(result["mode"], "OFFLINE_SHADOW_ONLY")
            self.assertFalse(result["live_detection_changed"])
            self.assertFalse(result["live_gate_available"])
            self.assertIn(
                "extractor_still_reconstructs_frames", result["live_gate_blockers"]
            )
            self.assertIn(
                "insufficient_or_missed_labeled_events", result["live_gate_blockers"]
            )
            self.assertFalse(result["replay"]["pid_persisted"])
            self.assertGreater(result["replay"]["sampled_frames"], 0)
            self.assertTrue(
                all(sample["would_decode"] for sample in result["replay"]["samples"])
            )
            counts = [
                item["frames"]
                for values in result["backend_timings"].values()
                for item in values
            ]
            self.assertEqual(len(set(counts)), 1)
            self.assertGreater(
                result["backend_timings"]["ffmpeg_vector_export"][0]["vectors"], 0
            )
            json.dumps(result, allow_nan=False)
            report_path = Path(directory) / "cli-report.json"
            with (
                patch(
                    "sys.argv",
                    [
                        "compressed_shadow",
                        str(path),
                        "--seconds",
                        "1",
                        "--repeats",
                        "1",
                        "--report",
                        str(report_path),
                    ],
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                main()
            saved = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["mode"], "OFFLINE_SHADOW_ONLY")
            self.assertFalse(saved["live_gate_available"])


if __name__ == "__main__":
    unittest.main()

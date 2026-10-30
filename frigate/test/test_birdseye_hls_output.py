"""Private Birdseye HLS input remains paced, cancellable, and frame-aligned."""

import os
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


@unittest.skipIf(os.name == "nt", "Birdseye raw output uses a Unix FIFO")
class TestBirdseyeHlsOutput(unittest.TestCase):
    def setUp(self):
        from frigate.output import birdseye

        self.module = birdseye
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.enterContext(
            patch.object(
                birdseye, "BIRDSEYE_PIPE", str(Path(self.directory.name) / "raw")
            )
        )
        self.stop_event = threading.Event()
        self.frames = queue.Queue(maxsize=10)
        self.writer = birdseye.BirdseyeRestreamWriter(self.frames, self.stop_event)
        self.addCleanup(self.close)

    def close(self):
        self.stop_event.set()
        if self.writer.is_alive():
            self.writer.join(2)
        if self.writer.bd_pipe is not None:
            os.close(self.writer.bd_pipe)
            self.writer.bd_pipe = None

    def test_idle_video_repeats_the_latest_frame_at_the_source_frame_rate(self):
        written = []

        def write(frame):
            written.append(frame)
            if len(written) == 3:
                self.stop_event.set()

        self.frames.put_nowait(b"old")
        self.frames.put_nowait(b"latest")
        with patch.object(self.writer, "_write_frame", side_effect=write):
            self.writer.start()
            self.writer.join(2)
        self.assertFalse(self.writer.is_alive())
        self.assertEqual(written, [b"latest"] * 3)

    def test_partial_fifo_writes_complete_the_same_frame(self):
        with patch.object(self.module.os, "write", side_effect=[2, 3]) as write:
            self.writer._write_frame(b"frame")
        self.assertEqual(bytes(write.call_args_list[0].args[1]), b"frame")
        self.assertEqual(bytes(write.call_args_list[1].args[1]), b"ame")

    def test_reader_disconnect_after_a_partial_frame_recreates_the_pipe(self):
        with (
            patch.object(self.module.os, "write", side_effect=[2, BrokenPipeError]),
            patch.object(self.writer, "recreate_birdseye_pipe") as recreate,
        ):
            self.writer._write_frame(b"frame")
        recreate.assert_called_once()

    def test_stopped_output_never_waits_for_a_reader(self):
        self.stop_event.set()
        with patch.object(self.module.os, "write") as write:
            self.writer._write_frame(b"frame")
        write.assert_not_called()

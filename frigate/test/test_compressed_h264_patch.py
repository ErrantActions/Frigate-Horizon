"""Guard the isolated H264 research patch against unknown source or missing bypasses."""

import importlib.util
import tempfile
import unittest
from pathlib import Path


def load_patch():
    path = Path(__file__).resolve().parents[2] / "docker/compressed-motion/patch_h264.py"
    spec = importlib.util.spec_from_file_location("compressed_h264_research_patch", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Research patch could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestCompressedH264Patch(unittest.TestCase):
    def test_unknown_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "libavcodec").mkdir()
            (root / "libavcodec/h264dec.h").write_text("unsupported source")
            with self.assertRaisesRegex(RuntimeError, "Unexpected FFmpeg source anchor"):
                load_patch().patch(root)

    def test_bypass_is_explicit_and_normal_reconstruction_stays_available(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codec = root / "libavcodec"
            codec.mkdir()
            (codec / "h264dec.h").write_text(
                "void ff_h264_hl_decode_mb(const H264Context *h, H264SliceContext *sl);"
            )
            (codec / "h264_mb.c").write_text(
                "void ff_h264_hl_decode_mb(const H264Context *h, H264SliceContext *sl)\n"
                "{\n    normal_pixel_reconstruction();\n}\n"
            )
            (codec / "h264_slice.c").write_text(
                "    if (sl->deblocking_filter) {\n        for (mb_x = start_x;\n"
                "    ff_h264_draw_horiz_band(h, sl, top, height);\n"
            )
            (codec / "mpegutils.c").write_text(
                "    if ((avctx->export_side_data & AV_CODEC_EXPORT_DATA_MVS) "
                "&& mbtype_table && motion_val[0]) {\n"
            )
            load_patch().patch(root)
            mb = (codec / "h264_mb.c").read_text()
            self.assertIn("skip_idct == AVDISCARD_ALL", mb)
            self.assertIn("memset(sl->mb, 0, sizeof(sl->mb))", mb)
            self.assertIn("memset(sl->mb_luma_dc, 0, sizeof(sl->mb_luma_dc))", mb)
            self.assertLess(mb.index("return;"), mb.index("normal_pixel_reconstruction"))
            self.assertIn("horizon_reconstructed_mbs", mb)
            slices = (codec / "h264_slice.c").read_text()
            self.assertIn(
                "if (h->avctx->skip_idct != AVDISCARD_ALL)\n"
                "        ff_h264_draw_horiz_band",
                slices,
            )
            self.assertLess(slices.index("return;"), slices.index("for (mb_x"))
            coded = (codec / "mpegutils.c").read_text()
            self.assertIn("FRIGATECMBGRID01", coded)
            self.assertIn("!IS_SKIP(mbtype_table", coded)


if __name__ == "__main__":
    unittest.main()

"""Live HLS plans preserve camera permissions, source identity and recording settings."""

import copy
import unittest
from types import SimpleNamespace

from frigate.util.live_hls import (
    LiveHlsRendition,
    camera_renditions,
    master_playlist,
    packager_config,
    parse_packager_master,
    public_media_playlist,
    rendition_path,
    resource_url,
)

PRIVATE_MASTER = (
    "#EXTM3U\n#EXT-X-VERSION:10\n"
    '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Audio",'
    'URI="audio2_stream.m3u8?session=private-test-1"\n'
    '#EXT-X-STREAM-INF:BANDWIDTH=1200000,CODECS="avc1.640029,mp4a.40.2",'
    'RESOLUTION=1280x720,FRAME-RATE=25.000,AUDIO="audio"\n'
    "video1_stream.m3u8?session=private-test-1\n"
)


def configuration(*, enabled=True, manual=True):
    qualities = [
        SimpleNamespace(height=1080, bitrate=2500),
        SimpleNamespace(height=720, bitrate=1200),
        SimpleNamespace(height=360, bitrate=250),
    ]
    return SimpleNamespace(
        ui=SimpleNamespace(live_compatibility=True),
        cameras={
            "front": SimpleNamespace(
                enabled=enabled,
                live=SimpleNamespace(
                    streams={
                        "Main": "main",
                        "Sub": "sub",
                        "1080p": "front_transcode_1080p",
                        "720p": "front_transcode_720p",
                        "360p": "front_transcode_360p",
                    },
                    transcode=SimpleNamespace(enabled=not manual, qualities=qualities),
                ),
            )
        },
        go2rtc=SimpleNamespace(
            streams={
                "front_transcode_1080p": ["ffmpeg:main#video=h264#height=1080"],
                "front_transcode_720p": ["ffmpeg:main#video=h264#height=720"],
            }
        ),
    )


class TestLiveHls(unittest.TestCase):
    def test_manual_profiles_do_not_require_server_generation(self):
        config = configuration()
        self.assertFalse(config.cameras["front"].live.transcode.enabled)
        self.assertEqual(
            camera_renditions(config, "front"),
            (
                LiveHlsRendition("front_transcode_720p", 720, 1200),
                LiveHlsRendition("front_transcode_1080p", 1080, 2500),
            ),
        )

    def test_disabled_missing_and_compatibility_blocked_cameras_have_no_plan(self):
        self.assertEqual(camera_renditions(configuration(enabled=False), "front"), ())
        self.assertEqual(camera_renditions(configuration(), "missing"), ())
        config = configuration()
        config.ui.live_compatibility = False
        self.assertEqual(camera_renditions(config, "front"), ())

    def test_unadvertised_or_unregistered_manual_streams_are_not_invented(self):
        config = configuration()
        del config.cameras["front"].live.streams["1080p"]
        del config.go2rtc.streams["front_transcode_720p"]
        self.assertEqual(camera_renditions(config, "front"), ())

    def test_generated_profiles_are_allowed_when_advertised(self):
        config = configuration(manual=False)
        config.go2rtc.streams = {}
        self.assertEqual(len(camera_renditions(config, "front")), 2)

    def test_packager_is_private_on_demand_and_has_no_publish_permission(self):
        result = packager_config(configuration())
        self.assertEqual(result["hlsAddress"], "127.0.0.1:8888")
        for protocol in (
            "api",
            "metrics",
            "pprof",
            "rtsp",
            "rtmp",
            "srt",
            "webrtc",
            "moq",
        ):
            self.assertFalse(result[protocol])
        self.assertFalse(result["hlsAlwaysRemux"])
        self.assertEqual(
            result["authInternalUsers"][0]["permissions"], [{"action": "read"}]
        )
        self.assertEqual(len(result["paths"]), 2)
        self.assertTrue(
            all(path["sourceOnDemand"] for path in result["paths"].values())
        )
        self.assertTrue(
            all(
                path["source"].startswith("rtsp://127.0.0.1:8554/")
                for path in result["paths"].values()
            )
        )

    def test_planning_does_not_change_config_or_enable_generation(self):
        config = configuration()
        before = copy.deepcopy(config)
        packager_config(config)
        self.assertEqual(config, before)

    def test_master_is_low_bitrate_first_and_uses_only_relative_variant_urls(self):
        renditions = camera_renditions(configuration(), "front")
        master = parse_packager_master(PRIVATE_MASTER)
        text = master_playlist(tuple((value, master) for value in renditions))
        self.assertTrue(text.startswith("#EXTM3U\n#EXT-X-VERSION:10\n"))
        self.assertLess(
            text.index("720p/video1_stream.m3u8"),
            text.index("1080p/video1_stream.m3u8"),
        )
        self.assertIn("AVERAGE-BANDWIDTH=1200000", text)
        self.assertNotIn("rtsp:", text)
        self.assertNotIn("127.0.0.1", text)
        self.assertNotIn("private-test-1", text)
        self.assertIn('CODECS="avc1.640029,mp4a.40.2"', text)
        self.assertIn('URI="720p/audio2_stream.m3u8"', text)

    def test_private_bootstrap_cannot_point_at_an_external_or_parent_resource(self):
        for uri in (
            "https://outside.invalid/stream.m3u8?session=private-test-1",
            "../stream.m3u8?session=private-test-1",
            "video1_stream.m3u8?session=first&session=second",
            "video1_stream.m3u8",
        ):
            with self.subTest(uri=uri), self.assertRaises(ValueError):
                parse_packager_master(
                    PRIVATE_MASTER.replace(
                        "video1_stream.m3u8?session=private-test-1", uri
                    )
                )

    def test_private_sessions_are_removed_from_maps_parts_audio_and_segments(self):
        source = (
            "#EXTM3U\n#EXT-X-VERSION:10\n"
            '#EXT-X-MAP:URI="video1_init.mp4?session=private-test-1"\n'
            "#EXT-X-GAP\n#EXTINF:2,\ngap.mp4\n"
            '#EXT-X-PART:DURATION=0.2,URI="video1_part0.mp4?session=private-test-1"\n'
            "#EXTINF:2,\nvideo1_seg0.mp4?session=private-test-1\n"
        )
        result = public_media_playlist(source)
        self.assertNotIn("session", result)
        self.assertIn('URI="video1_init.mp4"', result)
        self.assertIn('URI="video1_part0.mp4"', result)
        self.assertIn("\ngap.mp4\n", result)
        self.assertIn("\nvideo1_seg0.mp4\n", result)

    def test_resource_paths_cannot_escape_or_choose_an_upstream(self):
        for resource in (
            "../init.mp4",
            "a/part.mp4",
            "/init.mp4",
            "https://evil/init.mp4",
            "a..mp4",
        ):
            with self.subTest(resource=resource), self.assertRaises(ValueError):
                resource_url("front", 720, resource)
        self.assertEqual(
            resource_url("front", 720, "init.mp4"),
            f"http://127.0.0.1:8888/{rendition_path('front', 720)}/init.mp4",
        )

    def test_private_paths_are_stable_and_separate_cameras_and_heights(self):
        first = rendition_path("front", 720)
        self.assertEqual(first, rendition_path("front", 720))
        self.assertNotEqual(first, rendition_path("back", 720))
        self.assertNotEqual(first, rendition_path("front", 1080))
        self.assertNotIn("front", first)

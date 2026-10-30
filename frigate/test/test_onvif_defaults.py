"""Infer ONVIF defaults without contacting cameras or exposing credentials."""

import unittest
from unittest.mock import patch

from frigate.config.camera.onvif import OnvifConfig, infer_onvif_connection


class TestOnvifDefaults(unittest.TestCase):
    def infer(self, values=None, paths=None, streams=None):
        config = OnvifConfig(**(values or {}))
        infer_onvif_connection(
            config,
            paths or ["rtsp://camera_user:camera_pass@192.0.2.10:554/main"],
            streams or {},
            "synthetic",
        )
        return config

    def test_direct_source_decodes_credentials_without_using_rtsp_port(self):
        config = self.infer(
            paths=["rtsp://user%40home:p%3Ass%2Bword@[2001:db8::1]:554/main"]
        )
        self.assertEqual(config.host, "2001:db8::1")
        self.assertEqual(config.user, "user@home")
        self.assertEqual(config.password, "p:ss+word")
        self.assertEqual(config.port, 8000)

    def test_explicit_values_are_preserved(self):
        config = self.infer(
            {"host": "other-camera", "user": "onvif_user", "password": "onvif_pass"}
        )
        self.assertEqual(
            (config.host, config.user, config.password),
            ("other-camera", "onvif_user", "onvif_pass"),
        )

    def test_local_restream_resolves_upstream_and_aliases(self):
        config = self.infer(
            paths=["rtsp://relay:relay-pass@127.0.0.1:8554/main"],
            streams={
                "main": ["ffmpeg:source#video=copy"],
                "source": "rtsp://real:secret@192.0.2.20/main",
            },
        )
        self.assertEqual(
            (config.host, config.user, config.password),
            ("192.0.2.20", "real", "secret"),
        )

    def test_cycles_and_missing_sources_do_not_infer_localhost(self):
        config = self.infer(
            paths=["rtsp://localhost:8554/a"],
            streams={"a": "ffmpeg:b#video=copy", "b": "a"},
        )
        self.assertEqual(config.host, "")
        self.assertIsNone(config.password)

    def test_ambiguous_hosts_require_explicit_configuration(self):
        with self.assertLogs("frigate.config.camera.onvif", "WARNING") as logs:
            config = self.infer(
                paths=[
                    "rtsp://private:secret@192.0.2.10/main",
                    "rtsp://other:password@192.0.2.11/sub",
                ]
            )
        self.assertEqual(config.host, "")
        self.assertNotIn("secret", "".join(logs.output))
        self.assertNotIn("password", "".join(logs.output))

    def test_explicit_host_selects_matching_source_only(self):
        config = self.infer(
            {"host": "https://192.0.2.20"},
            paths=["rtsp://a:b@192.0.2.10/main", "rtsp://right:secret@192.0.2.20/main"],
        )
        self.assertEqual(config.host, "https://192.0.2.20")
        self.assertEqual((config.user, config.password), ("right", "secret"))

    def test_explicit_different_username_never_borrows_rtsp_password(self):
        config = self.infer({"user": "separate_onvif_user"})
        self.assertEqual(config.user, "separate_onvif_user")
        self.assertIsNone(config.password)

    def test_empty_fields_are_filled_independently(self):
        config = self.infer({"user": "", "password": "explicit"})
        self.assertEqual(config.user, "camera_user")
        self.assertEqual(config.password, "explicit")

    def test_conflicting_accounts_do_not_guess(self):
        with self.assertLogs("frigate.config.camera.onvif", "WARNING"):
            config = self.infer(
                paths=["rtsp://a:b@192.0.2.10/main", "rtsp://c:d@192.0.2.10/sub"]
            )
        self.assertEqual(config.host, "192.0.2.10")
        self.assertIsNone(config.user)
        self.assertIsNone(config.password)

    def test_non_rtsp_inputs_are_not_used(self):
        config = self.infer(paths=["http://private:secret@192.0.2.10/video"])
        self.assertEqual(config.host, "")
        self.assertIsNone(config.user)

    def test_invalid_source_logs_no_secrets(self):
        with self.assertLogs("frigate.config.camera.onvif", "WARNING") as logs:
            config = self.infer(paths=["rtsp://private:secret@[broken/main"])
        self.assertEqual(config.host, "")
        self.assertNotIn("private", "".join(logs.output))
        self.assertNotIn("secret", "".join(logs.output))

    def test_local_restream_resolves_environment_credentials_once(self):
        with patch.dict(
            "frigate.util.env.FRIGATE_ENV_VARS",
            {
                "FRIGATE_TEST_USER": "synthetic",
                "FRIGATE_TEST_PASSWORD": "p%40ss",
            },
        ):
            config = self.infer(
                paths=["rtsp://localhost:8554/main"],
                streams={
                    "main": "rtsp://{FRIGATE_TEST_USER}:{FRIGATE_TEST_PASSWORD}@192.0.2.10/main"
                },
            )
        self.assertEqual(config.user, "synthetic")
        self.assertEqual(config.password, "p@ss")

    def test_explicit_ipv6_host_can_infer_matching_credentials(self):
        config = self.infer(
            {"host": "2001:db8::1"},
            paths=["rtsp://synthetic:secret@[2001:db8:0:0:0:0:0:1]/main"],
        )
        self.assertEqual(config.host, "2001:db8::1")
        self.assertEqual(config.user, "synthetic")
        self.assertEqual(config.password, "secret")

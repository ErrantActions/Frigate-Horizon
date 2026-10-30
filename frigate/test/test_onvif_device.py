"""Validation tests for camera-reported ONVIF encoder and imaging controls."""

import copy
import unittest

from pydantic import ValidationError

from frigate.api.defs.request.onvif_body import (
    OnvifConnectionBody,
    OnvifEncoderChanges,
    OnvifEncoderUpdateBody,
)
from frigate.util.onvif_device import (
    OnvifSettingsConflict,
    apply_encoder_changes,
    apply_imaging_changes,
    encoder_state,
    imaging_state,
)


class TestOnvifEncoderSettings(unittest.TestCase):
    def setUp(self):
        self.configuration = {
            "token": "encoder-1",
            "Encoding": "H264",
            "Resolution": {"Width": 1920, "Height": 1080},
            "RateControl": {
                "FrameRateLimit": 15,
                "BitrateLimit": 2048,
                "EncodingInterval": 1,
            },
            "Quality": 4,
            "H264": {"GovLength": 30, "H264Profile": "High"},
            "Extension": {"VendorSetting": "preserve"},
        }
        self.options = {
            "QualityRange": {"Min": 1, "Max": 6},
            "H264": {
                "ResolutionsAvailable": [
                    {"Width": 1920, "Height": 1080},
                    {"Width": 1280, "Height": 720},
                ],
                "FrameRateRange": {"Min": 5, "Max": 30},
                "GovLengthRange": {"Min": 1, "Max": 120},
                "EncodingIntervalRange": {"Min": 1, "Max": 3},
            },
            "Extension": {"H264": {"BitrateRange": {"Min": 512, "Max": 8192}}},
        }
        self.state = encoder_state(self.configuration, self.options, ["Main"])

    def test_reports_camera_limits_without_fallbacks(self):
        self.assertTrue(self.state.editable)
        self.assertEqual(self.state.options.frame_rate.minimum, 5)
        self.assertEqual(self.state.options.bitrate_kbps.maximum, 8192)
        self.assertEqual(self.state.shared_profiles, ["Main"])
        self.assertEqual(len(self.state.revision), 64)

    def test_codec_switch_uses_only_the_target_codecs_advertised_options(self):
        self.options["MPEG4"] = copy.deepcopy(self.options["H264"])
        self.options["MPEG4"]["Mpeg4ProfilesSupported"] = ["SP"]
        self.options["Extension"]["MPEG4"] = copy.deepcopy(
            self.options["Extension"]["H264"]
        )
        state = encoder_state(self.configuration, self.options, ["Main"])
        updated = apply_encoder_changes(
            self.configuration,
            state,
            OnvifEncoderChanges(encoding="MPEG4", codec_profile="SP", gop_length=60),
            state.revision,
        )
        self.assertEqual(updated["Encoding"], "MPEG4")
        self.assertEqual(updated["MPEG4"], {"Mpeg4Profile": "SP", "GovLength": 60})
        self.assertIsNone(updated["H264"])
        self.assertEqual(self.configuration["Encoding"], "H264")
        self.assertEqual(updated["Extension"], self.configuration["Extension"])

    def test_codec_switch_rejects_unreported_codecs_and_missing_target_profiles(self):
        for changes in (
            OnvifEncoderChanges(encoding="H265"),
            OnvifEncoderChanges(encoding="MPEG4", gop_length=60),
        ):
            with self.assertRaises(ValueError):
                apply_encoder_changes(
                    self.configuration, self.state, changes, self.state.revision
                )

    def test_encoder_bounds_include_exact_endpoints(self):
        for frame_rate, bitrate in ((5, 512), (30, 8192)):
            with self.subTest(frame_rate=frame_rate, bitrate=bitrate):
                updated = apply_encoder_changes(
                    self.configuration,
                    self.state,
                    OnvifEncoderChanges(frame_rate=frame_rate, bitrate_kbps=bitrate),
                    self.state.revision,
                )
                self.assertEqual(updated["RateControl"]["FrameRateLimit"], frame_rate)
                self.assertEqual(updated["RateControl"]["BitrateLimit"], bitrate)

    def test_changes_preserve_unmodified_vendor_settings_and_source(self):
        original = copy.deepcopy(self.configuration)
        updated = apply_encoder_changes(
            self.configuration,
            self.state,
            OnvifEncoderChanges(
                resolution={"width": 1280, "height": 720},
                frame_rate=20,
                gop_length=40,
            ),
            self.state.revision,
        )
        self.assertEqual(updated["Resolution"], {"Width": 1280, "Height": 720})
        self.assertEqual(updated["H264"]["GovLength"], 40)
        self.assertEqual(updated["Extension"], original["Extension"])
        self.assertEqual(self.configuration, original)

    def test_rejects_unadvertised_resolution(self):
        with self.assertRaises(ValueError):
            apply_encoder_changes(
                self.configuration,
                self.state,
                OnvifEncoderChanges(resolution={"width": 640, "height": 480}),
                self.state.revision,
            )

    def test_rejects_out_of_range_encoder_values(self):
        for value in (4, 31):
            with self.subTest(frame_rate=value), self.assertRaises(ValueError):
                apply_encoder_changes(
                    self.configuration,
                    self.state,
                    OnvifEncoderChanges(frame_rate=value),
                    self.state.revision,
                )

    def test_rejects_fractional_media1_frame_rate(self):
        with self.assertRaises(ValueError):
            apply_encoder_changes(
                self.configuration,
                self.state,
                OnvifEncoderChanges(frame_rate=10.5),
                self.state.revision,
            )

    def test_rejects_stale_encoder_revision(self):
        with self.assertRaises(OnvifSettingsConflict):
            apply_encoder_changes(
                self.configuration,
                self.state,
                OnvifEncoderChanges(frame_rate=20),
                "0" * 64,
            )

    def test_does_not_invent_options_for_missing_codec(self):
        state = encoder_state(self.configuration, {}, [])
        self.assertFalse(state.editable)
        self.assertIsNone(state.options.frame_rate)
        self.assertEqual(state.options.resolutions, [])
        with self.assertRaises(ValueError):
            apply_encoder_changes(
                self.configuration,
                state,
                OnvifEncoderChanges(frame_rate=20),
                state.revision,
            )

    def test_unsupported_encoding_is_explicitly_read_only(self):
        self.configuration["Encoding"] = "H265"
        state = encoder_state(self.configuration, self.options, [])
        self.assertFalse(state.editable)
        self.assertEqual(state.reason, "unsupported_encoding")

    def test_rejects_empty_encoder_update(self):
        with self.assertRaises(ValidationError):
            OnvifEncoderChanges()


class TestOnvifImagingSettings(unittest.TestCase):
    def test_noise_reduction_uses_explicit_level_support_and_the_documented_standard_interval(self):
        settings = {"Extension": {"Extension": {"Extension": {"NoiseReduction": {"Level": 0.4}}}}}
        options = {"Extension": {"Extension": {"Extension": {"NoiseReductionOptions": {"Level": True}}}}}
        state = imaging_state(settings, options, "sensor")
        control = next(item for item in state.controls if item.key == "noise_reduction_level")
        self.assertTrue(control.available)
        self.assertEqual(control.limits_source, "onvif_standard")
        self.assertEqual((control.limits.minimum, control.limits.maximum), (0, 1))
        updated = apply_imaging_changes(settings, state, {"noise_reduction_level": 0.6}, {"noise_reduction_level": 0.4})
        self.assertEqual(updated["Extension"]["Extension"]["Extension"]["NoiseReduction"]["Level"], 0.6)
        for unsupported in ({}, {"Extension": {"Extension": {"Extension": {"NoiseReductionOptions": {"Level": False}}}}}):
            observed = imaging_state(settings, unsupported, "sensor")
            item = next(item for item in observed.controls if item.key == "noise_reduction_level")
            self.assertFalse(item.available)

    def setUp(self):
        self.settings = {
            "Brightness": 20,
            "Contrast": 30,
            "Exposure": {
                "Mode": "AUTO",
                "ExposureTime": 1000,
                "Gain": 2,
                "MinExposureTime": 10,
                "MaxExposureTime": 10000,
            },
            "WhiteBalance": {"Mode": "AUTO", "CrGain": 1, "CbGain": 1},
            "Extension": {"VendorSetting": "preserve"},
        }
        self.options = {
            "Brightness": {"Min": 0, "Max": 100},
            "Contrast": {"Min": 0, "Max": 100},
            "Exposure": {
                "Mode": ["AUTO", "MANUAL"],
                "ExposureTime": {"Min": 10, "Max": 20000},
                "Gain": {"Min": 0, "Max": 16},
                "MinExposureTime": {"Min": 10, "Max": 20000},
                "MaxExposureTime": {"Min": 10, "Max": 20000},
            },
            "WhiteBalance": {
                "Mode": ["AUTO", "MANUAL"],
                "CrGain": {"Min": 0, "Max": 10},
                "CbGain": {"Min": 0, "Max": 10},
            },
        }
        self.state = imaging_state(self.settings, self.options, "video-source-1")

    def test_missing_controls_remain_explicitly_unavailable(self):
        controls = {item.key: item for item in self.state.controls}
        self.assertTrue(controls["brightness"].available)
        self.assertFalse(controls["sharpness"].available)
        self.assertIsNone(controls["sharpness"].limits)

    def test_imaging_bounds_include_exact_endpoints(self):
        for value in (0, 100):
            with self.subTest(brightness=value):
                updated = apply_imaging_changes(
                    self.settings,
                    self.state,
                    {"brightness": value},
                    {"brightness": 20},
                )
                self.assertEqual(updated["Brightness"], value)
                self.assertEqual(updated["Extension"], self.settings["Extension"])
        self.assertEqual(self.settings["Brightness"], 20)

    def test_rejects_unadvertised_controls_and_values(self):
        for changes, expected in (
            ({"unknown": 1}, {"unknown": None}),
            ({"sharpness": 50}, {"sharpness": None}),
            ({"brightness": 101}, {"brightness": 20}),
            ({"exposure_mode": "OFF"}, {"exposure_mode": "AUTO"}),
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                apply_imaging_changes(
                    self.settings,
                    self.state,
                    changes,
                    expected,
                )

    def test_detects_conflicting_imaging_edit(self):
        with self.assertRaises(OnvifSettingsConflict):
            apply_imaging_changes(
                self.settings,
                self.state,
                {"brightness": 40},
                {"brightness": 10},
            )

    def test_requires_previous_value_for_each_change(self):
        with self.assertRaises(ValueError):
            apply_imaging_changes(
                self.settings,
                self.state,
                {"brightness": 40},
                {},
            )

    def test_manual_exposure_requires_manual_mode(self):
        with self.assertRaises(ValueError):
            apply_imaging_changes(
                self.settings,
                self.state,
                {"exposure_time": 2000},
                {"exposure_time": 1000},
            )
        updated = apply_imaging_changes(
            self.settings,
            self.state,
            {"exposure_mode": "MANUAL", "exposure_time": 2000},
            {"exposure_mode": "AUTO", "exposure_time": 1000},
        )
        self.assertEqual(updated["Exposure"]["Mode"], "MANUAL")
        self.assertEqual(updated["Exposure"]["ExposureTime"], 2000)

    def test_automatic_exposure_readback_does_not_block_switch_to_manual(self):
        self.settings["Exposure"]["ExposureTime"] = 1200
        latest = imaging_state(self.settings, self.options, "video-source-1")
        updated = apply_imaging_changes(
            self.settings,
            latest,
            {"exposure_mode": "MANUAL", "exposure_time": 2000},
            {"exposure_mode": "AUTO", "exposure_time": 1000},
        )
        self.assertEqual(updated["Exposure"]["ExposureTime"], 2000)

    def test_manual_white_balance_requires_manual_mode(self):
        with self.assertRaises(ValueError):
            apply_imaging_changes(
                self.settings,
                self.state,
                {"white_balance_cr_gain": 2},
                {"white_balance_cr_gain": 1},
            )

    def test_rejects_reversed_exposure_limits(self):
        with self.assertRaises(ValueError):
            apply_imaging_changes(
                self.settings,
                self.state,
                {"exposure_min_time": 15000},
                {"exposure_min_time": 10},
            )


class TestOnvifDeviceRequests(unittest.TestCase):
    def test_credentials_are_not_in_request_repr(self):
        body = OnvifConnectionBody(host="camera.local", password="private-example")
        self.assertNotIn("private-example", repr(body))

    def test_rejects_inline_credentials_and_command_fragments(self):
        for host in ("http://user:pass@camera.local", "camera;command", "camera/path"):
            with self.subTest(host=host), self.assertRaises(ValidationError):
                OnvifConnectionBody(host=host)

    def test_accepts_camera_addresses(self):
        for host in (
            "camera.local",
            "192.0.2.10",
            "2001:db8::1",
            "https://camera.local",
        ):
            with self.subTest(host=host):
                self.assertEqual(OnvifConnectionBody(host=host).host, host)

    def test_device_write_requires_explicit_confirmation(self):
        for confirmation in (False, 1, "true"):
            with (
                self.subTest(confirmation=confirmation),
                self.assertRaises(ValidationError),
            ):
                OnvifEncoderUpdateBody(
                    host="camera.local",
                    profile_token="profile-1",
                    expected_revision="0" * 64,
                    confirm_device_change=confirmation,
                    changes={"frame_rate": 20},
                )


if __name__ == "__main__":
    unittest.main()

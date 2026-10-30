"""Pure reported audio option and optimistic-update tests, without SDK or cameras."""

import copy
import unittest

from pydantic import ValidationError

from frigate.api.defs.request.onvif_body import OnvifAudioChanges
from frigate.util.onvif_audio import apply_audio_changes, audio_state
from frigate.util.onvif_device import OnvifSettingsConflict


class TestReportedAudioOptions(unittest.TestCase):
    def setUp(self):
        self.configuration = {
            "token": "audio",
            "Encoding": "AAC",
            "Bitrate": 64,
            "SampleRate": 16,
            "Name": "Microphone",
            "UseCount": 2,
            "Vendor": {"preserve": True},
        }
        self.rows = [
            {
                "Encoding": "AAC",
                "BitrateList": {"Items": [64, 128]},
                "SampleRateList": {"Items": [16, 48]},
            },
            {
                "Encoding": "G711",
                "BitrateList": {"Items": [64]},
                "SampleRateList": {"Items": [8]},
            },
        ]
        self.state = audio_state(
            self.configuration, {"Options": self.rows}, ["main", "sub"], "microphone"
        )

    def test_only_camera_reported_rates_and_codecs_are_exposed(self):
        self.assertEqual(self.state["options"][0]["sample_rates_khz"], [16, 48])
        self.assertEqual(self.state["options"][1]["sample_rates_khz"], [8])
        self.assertEqual(self.state["shared_profiles"], ["main", "sub"])
        self.assertNotIn("password", self.state)

    def test_media2_list_options_use_the_same_reported_units(self):
        state = audio_state(self.configuration, self.rows, ["main"], "microphone")
        self.assertEqual(state["options"], self.state["options"])

    def test_missing_options_never_invent_48khz_or_aac(self):
        state = audio_state(self.configuration, {}, ["main"], "microphone")
        self.assertEqual(state["options"], [])
        self.assertFalse(state["editable"])

    def test_valid_change_preserves_unrelated_fields_and_fetched_object(self):
        before = copy.deepcopy(self.configuration)
        updated = apply_audio_changes(
            self.configuration,
            self.state,
            OnvifAudioChanges(sample_rate_khz=48),
            self.state["revision"],
        )
        self.assertEqual(updated["SampleRate"], 48)
        self.assertEqual(updated["Vendor"], before["Vendor"])
        self.assertEqual(self.configuration, before)

    def test_complete_target_tuple_must_be_reported(self):
        for changes in (
            OnvifAudioChanges(sample_rate_khz=44),
            OnvifAudioChanges(encoding="OPUS"),
            OnvifAudioChanges(encoding="G711"),
        ):
            with self.assertRaises(ValueError):
                apply_audio_changes(
                    self.configuration, self.state, changes, self.state["revision"]
                )

    def test_codec_switch_does_not_retain_an_unreported_sample_rate(self):
        updated = apply_audio_changes(
            self.configuration,
            self.state,
            OnvifAudioChanges(encoding="G711", sample_rate_khz=8),
            self.state["revision"],
        )
        self.assertEqual(updated["Encoding"], "G711")
        self.assertEqual(updated["SampleRate"], 8)

    def test_disjoint_rows_are_not_merged_into_fabricated_combinations(self):
        options = [
            {"Encoding": "AAC", "BitrateList": [64], "SampleRateList": [16]},
            {"Encoding": "AAC", "BitrateList": [128], "SampleRateList": [48]},
        ]
        state = audio_state(self.configuration, options, ["main"], "microphone")
        with self.assertRaises(ValueError):
            apply_audio_changes(
                self.configuration,
                state,
                OnvifAudioChanges(sample_rate_khz=48),
                state["revision"],
            )

    def test_configuration_option_and_source_changes_conflict(self):
        for state in (
            audio_state(
                {**self.configuration, "Bitrate": 128},
                self.rows,
                ["main", "sub"],
                "microphone",
            ),
            audio_state(
                self.configuration, self.rows[:1], ["main", "sub"], "microphone"
            ),
            audio_state(
                self.configuration, self.rows, ["main", "sub"], "different-source"
            ),
        ):
            with self.assertRaises(OnvifSettingsConflict):
                apply_audio_changes(
                    self.configuration,
                    state,
                    OnvifAudioChanges(sample_rate_khz=48),
                    self.state["revision"],
                )

    def test_empty_and_coerced_updates_are_rejected(self):
        for body in (
            {},
            {"sample_rate_khz": True},
            {"bitrate_kbps": "64"},
            {"encoding": ""},
        ):
            with self.assertRaises(ValidationError):
                OnvifAudioChanges(**body)

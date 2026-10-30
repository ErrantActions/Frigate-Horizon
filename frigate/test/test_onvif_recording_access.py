"""Pure source ACL regressions for two logical lenses sharing one ONVIF host."""

import unittest
from types import SimpleNamespace

from frigate.ptz.recording_access import (
    RecordingSourceDenied,
    attributed_recordings,
    recording_binding,
    source_reference,
    verify_media_binding,
    verify_recording_source,
    verify_source_catalog,
)

SOURCE_KEY = b"synthetic-onvif-source-key-testing-only"


def information(source, token):
    return {"RecordingToken": token, "Source": {"SourceId": source}}


class TestOnvifRecordingSourceAccess(unittest.TestCase):
    def setUp(self):
        self.a = information("urn:camera:physical-lens-a", "recording-a")
        self.b = information("urn:camera:physical-lens-b", "guessed-recording-b")

        def camera(profile, video, ref):
            return SimpleNamespace(
                onvif=SimpleNamespace(
                    host="shared-device.invalid",
                    port=80,
                    recording_profile=profile,
                    recording_video_source_token=video,
                    recording_source_ref=ref,
                )
            )

        self.config = SimpleNamespace(
            cameras={
                "lens_a": camera(
                    "profile-a", "source-a", source_reference(self.a, SOURCE_KEY)
                ),
                "lens_b": camera(
                    "profile-b", "source-b", source_reference(self.b, SOURCE_KEY)
                ),
            }
        )
        self.profiles = [
            {
                "token": "profile-a",
                "VideoSourceConfiguration": {"SourceToken": "source-a"},
            },
            {
                "token": "profile-b",
                "VideoSourceConfiguration": {"SourceToken": "source-b"},
            },
        ]

    def test_shared_host_lens_a_search_never_exposes_lens_b_recordings(self):
        binding = recording_binding(self.config, "lens_a", SOURCE_KEY)
        verify_media_binding(binding, self.profiles)
        self.assertEqual(attributed_recordings(binding, [self.b, self.a]), [self.a])
        verify_recording_source(binding, self.a)
        with self.assertRaises(RecordingSourceDenied):
            verify_recording_source(binding, self.b)

    def test_guessed_token_cannot_replace_the_source_binding(self):
        binding = recording_binding(self.config, "lens_a", SOURCE_KEY)
        forged = {"RecordingToken": "recording-a", "Source": self.b["Source"]}
        with self.assertRaises(RecordingSourceDenied):
            verify_recording_source(binding, forged)

    def test_missing_attribution_and_unconfigured_logical_cameras_fail_closed(self):
        binding = recording_binding(self.config, "lens_a", SOURCE_KEY)
        with self.assertRaises(RecordingSourceDenied):
            attributed_recordings(binding, [{"RecordingToken": "unattributed"}])
        with self.assertRaises(RecordingSourceDenied):
            verify_recording_source(binding, {"RecordingToken": "unattributed"})
        self.config.cameras["lens_a"].onvif.recording_source_ref = None
        with self.assertRaises(RecordingSourceDenied):
            recording_binding(self.config, "lens_a", SOURCE_KEY)

    def test_reused_profile_or_changed_physical_source_requires_new_binding(self):
        binding = recording_binding(self.config, "lens_a", SOURCE_KEY)
        self.profiles[0]["VideoSourceConfiguration"]["SourceToken"] = "source-b"
        with self.assertRaises(RecordingSourceDenied):
            verify_media_binding(binding, self.profiles)

    def test_same_recording_source_ref_for_different_lenses_is_ambiguous(self):
        self.config.cameras["lens_b"].onvif.recording_source_ref = source_reference(
            self.a, SOURCE_KEY
        )
        with self.assertRaises(RecordingSourceDenied):
            recording_binding(self.config, "lens_a", SOURCE_KEY)

    def test_private_source_uri_is_only_an_opaque_reference(self):
        info = information(
            "rtsp://user:private-password@camera.invalid/source?secret=query-token",
            "recording",
        )
        reference = source_reference(info, SOURCE_KEY)
        self.assertEqual(len(reference), 64)
        self.assertNotIn("private-password", reference)
        self.assertNotIn("query-token", reference)

    def test_multi_lens_device_with_one_global_source_id_is_not_safe_attribution(self):
        with self.assertRaises(RecordingSourceDenied):
            verify_source_catalog(
                self.profiles,
                [self.a, {**self.a, "RecordingToken": "lens-b"}],
                SOURCE_KEY,
            )
        verify_source_catalog(self.profiles, [self.a, self.b], SOURCE_KEY)

    def test_malformed_video_source_is_not_silently_ignored(self):
        self.profiles[1]["VideoSourceConfiguration"]["SourceToken"] = None
        with self.assertRaises(RecordingSourceDenied):
            verify_source_catalog(self.profiles, [self.a, self.b], SOURCE_KEY)

    def test_references_are_keyed_and_rotate_without_exposing_source_credentials(self):
        self.assertNotEqual(
            source_reference(self.a, SOURCE_KEY),
            source_reference(self.a, b"another-synthetic-source-key-testing-only"),
        )
        with self.assertRaises(RecordingSourceDenied):
            source_reference(self.a, b"")

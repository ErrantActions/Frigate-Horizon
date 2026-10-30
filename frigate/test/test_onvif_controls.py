"""Mock-only audio, focus, and ownership regression tests with the installed SDK."""

import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from zeep.exceptions import Fault

from frigate.api.defs.request.onvif_body import OnvifAudioChanges, OnvifConnectionBody
from frigate.ptz.audio_encoder import read_audio_encoder, write_audio_encoder
from frigate.ptz.device_manager import ImageLease, OnvifDeviceManager
from frigate.ptz.focus import read_focus, write_focus
from frigate.ptz.imaging import ImagingWriteUnverified
from frigate.util.onvif_device import OnvifSettingsConflict


@asynccontextmanager
async def session(connection):
    yield object()


class TestAudioService(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.connection = OnvifConnectionBody(
            host="camera.invalid", profile_token="main"
        )
        self.configuration = {
            "token": "audio",
            "Encoding": "AAC",
            "Bitrate": 64,
            "SampleRate": 16,
            "Name": "Mic",
        }
        self.rows = [
            {
                "Encoding": "AAC",
                "BitrateList": {"Items": [64, 128]},
                "SampleRateList": {"Items": [16, 48]},
            }
        ]
        self.service = SimpleNamespace(
            GetProfiles=AsyncMock(
                return_value=[
                    {
                        "token": "main",
                        "Name": "Main",
                        "AudioEncoderConfiguration": {"token": "audio"},
                        "AudioSourceConfiguration": {"SourceToken": "mic"},
                    }
                ]
            ),
            GetAudioEncoderConfiguration=AsyncMock(return_value=self.configuration),
            GetAudioEncoderConfigurations=AsyncMock(return_value=[self.configuration]),
            GetAudioEncoderConfigurationOptions=AsyncMock(
                return_value={"Options": self.rows}
            ),
            SetAudioEncoderConfiguration=AsyncMock(),
        )
        self.session_patch = patch("frigate.ptz.audio_encoder.camera_session", session)
        self.service_patch = patch(
            "frigate.ptz.audio_encoder.create_camera_service",
            AsyncMock(return_value=self.service),
        )
        self.session_patch.start()
        self.service_patch.start()
        self.addCleanup(self.session_patch.stop)
        self.addCleanup(self.service_patch.stop)

    async def test_missing_audio_source_is_terminal_unsupported(self):
        self.service.GetProfiles.return_value[0]["AudioSourceConfiguration"] = None
        result = await read_audio_encoder(self.connection)
        self.assertEqual(result["state"], "unsupported")
        self.assertIsNone(result["encoder"])
        self.service.GetAudioEncoderConfigurationOptions.assert_not_awaited()

    async def test_temporary_change_is_verified_and_does_not_mutate_the_fetched_configuration(
        self,
    ):
        state = (await read_audio_encoder(self.connection))["encoder"]
        observed = {**self.configuration, "SampleRate": 48}
        self.service.GetAudioEncoderConfiguration.side_effect = [
            self.configuration,
            observed,
        ]
        guard = Mock()
        result = await write_audio_encoder(
            self.connection,
            OnvifAudioChanges(sample_rate_khz=48),
            state["revision"],
            before_write=guard,
        )
        self.assertEqual(result["encoder"]["sample_rate_khz"], 48)
        request = self.service.SetAudioEncoderConfiguration.await_args.args[0]
        self.assertFalse(request["ForcePersistence"])
        self.assertEqual(self.configuration["SampleRate"], 16)
        self.service.SetAudioEncoderConfiguration.assert_awaited_once()
        guard.assert_called_once()

    async def test_stale_revision_never_writes(self):
        with self.assertRaises(OnvifSettingsConflict):
            await write_audio_encoder(
                self.connection, OnvifAudioChanges(sample_rate_khz=48), "0" * 64
            )
        self.service.SetAudioEncoderConfiguration.assert_not_awaited()

    async def test_readback_mismatch_is_unverified_and_not_retried(self):
        state = (await read_audio_encoder(self.connection))["encoder"]
        with self.assertRaises(ImagingWriteUnverified):
            await write_audio_encoder(
                self.connection,
                OnvifAudioChanges(sample_rate_khz=48),
                state["revision"],
            )
        self.service.SetAudioEncoderConfiguration.assert_awaited_once()

    async def test_lost_write_acknowledgement_is_not_retried(self):
        state = (await read_audio_encoder(self.connection))["encoder"]
        self.service.SetAudioEncoderConfiguration.side_effect = httpx.ReadTimeout(
            "synthetic failure"
        )
        with self.assertRaises(ImagingWriteUnverified):
            await write_audio_encoder(
                self.connection,
                OnvifAudioChanges(sample_rate_khz=48),
                state["revision"],
            )
        self.service.SetAudioEncoderConfiguration.assert_awaited_once()

    async def test_media2_requires_explicit_persistence_and_uses_real_plural_methods(
        self,
    ):
        self.service.GetProfiles.return_value = [
            {
                "token": "main",
                "Name": "Main",
                "Configurations": {
                    "AudioEncoder": {"token": "audio"},
                    "AudioSource": {"SourceToken": "mic"},
                },
            }
        ]
        self.service.GetAudioEncoderConfigurationOptions.return_value = self.rows
        state = (await read_audio_encoder(self.connection, kind="media2"))["encoder"]
        self.assertTrue(state["persistence_required"])
        with self.assertRaises(ValueError):
            await write_audio_encoder(
                self.connection,
                OnvifAudioChanges(sample_rate_khz=48),
                state["revision"],
                kind="media2",
            )
        self.service.SetAudioEncoderConfiguration.assert_not_awaited()
        self.service.GetAudioEncoderConfigurations.side_effect = [
            [self.configuration],
            [{**self.configuration, "SampleRate": 48}],
        ]
        result = await write_audio_encoder(
            self.connection,
            OnvifAudioChanges(sample_rate_khz=48),
            state["revision"],
            kind="media2",
            force_persistence=True,
        )
        self.assertEqual(result["encoder"]["sample_rate_khz"], 48)
        self.assertNotIn(
            "ForcePersistence",
            self.service.SetAudioEncoderConfiguration.await_args.args[0],
        )


class TestFocusService(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.connection = OnvifConnectionBody(
            host="camera.invalid", profile_token="main"
        )
        self.position = {"FocusStatus": {"Position": 0.3}}
        self.media = SimpleNamespace(
            GetProfiles=AsyncMock(
                return_value=[
                    {
                        "token": "main",
                        "Name": "Fixed PTZ-independent lens",
                        "VideoSourceConfiguration": {"SourceToken": "sensor"},
                    }
                ]
            )
        )
        self.imaging = SimpleNamespace(
            GetMoveOptions=AsyncMock(
                return_value={
                    "Absolute": {
                        "Position": {"Min": 0, "Max": 1},
                        "Speed": {"Min": 0.1, "Max": 0.5},
                    }
                }
            ),
            GetImagingSettings=AsyncMock(
                return_value={"Focus": {"AutoFocusMode": "MANUAL"}}
            ),
            GetStatus=AsyncMock(return_value=self.position),
            Move=AsyncMock(),
        )

        async def services(camera, name, connection):
            return self.media if name == "media" else self.imaging

        self.session_patch = patch("frigate.ptz.focus.camera_session", session)
        self.services_patch = patch(
            "frigate.ptz.focus.create_camera_service", side_effect=services
        )
        self.session_patch.start()
        self.services_patch.start()
        self.addCleanup(self.session_patch.stop)
        self.addCleanup(self.services_patch.stop)

    async def test_fixed_camera_unsupported_is_explicit_and_never_moves(self):
        self.imaging.GetMoveOptions.return_value = {}
        result = await read_focus(self.connection)
        self.assertEqual(result["state"], "unsupported")
        self.imaging.Move.assert_not_awaited()
        self.imaging.GetStatus.assert_not_awaited()

    async def test_only_explicit_unsupported_fault_is_treated_as_unsupported(self):
        self.imaging.GetMoveOptions.side_effect = Fault(
            "unsupported", code="ter:ActionNotSupported"
        )
        self.assertEqual((await read_focus(self.connection))["state"], "unsupported")
        self.imaging.GetMoveOptions.side_effect = Fault(
            "denied", code="ter:NotAuthorized"
        )
        with self.assertRaises(Fault):
            await read_focus(self.connection)

    async def test_auto_mode_and_stale_positions_never_move(self):
        state = (await read_focus(self.connection))["focus"]
        with self.assertRaises(OnvifSettingsConflict):
            await write_focus(self.connection, 0.8, None, "0" * 64, before_write=Mock())
        self.imaging.GetImagingSettings.return_value = {
            "Focus": {"AutoFocusMode": "AUTO"}
        }
        with self.assertRaises(ValueError):
            await write_focus(
                self.connection, 0.8, None, state["revision"], before_write=Mock()
            )
        self.imaging.Move.assert_not_awaited()

    async def test_absolute_move_checks_guard_and_verifies_actual_position(self):
        state = (await read_focus(self.connection))["focus"]
        self.imaging.GetStatus.side_effect = [
            self.position,
            {"FocusStatus": {"Position": 0.8}},
        ]
        guard = Mock()
        result = await write_focus(
            self.connection, 0.8, 0.2, state["revision"], before_write=guard
        )
        self.assertEqual(result["focus"]["position"], 0.8)
        guard.assert_called_once_with("main")
        self.imaging.Move.assert_awaited_once_with(
            {
                "VideoSourceToken": "sensor",
                "Focus": {"Absolute": {"Position": 0.8, "Speed": 0.2}},
            }
        )

    async def test_unconfirmed_motion_is_not_retried(self):
        state = (await read_focus(self.connection))["focus"]
        with self.assertRaises(ImagingWriteUnverified):
            await write_focus(
                self.connection, 0.8, None, state["revision"], before_write=Mock()
            )
        self.imaging.Move.assert_awaited_once()


class TestAdministrationOwnership(unittest.TestCase):
    def test_automatic_or_uncertain_physical_image_ownership_blocks_admin_and_focus(
        self,
    ):
        manager = OnvifDeviceManager(lambda: None)
        for lease in (ImageLease(owned={"brightness": 30}), ImageLease(blocked=True)):
            manager._leases["camera"] = lease
            with self.assertRaises(OnvifSettingsConflict):
                manager.guard_administration("camera")
            with self.assertRaises(OnvifSettingsConflict):
                manager.guard_manual_focus("camera", "main")

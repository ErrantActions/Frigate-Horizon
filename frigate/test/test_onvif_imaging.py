"""Image-control tests with mock devices only, without network connections."""

import copy
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from onvif import ONVIFError
from zeep.exceptions import Fault

from frigate.api.defs.request.onvif_body import OnvifConnectionBody
from frigate.ptz.imaging import (
    ImagingReadbackMismatch,
    ImagingWriteUnverified,
    read_imaging,
    write_imaging,
)
from frigate.util.onvif_device import OnvifSettingsConflict


class TestOnvifImaging(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.connection = OnvifConnectionBody(host="camera.invalid")
        self.settings = {
            "Brightness": 50,
            "Contrast": 40,
            "Exposure": {
                "Mode": "AUTO",
                "Gain": 20,
                "ExposureTime": 10000,
                "MinGain": 0,
                "MaxGain": 50,
            },
            "Extension": {"VendorSetting": "untouched"},
        }
        self.options = {
            "Brightness": {"Min": 0, "Max": 100},
            "Contrast": {"Min": 0, "Max": 100},
            "Exposure": {
                "Mode": ["AUTO", "MANUAL"],
                "Gain": {"Min": 0, "Max": 50},
                "MinGain": {"Min": 0, "Max": 50},
                "MaxGain": {"Min": 0, "Max": 50},
            },
        }
        self.media = SimpleNamespace(
            GetProfiles=AsyncMock(
                return_value=[
                    self.profile("main", "source-1"),
                    self.profile("sub", "source-1"),
                ]
            )
        )
        self.imaging = SimpleNamespace(
            GetOptions=AsyncMock(return_value=self.options),
            GetImagingSettings=AsyncMock(return_value=self.settings),
            SetImagingSettings=AsyncMock(),
        )
        self.opened = []

        @asynccontextmanager
        async def session(connection):
            self.opened.append(connection.host)
            yield object()

        async def service(camera, name, connection):
            return {"media": self.media, "imaging": self.imaging}[name]

        self.session_patch = patch("frigate.ptz.imaging.camera_session", session)
        self.service_patch = patch(
            "frigate.ptz.imaging.create_camera_service", side_effect=service
        )
        self.session_patch.start()
        self.service_patch.start()
        self.addCleanup(self.session_patch.stop)
        self.addCleanup(self.service_patch.stop)

    @staticmethod
    def profile(token, source):
        return {
            "token": token,
            "Name": token.title(),
            "VideoSourceConfiguration": {"SourceToken": source},
        }

    async def apply(self, changes, expected, confirm=True):
        return await write_imaging(
            self.connection,
            changes,
            expected,
            confirm_device_change=confirm,
        )

    async def test_shared_main_and_substream_select_one_physical_source(self):
        response = await read_imaging(self.connection)
        self.assertEqual(response.state, "ready")
        self.assertEqual(response.profile_token, "main")
        self.assertEqual(response.imaging.video_source_token, "source-1")
        self.assertEqual(len(response.profiles), 2)
        self.imaging.GetOptions.assert_awaited_once_with(
            {
                "VideoSourceToken": "source-1",
            }
        )
        self.imaging.SetImagingSettings.assert_not_awaited()

    async def test_multi_source_camera_requires_selection_without_imaging_calls(self):
        self.media.GetProfiles.return_value.append(self.profile("lens-2", "source-2"))
        response = await read_imaging(self.connection)
        self.assertEqual(response.state, "choose_profile")
        self.assertIsNone(response.profile_token)
        self.assertIsNone(response.imaging)
        self.imaging.GetOptions.assert_not_awaited()
        self.imaging.GetImagingSettings.assert_not_awaited()
        with self.assertRaises(ValueError):
            await self.apply({"brightness": 60}, {"brightness": 50})
        self.imaging.SetImagingSettings.assert_not_awaited()

    async def test_selected_profile_targets_its_physical_source(self):
        self.media.GetProfiles.return_value.append(self.profile("lens-2", "source-2"))
        self.connection.profile_token = "lens-2"
        response = await read_imaging(self.connection)
        self.assertEqual(response.imaging.video_source_token, "source-2")
        self.imaging.GetOptions.assert_awaited_once_with(
            {
                "VideoSourceToken": "source-2",
            }
        )

    async def test_unknown_profile_or_missing_sources_never_write(self):
        self.connection.profile_token = "missing"
        with self.assertRaises(ValueError):
            await self.apply({"brightness": 60}, {"brightness": 50})
        self.connection.profile_token = None
        self.media.GetProfiles.return_value = []
        with self.assertRaises(ValueError):
            await read_imaging(self.connection)
        self.imaging.SetImagingSettings.assert_not_awaited()

    async def test_missing_options_are_explicitly_read_only(self):
        self.imaging.GetOptions.return_value = {}
        response = await read_imaging(self.connection)
        self.assertEqual(response.state, "read_only")
        self.assertEqual(response.imaging.controls[0].value, 50)
        with self.assertRaises(ValueError):
            await self.apply({"brightness": 60}, {"brightness": 50})
        self.imaging.SetImagingSettings.assert_not_awaited()

    async def test_confirmation_and_nonempty_changes_required_before_connection(self):
        for confirm in (False, 1, "true"):
            with self.subTest(confirm=confirm), self.assertRaises(ValueError):
                await self.apply({"brightness": 60}, {"brightness": 50}, confirm)
        with self.assertRaises(ValueError):
            await self.apply({}, {})
        self.assertEqual(self.opened, [])

    async def test_rejects_stale_unsupported_and_out_of_bounds_changes(self):
        for changes, expected, error in (
            ({"brightness": 60}, {"brightness": 49}, OnvifSettingsConflict),
            ({"brightness": 101}, {"brightness": 50}, ValueError),
            ({"noise_reduction": 1}, {"noise_reduction": None}, ValueError),
            ({"brightness": 60}, {}, ValueError),
        ):
            with self.subTest(changes=changes), self.assertRaises(error):
                await self.apply(changes, expected)
        self.imaging.SetImagingSettings.assert_not_awaited()

    async def test_write_is_minimal_temporary_and_verified(self):
        observed = copy.deepcopy(self.settings)
        observed["Brightness"] = 60
        self.imaging.GetImagingSettings.side_effect = [self.settings, observed]
        response = await self.apply({"brightness": 60}, {"brightness": 50})
        self.assertEqual(response.imaging.controls[0].value, 60)
        self.imaging.SetImagingSettings.assert_awaited_once_with(
            {
                "VideoSourceToken": "source-1",
                "ImagingSettings": {"Brightness": 60},
                "ForcePersistence": False,
            }
        )
        self.assertEqual(self.settings["Brightness"], 50)
        self.assertEqual(self.imaging.GetImagingSettings.await_count, 2)

    async def test_exposure_patch_includes_required_mode_not_unrelated_auto_readings(
        self,
    ):
        observed = copy.deepcopy(self.settings)
        observed["Exposure"]["MaxGain"] = 30
        self.imaging.GetImagingSettings.side_effect = [self.settings, observed]
        await self.apply({"exposure_max_gain": 30}, {"exposure_max_gain": 50})
        request = self.imaging.SetImagingSettings.await_args.args[0]
        self.assertEqual(
            request["ImagingSettings"],
            {
                "Exposure": {"Mode": "AUTO", "MaxGain": 30},
            },
        )
        self.assertEqual(self.settings["Exposure"]["MaxGain"], 50)

    async def test_manual_switch_allows_drifting_auto_gain_but_guards_mode(self):
        self.settings["Exposure"]["Gain"] = 35
        observed = copy.deepcopy(self.settings)
        observed["Exposure"].update({"Mode": "MANUAL", "Gain": 10})
        self.imaging.GetImagingSettings.side_effect = [self.settings, observed]
        await self.apply(
            {"exposure_mode": "MANUAL", "exposure_gain": 10},
            {"exposure_mode": "AUTO", "exposure_gain": 20},
        )
        request = self.imaging.SetImagingSettings.await_args.args[0]
        self.assertEqual(
            request["ImagingSettings"],
            {
                "Exposure": {"Mode": "MANUAL", "Gain": 10},
            },
        )

    async def test_camera_ignoring_change_is_not_reported_as_verified(self):
        with self.assertRaises(ImagingReadbackMismatch) as raised:
            await self.apply({"brightness": 60}, {"brightness": 50})
        self.assertEqual(raised.exception.fields, ("brightness",))
        self.assertEqual(raised.exception.readback.imaging.controls[0].value, 50)
        self.assertEqual(self.imaging.SetImagingSettings.await_count, 1)

    async def test_readback_timeout_does_not_retry_write(self):
        self.imaging.GetImagingSettings.side_effect = [
            self.settings,
            TimeoutError("simulated"),
        ]
        with self.assertRaises(ImagingWriteUnverified):
            await self.apply({"brightness": 60}, {"brightness": 50})
        self.assertEqual(self.imaging.SetImagingSettings.await_count, 1)

    async def test_uncertain_acknowledgements_never_retry(self):
        for error in (
            ONVIFError("synthetic SDK failure"),
            Fault("synthetic SOAP failure"),
            httpx.ReadTimeout("synthetic read timeout"),
            TimeoutError(),
            OSError(),
        ):
            with self.subTest(error=type(error).__name__):
                self.imaging.SetImagingSettings.reset_mock()
                self.imaging.SetImagingSettings.side_effect = error
                with self.assertRaises(ImagingWriteUnverified):
                    await self.apply({"brightness": 60}, {"brightness": 50})
                self.imaging.SetImagingSettings.assert_awaited_once()

    async def test_partial_write_reports_only_unconfirmed_fields(self):
        observed = copy.deepcopy(self.settings)
        observed["Brightness"] = 60
        self.imaging.GetImagingSettings.side_effect = [self.settings, observed]
        with self.assertRaises(ImagingReadbackMismatch) as raised:
            await self.apply(
                {"brightness": 60, "contrast": 70},
                {"brightness": 50, "contrast": 40},
            )
        self.assertEqual(raised.exception.fields, ("contrast",))
        self.imaging.SetImagingSettings.assert_awaited_once()

    async def test_generation_guard_runs_immediately_before_set(self):
        def stale():
            raise OnvifSettingsConflict("synthetic config replacement")

        with self.assertRaises(OnvifSettingsConflict):
            await write_imaging(
                self.connection,
                {"brightness": 60},
                {"brightness": 50},
                confirm_device_change=True,
                before_write=stale,
            )
        self.imaging.SetImagingSettings.assert_not_awaited()

    async def test_device_persistence_requires_an_explicit_argument(self):
        observed = copy.deepcopy(self.settings)
        observed["Brightness"] = 60
        self.imaging.GetImagingSettings.side_effect = [self.settings, observed]
        await write_imaging(
            self.connection,
            {"brightness": 60},
            {"brightness": 50},
            confirm_device_change=True,
            force_persistence=True,
        )
        self.assertTrue(
            self.imaging.SetImagingSettings.await_args.args[0]["ForcePersistence"]
        )

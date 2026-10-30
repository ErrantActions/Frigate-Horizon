"""Mock-only ownership, restoration, race, and uncertain-write regression tests."""

import asyncio
import copy
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from frigate.api.defs.response.onvif_response import OnvifImagingResponse
from frigate.ptz.device_manager import OnvifDeviceManager
from frigate.ptz.imaging import ImagingWriteUnverified
from frigate.util.onvif_device import OnvifSettingsConflict, imaging_state


class TestPhysicalImageOwner(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.values = {"Brightness": 50, "Contrast": 40}
        self.options = {
            "Brightness": {"Min": 0, "Max": 100},
            "Contrast": {"Min": 0, "Max": 100},
        }
        environment = SimpleNamespace(
            enabled=True,
            allow_device_changes=True,
            image_profiles={
                "night": SimpleNamespace(
                    profile_token="main", settings={"brightness": 60}
                ),
                "rain": SimpleNamespace(
                    profile_token="main", settings={"contrast": 30}
                ),
            },
        )
        self.camera = SimpleNamespace(
            enabled=True,
            environment=environment,
            onvif=SimpleNamespace(
                host="camera.invalid",
                port=80,
                user="test",
                password="not-a-real-secret",
                tls_insecure=False,
                ignore_time_mismatch=False,
            ),
        )
        self.config = SimpleNamespace(cameras={"front": self.camera})
        self.owner = OnvifDeviceManager(lambda: self.config)
        self.owner.start()
        self.assertTrue(await asyncio.to_thread(self.owner._ready.wait, 2))

        async def read(connection):
            return self.response()

        async def write(connection, changes, expected, **kwargs):
            before = kwargs.get("before_write")
            if before:
                result = before()
                if result is not None:
                    await result
            for key, value in expected.items():
                name = {"brightness": "Brightness", "contrast": "Contrast"}[key]
                if self.values[name] != value:
                    raise OnvifSettingsConflict("mock conflict")
            for key, value in changes.items():
                self.values[
                    {"brightness": "Brightness", "contrast": "Contrast"}[key]
                ] = value
            return self.response()

        self.read = AsyncMock(side_effect=read)
        self.write = AsyncMock(side_effect=write)
        self.read_patch = patch("frigate.ptz.device_manager.read_imaging", self.read)
        self.write_patch = patch("frigate.ptz.device_manager.write_imaging", self.write)
        self.read_patch.start()
        self.write_patch.start()

    async def asyncTearDown(self):
        self.owner.stop()
        await asyncio.to_thread(self.owner.join, 3)
        self.assertFalse(self.owner.is_alive())
        self.read_patch.stop()
        self.write_patch.stop()

    def response(self):
        return OnvifImagingResponse(
            profiles=[],
            profile_token="main",
            state="ready",
            imaging=imaging_state(copy.deepcopy(self.values), self.options, "sensor"),
        )

    async def apply(self, profiles):
        await self.owner.automatic_image("front", profiles, self.config)

    async def test_disabled_opt_in_never_reads_or_writes_camera(self):
        self.camera.environment.allow_device_changes = False
        await self.apply(("night",))
        self.read.assert_not_awaited()
        self.write.assert_not_awaited()

    async def test_layers_merge_and_restore_only_owned_baseline_values(self):
        await self.apply(("night", "rain"))
        self.assertEqual(self.values, {"Brightness": 60, "Contrast": 30})
        self.assertFalse(self.write.await_args.kwargs.get("force_persistence", False))
        await self.apply(("night",))
        self.assertEqual(self.values, {"Brightness": 60, "Contrast": 40})
        await self.apply(())
        self.assertEqual(self.values, {"Brightness": 50, "Contrast": 40})
        self.assertEqual((await self.owner.image_status("front"))["phase"], "restored")

    async def test_stable_plan_does_not_repeat_writes(self):
        await self.apply(("night",))
        await self.apply(("night",))
        self.write.assert_awaited_once()

    async def test_external_edit_blocks_restoration_and_does_not_overwrite_it(self):
        await self.apply(("night",))
        self.values["Brightness"] = 77
        await self.apply(())
        self.assertEqual(self.values["Brightness"], 77)
        self.assertEqual(
            (await self.owner.image_status("front"))["phase"], "external_change"
        )
        self.write.assert_awaited_once()

    async def test_uncertain_write_suspends_automation_without_retry(self):
        self.write.side_effect = ImagingWriteUnverified("mock acknowledgement lost")
        with self.assertRaises(ImagingWriteUnverified):
            await self.apply(("night",))
        await self.apply(("night",))
        await self.apply(())
        self.write.assert_awaited_once()
        self.assertTrue((await self.owner.image_status("front"))["blocked"])

    async def test_manual_change_overrides_automation_and_becomes_new_baseline(self):
        await self.owner.manual_image(
            "front", "main", {"brightness": 70}, {"brightness": 50}
        )
        await self.apply(("night",))
        self.assertEqual(self.values["Brightness"], 70)
        self.write.assert_awaited_once()
        await self.owner.resume("front")
        await self.apply(("night",))
        await self.apply(())
        self.assertEqual(self.values["Brightness"], 70)

    async def test_manual_edit_preserves_restoration_of_other_automatic_fields(self):
        await self.apply(("night", "rain"))
        await self.owner.manual_image(
            "front", "main", {"brightness": 70}, {"brightness": 60}
        )
        await self.owner.resume("front")
        await self.apply(())
        self.assertEqual(self.values, {"Brightness": 70, "Contrast": 40})
        self.assertEqual(self.write.await_count, 3)

    async def test_config_replacement_during_read_prevents_write(self):
        async def stale_read(connection):
            response = self.response()
            self.config = SimpleNamespace(cameras={"front": self.camera})
            return response

        self.read.side_effect = stale_read
        await self.apply(("night",))
        self.assertEqual(self.values["Brightness"], 50)
        self.assertEqual((await self.owner.image_status("front"))["phase"], "conflict")

    async def test_changed_camera_connection_never_restores_old_camera_baseline(self):
        await self.apply(("night",))
        self.camera.onvif.host = "different-camera.invalid"
        await self.apply(())
        self.write.assert_awaited_once()
        self.assertEqual(
            (await self.owner.image_status("front"))["phase"], "connection_changed"
        )

    async def test_different_physical_source_layers_are_not_combined(self):
        self.camera.environment.image_profiles["rain"].profile_token = "other-lens"
        await self.apply(("night", "rain"))
        self.read.assert_not_awaited()
        self.write.assert_not_awaited()
        self.assertEqual(
            (await self.owner.image_status("front"))["phase"], "source_conflict"
        )

    async def test_operations_for_one_camera_are_serialized(self):
        active = 0
        maximum = 0

        async def operation(config, guard):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1

        await asyncio.gather(
            *(self.owner.execute("front", operation) for _ in range(6))
        )
        self.assertEqual(maximum, 1)

    async def test_uncertain_intent_survives_owner_restart(self):
        path = Path.cwd() / f"onvif-owner-test-{uuid.uuid4().hex}.json"
        self.owner._journal_path = path
        restarted = None
        try:

            async def uncertain(connection, changes, expected, **kwargs):
                await kwargs["before_write"]()
                raise ImagingWriteUnverified("mock lost acknowledgement")

            self.write.side_effect = uncertain
            with self.assertRaises(ImagingWriteUnverified):
                await self.apply(("night",))
            self.owner.stop()
            await asyncio.to_thread(self.owner.join, 3)
            restarted = OnvifDeviceManager(lambda: self.config, path)
            restarted.start()
            self.assertTrue(await asyncio.to_thread(restarted._ready.wait, 2))
            await restarted.automatic_image("front", ("night",), self.config)
            self.assertEqual(self.write.await_count, 1)
            self.assertTrue((await restarted.image_status("front"))["blocked"])
        finally:
            if restarted:
                restarted.stop()
                await asyncio.to_thread(restarted.join, 3)
            path.unlink(missing_ok=True)
            path.with_suffix(".pending").unlink(missing_ok=True)

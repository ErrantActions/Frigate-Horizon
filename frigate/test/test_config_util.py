"""Tests for the shared runtime config swap helper."""

import unittest
from unittest.mock import MagicMock, patch

from frigate.api.config_util import archive_location_locked, swap_runtime_config
from frigate.config import FrigateConfig
from frigate.config.holder import ConfigHolder
from frigate.util.config import apply_section_update


class TestSwapRuntimeConfig(unittest.TestCase):
    """swap_runtime_config rebinds every collaborator to the new config."""

    def _make_app(self) -> MagicMock:
        app = MagicMock()
        app.dispatcher.comms = [MagicMock(), MagicMock()]
        app.config_holder = ConfigHolder(MagicMock(name="boot_config"))
        return app

    def test_rebinds_all_references(self) -> None:
        app = self._make_app()
        config = MagicMock(name="new_config")

        swap_runtime_config(app, config)

        self.assertIs(app.frigate_config, config)
        app.genai_manager.update_config.assert_called_once_with(config)
        app.profile_manager.update_config.assert_called_once_with(config)
        self.assertIs(app.stats_emitter.config, config)
        app.stats_emitter.hardware_stats.set_config.assert_called_once_with(config)
        self.assertIs(app.dispatcher.config, config)
        app.storage_maintainer.update_config.assert_called_once_with(config)
        app.state.vector_previews.update_cameras.assert_called_once_with(config.cameras)
        self.assertIs(app.record_cleanup.config, config)
        self.assertIs(app.event_cleanup.config, config)
        self.assertEqual(app.event_cleanup.camera_keys, list(config.cameras))
        for comm in app.dispatcher.comms:
            self.assertIs(comm.config, config)

    def test_reapplies_runtime_state_after_swap(self) -> None:
        app = self._make_app()
        config = MagicMock(name="new_config")

        swap_runtime_config(app, config)

        # the swap rebuilds cameras from yaml, so overrides must be re-layered
        app.dispatcher.reapply_runtime_state_to_config.assert_called_once_with()

    def test_updates_the_config_holder(self) -> None:
        app = self._make_app()
        holder = app.config_holder
        config = MagicMock(name="new_config")

        swap_runtime_config(app, config)

        self.assertIs(holder.config, config)

    def test_deferred_factory_builds_from_the_swapped_config(self) -> None:
        """A watchdog-style factory must not rebuild from the boot config.

        The factories in FrigateApp are lambdas evaluated when a process is
        restarted, long after a user may have saved. Reading through the
        holder is what keeps a rebuilt process from reverting every change
        made since Frigate started.
        """
        app = self._make_app()
        holder = app.config_holder
        boot_config = holder.config
        factory = lambda: holder.config  # noqa: E731
        self.assertIs(factory(), boot_config)

        config = MagicMock(name="new_config")
        swap_runtime_config(app, config)

        self.assertIs(factory(), config)

    def test_tolerates_missing_optional_collaborators(self) -> None:
        app = MagicMock()
        app.profile_manager = None
        app.stats_emitter = None
        app.dispatcher = None
        app.config_holder = None
        app.storage_maintainer = None
        app.record_cleanup = None
        app.event_cleanup = None
        config = MagicMock(name="new_config")

        # must not raise when the optional collaborators are absent
        swap_runtime_config(app, config)

        self.assertIs(app.frigate_config, config)
        app.genai_manager.update_config.assert_called_once_with(config)

    def test_profile_update_preserves_archive_metadata_retention(self) -> None:
        config = FrigateConfig(
            mqtt={"enabled": False},
            storage={"enabled": True},
            cameras={
                "front": {
                    "ffmpeg": {
                        "inputs": [{"path": "rtsp://camera/video", "roles": ["detect"]}]
                    },
                    "detect": {"width": 640, "height": 480},
                }
            },
        )
        camera = config.cameras["front"]
        error = apply_section_update(camera, "record", {"continuous": {"days": 14}})
        self.assertIsNone(error)
        self.assertEqual(camera.record.continuous.days, 14)
        self.assertEqual(camera.record.archive_retention_days, 90)

    def test_storage_is_not_published_before_runtime_overrides_are_reapplied(self):
        app = self._make_app()
        config = MagicMock(name="new_config")

        def update(updated):
            app.dispatcher.reapply_runtime_state_to_config.assert_called_once_with()
            self.assertIs(updated, config)

        app.storage_maintainer.update_config.side_effect = update
        swap_runtime_config(app, config)

    def test_location_change_waits_for_archive_worker_acknowledgment(self):
        previous = FrigateConfig(mqtt={"enabled": False}, cameras={})
        updated = previous.model_copy(deep=True)
        updated.storage.archive_path = "/mnt/new-archive"
        storage = MagicMock()
        storage.archive_config_pending.return_value = True
        with patch("frigate.api.config_util.Recordings") as recordings:
            recordings.select.return_value.where.return_value.exists.return_value = (
                False
            )
            self.assertTrue(archive_location_locked(previous, updated, storage))
            storage.archive_config_pending.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()

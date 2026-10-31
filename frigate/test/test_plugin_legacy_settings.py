"""Preserve version-1 plugin settings with inactive host-generated metadata."""

import copy
import unittest

from pydantic import ValidationError

from frigate.plugins.models import PluginSettings


def plugin_settings():
    """Return a synthetic saved Python plugin without credentials."""
    return {
        "developer_mode": False,
        "plugins": {
            "ring": {
                "manifest": {
                    "api_version": 1,
                    "id": "ring",
                    "name": "Ring",
                    "version": "1.0.0",
                    "description": "Synthetic plugin settings",
                    "entrypoint": "plugin.py",
                    "capabilities": ["camera", "motion", "doorbell"],
                    "actions": ["authenticate"],
                },
                "enabled": True,
                "developer": False,
                "digest": "0" * 64,
                "bindings": {"device-1": "front"},
            }
        },
    }


def host_metadata(settings):
    """Add the inactive fields serialized by a newer host for version-1 workers."""
    entry = settings["plugins"]["ring"]
    entry.update(notification_cameras=[], notification_config_id=None)
    entry["manifest"].update(
        runtime="python", platform=None, architecture=None, configuration=[]
    )
    return settings


class TestLegacyPluginSettings(unittest.TestCase):
    def test_inactive_host_defaults_preserve_plugin_and_bindings(self):
        expected = PluginSettings.model_validate(plugin_settings())
        saved = host_metadata(plugin_settings())
        original = copy.deepcopy(saved)
        with self.assertLogs("frigate.plugins.models", level="WARNING"):
            actual = PluginSettings.model_validate(saved)
        self.assertEqual(actual, expected)
        self.assertEqual(saved, original)
        self.assertEqual(
            PluginSettings.model_validate_json(actual.model_dump_json()), expected
        )

    def test_active_subscriptions_are_not_silently_removed(self):
        for field, value in (
            ("notification_cameras", ["front"]),
            ("notification_config_id", "1" * 32),
            ("notification_cameras", None),
        ):
            with self.subTest(field=field, value=value):
                saved = host_metadata(plugin_settings())
                saved["plugins"]["ring"][field] = value
                with self.assertRaises(ValidationError):
                    PluginSettings.model_validate(saved)

    def test_nondefault_runtime_configuration_is_not_silently_removed(self):
        for field, value in (
            ("runtime", "node"),
            ("platform", "linux"),
            ("architecture", "amd64"),
            ("configuration", [{"key": "endpoint", "label": "Endpoint"}]),
        ):
            with self.subTest(field=field):
                saved = host_metadata(plugin_settings())
                saved["plugins"]["ring"]["manifest"][field] = value
                with self.assertRaises(ValidationError):
                    PluginSettings.model_validate(saved)

    def test_unknown_fields_remain_invalid(self):
        saved = host_metadata(plugin_settings())
        saved["plugins"]["ring"]["unexpected"] = True
        with self.assertRaises(ValidationError):
            PluginSettings.model_validate(saved)

    def test_version_two_is_not_downgraded(self):
        saved = host_metadata(plugin_settings())
        saved["plugins"]["ring"]["manifest"]["api_version"] = 2
        with self.assertRaises(ValidationError):
            PluginSettings.model_validate(saved)

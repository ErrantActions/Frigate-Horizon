"""Normalized native signals and manual-event bridge tests, without cameras or IPC."""

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

from lxml import etree

from frigate.ptz.native_event_bridge import NativeEventBridge, normalize_native_signal

NOW = datetime(2026, 10, 18, 6, tzinfo=UTC)
SCHEMA = "http://www.onvif.org/ver10/schema"


def notification(
    active=True, *, when=NOW, kind="motion", token="sensor", operation="Changed"
):
    topic, field = (
        ("tns1:RuleEngine/CellMotionDetector/Motion", "IsMotion")
        if kind == "motion"
        else ("tns1:Device/Trigger/DigitalInput", "LogicalState")
    )
    root = etree.Element(
        f"{{{SCHEMA}}}Message",
        UtcTime=when.isoformat(),
        PropertyOperation=operation,
    )
    source = etree.SubElement(root, f"{{{SCHEMA}}}Source")
    if token is not None:
        etree.SubElement(
            source, f"{{{SCHEMA}}}SimpleItem", Name="VideoSourceToken", Value=token
        )
    data = etree.SubElement(root, f"{{{SCHEMA}}}Data")
    etree.SubElement(
        data,
        f"{{{SCHEMA}}}SimpleItem",
        Name=field,
        Value="true" if active is True else "false" if active is False else str(active),
    )
    return {"Topic": {"_value_1": topic}, "Message": {"_value_1": root}}


class TestNativeEventBridge(unittest.TestCase):
    def setUp(self):
        self.enabled = True
        self.publish = Mock()
        self.bridge = NativeEventBridge(
            "front",
            self.publish,
            lambda: self.enabled,
            expiry_seconds=30,
            source_tokens={"VideoSourceToken": "sensor"},
        )

    def test_start_and_end_use_external_onvif_labels_not_object_classes(self):
        self.assertTrue(self.bridge.process(notification(), NOW, 100))
        payload, topic = self.publish.call_args.args
        self.assertEqual(topic, "manual_event_create")
        self.assertEqual(payload[1:3], ("front", "onvif_motion"))
        self.assertEqual(payload[5], 0)
        self.assertEqual(payload[7:10], (30, "onvif", {}))
        event_id = payload[3]
        self.assertLessEqual(len(event_id), 30)
        self.assertRegex(event_id, r"^\d+\.\d{6}-[a-f0-9]{6}$")
        self.assertTrue(
            self.bridge.process(
                notification(False, when=NOW + timedelta(seconds=2)),
                NOW + timedelta(seconds=2),
                102,
            )
        )
        self.assertEqual(
            self.publish.call_args.args,
            (
                (event_id, (NOW + timedelta(seconds=2)).timestamp()),
                "manual_event_end",
            ),
        )
        self.assertEqual(self.bridge.snapshot()["active"], 0)

    def test_duplicate_true_false_and_out_of_order_messages_do_not_create_extra_events(
        self,
    ):
        self.bridge.process(notification(), NOW, 100)
        self.bridge.process(notification(), NOW, 101)
        self.bridge.process(
            notification(when=NOW + timedelta(seconds=1)),
            NOW + timedelta(seconds=1),
            101,
        )
        self.assertEqual(self.publish.call_count, 1)
        ended = notification(False, when=NOW + timedelta(seconds=2))
        self.bridge.process(ended, NOW + timedelta(seconds=2), 102)
        self.bridge.process(ended, NOW + timedelta(seconds=2), 103)
        self.bridge.process(notification(), NOW + timedelta(seconds=3), 103)
        self.assertEqual(self.publish.call_count, 2)

    def test_same_timestamp_start_end_then_repeated_start_is_deduplicated(self):
        self.bridge.process(notification(), NOW, 100)
        self.bridge.process(notification(False), NOW, 101)
        self.bridge.process(notification(), NOW, 102)
        self.assertEqual(self.publish.call_count, 2)

    def test_expiry_is_hard_and_duplicate_state_does_not_extend_it(self):
        self.bridge.process(notification(), NOW, 100)
        self.bridge.process(
            notification(when=NOW + timedelta(seconds=20)),
            NOW + timedelta(seconds=20),
            120,
        )
        self.bridge.expire(NOW + timedelta(seconds=31), 131)
        self.assertEqual(self.publish.call_count, 2)
        self.assertEqual(
            self.publish.call_args.args[0][1], (NOW + timedelta(seconds=30)).timestamp()
        )
        self.bridge.expire(NOW + timedelta(seconds=40), 140)
        self.assertEqual(self.publish.call_count, 2)

    def test_disabled_behavior_never_publishes_and_disable_ends_an_existing_event(self):
        self.enabled = False
        self.bridge.process(notification(), NOW, 100)
        self.publish.assert_not_called()
        self.enabled = True
        self.bridge.process(notification(), NOW, 100)
        self.enabled = False
        self.bridge.expire(NOW + timedelta(seconds=1), 101)
        self.assertEqual(self.publish.call_count, 2)
        self.assertEqual(self.bridge.snapshot()["active"], 0)

    def test_alarm_and_motion_are_distinct_bounded_external_events(self):
        self.bridge.process(notification(), NOW, 100)
        self.bridge.process(notification(kind="alarm"), NOW, 100)
        labels = [call.args[0][2] for call in self.publish.call_args_list]
        self.assertEqual(labels, ["onvif_motion", "onvif_alarm"])
        self.bridge.close(NOW + timedelta(seconds=1))
        self.bridge.close(NOW + timedelta(seconds=2))
        self.assertEqual(self.publish.call_count, 4)

    def test_unknown_vendor_topics_classes_invalid_values_and_stale_packets_are_ignored(
        self,
    ):
        messages = [
            notification(when=NOW - timedelta(seconds=16)),
            notification(when=NOW + timedelta(seconds=6)),
            notification(active="person"),
            notification(when=NOW.replace(tzinfo=None)),
        ]
        unknown = notification()
        unknown["Topic"]["_value_1"] = "vendor:PersonDetected"
        messages.append(unknown)
        for message in messages:
            with self.subTest(message=message["Topic"]):
                self.assertIsNone(normalize_native_signal(message, NOW))
                self.bridge.process(message, NOW, 100)
        self.publish.assert_not_called()

    def test_selected_physical_source_is_enforced(self):
        self.bridge.process(notification(token="other-lens"), NOW, 100)
        self.bridge.process(notification(token=None), NOW, 100)
        self.publish.assert_not_called()
        self.bridge.process(notification(), NOW, 100)
        self.publish.assert_called_once()

    def test_device_level_alarm_without_source_only_allowed_for_single_source_device(
        self,
    ):
        self.bridge.process(notification(kind="alarm", token=None), NOW, 100)
        self.publish.assert_not_called()
        self.bridge.allow_unscoped = True
        self.bridge.process(notification(kind="alarm", token=None), NOW, 100)
        self.publish.assert_called_once()

    def test_deleted_property_ends_owned_event(self):
        self.bridge.process(notification(), NOW, 100)
        deleted = notification(when=NOW + timedelta(seconds=1), operation="Deleted")
        self.bridge.process(deleted, NOW + timedelta(seconds=1), 101)
        self.assertEqual(self.publish.call_count, 2)
        self.assertEqual(self.bridge.snapshot()["active"], 0)

    def test_signal_bus_failure_is_explicit_and_no_raw_vendor_payload_is_published(
        self,
    ):
        self.publish.side_effect = RuntimeError("synthetic bus unavailable")
        self.bridge.process(notification(token="sensor"), NOW, 100)
        self.assertEqual(self.bridge.snapshot()["error_code"], "nvr_signal_unavailable")
        self.assertEqual(self.bridge.snapshot()["active"], 0)
        self.assertNotIn("sensor", repr(self.bridge.snapshot()))

    def test_unknown_source_configuration_is_not_treated_as_an_unscoped_alarm(self):
        message = notification(kind="alarm", token=None)
        source = message["Message"]["_value_1"].find(f"{{{SCHEMA}}}Source")
        etree.SubElement(
            source,
            f"{{{SCHEMA}}}SimpleItem",
            Name="VideoSourceConfigurationToken",
            Value="unknown",
        )
        self.bridge.allow_unscoped = True
        self.bridge.process(message, NOW, 100)
        self.publish.assert_not_called()

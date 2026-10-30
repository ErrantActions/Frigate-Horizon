"""Native alarms follow label policy without manual-event or zone assumptions."""

import unittest
from types import SimpleNamespace

from frigate.review.types import SeverityEnum, native_review_severity


def policy(
    *,
    alerts=True,
    detections=True,
    alert_labels=None,
    detection_labels=None,
    alert_zones=(),
    detection_zones=(),
):
    return SimpleNamespace(
        alerts=SimpleNamespace(
            enabled=alerts,
            labels=["person", "car"] if alert_labels is None else alert_labels,
            required_zones=list(alert_zones),
        ),
        detections=SimpleNamespace(
            enabled=detections,
            labels=detection_labels,
            required_zones=list(detection_zones),
        ),
    )


class TestNativeReviewSeverity(unittest.TestCase):
    def test_broad_vehicle_is_a_detection_not_an_invented_car_alert(self):
        self.assertEqual(
            native_review_severity(policy(), "vehicle"), SeverityEnum.detection
        )

    def test_person_matches_configured_alerts(self):
        self.assertEqual(native_review_severity(policy(), "person"), SeverityEnum.alert)

    def test_animals_follow_default_all_detection_labels(self):
        self.assertEqual(
            native_review_severity(policy(), "animal"), SeverityEnum.detection
        )

    def test_explicit_native_vehicle_alert_is_supported(self):
        self.assertEqual(
            native_review_severity(policy(alert_labels=["vehicle"]), "vehicle"),
            SeverityEnum.alert,
        )

    def test_car_and_pet_filters_do_not_match_broad_camera_labels(self):
        review = policy(detection_labels=["person", "car", "cat", "dog", "motion"])
        for label in ("vehicle", "animal"):
            with self.subTest(label=label):
                self.assertIsNone(native_review_severity(review, label))

    def test_explicit_camera_labels_restore_categories_without_renaming_objects(self):
        review = policy(
            alert_labels=["person", "car", "vehicle"],
            detection_labels=["person", "car", "cat", "dog", "vehicle", "animal"],
        )
        for label, severity in (
            ("person", SeverityEnum.alert),
            ("car", SeverityEnum.alert),
            ("vehicle", SeverityEnum.alert),
            ("cat", SeverityEnum.detection),
            ("dog", SeverityEnum.detection),
            ("animal", SeverityEnum.detection),
        ):
            with self.subTest(label=label):
                self.assertEqual(native_review_severity(review, label), severity)

    def test_explicit_camera_labels_still_cannot_claim_zone_entry(self):
        review = policy(
            alert_labels=["person", "car", "vehicle"],
            detection_labels=["vehicle", "animal"],
            alert_zones=["driveway"],
            detection_zones=["driveway"],
        )
        for label in ("vehicle", "animal"):
            with self.subTest(label=label):
                self.assertIsNone(native_review_severity(review, label))

    def test_disabled_alerts_fall_through_to_detections(self):
        self.assertEqual(
            native_review_severity(policy(alerts=False), "person"),
            SeverityEnum.detection,
        )

    def test_disabled_detections_do_not_fall_back_to_an_alert(self):
        self.assertIsNone(native_review_severity(policy(detections=False), "vehicle"))

    def test_explicit_detection_labels_are_respected(self):
        self.assertIsNone(
            native_review_severity(policy(detection_labels=["person"]), "vehicle")
        )

    def test_native_alarms_cannot_prove_alert_zone_entry(self):
        self.assertEqual(
            native_review_severity(policy(alert_zones=["driveway"]), "person"),
            SeverityEnum.detection,
        )

    def test_neither_category_can_claim_an_unverified_zone(self):
        self.assertIsNone(
            native_review_severity(
                policy(alert_zones=["driveway"], detection_zones=["driveway"]), "person"
            )
        )

    def test_alert_priority_is_preserved_when_both_label_lists_match(self):
        self.assertEqual(
            native_review_severity(policy(detection_labels=["person"]), "person"),
            SeverityEnum.alert,
        )

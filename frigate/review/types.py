from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from frigate.config.camera.review import ReviewConfig


class SeverityEnum(str, Enum):
    alert = "alert"
    detection = "detection"


def native_review_severity(review: "ReviewConfig", label: str) -> SeverityEnum | None:
    """Apply label rules without claiming camera-wide alarms entered a Frigate zone."""
    if (
        review.alerts.enabled
        and label in review.alerts.labels
        and not review.alerts.required_zones
    ):
        return SeverityEnum.alert
    if (
        review.detections.enabled
        and (review.detections.labels is None or label in review.detections.labels)
        and not review.detections.required_zones
    ):
        return SeverityEnum.detection
    return None

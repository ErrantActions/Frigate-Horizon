"""Strict ONVIF boolean signals bridged to bounded, explicitly external NVR events."""

import hashlib
import math
import secrets
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime

from lxml import etree

SCHEMA = "http://www.onvif.org/ver10/schema"
TOPICS = {
    "RuleEngine/CellMotionDetector/Motion": ("motion", "IsMotion"),
    "VideoSource/MotionAlarm": ("motion", "State"),
    "Device/Trigger/DigitalInput": ("alarm", "LogicalState"),
}
SOURCE_FIELDS = {
    "VideoSourceConfigurationToken",
    "VideoSourceToken",
    "InputToken",
    "Rule",
}


def _value(obj, name):
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


@dataclass(frozen=True)
class NativeSignal:
    kind: str
    active: bool
    source_key: str
    timestamp: datetime
    operation: str = "Changed"


def normalize_native_signal(
    notification, now: datetime, source_tokens=None, allow_unscoped=False
):
    """Recognize only allowlisted topics with fresh standard boolean XML values."""
    topic = _value(notification, "Topic")
    text = _value(topic, "_value_1")
    if not isinstance(text, str) or len(text) > 256:
        return None
    path = "/".join(part.rsplit(":", 1)[-1] for part in text.split("/"))
    definition = TOPICS.get(path)
    if definition is None:
        return None
    message = _value(notification, "Message")
    payload = _value(message, "_value_1")
    if payload is None:
        payload = message
    if not isinstance(payload, etree._Element):
        return None
    if payload.tag != f"{{{SCHEMA}}}Message":
        payload = payload.find(f".//{{{SCHEMA}}}Message")
    if payload is None or len(payload) > 8:
        return None
    for group in ("Source", "Data"):
        element = payload.find(f"{{{SCHEMA}}}{group}")
        if element is not None and len(element) > 64:
            return None
    operation = payload.get("PropertyOperation")
    if operation not in ("Changed", "Initialized", "Deleted"):
        return None
    try:
        timestamp = datetime.fromisoformat(
            payload.get("UtcTime", "").replace("Z", "+00:00")
        )
        age = (now - timestamp).total_seconds()
    except (ValueError, TypeError):
        return None
    if timestamp.tzinfo is None or not -5 <= age <= 15:
        return None
    sources = []
    for item in payload.findall(f"{{{SCHEMA}}}Source/{{{SCHEMA}}}SimpleItem"):
        if item.get("Name") in SOURCE_FIELDS:
            value = item.get("Value", "")
            if len(value) > 256:
                return None
            sources.append((item.get("Name"), value))
    if source_tokens is not None:
        physical = [
            (name, value)
            for name, value in sources
            if name in ("VideoSourceToken", "VideoSourceConfigurationToken")
        ]
        if physical:

            def matches(name, value):
                expected = source_tokens.get(name)
                return (
                    value in expected
                    if isinstance(expected, (set, frozenset, tuple, list))
                    else expected == value
                )

            if not all(matches(name, value) for name, value in physical):
                return None
        elif not allow_unscoped:
            return None
    expected = definition[1]
    values = [
        item.get("Value")
        for item in payload.findall(f"{{{SCHEMA}}}Data/{{{SCHEMA}}}SimpleItem")
        if item.get("Name") == expected
    ]
    if operation == "Deleted":
        active = False
    elif len(values) == 1 and values[0] in ("true", "1", "false", "0"):
        active = values[0] in ("true", "1")
    else:
        return None
    # Raw source identifiers can include private vendor data. Neither event
    # labels nor API status expose them.
    key = hashlib.sha256(repr((path, sorted(sources))).encode()).hexdigest()
    return NativeSignal(definition[0], active, key, timestamp, operation)


@dataclass
class SignalLease:
    event_id: str
    started: float
    expires: float


class NativeEventBridge:
    """Opt-in manual-event producer, with no inferred object class or bounding box."""

    def __init__(
        self,
        camera,
        publish,
        allowed,
        *,
        expiry_seconds=30,
        source_tokens=None,
        allow_unscoped=False,
    ):
        if type(expiry_seconds) is not int or not 1 <= expiry_seconds <= 300:
            raise ValueError("Native event expiry must be between 1 and 300 seconds")
        self.camera = camera
        self.publish = publish
        self.allowed = allowed
        self.expiry_seconds = expiry_seconds
        self.active: dict[str, SignalLease] = {}
        self.seen: OrderedDict[str, list[tuple[datetime, bool]]] = OrderedDict()
        self.last_start: dict[str, float] = {}
        self.error_code = None
        self.starts = 0
        self.ends = 0
        self.source_tokens = source_tokens
        self.allow_unscoped = allow_unscoped

    def snapshot(self):
        """Expose bounded signal status, never vendor payloads or credentials."""
        return {
            "active": len(self.active),
            "starts": self.starts,
            "ends": self.ends,
            "expiry_seconds": self.expiry_seconds,
            "error_code": self.error_code,
        }

    def _emit(self, payload, topic):
        try:
            self.publish(payload, topic)
        except RuntimeError:
            self.error_code = "nvr_signal_unavailable"
            return False
        return True

    def process(self, notification, now: datetime, monotonic_now: float):
        """Create/end on true/false edges; duplicates never create another event."""
        self.expire(now, monotonic_now)
        if not self.allowed() or not math.isfinite(monotonic_now):
            return False
        signal = normalize_native_signal(
            notification, now, self.source_tokens, self.allow_unscoped
        )
        if signal is None:
            return False
        markers = self.seen.get(signal.source_key, [])
        marker = (signal.timestamp, signal.active)
        if marker in markers or (markers and signal.timestamp < markers[-1][0]):
            return False
        markers.append(marker)
        self.seen[signal.source_key] = markers[-8:]
        self.seen.move_to_end(signal.source_key)
        while len(self.seen) > 128:
            self.seen.popitem(last=False)
        lease = self.active.get(signal.source_key)
        if not signal.active:
            if lease is not None:
                self._end(signal.source_key, now.timestamp())
                return True
            return False
        if lease is not None or len(self.active) >= 32:
            return False
        if monotonic_now - self.last_start.get(signal.kind, -float("inf")) < 1:
            return False
        event_id = f"{now.timestamp():.6f}-{secrets.token_hex(3)}"
        # For ONVIF only, duration is a hard expiry enforced independently in
        # the object processor, not a claim about how long motion will last.
        payload = (
            now.timestamp(),
            self.camera,
            f"onvif_{signal.kind}",
            event_id,
            True,
            0,
            None,
            self.expiry_seconds,
            "onvif",
            {},
            None,
        )
        if not self._emit(payload, "manual_event_create"):
            return False
        self.active[signal.source_key] = SignalLease(
            event_id,
            now.timestamp(),
            monotonic_now + self.expiry_seconds,
        )
        self.last_start[signal.kind] = monotonic_now
        self.starts += 1
        return True

    def _end(self, key, end_time):
        lease = self.active.pop(key)
        self._emit((lease.event_id, max(lease.started, end_time)), "manual_event_end")
        self.ends += 1

    def expire(self, now: datetime, monotonic_now: float):
        """Expire hard leases even while camera RPCs are waiting or reconnecting."""
        if not math.isfinite(monotonic_now):
            return
        allowed = self.allowed()
        for key, lease in list(self.active.items()):
            if not allowed or lease.expires <= monotonic_now:
                self._end(
                    key, min(now.timestamp(), lease.started + self.expiry_seconds)
                )

    def close(self, now: datetime | None = None):
        """End all owned external events on stop, config change, or disconnect."""
        timestamp = (now or datetime.now(UTC)).timestamp()
        for key in list(self.active):
            self._end(key, timestamp)

"""Opt-in native PullPoint monitoring with renewal, reconnect and bounded history."""

import asyncio
import time
from collections import deque
from datetime import UTC, datetime, timedelta

import httpx
from onvif import ONVIFError
from zeep.exceptions import Error as ZeepError

from frigate.ptz.connection import camera_session, create_camera_service
from frigate.ptz.native_event_bridge import normalize_native_signal
from frigate.util.onvif_device import device_value

EVENT_NS = "http://www.onvif.org/ver10/events/wsdl"
DEVICE_ERRORS = (
    ONVIFError,
    ZeepError,
    httpx.HTTPError,
    TimeoutError,
    OSError,
    ValueError,
)


def renewal_delay(response):
    """Use the device's relative lease interval, never the camera's wall clock."""
    current = device_value(response, "CurrentTime")
    termination = device_value(response, "TerminationTime")
    if not isinstance(current, datetime) or not isinstance(termination, datetime):
        raise ValueError("The camera did not report a usable subscription lease")
    try:
        seconds = (termination - current).total_seconds()
    except TypeError:
        raise ValueError(
            "The camera returned incompatible subscription timestamps"
        ) from None
    if seconds <= 1:
        raise ValueError("The camera returned an expired subscription")
    return min(seconds * 0.7, seconds - 0.1)


class NativeEventMonitor:
    """A runtime-only subscription; no device event action rules are changed."""

    def __init__(self, connection, current, clock=None, bridge=None):
        self.connection = connection
        self.current = current
        self.history = deque(maxlen=100)
        self.phase = "connecting"
        self.failures = 0
        self.task = None
        self.last_message = None
        self._clock = clock or time.monotonic
        self.bridge = bridge

    def snapshot(self):
        """Return bounded secret-safe status, excluding arbitrary vendor payloads."""
        return {
            "state": self.phase,
            "failures": self.failures,
            "last_message": self.last_message,
            "events": list(self.history),
            "integration": "manual_events" if self.bridge else "status_history_only",
            "signals": self.bridge.snapshot() if self.bridge else None,
        }

    async def run(self):
        """Own the subscription and close all external events on every exit path."""
        watchdog = asyncio.create_task(self._expire_signals()) if self.bridge else None
        try:
            await self._monitor()
        finally:
            if watchdog is not None:
                watchdog.cancel()
                await asyncio.gather(watchdog, return_exceptions=True)
            if self.bridge:
                self.bridge.close()
            self.phase = "stopped"

    async def _expire_signals(self):
        while self.current():
            self.bridge.expire(datetime.now(UTC), time.monotonic())
            await asyncio.sleep(0.5)
        self.bridge.close()

    async def _monitor(self):
        """Renew before expiry and recreate subscriptions after read/renew failures."""
        while self.current():
            try:
                async with camera_session(self.connection) as camera:
                    events = await create_camera_service(
                        camera, "events", self.connection
                    )
                    created = await events.CreatePullPointSubscription(
                        {
                            "InitialTerminationTime": "PT60S",
                        }
                    )
                    address = device_value(
                        created, "SubscriptionReference", "Address", "_value_1"
                    )
                    if not address:
                        raise ValueError(
                            "The camera did not report a subscription address"
                        )
                    camera.xaddrs[f"{EVENT_NS}/PullPointSubscription"] = (
                        camera.rewrite_url(address)
                    )
                    pull = await create_camera_service(
                        camera,
                        "pullpoint",
                        self.connection,
                        "PullPointSubscription",
                    )
                    subscription = await create_camera_service(
                        camera,
                        "subscription",
                        self.connection,
                        "PullPointSubscription",
                    )
                    renew_at = self._clock() + renewal_delay(created)
                    try:
                        self.phase = "subscribed"
                        self.failures = 0
                        while self.current():
                            if self._clock() >= renew_at:
                                renewed = await subscription.Renew(
                                    {"TerminationTime": "PT60S"}
                                )
                                renew_at = self._clock() + renewal_delay(renewed)
                            remaining = max(0.1, min(5, renew_at - self._clock()))
                            response = await pull.PullMessages(
                                {
                                    "Timeout": timedelta(seconds=remaining),
                                    "MessageLimit": 50,
                                }
                            )
                            for notification in (
                                device_value(response, "NotificationMessage") or []
                            ):
                                if self.bridge:
                                    self.bridge.process(
                                        notification,
                                        datetime.now(UTC),
                                        time.monotonic(),
                                    )
                                topic = device_value(notification, "Topic", "_value_1")
                                # Arbitrary native payloads can contain vendor secrets. The UI
                                # only displays the declared topic and receipt time.
                                if isinstance(topic, str):
                                    self.last_message = time.time()
                                    item = {
                                        "topic": topic[:256],
                                        "received_at": self.last_message,
                                    }
                                    signal = normalize_native_signal(
                                        notification, datetime.now(UTC)
                                    )
                                    if signal is not None:
                                        item.update(
                                            kind=signal.kind,
                                            active=signal.active,
                                            camera_time=signal.timestamp.timestamp(),
                                            operation=signal.operation,
                                            source_key=signal.source_key,
                                        )
                                    self.history.append(item)
                            await asyncio.sleep(0.1)
                    finally:
                        try:
                            await asyncio.wait_for(subscription.Unsubscribe(), 3)
                        except DEVICE_ERRORS:
                            if self.bridge:
                                self.bridge.close()
                            pass
            except DEVICE_ERRORS:
                self.phase = "reconnecting"
                self.failures += 1
                await asyncio.sleep(min(30, 2 ** min(self.failures, 5)))
        self.phase = "stopped"

"""Mock-only native event lifecycle, camera search, and secret-safe replay tests."""

import unittest
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from frigate.api.defs.request.onvif_body import OnvifConnectionBody
from frigate.ptz.device_services import (
    presets,
    replay_source,
    safe_uri,
    search_recordings,
)
from frigate.ptz.native_events import NativeEventMonitor, renewal_delay
from frigate.ptz.recording_access import (
    RecordingBinding,
    RecordingSourceDenied,
    source_reference,
)
from frigate.ptz.replay_transport import ReplaySource
from frigate.test.test_onvif_recording_access import SOURCE_KEY

NOW = datetime(2026, 10, 18, tzinfo=UTC)


class TestPtzProfiles(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_camera_profiles_are_terminal_unsupported_not_pending(self):
        media = SimpleNamespace(
            GetProfiles=AsyncMock(
                return_value=[
                    {
                        "token": "main",
                        "Name": "Main",
                        "PTZConfiguration": None,
                    }
                ]
            )
        )
        camera = object()

        @asynccontextmanager
        async def session(connection):
            yield camera

        with (
            patch("frigate.ptz.device_services.camera_session", session),
            patch(
                "frigate.ptz.device_services.create_camera_service",
                AsyncMock(return_value=media),
            ) as service,
        ):
            result = await presets(OnvifConnectionBody(host="camera.invalid"))
        self.assertEqual(
            result, {"state": "unsupported", "profiles": [], "presets": []}
        )
        media.GetProfiles.assert_awaited_once()
        service.assert_awaited_once()
        self.assertEqual(service.call_args.args[1], "media")


class TestNativeEvents(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.connection = OnvifConnectionBody(host="camera.invalid")

    def test_renewal_uses_reported_relative_lease(self):
        self.assertEqual(
            renewal_delay(
                {
                    "CurrentTime": NOW,
                    "TerminationTime": NOW + timedelta(seconds=60),
                }
            ),
            42,
        )
        for response in (
            {},
            {"CurrentTime": NOW, "TerminationTime": NOW},
        ):
            with self.assertRaises(ValueError):
                renewal_delay(response)

    async def test_subscription_renews_pulls_and_unsubscribes(self):
        active = True
        events = SimpleNamespace(
            CreatePullPointSubscription=AsyncMock(
                return_value={
                    "SubscriptionReference": {
                        "Address": {"_value_1": "http://camera.invalid/subscription"}
                    },
                    "CurrentTime": NOW,
                    "TerminationTime": NOW + timedelta(seconds=60),
                }
            )
        )
        subscription = SimpleNamespace(
            Renew=AsyncMock(
                return_value={
                    "CurrentTime": NOW,
                    "TerminationTime": NOW + timedelta(seconds=60),
                }
            ),
            Unsubscribe=AsyncMock(),
        )

        async def pull(_request):
            nonlocal active
            active = False
            return {
                "NotificationMessage": [
                    {"Topic": {"_value_1": "tns1:VideoSource/MotionAlarm"}}
                ]
            }

        pullpoint = SimpleNamespace(PullMessages=AsyncMock(side_effect=pull))
        camera = SimpleNamespace(xaddrs={}, rewrite_url=lambda value: value)

        @asynccontextmanager
        async def session(connection):
            yield camera

        async def service(camera, name, connection, port_type=None):
            return {
                "events": events,
                "pullpoint": pullpoint,
                "subscription": subscription,
            }[name]

        monitor = NativeEventMonitor(
            self.connection,
            lambda: active,
            clock=Mock(side_effect=[0, 50, 50, 50]),
        )
        with (
            patch("frigate.ptz.native_events.camera_session", session),
            patch(
                "frigate.ptz.native_events.create_camera_service", side_effect=service
            ),
            patch("frigate.ptz.native_events.asyncio.sleep", AsyncMock()),
        ):
            await monitor.run()
        subscription.Renew.assert_awaited_once()
        subscription.Unsubscribe.assert_awaited_once()
        self.assertEqual(monitor.phase, "stopped")
        self.assertEqual(len(monitor.history), 1)
        self.assertEqual(monitor.history[0]["topic"], "tns1:VideoSource/MotionAlarm")

    async def test_connection_failure_reconnects_without_image_or_encoder_writes(self):
        attempts = 0

        @asynccontextmanager
        async def session(connection):
            nonlocal attempts
            attempts += 1
            raise OSError("synthetic connection failure")
            yield  # pragma: no cover

        monitor = NativeEventMonitor(self.connection, lambda: attempts < 2)
        with (
            patch("frigate.ptz.native_events.camera_session", session),
            patch("frigate.ptz.native_events.asyncio.sleep", AsyncMock()),
        ):
            await monitor.run()
        self.assertEqual(attempts, 2)
        self.assertEqual(monitor.failures, 2)


class TestCameraRecordings(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.connection = OnvifConnectionBody(host="camera.invalid")
        self.source = {"SourceId": "urn:camera:physical-lens-a"}
        self.binding = RecordingBinding(
            "main",
            "sensor",
            source_reference({"Source": self.source}, SOURCE_KEY),
            SOURCE_KEY,
        )
        self.media = SimpleNamespace(
            GetProfiles=AsyncMock(
                return_value=[
                    {
                        "token": "main",
                        "VideoSourceConfiguration": {"SourceToken": "sensor"},
                    },
                ]
            )
        )
        self.search = SimpleNamespace(
            FindRecordings=AsyncMock(return_value="search-token"),
            GetRecordingSearchResults=AsyncMock(
                return_value={
                    "SearchState": "Completed",
                    "RecordingInformation": [
                        {
                            "RecordingToken": "recording",
                            "EarliestRecording": NOW,
                            "LatestRecording": NOW + timedelta(hours=1),
                            "Source": self.source,
                        }
                    ],
                }
            ),
            EndSearch=AsyncMock(),
            GetRecordingInformation=AsyncMock(
                return_value={
                    "RecordingToken": "recording",
                    "EarliestRecording": NOW,
                    "LatestRecording": NOW + timedelta(hours=1),
                    "Source": self.source,
                }
            ),
        )
        self.replay = SimpleNamespace(
            GetReplayUri=AsyncMock(
                return_value="rtsp://user:private@camera.invalid/replay"
            )
        )

        @asynccontextmanager
        async def session(connection):
            yield object()

        async def service(camera, name, connection):
            return {"media": self.media, "search": self.search, "replay": self.replay}[
                name
            ]

        self.session_patch = patch(
            "frigate.ptz.device_services.camera_session", session
        )
        self.service_patch = patch(
            "frigate.ptz.device_services.create_camera_service", side_effect=service
        )
        self.session_patch.start()
        self.service_patch.start()
        self.addCleanup(self.session_patch.stop)
        self.addCleanup(self.service_patch.stop)

    async def test_search_bounds_results_and_releases_search_session(self):
        start = NOW + timedelta(minutes=10)
        end = NOW + timedelta(minutes=20)
        result = await search_recordings(self.connection, start, end, self.binding)
        self.assertEqual(result["recordings"][0]["start"], start)
        self.assertEqual(result["recordings"][0]["end"], end)
        self.search.EndSearch.assert_awaited_once_with({"SearchToken": "search-token"})

    async def test_failed_search_still_releases_camera_session(self):
        self.search.GetRecordingSearchResults.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            await search_recordings(
                self.connection, NOW, NOW + timedelta(hours=1), self.binding
            )
        self.search.EndSearch.assert_awaited_once()

    async def test_replay_descriptor_stays_private_and_keeps_vendor_query_tokens_server_side(
        self,
    ):
        self.replay.GetReplayUri.return_value = (
            "rtsp://camera.invalid/replay?password=private"
        )
        descriptor = ReplaySource(
            "rtsp://camera.invalid/replay?password=private",
            "test",
            "private",
            "192.0.2.10",
            554,
            "camera.invalid",
        )
        with patch(
            "frigate.ptz.device_services.pin_replay_source",
            AsyncMock(return_value=descriptor),
        ) as pin:
            await replay_source(
                self.connection,
                "recording",
                NOW,
                NOW + timedelta(minutes=1),
                self.binding,
            )
        self.assertEqual(
            pin.await_args.args[0], "rtsp://camera.invalid/replay?password=private"
        )

    async def test_unconfirmed_recording_token_never_calls_replay_service(self):
        with self.assertRaises(ValueError):
            await replay_source(
                self.connection,
                "other-recording",
                NOW,
                NOW + timedelta(minutes=1),
                self.binding,
            )
        self.replay.GetReplayUri.assert_not_awaited()

    async def test_shared_host_other_lens_token_never_reaches_replay_uri(self):
        self.media.GetProfiles.return_value.append(
            {
                "token": "lens-b",
                "VideoSourceConfiguration": {"SourceToken": "sensor-b"},
            }
        )
        other = {
            "RecordingToken": "guessed-lens-b",
            "Source": {"SourceId": "urn:camera:physical-lens-b"},
            "EarliestRecording": NOW,
            "LatestRecording": NOW + timedelta(hours=1),
        }
        self.search.GetRecordingSearchResults.return_value[
            "RecordingInformation"
        ].append(other)
        result = await search_recordings(
            self.connection, NOW, NOW + timedelta(minutes=1), self.binding
        )
        self.assertEqual(
            [entry["token"] for entry in result["recordings"]], ["recording"]
        )
        self.search.GetRecordingInformation.return_value = other
        with self.assertRaises(RecordingSourceDenied):
            await replay_source(
                self.connection,
                "guessed-lens-b",
                NOW,
                NOW + timedelta(minutes=1),
                self.binding,
            )
        self.replay.GetReplayUri.assert_not_awaited()

    async def test_missing_recording_source_attribution_is_not_exposed(self):
        del self.search.GetRecordingSearchResults.return_value["RecordingInformation"][
            0
        ]["Source"]
        with self.assertRaises(RecordingSourceDenied):
            await search_recordings(
                self.connection, NOW, NOW + timedelta(minutes=1), self.binding
            )
        self.search.EndSearch.assert_awaited_once()

    def test_camera_urls_strip_userinfo_and_queries(self):
        self.assertEqual(
            safe_uri("http://user:private@camera.invalid:80/service?token=secret"),
            "http://camera.invalid:80/service",
        )

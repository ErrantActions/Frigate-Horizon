"""Offline tests for APNs authentication, privacy, and atomic device ownership."""

import importlib.util
import json
import logging
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from joserfc import jwt
from joserfc.jwk import ECKey
from pydantic import ValidationError

from frigate.api.defs.request.native_push_body import NativePushRegistrationBody
from frigate.api.defs.response.native_push_response import NativePushDeviceMetadata
from frigate.comms.apns import (
    APNsMessage,
    APNsProvider,
    APNsProviderError,
    APNsTransport,
)
from frigate.comms.apns_registry import APNsRegistry, NativePushDevice


class TestAPNsRegistry(unittest.TestCase):
    def setUp(self):
        self.database = sqlite3.connect(":memory:")
        self.database.execute(
            'CREATE TABLE "user" (username TEXT PRIMARY KEY, role TEXT, '
            "notification_tokens JSON NOT NULL DEFAULT '[]', "
            "native_push_devices JSON NOT NULL DEFAULT '[]')"
        )
        self.database.executemany(
            'INSERT INTO "user" (username, role) VALUES (?, ?)',
            [("first", "viewer"), ("second", "admin")],
        )
        self.registry = APNsRegistry(self.database.execute)
        self.identifier = str(uuid4())

    def tearDown(self):
        self.database.close()

    def device(self, token="a" * 64, at=100.0):
        return NativePushDevice(self.identifier, token, "production", None, at)

    def test_registration_is_idempotent_and_preserves_browser_tokens(self):
        browser = '[{"endpoint":"https://example.com/browser","keys":{"auth":"test"}}]'
        self.database.execute(
            'UPDATE "user" SET notification_tokens = ? WHERE username = ?',
            (browser, "first"),
        )
        self.assertTrue(self.registry.register("first", self.device()))
        self.assertTrue(self.registry.register("first", self.device()))
        self.assertEqual(len(self.registry.devices("first")[1]), 1)
        self.assertEqual(
            self.database.execute(
                'SELECT notification_tokens FROM "user" WHERE username = ?', ("first",)
            ).fetchone()[0],
            browser,
        )

    def test_database_debug_logging_does_not_expose_native_tokens(self):
        from peewee import SqliteDatabase

        database = SqliteDatabase(":memory:")
        self.addCleanup(database.close)
        token = "c" * 96
        with self.assertLogs("peewee", level=logging.DEBUG) as logs:
            database.execute_sql('CREATE TABLE "user" (native_push_devices JSON)')
            database.execute_sql(
                'INSERT INTO "user" (native_push_devices) VALUES (?)',
                (json.dumps([{"token": token}]),),
            )
        self.assertIn("parameters omitted", "\n".join(logs.output))
        self.assertNotIn(token, "\n".join(logs.output))

    def test_new_account_claim_moves_installation_without_cross_account_delivery(self):
        self.registry.register("first", self.device())
        self.registry.register("second", self.device(token="b" * 64, at=200))
        self.assertEqual(self.registry.devices("first")[1], [])
        self.assertEqual(self.registry.devices("second")[1][0].token, "b" * 64)

    def test_unknown_account_cannot_remove_another_accounts_device(self):
        self.registry.register("first", self.device())
        self.assertFalse(self.registry.register("unknown", self.device()))
        self.assertEqual(len(self.registry.devices("first")[1]), 1)

    def test_unregistration_is_scoped_to_the_current_account(self):
        self.registry.register("first", self.device())
        self.registry.unregister("second", self.identifier)
        self.assertEqual(len(self.registry.devices("first")[1]), 1)
        self.registry.unregister("first", self.identifier)
        self.registry.unregister("first", self.identifier)
        self.assertEqual(self.registry.devices("first")[1], [])

    def test_late_expiry_does_not_remove_a_rotated_or_reregistered_token(self):
        old = self.device(at=100)
        self.registry.register("first", old)
        self.registry.register("first", self.device(token="b" * 64, at=200))
        self.registry.invalidate("first", old, 150)
        self.assertEqual(self.registry.devices("first")[1][0].token, "b" * 64)
        current = self.registry.devices("first")[1][0]
        self.registry.invalidate("first", current, 150)
        self.assertEqual(len(self.registry.devices("first")[1]), 1)
        self.registry.invalidate("first", current, 250)
        self.assertEqual(self.registry.devices("first")[1], [])

    def test_delayed_future_invalidation_cannot_remove_same_token_renewal(self):
        old = self.device(at=100)
        self.registry.register("first", old)
        self.registry.register("first", self.device(at=200))
        self.registry.invalidate("first", old, 300)
        self.assertEqual(self.registry.devices("first")[1][0].registered_at, 200)

    def test_concurrent_accounts_and_rotations_preserve_single_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.db"
            with closing(sqlite3.connect(path)) as database, database:
                database.executescript(
                    'CREATE TABLE "user" (username TEXT PRIMARY KEY, role TEXT, '
                    "native_push_devices JSON NOT NULL DEFAULT '[]');"
                    "INSERT INTO user(username,role) VALUES ('first','viewer'),('second','admin');"
                )

            def register(index):
                with closing(sqlite3.connect(path, timeout=10)) as database, database:
                    return APNsRegistry(database.execute).register(
                        "first" if index % 2 else "second",
                        self.device(token=f"{index:064x}", at=100 + index),
                    )

            with ThreadPoolExecutor(max_workers=8) as pool:
                self.assertTrue(all(pool.map(register, range(32))))
            with closing(sqlite3.connect(path)) as database:
                rows = database.execute(
                    "SELECT native_push_devices FROM user"
                ).fetchall()
            self.assertEqual(sum(len(json.loads(row[0])) for row in rows), 1)

    def test_tokens_are_variable_length_hex_and_environments_are_fixed(self):
        for length in (2, 64, 96, 512):
            self.assertEqual(len(self.device(token="a" * length).token), length)
        for token in ("", "a", "bad", "x" * 64, "a" * 65, "a" * 514):
            with self.subTest(length=len(token)), self.assertRaises(ValueError):
                self.device(token=token)
        with self.assertRaises(ValueError):
            NativePushDevice(
                self.identifier, "a" * 64, "https://other.invalid", None, 100
            )

    def test_delivery_rechecks_roles_after_an_alert_has_been_queued(self):
        self.registry.register("first", self.device())
        roles = {"viewer": [], "front_only": ["front"]}
        cameras = {"front", "garden"}
        self.assertEqual(
            len(self.registry.authorized_devices("first", "garden", roles, cameras)), 1
        )
        self.database.execute(
            'UPDATE "user" SET role = ? WHERE username = ?', ("front_only", "first")
        )
        self.assertEqual(
            self.registry.authorized_devices("first", "garden", roles, cameras), []
        )
        self.assertEqual(
            len(self.registry.authorized_devices("first", "front", roles, cameras)), 1
        )
        self.database.execute('DELETE FROM "user" WHERE username = ?', ("first",))
        self.assertEqual(
            self.registry.authorized_devices("first", "front", roles, cameras), []
        )

    def test_native_device_migration_preserves_existing_user_data(self):
        database = sqlite3.connect(":memory:")
        self.addCleanup(database.close)
        database.execute('CREATE TABLE "user" (username TEXT PRIMARY KEY, role TEXT)')
        database.execute('INSERT INTO "user" VALUES (?, ?)', ("existing", "viewer"))
        path = (
            Path(__file__).parents[2] / "migrations" / "045_add_native_push_devices.py"
        )
        spec = importlib.util.spec_from_file_location("apns_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)

        class Migrator:
            sql = staticmethod(database.execute)

        migration.migrate(Migrator(), database)
        self.assertEqual(
            database.execute(
                'SELECT username, role, native_push_devices FROM "user"'
            ).fetchone(),
            ("existing", "viewer", "[]"),
        )
        migration.rollback(Migrator(), database)
        self.assertEqual(
            database.execute('SELECT username, role FROM "user"').fetchone(),
            ("existing", "viewer"),
        )


class TestAPNsRequestModels(unittest.TestCase):
    def body(self):
        return {
            "installation_id": str(uuid4()),
            "token": "A" * 64,
            "environment": "production",
        }

    def test_registration_normalizes_tokens_without_selecting_an_owner(self):
        body = NativePushRegistrationBody.model_validate(self.body())
        self.assertEqual(body.token, "a" * 64)
        self.assertIsNone(body.camera_ids)
        for field in ("username", "topic", "endpoint", "private_key"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                NativePushRegistrationBody.model_validate(
                    {**self.body(), field: "not-allowed"}
                )

    def test_camera_opt_out_is_distinct_from_all_allowed_cameras(self):
        body = NativePushRegistrationBody.model_validate(
            {**self.body(), "camera_ids": []}
        )
        self.assertEqual(body.camera_ids, [])
        body = NativePushRegistrationBody.model_validate(
            {**self.body(), "camera_ids": ["front", "front", "garden"]}
        )
        self.assertEqual(body.camera_ids, ["front", "garden"])

    def test_invalid_token_environment_and_installation_are_rejected(self):
        for change in (
            {"token": "a" * 65},
            {"token": "non-hex-token"},
            {"environment": "https://other.invalid"},
            {"installation_id": "not-an-installation-id"},
        ):
            with self.subTest(fields=list(change)), self.assertRaises(ValidationError):
                NativePushRegistrationBody.model_validate({**self.body(), **change})

    def test_registration_metadata_never_serializes_token_or_credentials(self):
        metadata = NativePushDeviceMetadata.model_validate(
            {
                **self.body(),
                "camera_ids": None,
                "registered_at": 100,
                "private_key": "must-not-be-returned",
            }
        ).model_dump(mode="json")
        self.assertNotIn("token", metadata)
        self.assertNotIn("private_key", metadata)


class TestAPNsTransport(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.path = Path(self.directory.name) / "test-only.p8"
        self.path.write_bytes(
            self.key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        self.provider = APNsProvider(
            "test-nvr",
            "com.example.horizon",
            "TEAMID1234",
            "PUSHKEY123",
            str(self.path),
        )
        self.device = NativePushDevice(str(uuid4()), "a" * 64, "production", None, 100)
        self.now = 1000.0
        self.requests = []

    def notification(self):
        return APNsMessage(
            "first",
            {"type": "end", "after": {"id": "review-1", "camera": "front"}},
            "Sensitive person identity",
            "Private event description",
            ttl=3600,
        )

    async def test_health_requires_feature_and_explicit_opt_in_and_never_mutable_content(
        self,
    ):
        from dataclasses import replace

        from frigate.comms.native_preferences import DevicePreferences

        provider = replace(
            self.provider, image_notifications=True, include_details=True
        )
        message = APNsMessage(
            "first",
            {"identifier": "health-issue-1"},
            "Sensitive title",
            "Private detail",
            notification_type="health",
            ttl=3600,
        )
        requests = []

        async def handler(request):
            requests.append(request)
            return httpx.Response(200, extensions={"http_version": b"HTTP/2"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            transport = APNsTransport(provider, client=client, clock=lambda: self.now)
            self.assertEqual(await transport.send(message, [self.device]), [])
            featured = replace(self.device, features=["health-v1"])
            self.assertEqual(await transport.send(message, [featured]), [])
            enabled = replace(
                featured, preferences=DevicePreferences(health_alerts=True)
            )
            outcomes = await transport.send(message, [enabled])
            self.assertTrue(outcomes[0].accepted)
        self.assertEqual(len(requests), 1)
        payload = json.loads(requests[0].content)
        self.assertEqual(payload["kind"], "health")
        self.assertEqual(payload["identifier"], "health-issue-1")
        self.assertEqual(payload["aps"]["category"], "HORIZON_HEALTH")
        self.assertNotIn("mutable-content", payload["aps"])
        self.assertNotIn("Sensitive", json.dumps(payload))
        self.assertNotIn("Private", json.dumps(payload))

    async def test_installation_snooze_filters_activity_without_affecting_explicit_test(
        self,
    ):
        from dataclasses import replace

        from frigate.comms.native_preferences import DevicePreferences

        muted = replace(
            self.device,
            preferences=DevicePreferences(camera_snooze_until={"front": 5000}),
        )
        requests = []

        async def handler(request):
            requests.append(request)
            return httpx.Response(200, extensions={"http_version": b"HTTP/2"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            transport = APNsTransport(
                self.provider, client=client, clock=lambda: self.now
            )
            self.assertEqual(await transport.send(self.notification(), [muted]), [])
            explicit = APNsMessage(
                "first", {}, "Test", "Test", notification_type="test"
            )
            self.assertTrue((await transport.send(explicit, [muted]))[0].accepted)
        self.assertEqual(len(requests), 1)
        payload = json.loads(requests[0].content)
        self.assertEqual(payload["kind"], "test")
        self.assertNotIn("category", payload["aps"])

    async def send_with(self, handler, devices=None):
        async def capture(request):
            self.requests.append(request)
            return handler(request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(capture)) as client:
            transport = APNsTransport(
                self.provider, client=client, clock=lambda: self.now
            )
            result = await transport.send(self.notification(), devices or [self.device])
            return transport, result

    async def test_real_es256_provider_token_and_http2_headers(self):
        _, result = await self.send_with(
            lambda _: httpx.Response(200, extensions={"http_version": b"HTTP/2"})
        )
        self.assertTrue(result[0].accepted)
        request = self.requests[0]
        self.assertEqual(request.url.host, "api.push.apple.com")
        self.assertEqual(request.headers["apns-topic"], "com.example.horizon")
        self.assertEqual(request.headers["apns-push-type"], "alert")
        self.assertEqual(request.headers["apns-expiration"], "4600")
        public = ECKey.import_key(
            self.key.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )
        token = jwt.decode(
            request.headers["authorization"].split(" ", 1)[1],
            public,
            algorithms=["ES256"],
        )
        self.assertEqual(token.claims, {"iss": "TEAMID1234", "iat": 1000})

    async def test_payload_defaults_do_not_expose_event_details_or_server_urls(self):
        _, result = await self.send_with(
            lambda _: httpx.Response(200, extensions={"http_version": b"HTTP/2"})
        )
        self.assertTrue(result[0].accepted)
        payload = json.loads(self.requests[0].content)
        self.assertEqual(payload["kind"], "review")
        self.assertEqual(payload["identifier"], "review-1")
        self.assertEqual(payload["server_id"], "test-nvr")
        self.assertNotIn("Sensitive", self.requests[0].content.decode())
        self.assertNotIn("Private", self.requests[0].content.decode())
        self.assertNotIn("image", payload)
        self.assertNotIn("direct_url", payload)
        self.assertNotIn("mutable-content", payload["aps"])

    async def test_image_flag_is_opt_in_and_reference_free_tests_stay_text_only(self):
        from dataclasses import replace

        provider = replace(self.provider, image_notifications=True)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, extensions={"http_version": b"HTTP/2"})
            )
        ) as client:
            transport = APNsTransport(provider, client=client)
            for message in (
                self.notification(),
                APNsMessage(
                    "first", {"event_id": "event-1", "camera": "front"}, "Title", "Body"
                ),
                APNsMessage("first", {"camera": "front"}, "Title", "Body"),
            ):
                payload = transport.payload(message)
                self.assertEqual(payload["aps"]["mutable-content"], 1)
                self.assertNotIn("url", json.dumps(payload))
                self.assertNotIn("image", payload)
            payload = transport.payload(
                APNsMessage("first", {}, "Title", "Body", "test")
            )
            self.assertNotIn("mutable-content", payload["aps"])

    async def test_empty_device_scope_does_not_receive_camera_notifications(self):
        blocked = NativePushDevice(
            self.device.installation_id, "a" * 64, "production", [], 100
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: self.fail("An opted-out camera must not send a request")
            )
        ) as client:
            transport = APNsTransport(self.provider, client=client)
            self.assertEqual(await transport.send(self.notification(), [blocked]), [])

    async def test_explicit_test_reaches_empty_camera_selection(self):
        blocked = NativePushDevice(
            self.device.installation_id, "a" * 64, "sandbox", [], 100
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, extensions={"http_version": b"HTTP/2"})
            )
        ) as client:
            transport = APNsTransport(self.provider, client=client)
            result = await transport.send(
                APNsMessage("first", {}, "Test", "Test", "test"), [blocked]
            )
            self.assertTrue(result[0].accepted)

    async def test_retry_backoff_is_bounded_and_reuses_provider_token(self):
        responses = iter([429, 503, 200])

        def response(_):
            status = next(responses)
            return httpx.Response(
                status,
                json={"reason": "TooManyRequests"},
                extensions={"http_version": b"HTTP/2"},
            )

        with patch("frigate.comms.apns.asyncio.sleep") as sleep:
            _, result = await self.send_with(response)
        self.assertTrue(result[0].accepted)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(
            len({request.headers["authorization"] for request in self.requests}), 1
        )

    async def test_expired_provider_is_refreshed_once_and_invalid_token_not_retried(
        self,
    ):
        for reason, expected in (
            ("ExpiredProviderToken", 2),
            ("BadDeviceToken", 1),
            ("InvalidProviderToken", 1),
        ):
            self.requests.clear()
            with self.assertLogs("frigate.comms.apns", level=logging.WARNING):
                _, result = await self.send_with(
                    lambda _: httpx.Response(403, json={"reason": reason})
                )
            self.assertFalse(result[0].accepted)
            self.assertEqual(len(self.requests), expected)

    async def test_long_retry_after_and_non_http2_are_not_accepted(self):
        for response in (
            httpx.Response(
                429, headers={"retry-after": "120"}, json={"reason": "TooManyRequests"}
            ),
            httpx.Response(200, extensions={"http_version": b"HTTP/1.1"}),
        ):
            self.requests.clear()
            with self.assertLogs("frigate.comms.apns", level=logging.WARNING):
                _, result = await self.send_with(lambda _: response)
            self.assertFalse(result[0].accepted)
            self.assertEqual(len(self.requests), 1)

    async def test_details_are_opt_in_and_invalid_routes_are_rejected(self):
        from dataclasses import replace

        self.provider = replace(self.provider, include_details=True)
        transport, _ = await self.send_with(
            lambda _: httpx.Response(200, extensions={"http_version": b"HTTP/2"})
        )
        payload = json.loads(self.requests[0].content)
        self.assertEqual(payload["aps"]["alert"]["title"], "Sensitive person identity")
        for identifier in ("https://example.com", "../event", "unsafe%20", "bad\nid"):
            message = APNsMessage(
                "first", {"event_id": identifier, "camera": "front"}, "Title", "Body"
            )
            with self.assertRaises(APNsProviderError):
                transport.payload(message)

    async def test_unscoped_alert_cannot_bypass_camera_preferences(self):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: self.fail("An unscoped alert must not send")
            )
        ) as client:
            transport = APNsTransport(self.provider, client=client)
            with self.assertRaises(APNsProviderError):
                await transport.send(
                    APNsMessage("first", {}, "Title", "Body"), [self.device]
                )

    async def test_unregistered_token_reports_invalidation_without_exposing_token(self):
        with self.assertLogs("frigate.comms.apns", level=logging.WARNING) as logs:
            _, result = await self.send_with(
                lambda _: httpx.Response(
                    410,
                    json={"reason": "Unregistered", "timestamp": 150000},
                    extensions={"http_version": b"HTTP/2"},
                )
            )
        self.assertFalse(result[0].accepted)
        self.assertEqual(result[0].invalidated_at, 150)
        self.assertNotIn(self.device.token, "\n".join(logs.output))

    async def test_http_diagnostic_logs_redact_device_tokens(self):
        with self.assertLogs("httpx", level=logging.INFO) as logs:
            await self.send_with(
                lambda _: httpx.Response(200, extensions={"http_version": b"HTTP/2"})
            )
        self.assertIn("[redacted]", "\n".join(logs.output))
        self.assertNotIn(self.device.token, "\n".join(logs.output))

    async def test_transport_failure_is_explicit_and_does_not_log_request_url(self):
        def fail(request):
            raise httpx.ConnectError(
                f"failed request to {request.url}", request=request
            )

        with self.assertLogs("frigate.comms.apns", level=logging.WARNING) as logs:
            _, result = await self.send_with(fail)
        self.assertFalse(result[0].accepted)
        self.assertEqual(result[0].reason, "TransportError")
        self.assertNotIn(self.device.token, "\n".join(logs.output))

    async def test_malformed_provider_failure_is_reported_without_echoing_content(self):
        with self.assertLogs("frigate.comms.apns", level=logging.WARNING) as logs:
            _, result = await self.send_with(
                lambda _: httpx.Response(400, json={"reason": [self.device.token]})
            )
        self.assertFalse(result[0].accepted)
        self.assertEqual(result[0].reason, "InvalidResponse")
        self.assertNotIn(self.device.token, "\n".join(logs.output))

    async def test_provider_token_is_reused_then_refreshed_before_one_hour(self):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, extensions={"http_version": b"HTTP/2"})
            )
        ) as client:
            transport = APNsTransport(
                self.provider, client=client, clock=lambda: self.now
            )
            first = await transport._provider_token()
            self.now += 1199
            self.assertEqual(await transport._provider_token(), first)
            self.now += 1
            self.assertNotEqual(await transport._provider_token(), first)

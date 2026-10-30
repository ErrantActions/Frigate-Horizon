"""Verify Horizon routes appear in the generated API authorization document."""

import unittest

from fastapi import Depends
from fastapi.routing import APIRoute

from frigate.api import audio_repair, environment, native, onvif, playback
from frigate.api.auth import allow_any_authenticated, require_role
from generate_api_auth_spec import (
    ROUTERS,
    build_spec,
    classify_route,
    read_exempt_rules,
)


class TestApiAuthSpec(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Generate the document without starting camera or playback services."""
        cls.spec = build_spec()[0]

    def test_horizon_routers_are_documented(self):
        for router in (
            environment.router,
            onvif.router,
            playback.router,
            audio_repair.router,
            native.router,
        ):
            with self.subTest(router=router):
                self.assertIn(router, ROUTERS)
                for route in router.routes:
                    if isinstance(route, APIRoute):
                        for method in route.methods:
                            self.assertIn(
                                method.lower(), self.spec["paths"][route.path_format]
                            )

    def test_physical_device_writes_require_admin(self):
        operation = self.spec["paths"]["/onvif/cameras/{camera_name}/imaging"]["put"]
        self.assertEqual(operation["x-required-role"], "admin")

    def test_recording_audio_repairs_require_admin(self):
        operation = self.spec["paths"]["/recordings/audio-repair/jobs"]["post"]
        self.assertEqual(operation["x-required-role"], "admin")

    def test_replay_and_playback_require_camera_access(self):
        for path in (
            "/onvif/cameras/{camera_name}/replay/sessions",
            "/{camera_name}/recordings/playback-compatibility",
        ):
            with self.subTest(path=path):
                self.assertEqual(
                    self.spec["paths"][path]["post"]["x-required-role"], "camera"
                )

    def test_native_and_evidence_auth_metadata_matches_current_role_contracts(self):
        expected = {
            ("/native/features", "get"): "any",
            ("/native/cameras/{camera}/controls", "get"): "camera",
            ("/native/cameras/{camera}/ptz", "post"): "admin",
            ("/native/cameras/{camera}/talkback", "post"): "admin",
            ("/native/cameras/{camera}/talkback/{lease_id}/renew", "post"): "admin",
            ("/native/cameras/{camera}/talkback/{lease_id}", "delete"): "any",
            ("/exports/{export_id}/protect", "post"): "admin",
        }
        for (path, method), role in expected.items():
            with self.subTest(path=path, method=method):
                self.assertEqual(
                    self.spec["paths"][path][method]["x-required-role"], role
                )


class TestExplicitApiAuthMetadata(unittest.TestCase):
    def route(self, value, dependency=None):
        def endpoint():
            return {}

        return APIRoute(
            "/notifications/metadata-fixture",
            endpoint,
            methods=["POST"],
            dependencies=[Depends(dependency or allow_any_authenticated())],
            openapi_extra={"x-required-role": value},
        )

    def test_explicit_in_body_admin_and_camera_checks_override_any_authenticated(self):
        for role in ("admin", "camera"):
            result = classify_route(self.route(role), *read_exempt_rules())
            self.assertEqual(result[0], role)
            self.assertIsNone(result[2])

    def test_metadata_cannot_weaken_real_dependencies_or_global_admin(self):
        with self.assertRaisesRegex(ValueError, "weakens"):
            classify_route(
                self.route("any", require_role(["admin"])), *read_exempt_rules()
            )
        route = self.route("any")
        route.path = "/not-exempt"
        with self.assertRaisesRegex(ValueError, "weakens"):
            classify_route(route, *read_exempt_rules())

    def test_invalid_explicit_auth_metadata_is_an_error_not_an_inferred_fallback(self):
        for value in ("unknown", ["admin"], True, None):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "Invalid explicit auth"):
                    classify_route(self.route(value), *read_exempt_rules())

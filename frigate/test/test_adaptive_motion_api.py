"""Adaptive diagnostic bounds, camera authorization and stale-state behavior."""

import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from frigate.api.adaptive_motion import router
from frigate.api.auth import require_admin_by_default
from frigate.config.camera.motion import MotionConfig
from frigate.motion.adaptive_runtime import atomic_json, cache_path


class TestAdaptiveMotionApi(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch("frigate.motion.adaptive_runtime.CACHE_DIR", directory))
        self.app = FastAPI(dependencies=[Depends(require_admin_by_default())])
        self.motion = MotionConfig()
        self.app.frigate_config = SimpleNamespace(
            cameras={
                "front": SimpleNamespace(motion=self.motion),
                "back": SimpleNamespace(motion=MotionConfig()),
            },
            auth=SimpleNamespace(roles={"front_only": ["front"]}),
            proxy=SimpleNamespace(separator=","),
        )
        self.app.include_router(router)
        self.client = self.enterContext(TestClient(self.app))
        self.headers = {"remote-user": "synthetic", "remote-role": "front_only"}

    def test_disabled_has_no_invented_activity_or_disk_request(self):
        response = self.client.get("/front/adaptive-motion", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "disabled")
        self.assertEqual(response.json()["grid"]["values"], [])
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertFalse(cache_path("front", "request").exists())

    def test_camera_scope_and_authentication(self):
        self.assertEqual(
            self.client.get("/back/adaptive-motion", headers=self.headers).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(
                "/front/adaptive-motion", headers={"remote-user": "", "remote-role": ""}
            ).status_code,
            401,
        )

    def test_view_requests_are_bounded_and_missing_worker_is_explicit(self):
        self.motion.adaptive.enabled = True
        response = self.client.get(
            "/front/adaptive-motion?resolution=64", headers=self.headers
        )
        self.assertEqual(response.json()["status"], "warming_up")
        self.assertTrue(cache_path("front", "request").exists())
        for query in (
            "resolution=10000",
            "column=0",
            "column=500&row=0",
            "field=password",
        ):
            self.assertEqual(
                self.client.get(
                    "/front/adaptive-motion?" + query, headers=self.headers
                ).status_code,
                422,
            )

    def test_aspect_scaled_long_edge_cells_are_addressable(self):
        self.motion.adaptive.enabled = True
        self.motion.adaptive.resolve_grid((1080, 1920))
        self.assertEqual(self.motion.adaptive.columns, 889)
        response = self.client.get(
            "/front/adaptive-motion?column=888&row=499", headers=self.headers
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["grid"]["columns"], 889)
        self.assertEqual(
            self.client.get(
                "/front/adaptive-motion?column=889&row=499", headers=self.headers
            ).status_code,
            422,
        )

    def test_stale_cached_values_cannot_claim_current_applied_state(self):
        self.motion.adaptive.enabled = True
        atomic_json(
            cache_path("front", "snapshot"),
            {
                "camera": "front",
                "mode": "shadow",
                "status": "learning",
                "updated_at": time.time() - 60,
                "applied": True,
                "grid": {
                    "field": "adjustment",
                    "columns": 500,
                    "rows": 500,
                    "values": [2],
                },
            },
        )
        response = self.client.get("/front/adaptive-motion", headers=self.headers)
        self.assertEqual(response.json()["status"], "stale")
        self.assertFalse(response.json()["applied"])

    def test_old_field_or_geometry_is_not_relabelled(self):
        self.motion.adaptive.enabled = True
        atomic_json(
            cache_path("front", "snapshot"),
            {
                "camera": "front",
                "mode": "shadow",
                "status": "learning",
                "updated_at": time.time(),
                "applied": False,
                "grid": {"field": "p", "columns": 32, "rows": 32, "values": [2]},
            },
        )
        response = self.client.get(
            "/front/adaptive-motion?field=activity", headers=self.headers
        )
        self.assertEqual(response.json()["status"], "warming_up")
        self.assertEqual(response.json()["grid"]["field"], "activity")

    def test_corrupt_diagnostics_report_service_error(self):
        self.motion.adaptive.enabled = True
        path = cache_path("front", "snapshot")
        path.parent.mkdir(parents=True)
        path.write_text("{broken")
        with self.assertLogs("frigate.api.adaptive_motion", "ERROR"):
            response = self.client.get("/front/adaptive-motion", headers=self.headers)
        self.assertEqual(response.status_code, 503)

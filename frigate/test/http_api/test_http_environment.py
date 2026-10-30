"""Environmental status authorization with the production default-admin gate."""

import multiprocessing as mp
import unittest
from unittest.mock import MagicMock

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from frigate.api.auth import require_admin_by_default
from frigate.api.environment import router
from frigate.config import FrigateConfig
from frigate.config.profile_manager import ProfileManager
from frigate.environment.runtime import EnvironmentManager
from frigate.environment.weather import parse_open_meteo
from frigate.test.test_environment_weather import NOW, weather_payload


class TestEnvironmentHttp(unittest.TestCase):
    def setUp(self):
        camera = {
            "ffmpeg": {
                "inputs": [
                    {
                        "path": "rtsp://camera.invalid/test",
                        "roles": ["detect"],
                    }
                ]
            },
            "detect": {"height": 720, "width": 1280, "fps": 5},
        }
        config = FrigateConfig(
            mqtt={"enabled": False},
            auth={"roles": {"front_only": ["front"]}},
            weather={"enabled": True, "latitude": 52.52, "longitude": 13.41},
            cameras={"front": camera, "back": camera},
        )
        manager = EnvironmentManager(
            config, ProfileManager(config, MagicMock()), mp.Event()
        )
        manager._weather = parse_open_meteo(weather_payload(), NOW)
        self.app = FastAPI(dependencies=[Depends(require_admin_by_default())])
        self.app.frigate_config = config
        self.app.environment_manager = manager
        self.app.include_router(router)

        @self.app.get("/environment/cameras/{camera_name}/admin-only")
        def admin_only(camera_name: str):
            return {"camera": camera_name}

        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def headers(self, role):
        return {"remote-user": "test-user", "remote-role": role}

    def test_authenticated_weather_is_available_without_coordinates(self):
        response = self.client.get(
            "/environment/weather", headers=self.headers("viewer")
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["data"]["modeled"])
        self.assertNotIn("latitude", response.text)
        self.assertNotIn("longitude", response.text)

    def test_restricted_role_can_only_read_its_camera(self):
        headers = self.headers("front_only")
        allowed = self.client.get("/environment/cameras/front", headers=headers)
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.json()["camera"], "front")
        self.assertNotIn("back", allowed.json())
        self.assertEqual(
            self.client.get("/environment/cameras/back", headers=headers).status_code,
            403,
        )
        self.assertEqual(
            self.client.get("/environment", headers=headers).status_code, 403
        )

    def test_admin_can_read_all_status_and_missing_camera_is_explicit(self):
        response = self.client.get("/environment", headers=self.headers("admin"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()["cameras"]), {"front", "back"})
        self.assertNotIn("latitude", response.text)
        self.assertNotIn("longitude", response.text)
        self.assertEqual(
            self.client.get(
                "/environment/cameras/missing", headers=self.headers("admin")
            ).status_code,
            404,
        )

    def test_anonymous_requests_cannot_read_weather_or_camera_status(self):
        for path in ("/environment/weather", "/environment/cameras/front"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)

    def test_camera_exemption_does_not_cover_other_environment_endpoints(self):
        self.assertEqual(
            self.client.get(
                "/environment/cameras/front/admin-only",
                headers=self.headers("front_only"),
            ).status_code,
            403,
        )

    def test_missing_runtime_is_unavailable_not_success(self):
        self.app.environment_manager = None
        response = self.client.get(
            "/environment/weather", headers=self.headers("viewer")
        )
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()["success"])


if __name__ == "__main__":
    unittest.main()

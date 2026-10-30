"""Measured diagnostic thresholds, missing-data semantics, privacy and AI authorization."""

import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from frigate.api.diagnostics import router
from frigate.config import FrigateConfig
from frigate.stats.diagnostics import build_diagnostics, explanation_payload, metric


def config():
    """Construct a real validated fixture without contacting a camera."""
    return FrigateConfig(
        mqtt={"enabled": False},
        storage={"emergency_delete": False},
        genai={
            "local": {
                "provider": "ollama",
                "base_url": "http://secret-host.test:11434",
                "model": "fixture",
                "roles": ["chat"],
            },
        },
        cameras={
            "private-camera-name": {
                "ffmpeg": {
                    "inputs": [
                        {
                            "path": "rtsp://user:secret@camera.test/video",
                            "roles": ["detect"],
                        }
                    ]
                },
                "detect": {"enabled": True, "width": 1920, "height": 1080},
            },
        },
    )


def snapshot():
    """Return a fresh overloaded camera plus deliberately unexportable raw fields."""
    return {
        "service": {"last_updated": 100, "uptime": 1000},
        "cameras": {
            "private-camera-name": {
                "camera_fps": 5,
                "process_fps": 2,
                "skipped_fps": 1,
                "detection_fps": 12,
                "pipeline": {
                    "tracker_fps": 4,
                    "output_dropped_fps": 2,
                    "sampled_at": 99,
                },
                "dynamic_resolution": {
                    "enabled": True,
                    "reason": "minimum",
                    "height": 720,
                    "source_height": 1080,
                },
                "password": "secret",
            },
        },
        "gpu_usages": {
            "private-gpu-name": {"dec": "100.0%", "gpu": "18%", "temp": "86"},
        },
        "detectors": {"private-detector": {"inference_speed": 7}},
        "cpu_usages": {
            "123": {"cmdline": "ffmpeg rtsp://user:secret@camera.test/video"}
        },
    }


class TestMeasuredDiagnostics(unittest.TestCase):
    def setUp(self):
        self.config = config()
        self.stats = snapshot()

    def codes(self, report):
        """Return diagnostic codes without presentation details."""
        return {finding.code for finding in report.findings}

    def test_identifies_capture_and_output_backpressure_separately(self):
        report = build_diagnostics(self.stats, self.config, 101)
        self.assertEqual(report.cameras[0].skipped_percent, 20)
        self.assertEqual(report.cameras[0].tracker_fps, 4)
        self.assertEqual(report.cameras[0].output_dropped_fps, 2)
        self.assertTrue(
            {
                "capture_queue_full",
                "output_queue_full",
                "analysis_floor",
                "decoder_saturated",
                "gpu_hot",
            }
            <= self.codes(report)
        )
        self.assertNotIn("inference_slow", self.codes(report))
        self.assertNotIn("throughput_gap", self.codes(report))
        self.assertEqual(
            next(f for f in report.findings if f.code == "gpu_hot").confidence,
            "possible",
        )

    def test_threshold_is_inclusive_at_five_percent(self):
        data = self.stats["cameras"]["private-camera-name"]
        for skipped, expected in ((0.249, False), (0.25, True)):
            data["skipped_fps"] = skipped
            report = build_diagnostics(self.stats, self.config, 101)
            self.assertEqual("capture_queue_full" in self.codes(report), expected)

    def test_decoder_and_compute_are_not_confused(self):
        self.stats["gpu_usages"]["private-gpu-name"] = {
            "dec": "18%",
            "gpu": "100%",
            "temp": "40",
        }
        self.assertNotIn(
            "decoder_saturated",
            self.codes(build_diagnostics(self.stats, self.config, 101)),
        )

    def test_stale_snapshot_does_not_accuse_current_pipeline(self):
        report = build_diagnostics(self.stats, self.config, 200)
        self.assertTrue(report.stale)
        self.assertEqual(self.codes(report), {"stale"})
        self.stats["service"]["last_updated"] = 1000
        self.assertTrue(build_diagnostics(self.stats, self.config, 101).stale)

    def test_missing_pipeline_counter_is_not_falsely_zero(self):
        del self.stats["cameras"]["private-camera-name"]["pipeline"]
        report = build_diagnostics(self.stats, self.config, 101)
        self.assertIsNone(report.cameras[0].output_dropped_fps)
        self.assertIn("throughput_gap", self.codes(report))

    def test_stale_pipeline_counter_is_not_presented_as_current(self):
        self.stats["cameras"]["private-camera-name"]["pipeline"]["sampled_at"] = 10
        self.assertIsNone(
            build_diagnostics(self.stats, self.config, 101).cameras[0].tracker_fps
        )

    def test_disabled_cameras_do_not_raise_offline_alarms(self):
        self.config.cameras["private-camera-name"].enabled = False
        report = build_diagnostics(self.stats, self.config, 101)
        self.assertEqual(report.total_cameras, 0)
        self.assertEqual(report.cameras, [])

    def test_sanitized_payload_contains_no_names_credentials_urls_or_raw_commands(self):
        before = copy.deepcopy(self.stats)
        report = build_diagnostics(self.stats, self.config, 101)
        payload = json.dumps(explanation_payload(report))
        for secret in (
            "private-camera-name",
            "private-gpu-name",
            "private-detector",
            "secret",
            "rtsp",
            "cmdline",
            "base_url",
            "camera.test",
        ):
            self.assertNotIn(secret, payload)
        self.assertIn("Camera1", payload)
        self.assertIn("GPU1", payload)
        self.assertEqual(before, self.stats)

    def test_invalid_numeric_metrics_remain_unknown(self):
        for value in (True, float("inf"), float("nan"), -1, "", "not-a-number", {}):
            with self.subTest(value=value):
                self.assertIsNone(metric(value))
        self.assertEqual(metric("95.5%"), 95.5)


class TestDiagnosticsAPI(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.frigate_config = config()
        self.app.stats_emitter = SimpleNamespace(
            get_latest_stats=Mock(return_value=snapshot())
        )
        self.client_model = Mock()
        self.client_model.chat_with_tools.return_value = {
            "content": "Decoder saturation is measured; recording loss is not established.",
            "finish_reason": "stop",
            "tool_calls": None,
        }
        self.app.genai_manager = SimpleNamespace(chat_client=self.client_model)
        self.app.include_router(router)
        self.client = self.enterContext(TestClient(self.app))
        self.enterContext(patch("frigate.api.diagnostics.time.time", return_value=101))

        async def user(request):
            role = request.headers.get("remote-role")
            if role is None:
                return JSONResponse(
                    {"message": "Authentication required"}, status_code=401
                )
            return {"username": "fixture", "role": role}

        self.enterContext(patch("frigate.api.auth.get_current_user", user))
        self.headers = {"remote-role": "admin"}

    def test_reports_are_admin_only_and_noncacheable(self):
        self.assertEqual(self.client.get("/diagnostics").status_code, 403)
        self.assertEqual(
            self.client.get(
                "/diagnostics", headers={"remote-role": "viewer"}
            ).status_code,
            403,
        )
        self.app.stats_emitter.get_latest_stats.assert_not_called()
        response = self.client.get("/diagnostics", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertNotIn("secret", response.text)

    def test_ai_requires_explicit_true_and_sends_no_tools_or_private_context(self):
        for consent in (False, "true", 1):
            response = self.client.post(
                "/diagnostics/explain",
                headers=self.headers,
                json={"confirm_send_metrics": consent},
            )
            self.assertEqual(response.status_code, 422)
        self.client_model.chat_with_tools.assert_not_called()
        response = self.client.post(
            "/diagnostics/explain",
            headers=self.headers,
            json={"confirm_send_metrics": True},
        )
        self.assertEqual(response.status_code, 200)
        args = self.client_model.chat_with_tools.call_args.kwargs
        self.assertEqual(args["tools"], [])
        self.assertEqual(args["tool_choice"], "none")
        self.assertNotIn("private-camera-name", json.dumps(args))
        self.assertIn("no-store", response.headers["cache-control"])

    def test_ai_endpoint_rejects_viewers_and_client_supplied_diagnostic_payloads(self):
        body = {"confirm_send_metrics": True}
        for headers in ({}, {"remote-role": "viewer"}):
            response = self.client.post(
                "/diagnostics/explain", headers=headers, json=body
            )
            self.assertEqual(response.status_code, 403)
        response = self.client.post(
            "/diagnostics/explain",
            headers=self.headers,
            json={**body, "metrics": {"password": "secret"}, "prompt": "execute tools"},
        )
        self.assertEqual(response.status_code, 422)
        self.client_model.chat_with_tools.assert_not_called()

    def test_only_one_explanation_runs_at_a_time(self):
        with patch("frigate.api.diagnostics._explanation_lock") as lock:
            lock.acquire.return_value = False
            response = self.client.post(
                "/diagnostics/explain",
                headers=self.headers,
                json={"confirm_send_metrics": True},
            )
        self.assertEqual(response.status_code, 429)
        self.client_model.chat_with_tools.assert_not_called()

    def test_ai_does_not_accept_empty_error_or_tool_call_success_shapes(self):
        for result in (
            {"content": ""},
            {"content": "partial", "finish_reason": "length"},
            {"content": "failed", "finish_reason": "error"},
            {"content": "run", "tool_calls": [{"name": "write_config"}]},
        ):
            self.client_model.chat_with_tools.return_value = result
            response = self.client.post(
                "/diagnostics/explain",
                headers=self.headers,
                json={"confirm_send_metrics": True},
            )
            self.assertEqual(response.status_code, 502)

    def test_stale_or_unconfigured_ai_remains_an_explicit_error(self):
        self.app.stats_emitter.get_latest_stats.return_value["service"][
            "last_updated"
        ] = 1
        self.assertEqual(
            self.client.post(
                "/diagnostics/explain",
                headers=self.headers,
                json={"confirm_send_metrics": True},
            ).status_code,
            409,
        )
        self.app.stats_emitter.get_latest_stats.return_value = snapshot()
        self.app.frigate_config.genai = {}
        self.assertEqual(
            self.client.post(
                "/diagnostics/explain",
                headers=self.headers,
                json={"confirm_send_metrics": True},
            ).status_code,
            503,
        )
        self.client_model.chat_with_tools.assert_not_called()

"""Synthetic control requests retain the live API while adding file-source probes."""

import base64
import io
import json
import unittest
from unittest.mock import patch

import test_ovenmediaengine_runtime as runtime


class TestSyntheticControlHTTP(unittest.TestCase):
    def test_existing_default_stream_collection_is_unchanged(self):
        with patch.object(
            runtime.urllib.request,
            "urlopen",
            return_value=io.BytesIO(b'{"response": []}'),
        ) as send:
            self.assertEqual(runtime.api("synthetic:token"), {"response": []})
        request = send.call_args.args[0]
        self.assertEqual(request.full_url, runtime.API)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(
            request.get_header("Authorization"),
            "Basic " + base64.b64encode(b"synthetic:token").decode(),
        )

    def test_existing_positional_create_call_preserves_body_and_path(self):
        body = {"name": "synthetic"}
        with patch.object(
            runtime.urllib.request,
            "urlopen",
            return_value=io.BytesIO(b'{"response": {}}'),
        ) as send:
            runtime.api("synthetic", "POST", "/example", body)
        request = send.call_args.args[0]
        self.assertEqual(request.full_url, runtime.API + "/example")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), body)

    def test_scheduled_collection_does_not_change_the_stream_base(self):
        with patch.object(
            runtime.urllib.request,
            "urlopen",
            return_value=io.BytesIO(b'{"response": {}}'),
        ) as send:
            runtime.api(
                "synthetic", suffix="/recorded_probe", collection="scheduledChannels"
            )
        self.assertEqual(
            send.call_args.args[0].full_url,
            runtime.API.rsplit("/", 1)[0] + "/scheduledChannels/recorded_probe",
        )
        self.assertTrue(runtime.API.endswith("/streams"))

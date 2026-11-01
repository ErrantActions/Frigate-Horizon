"""Exercise the real recording proxy includes, without cameras or private media."""

import http.client
import os
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CONF = (
    Path(os.environ["FRIGATE_HTTP_TEST_CONF_DIR"])
    if os.environ.get("FRIGATE_HTTP_TEST_CONF_DIR")
    else Path(__file__).resolve().parents[2] / "docker/main/rootfs/usr/local/nginx/conf"
)


def media_location() -> str:
    """Read the actual nested location rather than recreating its directives."""
    text = (CONF / "nginx.conf").read_text()
    start = text.index("location ~ ^/api/[^/]+/recordings/http/")
    return text[start : text.index("}", start) + 1]


class TestRecordedHTTPProxyConfiguration(unittest.TestCase):
    def test_private_media_disables_cache_and_upgrade(self):
        location = media_location()
        self.assertIn("include auth_request.conf;", location)
        self.assertIn("include proxy_media.conf;", location)
        proxy = (CONF / "proxy_media.conf").read_text()
        self.assertIn("proxy_cache off;", proxy)
        self.assertIn("proxy_buffering off;", proxy)
        self.assertIn('proxy_set_header Connection "";', proxy)
        self.assertIn("include proxy_common.conf;", proxy)
        self.assertIn(
            'proxy_set_header Connection "Upgrade";', (CONF / "proxy.conf").read_text()
        )


@unittest.skipUnless(
    os.environ.get("FRIGATE_HTTP_TEST_NGINX"),
    "Set FRIGATE_HTTP_TEST_NGINX to the actual Linux Nginx binary",
)
class TestRecordedHTTPProxyLinux(unittest.TestCase):
    def test_real_proxy_preserves_auth_ranges_if_range_and_head(self):
        calls = []
        payload = bytes(range(256))

        class Upstream(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                return

            def do_HEAD(self):
                self.do_GET()

            def do_GET(self):
                if self.path == "/auth":
                    allowed = (
                        self.headers.get("Authorization") == "Bearer synthetic-test"
                    )
                    self.send_response(200 if allowed else 401)
                    self.send_header("Remote-User", "fixture")
                    self.send_header("Remote-Role", "admin")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                calls.append(dict(self.headers))
                start, end, status = 0, len(payload) - 1, 200
                requested = self.headers.get("Range")
                if requested == "bytes=0-15":
                    start, end, status = 0, 15, 206
                elif requested:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{len(payload)}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(status)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Cache-Control", "private, no-store")
                self.send_header("ETag", '"fixture-etag"')
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(end - start + 1))
                if status == 206:
                    self.send_header(
                        "Content-Range", f"bytes {start}-{end}/{len(payload)}"
                    )
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(payload[start : end + 1])

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        process = None
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for name in (
                    "auth_request.conf",
                    "proxy.conf",
                    "proxy_common.conf",
                    "proxy_media.conf",
                    "security_headers.conf",
                ):
                    (root / name).write_bytes((CONF / name).read_bytes())
                with socket.socket() as reservation:
                    reservation.bind(("127.0.0.1", 0))
                    port = reservation.getsockname()[1]
                (root / "nginx.conf").write_text(
                    f"""
daemon off;
pid {root}/nginx.pid;
error_log {root}/error.log;
events {{ worker_connections 32; }}
http {{
    access_log {root}/access.log;
    proxy_cache_path {root}/cache keys_zone=api_cache:1m;
    client_body_temp_path {root}/body;
    proxy_temp_path {root}/proxy;
    upstream frigate_api {{ server 127.0.0.1:{upstream.server_port}; }}
    server {{
        listen 127.0.0.1:{port};
        location = /auth {{
            internal;
            proxy_pass http://frigate_api/auth;
            proxy_pass_request_body off;
            proxy_set_header Content-Length "";
        }}
        location /api/ {{
            include auth_request.conf;
            proxy_pass http://frigate_api/;
            include proxy.conf;
            proxy_cache api_cache;
            {media_location()}
        }}
    }}
}}
"""
                )
                command = [
                    os.environ["FRIGATE_HTTP_TEST_NGINX"],
                    "-p",
                    str(root) + os.sep,
                    "-c",
                    str(root / "nginx.conf"),
                ]
                subprocess.run(command + ["-t"], check=True, capture_output=True)
                process = subprocess.Popen(
                    command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                deadline = time.monotonic() + 5
                while True:
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                            break
                    except OSError:
                        if process.poll() is not None or time.monotonic() >= deadline:
                            self.fail((root / "error.log").read_text())
                        time.sleep(0.02)

                def request(method, extra=None, authorized=True):
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", port, timeout=5
                    )
                    headers = (
                        {"Authorization": "Bearer synthetic-test"} if authorized else {}
                    )
                    headers.update(extra or {})
                    try:
                        connection.request(
                            method,
                            "/api/fixture/recordings/http/sessions/fixture/media.mp4",
                            headers=headers,
                        )
                        response = connection.getresponse()
                        return (
                            response.status,
                            dict(response.getheaders()),
                            response.read(),
                        )
                    finally:
                        connection.close()

                self.assertEqual(request("GET", authorized=False)[0], 401)
                self.assertEqual(calls, [])
                status, headers, body = request("HEAD")
                self.assertEqual(status, 200)
                self.assertEqual(headers["Content-Length"], "256")
                self.assertEqual(body, b"")
                status, headers, body = request(
                    "GET",
                    {"Range": "bytes=0-15", "If-Range": '"fixture-etag"'},
                )
                self.assertEqual(status, 206)
                self.assertEqual(headers["Content-Range"], "bytes 0-15/256")
                self.assertEqual(body, payload[:16])
                self.assertEqual(calls[-1]["Range"], "bytes=0-15")
                self.assertEqual(calls[-1]["If-Range"], '"fixture-etag"')
                self.assertEqual(calls[-1]["Remote-Role"], "admin")
                self.assertNotIn("Upgrade", calls[-1])
                self.assertNotEqual(calls[-1].get("Connection"), "Upgrade")
                self.assertEqual(
                    request("GET", {"Range": "bytes=99999999999-"})[0], 416
                )
        finally:
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            upstream.shutdown()
            upstream.server_close()
            thread.join(timeout=3)

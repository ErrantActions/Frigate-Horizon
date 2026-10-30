"""Optional real nginx-vod packaging and seeking of synthetic preview/export media."""

import asyncio
import hashlib
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.error import URLError
from urllib.parse import urljoin
from urllib.request import urlopen

FFMPEG = os.environ.get("ONVIF_TEST_FFMPEG") or shutil.which("ffmpeg")
NGINX = os.environ.get("HLS_TEST_NGINX") or shutil.which("nginx")


@unittest.skipUnless(
    os.name != "nt" and FFMPEG and NGINX,
    "Set ONVIF_TEST_FFMPEG and HLS_TEST_NGINX to existing image binaries",
)
class TestMappedMediaRealNginx(unittest.TestCase):
    def test_preview_and_export_decode_and_seek_without_rewriting_originals(self):
        from frigate.api import media

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o755)
            preview = root / "previews" / "front" / "preview.mp4"
            preview.parent.mkdir(parents=True)
            subprocess.run(
                [
                    FFMPEG,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=128x72:rate=5",
                    "-t",
                    "8",
                    "-an",
                    "-c:v",
                    "libx264",
                    "-threads",
                    "1",
                    "-preset",
                    "ultrafast",
                    "-g",
                    "10",
                    "-movflags",
                    "+faststart",
                    str(preview),
                ],
                check=True,
                capture_output=True,
                timeout=15,
            )
            export = root / "export.mp4"
            shutil.copyfile(preview, export)
            originals = {
                path: hashlib.sha256(path.read_bytes()).digest()
                for path in (preview, export)
            }
            request = SimpleNamespace(
                app=SimpleNamespace(
                    frigate_config=SimpleNamespace(cameras={"front": object()})
                )
            )
            with (
                patch.object(media, "CLIPS_DIR", str(root)),
                patch.object(media, "EXPORT_DIR", str(root)),
                patch.object(media, "require_camera_access", AsyncMock()) as access,
                patch.object(
                    media.Export,
                    "get",
                    return_value=SimpleNamespace(
                        camera="front", in_progress=False, video_path=str(export)
                    ),
                ),
                patch.object(
                    media.Previews,
                    "get",
                    return_value=SimpleNamespace(path=str(preview), duration=8.0),
                ),
            ):
                mappings = {
                    "/vod/export/export-1": asyncio.run(
                        media.vod_export(request, "export-1")
                    ).body,
                    "/vod/preview/front/start/100/end/108": asyncio.run(
                        media.vod_preview(request, "front", 100, 108)
                    ).body,
                }
                access.assert_awaited_once_with("front", request=request)

            class Mapper(BaseHTTPRequestHandler):
                def do_GET(self):
                    body = mappings.get(self.path.removeprefix("/api"))
                    self.send_response(200 if body else 404)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body or b"")))
                    self.end_headers()
                    self.wfile.write(body or b"")

                def log_message(self, *_args):
                    pass

            server = ThreadingHTTPServer(("127.0.0.1", 0), Mapper)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                self._verify_playback(root, server.server_port, mappings)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
            for path, digest in originals.items():
                self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), digest)

    def _verify_playback(self, root, mapper_port, mappings):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        config = root / "nginx.conf"
        config.write_text(
            f"""
daemon off;
worker_processes 1;
error_log {root}/error.log warn;
pid {root}/nginx.pid;
events {{ worker_connections 64; }}
http {{
    access_log off;
    client_body_temp_path {root}/client;
    proxy_temp_path {root}/proxy;
    fastcgi_temp_path {root}/fastcgi;
    uwsgi_temp_path {root}/uwsgi;
    scgi_temp_path {root}/scgi;
    server {{
        listen 127.0.0.1:{port};
        vod_mode mapped;
        vod_upstream_location /api;
        vod_segment_duration 2000;
        vod_align_segments_to_key_frames on;
        location /vod/ {{
            vod hls;
            vod_hls_container_format fmp4;
            vod_hls_version 6;
        }}
        location /api/ {{
            internal;
            proxy_pass http://127.0.0.1:{mapper_port};
        }}
    }}
}}
""",
            encoding="ascii",
        )
        process = subprocess.Popen(
            [NGINX, "-p", str(root), "-c", str(config)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            for mapping in mappings:
                with self.subTest(mapping=mapping):
                    playlist = f"http://127.0.0.1:{port}{mapping}/index.m3u8"
                    deadline = time.monotonic() + 5
                    while True:
                        try:
                            with urlopen(playlist, timeout=1) as response:
                                text = response.read().decode()
                            break
                        except URLError:
                            if (
                                process.poll() is not None
                                or time.monotonic() > deadline
                            ):
                                self.fail((root / "error.log").read_text())
                            time.sleep(0.02)
                    self.assertIn("#EXT-X-MAP", text)
                    duration = sum(
                        float(value) for value in re.findall(r"#EXTINF:([0-9.]+)", text)
                    )
                    self.assertAlmostEqual(duration, 8.0, delta=0.25)
                    child = next(
                        line
                        for line in text.splitlines()
                        if line and not line.startswith("#")
                    )
                    with urlopen(urljoin(playlist, child), timeout=2) as response:
                        self.assertGreater(len(response.read()), 0)
                    result = subprocess.run(
                        [
                            FFMPEG,
                            "-hide_banner",
                            "-loglevel",
                            "error",
                            "-nostdin",
                            "-ss",
                            "4",
                            "-i",
                            playlist,
                            "-frames:v",
                            "1",
                            "-f",
                            "framemd5",
                            "-",
                        ],
                        capture_output=True,
                        timeout=15,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr.decode())
                    self.assertRegex(result.stdout.decode(), r"0,\s+\d+,\s+\d+,")
        finally:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)

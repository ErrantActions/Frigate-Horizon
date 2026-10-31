"""Probe real synthetic RTSP/HLS clocks without Frigate, cameras, or network access."""

import argparse
import json
import os
import re
import socket
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

ORIGIN = "http://127.0.0.1:18888/"


def read(path: str) -> bytes:
    """Fetch one fixed-loopback packager resource without following external URLs."""
    url = urllib.parse.urljoin(ORIGIN, path)
    if not url.startswith(ORIGIN):
        raise ValueError("Unexpected packager resource origin")
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.read()


def boxes(data: bytes, kind: bytes) -> Iterator[bytes]:
    """Read bounded ISO BMFF containers, never search inside compressed payloads."""
    position = 0
    while position < len(data):
        if len(data) - position < 8:
            raise ValueError("Truncated MP4 header")
        size, name = struct.unpack_from(">I4s", data, position)
        header = 8
        if size == 1:
            if len(data) - position < 16:
                raise ValueError("Truncated extended MP4 header")
            size = struct.unpack_from(">Q", data, position + 8)[0]
            header = 16
        elif size == 0:
            size = len(data) - position
        if size < header or position + size > len(data):
            raise ValueError("Invalid MP4 box size")
        body = data[position + header : position + size]
        if name == kind:
            yield body
        if name in (b"moov", b"trak", b"mdia", b"moof", b"traf"):
            yield from boxes(body, kind)
        position += size


def bootstrap(path: str) -> str:
    """Resolve the actual private media URI from the pinned packager's master."""
    master = read(f"{path}/index.m3u8?cookieCheck=1").decode()
    media = next(
        line for line in master.splitlines() if line and not line.startswith("#")
    )
    return f"{path}/{media}"


def audio_playlist(path: str) -> str:
    """Resolve the actual AAC rendition without assuming a generated child name."""
    master = read(f"{path}/index.m3u8?cookieCheck=1").decode()
    match = re.search(r'#EXT-X-MEDIA:TYPE=AUDIO,[^\n]*URI="([^"]+)"', master)
    if match is None:
        raise ValueError("Synthetic AAC rendition is missing")
    return f"{path}/{match.group(1)}"


def snapshot(media: str) -> dict[str, float]:
    """Measure the presentation clock from actual manifests and fMP4 objects."""
    text = read(media).decode()
    initialization = re.search(r'#EXT-X-MAP:URI="([^"]+)"', text)
    if initialization is None:
        raise ValueError("Missing HLS initialization")
    base = urllib.parse.urljoin(ORIGIN, media)
    stamp = None
    segments = []
    for line in text.splitlines():
        if line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            stamp = datetime.fromisoformat(
                line.split(":", 1)[1].replace("Z", "+00:00")
            ).timestamp()
        elif line and not line.startswith("#"):
            segments.append((line, stamp))
    if not segments or segments[-1][1] is None:
        raise ValueError("No complete timestamped HLS segment")

    def resource(uri: str) -> str:
        parsed = urllib.parse.urlsplit(urllib.parse.urljoin(base, uri))
        query = urllib.parse.parse_qs(parsed.query)
        session = urllib.parse.parse_qs(urllib.parse.urlsplit(base).query).get(
            "session"
        )
        if session is not None and "session" not in query:
            query["session"] = session
        return urllib.parse.urlunsplit(
            parsed._replace(query=urllib.parse.urlencode(query, doseq=True))
        )

    init = read(resource(initialization.group(1)))
    fragment = read(resource(segments[-1][0]))
    mdhd = next(boxes(init, b"mdhd"))
    scale = struct.unpack_from(">I", mdhd, 20 if mdhd[0] == 1 else 12)[0]
    tfdt = next(boxes(fragment, b"tfdt"))
    ticks = struct.unpack_from(">Q" if tfdt[0] == 1 else ">I", tfdt, 4)[0]
    if scale == 0:
        raise ValueError("Invalid MP4 clock rate")
    return {
        "clock_origin": segments[-1][1] - ticks / scale,
        "decode_seconds": ticks / scale,
    }


def wait_media(path: str) -> str:
    """Wait for a real publisher, not a success-shaped empty playlist."""
    deadline = time.monotonic() + 30
    while True:
        try:
            media = bootstrap(path)
            snapshot(media)
            return media
        except (urllib.error.URLError, TimeoutError, ValueError, StopIteration):
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)


def terminate(process: subprocess.Popen) -> None:
    """Stop only a process created by this probe."""
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def probe(binary: Path, ffmpeg: Path, enabled: bool) -> dict:
    """Compare cold and restarted rendition clocks with a negative control."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        config = root / "config.yml"
        config.write_text(
            "logLevel: error\napi: false\nmetrics: false\npprof: false\n"
            "rtsp: true\nrtspAddress: 127.0.0.1:18554\nrtspTransports: [tcp]\n"
            "rtmp: false\nwebrtc: false\nsrt: false\nmoq: false\n"
            "hls: true\nhlsAddress: 127.0.0.1:18888\nhlsAlwaysRemux: false\n"
            "hlsVariant: lowLatency\nhlsSegmentDuration: 1s\nhlsMuxerCloseAfter: 30s\n"
            "paths:\n  low:\n    source: publisher\n  high:\n    source: publisher\n"
        )
        processes = []
        logs = []
        try:
            environment = {
                **os.environ,
                "HORIZON_HLS_SHARED_CLOCK": "1" if enabled else "0",
            }
            server_log = (root / "server.log").open("wb")
            logs.append(server_log)
            server = subprocess.Popen(
                [str(binary), str(config)],
                stdout=server_log,
                stderr=server_log,
                env=environment,
            )
            processes.append(server)
            deadline = time.monotonic() + 10
            while True:
                if server.poll() is not None:
                    raise RuntimeError("Synthetic packager failed to start")
                try:
                    with socket.create_connection(("127.0.0.1", 18554), timeout=0.2):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.1)

            def publish(path: str, size: str) -> subprocess.Popen:
                log = (root / f"{path}-{len(processes)}.log").open("wb")
                logs.append(log)
                process = subprocess.Popen(
                    [
                        str(ffmpeg),
                        "-nostdin",
                        "-loglevel",
                        "error",
                        "-re",
                        "-f",
                        "lavfi",
                        "-i",
                        f"color=black:size={size}:rate=25",
                        "-re",
                        "-f",
                        "lavfi",
                        "-i",
                        "anullsrc=channel_layout=mono:sample_rate=48000",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "ultrafast",
                        "-tune",
                        "zerolatency",
                        "-g",
                        "25",
                        "-bf",
                        "0",
                        "-pix_fmt",
                        "yuv420p",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "64k",
                        "-f",
                        "rtsp",
                        "-rtsp_transport",
                        "tcp",
                        f"rtsp://127.0.0.1:18554/{path}",
                    ],
                    stdout=log,
                    stderr=log,
                )
                processes.append(process)
                return process

            publish("low", "128x72")
            low = wait_media("low")
            for _ in range(5):
                read(low)
                time.sleep(1)
            high_process = publish("high", "192x108")
            high = wait_media("high")
            first = (snapshot(low), snapshot(high))
            first_audio = (
                snapshot(audio_playlist("low")),
                snapshot(audio_playlist("high")),
            )
            terminate(high_process)
            for _ in range(3):
                read(low)
                time.sleep(1)
            publish("high", "192x108")
            high = wait_media("high")
            restarted = (snapshot(low), snapshot(high))
            restarted_audio = (
                snapshot(audio_playlist("low")),
                snapshot(audio_playlist("high")),
            )
            differences = [
                abs(left["clock_origin"] - right["clock_origin"])
                for left, right in (first, restarted)
            ]
            audio_differences = [
                abs(video["clock_origin"] - audio["clock_origin"])
                for video, audio in (
                    *zip(first, first_audio),
                    *zip(restarted, restarted_audio),
                )
            ]
            if enabled:
                if any(value > 0.1 for value in differences):
                    raise AssertionError(
                        f"Shared fMP4 clocks differ by {differences} seconds"
                    )
            elif any(value < 3 for value in differences):
                raise AssertionError(
                    "The negative control did not reproduce independent clocks"
                )
            decoded = None
            if enabled:
                # Audio segment PDT follows the leading video boundary, not the
                # first AAC sample. Exact A/V offset preservation is tested in Go.
                ffprobe = ffmpeg.with_name("ffprobe").resolve(strict=True)
                result = subprocess.run(
                    [
                        str(ffprobe),
                        "-v",
                        "error",
                        "-read_intervals",
                        "%+2",
                        "-show_frames",
                        "-show_entries",
                        "frame=media_type",
                        "-of",
                        "json",
                        ORIGIN + "low/index.m3u8?cookieCheck=1",
                    ],
                    capture_output=True,
                    check=True,
                    timeout=20,
                )
                frames = json.loads(result.stdout)["frames"]
                decoded = {
                    kind: sum(frame["media_type"] == kind for frame in frames)
                    for kind in ("video", "audio")
                }
                if any(count < 10 for count in decoded.values()):
                    raise AssertionError(
                        "Synthetic HLS did not decode both video and AAC"
                    )
            return {
                "shared_clock": enabled,
                "origin_differences_seconds": differences,
                "audio_video_origin_differences_seconds": audio_differences,
                "decoded_frames": decoded,
                "publisher_restart_checked": True,
            }
        finally:
            for process in reversed(processes):
                terminate(process)
            for log in logs:
                log.close()


def main() -> None:
    """Run isolated real-media proof for both the original and corrected timing."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    ffmpeg = args.ffmpeg.resolve(strict=True)
    results = []
    for enabled in (False, True):
        result = probe(binary, ffmpeg, enabled)
        results.append(result)
        print(json.dumps({"clock_test": result}), flush=True)
    print(json.dumps({"tests": results}), flush=True)


if __name__ == "__main__":
    main()

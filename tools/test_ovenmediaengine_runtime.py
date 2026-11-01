"""Isolated Linux qualification of the packaged CPU OME and authenticated TURN.

Run only in the bounded ovenmediaengine-qualification Docker target. No camera,
saved Frigate configuration, recorder, external network, or production port is used.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import socketserver
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Literal

PREFIX = Path("/opt/ovenmediaengine")
API = "http://127.0.0.1:8081/v1/vhosts/default/apps/qualify/streams"


def wait_for(predicate, timeout: float = 20):
    """Wait for a bounded readiness condition, retaining its last error."""
    deadline = time.monotonic() + timeout
    error = None
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except (OSError, ValueError, AssertionError) as caught:
            error = caught
        time.sleep(0.1)
    raise AssertionError(f"Readiness timeout: {error}")


def api(
    token: str,
    method: str = "GET",
    suffix: str = "",
    body=None,
    *,
    collection: Literal["streams", "scheduledChannels"] = "streams",
):
    """Call only the synthetic application's loopback API."""
    headers = {"Authorization": "Basic " + base64.b64encode(token.encode()).decode()}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    url = API.rsplit("/", 1)[0] + "/" + collection + suffix
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=3) as response:
        return json.load(response)


def usage(pid: int) -> dict:
    """Read OME-only CPU, resident memory and thread measurements."""
    stat = Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()
    status = Path(f"/proc/{pid}/status").read_text()
    return {
        "cpu_seconds": (int(stat[11]) + int(stat[12])) / os.sysconf("SC_CLK_TCK"),
        "rss_kib": int(re.search(r"VmRSS:\s+(\d+)", status)[1]),
        "threads": int(re.search(r"Threads:\s+(\d+)", status)[1]),
        "monotonic": time.monotonic(),
    }


def measured_interval(pid: int, duration: float) -> dict:
    """Measure, rather than infer, the CPU cost of one test phase."""
    before = usage(pid)
    time.sleep(duration)
    after = usage(pid)
    after["cpu_percent"] = round(
        100
        * (after["cpu_seconds"] - before["cpu_seconds"])
        / (after["monotonic"] - before["monotonic"]),
        2,
    )
    return after


def annexb_frames(data: bytes) -> tuple[list[list[bytes]], bytes, bytes]:
    """Split the fixture's AUD-delimited Annex B stream into access units."""
    nals = [nal for nal in re.split(b"\x00\x00\x00?\x01", data) if nal]
    sps = next(nal for nal in nals if nal[0] & 31 == 7)
    pps = next(nal for nal in nals if nal[0] & 31 == 8)
    frames = []
    current = []
    for nal in nals:
        if nal[0] & 31 == 9 and current:
            frames.append(current)
            current = []
        current.append(nal)
    if current:
        frames.append(current)
    return frames, sps, pps


def generate_fixture(path: Path) -> None:
    """Generate an owned H264 source rather than reading a camera or recording."""
    subprocess.run(
        [
            os.environ.get("OME_TEST_FFMPEG", "ffmpeg"),
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=10",
            "-t",
            "2",
            "-an",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-g",
            "10",
            "-x264-params",
            "aud=1:repeat-headers=1",
            "-f",
            "h264",
            str(path),
        ],
        check=True,
        timeout=20,
    )


class FixtureServer(socketserver.ThreadingTCPServer):
    """Serve a generated H.264 RTSP source over loopback interleaved TCP."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, frames, sps: bytes, pps: bytes):
        self.frames = frames
        self.sps = sps
        self.pps = pps
        self.active = set()
        self.active_lock = threading.Lock()
        super().__init__(("127.0.0.1", 18554), FixtureHandler)


class FixtureHandler(socketserver.StreamRequestHandler):
    """Implement only the RTSP methods required by this synthetic pull."""

    def handle(self):
        self.stop_stream = threading.Event()
        self.send_lock = threading.Lock()
        self.sequence = 0
        self.timestamp = 0
        sender = None
        with self.server.active_lock:
            self.server.active.add(self)
        try:
            while not self.stop_stream.is_set():
                first = self.rfile.read(1)
                if not first:
                    break
                if first == b"$":
                    header = self.rfile.read(3)
                    if len(header) != 3:
                        break
                    self.rfile.read(struct.unpack("!H", header[1:])[0])
                    continue
                line = first + self.rfile.readline()
                method = line.decode().split()[0]
                headers = {}
                while True:
                    header = self.rfile.readline()
                    if header == b"\r\n" or not header:
                        break
                    key, value = header.decode().split(":", 1)
                    headers[key.lower()] = value.strip()
                self.rfile.read(int(headers.get("content-length", "0")))
                body = b""
                extra = "Session: qualification\r\n"
                if method == "DESCRIBE":
                    parameter_sets = ",".join(
                        base64.b64encode(value).decode()
                        for value in (self.server.sps, self.server.pps)
                    )
                    body = (
                        "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Fixture\r\n"
                        "c=IN IP4 127.0.0.1\r\nt=0 0\r\na=control:*\r\n"
                        "m=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n"
                        "a=fmtp:96 packetization-mode=1;sprop-parameter-sets="
                        f"{parameter_sets}\r\na=control:trackID=0\r\n"
                    ).encode()
                    extra += (
                        "Content-Type: application/sdp\r\n"
                        "Content-Base: rtsp://127.0.0.1:18554/fixture/\r\n"
                    )
                elif method == "SETUP":
                    if "TCP" not in headers.get("transport", ""):
                        raise AssertionError("Fixture expects RTSP interleaved TCP")
                    extra += "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n"
                elif method == "OPTIONS":
                    extra += (
                        "Public: OPTIONS,DESCRIBE,SETUP,PLAY,GET_PARAMETER,TEARDOWN\r\n"
                    )
                response = (
                    f"RTSP/1.0 200 OK\r\nCSeq: {headers['cseq']}\r\n"
                    f"{extra}Content-Length: {len(body)}\r\n\r\n"
                ).encode() + body
                with self.send_lock:
                    self.request.sendall(response)
                if method == "PLAY" and sender is None:
                    sender = threading.Thread(target=self.stream, daemon=True)
                    sender.start()
                if method == "TEARDOWN":
                    break
        except (OSError, ValueError):
            pass
        finally:
            self.stop_stream.set()
            if sender is not None:
                sender.join(timeout=2)
            with self.server.active_lock:
                self.server.active.discard(self)

    def send_nal(self, nal: bytes, marker: bool):
        """Fragment oversized H.264 NALs using RTP FU-A packets."""
        if len(nal) <= 1200:
            payloads = [nal]
        else:
            chunks = [nal[i : i + 1198] for i in range(1, len(nal), 1198)]
            payloads = [
                bytes(
                    [
                        (nal[0] & 0xE0) | 28,
                        (nal[0] & 31)
                        | (0x80 if i == 0 else 0)
                        | (0x40 if i == len(chunks) - 1 else 0),
                    ]
                )
                + chunk
                for i, chunk in enumerate(chunks)
            ]
        for i, payload in enumerate(payloads):
            packet = (
                struct.pack(
                    "!BBHII",
                    0x80,
                    96 | (0x80 if marker and i == len(payloads) - 1 else 0),
                    self.sequence,
                    self.timestamp,
                    0x12345678,
                )
                + payload
            )
            with self.send_lock:
                self.request.sendall(b"$\x00" + struct.pack("!H", len(packet)) + packet)
            self.sequence = (self.sequence + 1) & 0xFFFF

    def stream(self):
        """Pace a repeating ten-frame-per-second source until the pull closes."""
        try:
            while not self.stop_stream.is_set():
                for frame in self.server.frames:
                    for i, nal in enumerate(frame):
                        self.send_nal(nal, i == len(frame) - 1)
                    self.timestamp = (self.timestamp + 9000) & 0xFFFFFFFF
                    if self.stop_stream.wait(0.1):
                        return
        except OSError:
            self.stop_stream.set()


def stun_attribute(kind: int, value: bytes) -> bytes:
    """Encode a padded STUN attribute."""
    return struct.pack("!HH", kind, len(value)) + value + b"\0" * (-len(value) % 4)


def stun_exchange(connection, attributes: bytes, key=None) -> tuple[int, dict]:
    """Send a TCP TURN Allocate, optionally with long-term integrity."""
    transaction = secrets.token_bytes(12)
    length = len(attributes) + (24 if key is not None else 0)
    header = struct.pack("!HHI", 3, length, 0x2112A442) + transaction
    message = header + attributes
    if key is not None:
        message += stun_attribute(8, hmac.digest(key, message, "sha1"))
    connection.sendall(message)

    def receive(length):
        data = b""
        while len(data) < length:
            chunk = connection.recv(length - len(data))
            if not chunk:
                raise OSError("TURN closed the connection")
            data += chunk
        return data

    response = receive(20)
    kind, length, cookie = struct.unpack("!HHI", response[:8])
    assert cookie == 0x2112A442 and response[8:] == transaction
    payload = receive(length)
    result = {}
    offset = 0
    while offset < len(payload):
        attribute, size = struct.unpack("!HH", payload[offset : offset + 4])
        result[attribute] = payload[offset + 4 : offset + 4 + size]
        offset += 4 + ((size + 3) & ~3)
    return kind, result


def verify_turn(secret: str) -> dict:
    """Reject missing, bad and expired credentials; accept a temporary REST user."""
    user = f"{int(time.time()) + 120}:qualification"
    password = base64.b64encode(hmac.digest(secret.encode(), user.encode(), "sha1"))
    expired = f"{int(time.time()) - 60}:qualification"
    expired_password = base64.b64encode(
        hmac.digest(secret.encode(), expired.encode(), "sha1")
    )
    result = {}
    for label, username, credential in (
        ("bad_password", user, b"incorrect"),
        ("expired", expired, expired_password),
        ("temporary", user, password),
    ):
        with socket.create_connection(("127.0.0.1", 3478), timeout=3) as connection:
            requested_transport = stun_attribute(0x0019, b"\x11\0\0\0")
            kind, challenge = stun_exchange(connection, requested_transport)
            assert kind == 0x0113 and challenge[9][2:4] == b"\x04\x01"
            result["unauthenticated"] = "401"
            realm, nonce = challenge[0x0014], challenge[0x0015]
            attributes = (
                requested_transport
                + stun_attribute(6, username.encode())
                + stun_attribute(0x0014, realm)
                + stun_attribute(0x0015, nonce)
            )
            key = hashlib.md5(
                username.encode() + b":" + realm + b":" + credential
            ).digest()
            kind, response = stun_exchange(connection, attributes, key)
            if label != "temporary":
                assert kind == 0x0113 and response[9][2] == 4
                result[label] = "rejected"
            else:
                assert kind == 0x0103 and 0x0016 in response
                result[label] = "allocation accepted"
    return result


def server_xml(token: str) -> str:
    """Generate an isolated app with no startup source and one tiny CPU profile."""
    return f"""<Server version="8">
<Name>qualification</Name><Type>origin</Type><IP>127.0.0.1</IP>
<PrivacyProtection>true</PrivacyProtection>
<Bind>
  <Managers><API><Port>8081</Port><WorkerCount>1</WorkerCount></API></Managers>
  <Providers><RTSPC><WorkerCount>1</WorkerCount></RTSPC></Providers>
  <Publishers><WebRTC>
    <Signalling><Port>3333</Port><WorkerCount>1</WorkerCount></Signalling>
    <IceCandidates><BindIP>0.0.0.0</BindIP>
      <IceCandidate>127.0.0.1:8189/udp</IceCandidate>
      <DefaultTransport>udp</DefaultTransport><IceWorkerCount>1</IceWorkerCount>
    </IceCandidates>
  </WebRTC></Publishers>
</Bind>
<Managers><API><AccessToken>{token}</AccessToken></API></Managers>
<VirtualHosts><VirtualHost><Name>default</Name>
  <Host><Names><Name>*</Name></Names></Host>
  <Applications><Application><Name>qualify</Name><Type>live</Type>
    <OutputProfiles><OutputProfile><Name>cpu</Name>
      <OutputStreamName>${{OriginStreamName}}</OutputStreamName>
      <Playlist><Name>master</Name><FileName>master</FileName>
        <Options><WebRtcAutoAbr>true</WebRtcAutoAbr></Options>
        <Rendition><Name>tiny</Name><Video>tiny</Video></Rendition>
      </Playlist>
      <Encodes><Video><Name>tiny</Name><Codec>h264</Codec>
        <Width>160</Width><Height>96</Height><Framerate>5</Framerate>
        <Bitrate>100000</Bitrate><ThreadCount>1</ThreadCount>
        <Profile>baseline</Profile><BFrames>0</BFrames>
        <KeyFrameInterval>5</KeyFrameInterval>
      </Video></Encodes>
    </OutputProfile></OutputProfiles>
    <Providers><RTSPPull /></Providers>
    <Publishers><WebRTC><Timeout>5000</Timeout></WebRTC></Publishers>
  </Application></Applications>
</VirtualHost></VirtualHosts></Server>"""


def verify_binds() -> dict:
    """Assert private HTTP listeners and a separately bound UDP media socket."""
    tcp = Path("/proc/net/tcp").read_text().splitlines()[1:]
    udp = Path("/proc/net/udp").read_text().splitlines()[1:]
    tcp6 = Path("/proc/net/tcp6").read_text().splitlines()[1:]
    for port in (3333, 8081):
        addresses = [
            line.split()[1].split(":")[0]
            for line in tcp
            if int(line.split()[1].split(":")[1], 16) == port
            and line.split()[3] == "0A"
        ]
        assert addresses == ["0100007F"], (port, addresses)
        assert not any(
            int(line.split()[1].split(":")[1], 16) == port and line.split()[3] == "0A"
            for line in tcp6
        ), f"Unexpected IPv6 control-plane listener on {port}"
    assert any(line.split()[1] == "00000000:1FFD" for line in udp)
    return {
        "signaling": "127.0.0.1:3333",
        "api": "127.0.0.1:8081",
        "media": "0.0.0.0:8189/udp",
    }


def main() -> None:
    """Run bounded synthetic create/transcode/delete, idle and TURN checks."""
    assert os.getuid() == 1000 and os.getgid() == 1000, "Run as UID/GID 1000"
    work = Path.cwd() / f"ome-qualification-{os.getpid()}"
    work.mkdir(mode=0o700)
    processes = []
    logs = []
    fixture = None
    try:
        subprocess.run(
            ["/bin/sh", str(PREFIX / "check-cli.sh")],
            check=True,
            timeout=20,
            stdout=subprocess.DEVNULL,
        )
        subprocess.run(["turnserver", "--version"], check=True, timeout=10)
        token = "qualification:" + secrets.token_hex(24)
        secret = secrets.token_hex(32)
        (work / "Server.xml").write_text(server_xml(token))
        (work / "Logger.xml").write_text(
            '<Logger version="2"><Path>./logs</Path>'
            '<Tag name=".*" level="warn" /></Logger>'
        )
        (work / "turnserver.conf").write_text(
            "listening-ip=127.0.0.1\nrelay-ip=127.0.0.1\nlistening-port=3478\n"
            "realm=qualification.invalid\nuse-auth-secret\n"
            f"static-auth-secret={secret}\n"
            "min-port=49160\nmax-port=49165\nuser-quota=2\ntotal-quota=4\n"
            "relay-threads=1\nno-tls\nno-dtls\nno-cli\nno-rfc5780\n"
            "no-multicast-peers\nno-tcp-relay\nlog-file=stdout\nsimple-log\n"
            f"pidfile={work / 'turn.pid'}\n"
            f"userdb={work / 'turnserver.db'}\n"
        )
        for path in work.iterdir():
            path.chmod(0o600)
        for command, name in (
            ([str(PREFIX / "bin/OvenMediaEngine"), "-c", str(work)], "ome"),
            (["turnserver", "-c", str(work / "turnserver.conf")], "turn"),
        ):
            log = (work / f"{name}.log").open("w")
            logs.append(log)
            processes.append(
                subprocess.Popen(
                    command,
                    cwd=work,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env={
                        **os.environ,
                        "OMP_NUM_THREADS": "2",
                        "OPENBLAS_NUM_THREADS": "2",
                    },
                )
            )
        ome = processes[0]
        wait_for(lambda: api(token))
        assert api(token)["response"] == []
        binds = verify_binds()
        idle = measured_interval(ome.pid, 3)
        assert idle["cpu_percent"] < 20, "Excessive idle OME CPU usage"
        assert api(token)["response"] == [], "Startup must not create sources"

        h264 = work / "fixture.h264"
        generate_fixture(h264)
        fixture = FixtureServer(*annexb_frames(h264.read_bytes()))
        threading.Thread(target=fixture.serve_forever, daemon=True).start()
        assert not fixture.active
        api(
            token,
            "POST",
            body={
                "name": "synthetic",
                "urls": ["rtsp://127.0.0.1:18554/fixture"],
                "properties": {
                    "persistent": False,
                    "unusedStreamDeletionTimeoutMs": 30000,
                    "ignoreRtcpSRTimestamp": True,
                    "retryCount": 0,
                },
            },
        )

        def encoded_output():
            stream = api(token, suffix="/synthetic")["response"]
            return any(
                not track["video"].get("bypass", True)
                and track["video"].get("width") == 160
                and track["video"].get("codecStatus") == "Ready"
                for output in stream.get("outputs", [])
                for track in output.get("tracks", [])
                if track.get("type") == "Video"
            )

        wait_for(encoded_output)
        active = measured_interval(ome.pid, 3)
        api(token, "DELETE", "/synthetic")
        wait_for(lambda: api(token)["response"] == [])
        wait_for(lambda: not fixture.active)
        after_delete = measured_interval(ome.pid, 3)
        assert after_delete["cpu_percent"] < 20, "OME did not return to idle"
        assert api(token)["response"] == []
        turn = wait_for(lambda: verify_turn(secret))
        assert all(process.poll() is None for process in processes)
        print(
            json.dumps(
                {
                    "result": "passed",
                    "uid": os.getuid(),
                    "gid": os.getgid(),
                    "receipt": json.loads((PREFIX / "share/version.json").read_text()),
                    "binds": binds,
                    "idle": idle,
                    "active_160x90_5fps": active,
                    "after_last_viewer_delete": after_delete,
                    "turn": turn,
                    "qualification": "REST encoder readiness, not browser WebRTC playback",
                },
                indent=2,
            )
        )
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if fixture is not None:
            fixture.shutdown()
            fixture.server_close()
        for log in logs:
            log.close()
        # Do not print generated config, credentials, or source-bearing OME logs.
        shutil.rmtree(work)


if __name__ == "__main__":
    main()

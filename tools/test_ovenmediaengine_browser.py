"""Decode actual OME WebRTC through direct ICE and authenticated TURN paths."""

import base64
import hashlib
import hmac
import json
import math
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import test_ovenmediaengine_runtime as runtime
import websocket

PAGE = b"""<!doctype html><video id="video" muted autoplay playsinline></video>
<script>
window.probe = {
  peer: null, pending: [], error: null, firstSourceFrame: null, sourceFrame: null,
  readSourceFrame() {
    const video = document.querySelector("video"), canvas = document.createElement("canvas");
    canvas.width = video.videoWidth; canvas.height = video.videoHeight;
    const context = canvas.getContext("2d"); context.drawImage(video, 0, 0);
    let frame = 0;
    for (let bit = 0; bit < 16; bit++) {
      const x = Math.floor((bit + 0.5) * canvas.width / 16);
      if (context.getImageData(x, Math.floor(canvas.height / 2), 1, 1).data[0] > 128)
        frame += 2 ** bit;
    }
    return frame;
  },
  async start(offer, candidates, servers, policy, identifyFrames = false, inspectAudio = false) {
    if (this.audioContext && this.audioContext.state !== "closed") await this.audioContext.close();
    this.audioContext = null; this.analyser = null; this.audioSource = null;
    if (inspectAudio) {
      this.audioContext = new AudioContext({sampleRate:48000});
      await this.audioContext.resume();
      this.analyser = this.audioContext.createAnalyser();
      const silentOutput = this.audioContext.createGain(); silentOutput.gain.value = 0;
      this.analyser.connect(silentOutput); silentOutput.connect(this.audioContext.destination);
    }
    this.peer?.close(); this.pending = []; this.error = null;
    this.firstSourceFrame = null; this.sourceFrame = null;
    this.sourceRtpTimestamp = null; this.sourceMediaTime = null;
    this.firstSourceRtpTimestamp = null; this.firstSourceMediaTime = null;
    const video = document.querySelector("video");
    const stream = new MediaStream(); video.srcObject = stream;
    const peer = new RTCPeerConnection({iceServers:servers,iceTransportPolicy:policy});
    this.peer = peer;
    if (identifyFrames) {
      const capture = (_now, metadata) => {
        if (this.peer !== peer) return;
        this.sourceFrame = this.readSourceFrame();
        this.firstSourceFrame ??= this.sourceFrame;
        this.sourceRtpTimestamp = metadata.rtpTimestamp ?? null;
        this.sourceMediaTime = metadata.mediaTime;
        this.firstSourceRtpTimestamp ??= this.sourceRtpTimestamp;
        this.firstSourceMediaTime ??= this.sourceMediaTime;
        video.requestVideoFrameCallback(capture);
      };
      video.requestVideoFrameCallback(capture);
    }
    peer.ontrack = (event) => {
      stream.addTrack(event.track);
      if (event.track.kind === "audio" && this.audioContext) {
        this.audioSource = this.audioContext.createMediaStreamSource(new MediaStream([event.track]));
        this.audioSource.connect(this.analyser);
      }
      video.play().catch((error) => { this.error = error.name; });
    };
    peer.onicecandidate = (event) => {
      if (event.candidate?.candidate) this.pending.push(event.candidate.toJSON());
    };
    await peer.setRemoteDescription(offer);
    for (const candidate of candidates) await peer.addIceCandidate(candidate);
    const answer = await peer.createAnswer(); await peer.setLocalDescription(answer);
    return {type:answer.type,sdp:answer.sdp};
  },
  async stats() {
    const report = await this.peer.getStats(); let inbound, inboundAudio, transport, pair;
    const candidateTypes = [];
    report.forEach((value) => {
      if (value.type === "inbound-rtp" && value.kind === "video") inbound = value;
      if (value.type === "inbound-rtp" && value.kind === "audio") inboundAudio = value;
      if (value.type === "transport" && value.selectedCandidatePairId) transport = value;
      if (value.type === "local-candidate") candidateTypes.push(value.candidateType);
    });
    if (transport) pair = report.get(transport.selectedCandidatePairId);
    const local = pair ? report.get(pair.localCandidateId) : null;
    const codec = inbound ? report.get(inbound.codecId) : null;
    const audioCodec = inboundAudio ? report.get(inboundAudio.codecId) : null;
    const video = document.querySelector("video");
    let rms = null;
    if (this.analyser) {
      const samples = new Float32Array(this.analyser.fftSize);
      this.analyser.getFloatTimeDomainData(samples);
      rms = Math.sqrt(samples.reduce((sum,value) => sum + value*value, 0) / samples.length);
    }
    return {state:this.peer.connectionState,error:this.error,
      ice_state:this.peer.iceConnectionState,gathering_state:this.peer.iceGatheringState,
      candidate_types:candidateTypes,packets:inbound?.packetsReceived ?? 0,
      width:video.videoWidth,height:video.videoHeight,
      frames:inbound?.framesDecoded ?? 0,codec:codec?.mimeType ?? null,
      candidate_type:local?.candidateType ?? null,
      relay_protocol:local?.relayProtocol ?? null,protocol:local?.protocol ?? null,
      source_frame:this.sourceFrame,first_source_frame:this.firstSourceFrame,
      source_rtp_timestamp:this.sourceRtpTimestamp,source_media_time:this.sourceMediaTime,
      first_source_rtp_timestamp:this.firstSourceRtpTimestamp,
      first_source_media_time:this.firstSourceMediaTime,
      audio_codec:audioCodec?.mimeType ?? null,
      audio_samples:inboundAudio?.totalSamplesReceived ?? 0,
      audio_energy:inboundAudio?.totalAudioEnergy ?? 0,audio_rms:rms};
  }
};
</script>"""


class PageHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        """Serve only the receive-only synthetic peer page."""
        self.send_response(200 if self.path == "/probe" else 404)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        if self.path == "/probe":
            self.wfile.write(PAGE)

    def log_message(self, _format, *_args):
        """Avoid access-log noise in the isolated test."""


class Browser:
    def __init__(self, address: str):
        self.socket = websocket.create_connection(
            address, suppress_origin=True, timeout=10
        )
        self.identifier = 0

    def evaluate(self, expression: str):
        """Run trusted test code without printing SDP or temporary credentials."""
        self.identifier += 1
        self.socket.send(
            json.dumps(
                {
                    "id": self.identifier,
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": expression,
                        "awaitPromise": True,
                        "returnByValue": True,
                        "userGesture": True,
                    },
                }
            )
        )
        while True:
            response = json.loads(self.socket.recv())
            if response.get("id") != self.identifier:
                continue
            if "error" in response or "exceptionDetails" in response.get("result", {}):
                raise RuntimeError("Browser peer evaluation failed")
            return response["result"]["result"].get("value")

    def close(self):
        """Close the owned debugger connection."""
        self.socket.close()


def prepare_tls(work: Path, home: Path) -> tuple[Path, Path]:
    """Trust an isolated test CA rather than bypassing certificate verification."""
    ca, ca_key = work / "ca.pem", work / "ca.key"
    certificate, key = work / "turn.pem", work / "turn.key"
    request, extensions = work / "turn.csr", work / "turn.ext"
    database = home / ".pki" / "nssdb"
    database.mkdir(mode=0o700, parents=True)
    extensions.write_text(
        "basicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\nsubjectAltName=IP:127.0.0.1\n"
    )
    commands = (
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "2",
            "-subj",
            "/CN=Isolated OME qualification CA",
            "-keyout",
            str(ca_key),
            "-out",
            str(ca),
            "-addext",
            "basicConstraints=critical,CA:TRUE",
        ],
        [
            "openssl",
            "req",
            "-new",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-subj",
            "/CN=127.0.0.1",
            "-keyout",
            str(key),
            "-out",
            str(request),
        ],
        [
            "openssl",
            "x509",
            "-req",
            "-in",
            str(request),
            "-CA",
            str(ca),
            "-CAkey",
            str(ca_key),
            "-CAcreateserial",
            "-out",
            str(certificate),
            "-days",
            "2",
            "-extfile",
            str(extensions),
        ],
        ["certutil", "-N", "--empty-password", "-d", "sql:" + str(database)],
        [
            "certutil",
            "-A",
            "-d",
            "sql:" + str(database),
            "-n",
            "Isolated OME qualification CA",
            "-t",
            "C,,",
            "-i",
            str(ca),
        ],
    )
    for command in commands:
        subprocess.run(
            command,
            check=True,
            timeout=30,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    for private_key in (ca_key, key):
        private_key.chmod(0o600)
    return certificate, key


def drain_signaling(
    connection: websocket.WebSocket, stopped: threading.Event, errors: list[str]
) -> None:
    """Consume control frames so the real signaling socket answers server pings."""
    while not stopped.is_set():
        try:
            message = connection.recv()
        except websocket.WebSocketTimeoutException:
            continue
        except (websocket.WebSocketException, OSError) as error:
            if not stopped.is_set():
                errors.append(type(error).__name__)
            return
        if stopped.is_set():
            return
        if not message:
            errors.append("signaling_closed")
            return
        try:
            response = json.loads(message)
        except ValueError:
            errors.append("invalid_signaling_json")
            return
        if not isinstance(response, dict) or response.get("code", 200) != 200:
            errors.append("signaling_error_response")
            return


def main(
    *, recorded_hold_only: bool = False, recorded_audio_only: bool = False
) -> None:
    """Require decoded H264, selected relay routes, renewal and final-source cleanup."""
    assert os.getuid() == 1000 and os.getgid() == 1000
    work = Path.cwd() / f"ome-browser-{os.getpid()}"
    work.mkdir(mode=0o700)
    home = work / "home"
    home.mkdir(mode=0o700)
    temporary = work / "tmp"
    temporary.mkdir(mode=0o700)
    processes = []
    logs = []
    fixture = None
    server = None
    browser = None
    signaling = None
    signaling_stop = None
    signaling_reader = None
    try:
        token, secret = secrets.token_hex(32), secrets.token_hex(32)
        certificate, key = prepare_tls(work, home)
        schedules = work / "schedules"
        schedules.mkdir(mode=0o700)
        tree = ET.fromstring(runtime.server_xml(token))
        provider = tree.find(".//Applications/Application/Providers")
        assert provider is not None
        schedule = ET.SubElement(provider, "Schedule")
        ET.SubElement(schedule, "MediaRootDir").text = str(work)
        ET.SubElement(schedule, "ScheduleFilesDir").text = str(schedules)
        if recorded_audio_only:
            encodes = tree.find(".//Encodes")
            rendition = tree.find(".//Rendition")
            assert encodes is not None and rendition is not None
            audio = ET.SubElement(encodes, "Audio")
            for name, value in (
                ("Name", "opus"),
                ("Codec", "opus"),
                ("Bitrate", "64000"),
                ("Samplerate", "48000"),
                ("Channel", "2"),
            ):
                ET.SubElement(audio, name).text = value
            ET.SubElement(rendition, "Audio").text = "opus"
        (work / "Server.xml").write_text(ET.tostring(tree, encoding="unicode"))
        (work / "Logger.xml").write_text(
            '<Logger version="2"><File enabled="false"/><Tag name=".*" level="warn"/></Logger>'
        )
        (work / "turnserver.conf").write_text(
            "listening-ip=127.0.0.1\nrelay-ip=127.0.0.1\nlistening-port=3478\n"
            "realm=browser.qualification.invalid\nuse-auth-secret\n"
            f"static-auth-secret={secret}\n"
            "min-port=49160\nmax-port=49179\nuser-quota=4\ntotal-quota=16\n"
            "relay-threads=1\nno-dtls\nno-cli\nno-rfc5780\n"
            f"tls-listening-port=5349\ncert={certificate}\npkey={key}\n"
            f"cli-password={secrets.token_hex(32)}\n"
            "no-multicast-peers\nno-tcp-relay\nallow-loopback-peers\n"
            "max-allocate-lifetime=120\nlog-file=stdout\nsimple-log\n"
            f"pidfile={work / 'turn.pid'}\nuserdb={work / 'turnserver.db'}\n"
        )
        source = work / "source.h264"
        runtime.generate_fixture(source)
        recording = work / "binary-frames.mp4"
        subprocess.run(
            [
                os.environ.get("OME_TEST_FFMPEG", "ffmpeg"),
                "-nostdin",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=320x180:r=10:d=20",
                "-vf",
                "geq=lum='16+200*mod(floor(N/pow(2,floor(X/20))),2)':cb=128:cr=128",
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
                "-bf",
                "0",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(recording),
            ],
            check=True,
            timeout=30,
        )
        if recorded_audio_only:
            audio_recording = work / "binary-frames-audio.mp4"
            subprocess.run(
                [
                    os.environ.get("OME_TEST_FFMPEG", "ffmpeg"),
                    "-nostdin",
                    "-v",
                    "error",
                    "-i",
                    str(recording),
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=48000:duration=20",
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-c:v",
                    "copy",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "64000",
                    "-ac",
                    "1",
                    "-t",
                    "20",
                    "-movflags",
                    "+faststart",
                    str(audio_recording),
                ],
                check=True,
                timeout=30,
            )
            audio_recording.replace(recording)
        original_digest = hashlib.sha256(recording.read_bytes()).hexdigest()
        fixture = runtime.FixtureServer(*runtime.annexb_frames(source.read_bytes()))
        threading.Thread(target=fixture.serve_forever, daemon=True).start()
        server = ThreadingHTTPServer(("127.0.0.1", 19080), PageHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        commands = (
            ([str(runtime.PREFIX / "bin/OvenMediaEngine"), "-c", str(work)], "ome"),
            (["turnserver", "-c", str(work / "turnserver.conf")], "turn"),
            (
                [
                    "chromium",
                    "--headless=new",
                    "--no-sandbox",
                    "--no-first-run",
                    "--disable-background-networking",
                    "--disable-component-update",
                    "--disable-sync",
                    "--allow-loopback-in-peer-connection",
                    "--disable-features=WebRtcHideLocalIpsWithMdns",
                    "--remote-debugging-address=127.0.0.1",
                    "--remote-debugging-port=9222",
                    f"--user-data-dir={home}",
                    "http://127.0.0.1:19080/probe",
                ],
                "browser",
            ),
        )
        for command, name in commands:
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
                        "HOME": str(home),
                        "TMPDIR": str(temporary),
                        "OMP_NUM_THREADS": "2",
                    },
                )
            )
        runtime.wait_for(lambda: runtime.api(token))

        def browser_target():
            with urllib.request.urlopen(
                "http://127.0.0.1:9222/json/list", timeout=3
            ) as response:
                targets = json.load(response)
            return next(
                (
                    target["webSocketDebuggerUrl"]
                    for target in targets
                    if target.get("type") == "page"
                    and target.get("url") == "http://127.0.0.1:19080/probe"
                ),
                None,
            )

        browser = Browser(runtime.wait_for(browser_target))
        runtime.wait_for(lambda: browser.evaluate("Boolean(window.probe)"))
        results = []
        cases = (
            ("direct", None, 3, None, False),
            ("recorded_seek_5000", None, 2, 5000, False),
            ("recorded_seek_11000", None, 2, 11000, False),
            ("relay_udp", "udp", 3, None, False),
            ("relay_tls", "tls", 3, None, False),
            ("relay_tcp_refresh", "tcp", 150, None, False),
        )
        if recorded_hold_only:
            cases = (
                ("recorded_hold_5000", None, 2, 5000, True),
                ("recorded_hold_11000", None, 2, 11000, True),
            )
        if recorded_audio_only:
            cases = (
                ("recorded_audio_seek_5000", None, 2, 5000, False),
                ("recorded_audio_hold_5000", None, 2, 5000, True),
            )
        for label, route, duration, offset, hold in cases:
            assert runtime.api(token)["response"] == []
            started = time.monotonic()
            name = "synthetic_" + label
            if offset is None:
                runtime.api(
                    token,
                    "POST",
                    body={
                        "name": name,
                        "urls": ["rtsp://127.0.0.1:18554/fixture"],
                        "properties": {
                            "persistent": False,
                            "retryCount": 0,
                            "ignoreRtcpSRTimestamp": True,
                            "unusedStreamDeletionTimeoutMs": 30000,
                        },
                    },
                )
            else:
                runtime.api(
                    token,
                    "POST",
                    body={
                        "stream": {
                            "name": name,
                            "bypassTranscoder": False,
                            "videoTrack": True,
                            "audioTrack": recorded_audio_only,
                        },
                        "programs": [
                            {
                                "name": label + "_hold" if hold else label,
                                "scheduled": datetime.now(UTC).isoformat(
                                    timespec="milliseconds"
                                ),
                                "repeat": hold,
                                "items": [
                                    {
                                        "url": "file://binary-frames.mp4",
                                        "start": offset,
                                        "duration": 200 if hold else 8000,
                                    }
                                ],
                            }
                        ],
                    },
                    collection="scheduledChannels",
                )

            def source_ready():
                value = runtime.api(token, suffix="/" + name)["response"]
                return any(
                    track.get("video", {}).get("codecStatus") == "Ready"
                    for output in value.get("outputs", [])
                    for track in output.get("tracks", [])
                    if track.get("type") == "Video"
                )

            runtime.wait_for(source_ready)
            signaling = websocket.create_connection(
                f"ws://127.0.0.1:3333/qualify/{name}/master?transport=udp",
                origin="http://127.0.0.1",
                timeout=10,
                header={"User-Agent": "FrigateLiveWebRTC/1"},
            )
            signaling.send(json.dumps({"command": "request_offer"}))
            offer = json.loads(signaling.recv())
            assert offer.get("command") == "offer" and offer.get("code") == 200, {
                "command": offer.get("command"),
                "code": offer.get("code"),
                "response_fields": sorted(offer),
            }
            signaling_stop = threading.Event()
            signaling_errors = []
            signaling_reader = threading.Thread(
                target=drain_signaling,
                args=(signaling, signaling_stop, signaling_errors),
                daemon=True,
            )
            signaling_reader.start()
            servers = []
            if route:
                username = f"{int(time.time()) + 90}:{label}"
                password = base64.b64encode(
                    hmac.digest(secret.encode(), username.encode(), "sha1")
                ).decode()
                servers = [
                    {
                        "urls": [
                            "turns:127.0.0.1:5349?transport=tcp"
                            if route == "tls"
                            else f"turn:127.0.0.1:3478?transport={route}"
                        ],
                        "username": username,
                        "credential": password,
                    }
                ]
            answer = browser.evaluate(
                "window.probe.start("
                + ",".join(
                    json.dumps(value)
                    for value in (
                        offer["sdp"],
                        offer["candidates"],
                        servers,
                        "relay" if route else "all",
                        offset is not None,
                        recorded_audio_only,
                    )
                )
                + ")"
            )
            signaling.send(
                json.dumps(
                    {
                        "command": "answer",
                        "id": offer["id"],
                        "peer_id": offer["peer_id"],
                        "sdp": answer,
                    }
                )
            )

            last_stats = {}

            def decoded():
                nonlocal last_stats
                assert not signaling_errors, {
                    "path": label,
                    "signaling_errors": signaling_errors,
                }
                candidates = browser.evaluate("window.probe.pending.splice(0, 32)")
                if candidates:
                    signaling.send(
                        json.dumps(
                            {
                                "command": "candidate",
                                "id": offer["id"],
                                "peer_id": offer["peer_id"],
                                "candidates": candidates,
                            }
                        )
                    )
                value = browser.evaluate("window.probe.stats()")
                last_stats = value
                if value["error"] or value["state"] == "failed":
                    raise RuntimeError(f"Actual {label} WebRTC peer failed: {value}")
                return (
                    value
                    if value["frames"] >= 5
                    and value["width"] > 0
                    and value["height"] > 0
                    else None
                )

            try:
                first = runtime.wait_for(decoded, timeout=20)
                if hold:
                    runtime.api(
                        token,
                        "PATCH",
                        "/" + name,
                        body={
                            "programs": [
                                {
                                    "name": label,
                                    "scheduled": datetime.now(UTC).isoformat(
                                        timespec="milliseconds"
                                    ),
                                    "repeat": False,
                                    "items": [
                                        {
                                            "url": "file://binary-frames.mp4",
                                            "start": offset,
                                            "duration": 8000,
                                        }
                                    ],
                                }
                            ],
                        },
                        collection="scheduledChannels",
                    )
                    runtime.wait_for(
                        lambda: (
                            runtime.api(
                                token, suffix="/" + name, collection="scheduledChannels"
                            )["response"]
                            .get("currentProgram", {})
                            .get("name")
                            == label
                        )
                    )
                    first = runtime.wait_for(decoded, timeout=20)
            except AssertionError as error:
                raise AssertionError(
                    f"Actual decoded media did not become ready: {last_stats}"
                ) from error
            assert first["width"] == 160 and first["height"] == 96, first
            assert first["codec"].lower() == "video/h264"
            assert first["candidate_type"] in (
                {"relay"} if route else {"host", "prflx"}
            ), first
            if route:
                assert first["relay_protocol"] == route
            if recorded_audio_only:
                assert (
                    first["audio_codec"]
                    and first["audio_codec"].lower() == "audio/opus"
                ), first
                assert first["audio_samples"] > 0 and first["audio_rms"] > 0.005, first
            recorded_evidence = None
            if offset is not None:
                assert first["first_source_frame"] is not None
                assert (
                    offset / 100
                    <= first["first_source_frame"]
                    <= first["source_frame"]
                    <= offset / 100 + 10 * (time.monotonic() - started + 1)
                ), first
                current = runtime.api(
                    token, suffix="/" + name, collection="scheduledChannels"
                )["response"]["currentProgram"]["currentItem"]
                decoded_position = first["source_frame"] / 10
                native_position = (offset + current["currentPosition"]) / 1000
                assert abs(decoded_position - native_position) <= 1, {
                    "decoded_seconds": decoded_position,
                    "native_seconds": native_position,
                }
                recorded_evidence = {
                    "requested_start_seconds": offset / 1000,
                    "first_decoded_source_seconds": first["first_source_frame"] / 10,
                    "decoded_source_seconds": decoded_position,
                    "native_source_seconds": native_position,
                    "maximum_timestamp_difference_seconds": 1,
                    "held_during_signaling": hold,
                    "first_source_rtp_timestamp": first["first_source_rtp_timestamp"],
                    "source_rtp_timestamp": first["source_rtp_timestamp"],
                    "first_source_media_time": first["first_source_media_time"],
                    "source_media_time": first["source_media_time"],
                    "audio_codec": first["audio_codec"],
                    "audio_samples": first["audio_samples"],
                    "audio_energy": first["audio_energy"],
                    "audio_rms": first["audio_rms"],
                }
                if hold:
                    assert first["first_source_frame"] / 10 - offset / 1000 <= 0.2, (
                        first
                    )
            ready = time.monotonic()
            print(
                json.dumps(
                    {
                        "phase": "decoded_media_ready",
                        "path": label,
                        "width": first["width"],
                        "height": first["height"],
                        "codec": first["codec"],
                        "selected_candidate_type": first["candidate_type"],
                        "relay_protocol": first["relay_protocol"],
                        "recorded_source": recorded_evidence,
                    }
                ),
                flush=True,
            )
            previous = first["frames"]
            for sample in range(1, math.ceil(duration / 3) + 1):
                target = ready + min(duration, sample * 3)
                time.sleep(max(0, target - time.monotonic()))
                progress = decoded()
                assert progress is not None and progress["frames"] > previous, {
                    "path": label,
                    "elapsed_seconds": round(time.monotonic() - ready, 3),
                    "previous_frames": previous,
                    "current_stats": progress,
                }
                if offset is not None:
                    assert progress["source_frame"] > first["source_frame"]
                if recorded_audio_only:
                    assert progress["audio_samples"] > first["audio_samples"]
                previous = progress["frames"]
            results.append(
                {
                    "path": label,
                    "create_to_five_decoded_frames_seconds": round(ready - started, 3),
                    "sustained_seconds": duration,
                    "final_decoded_frames": previous,
                    "selected_candidate_type": first["candidate_type"],
                    "relay_protocol": first["relay_protocol"],
                    "credential_lifetime_seconds": 90 if route else None,
                    "max_allocation_lifetime_seconds": 120 if route else None,
                    "recorded_source": recorded_evidence,
                }
            )
            print(
                json.dumps({"phase": "sustained_frames_passed", **results[-1]}),
                flush=True,
            )
            browser.evaluate("window.probe.peer.close()")
            signaling_stop.set()
            signaling.close()
            signaling_reader.join(timeout=3)
            signaling = None
            runtime.api(
                token,
                "DELETE",
                "/" + name,
                collection="streams" if offset is None else "scheduledChannels",
            )
            runtime.wait_for(lambda: runtime.api(token)["response"] == [])
            runtime.wait_for(lambda: not fixture.active)
            if offset is not None:
                runtime.wait_for(lambda: not list(schedules.glob("*.sch")))
        assert hashlib.sha256(recording.read_bytes()).hexdigest() == original_digest
        idle = runtime.measured_interval(processes[0].pid, 3)
        assert idle["cpu_percent"] < 20
        print(
            json.dumps(
                {
                    "result": "passed",
                    "decoded_transport_tests": results,
                    "after_last_viewer": idle,
                    "synthetic_original_sha256_unchanged": original_digest,
                    "qualification": "actual isolated WebRTC frames; not production LAN/cellular/TLS proof",
                },
                indent=2,
            )
        )
    except (
        OSError,
        ValueError,
        AssertionError,
        KeyError,
        RuntimeError,
        subprocess.SubprocessError,
        websocket.WebSocketException,
    ):
        for log in logs:
            log.flush()
        diagnostics = work / "browser.log"
        if diagnostics.exists():
            print(
                "Isolated Chromium diagnostics:\n"
                + diagnostics.read_text(errors="replace")[-4000:]
                .replace(token, "<redacted>")
                .replace(secret, "<redacted>"),
                file=sys.stderr,
            )
        turn_diagnostics = work / "turn.log"
        if turn_diagnostics.exists():
            lines = turn_diagnostics.read_text(errors="replace").splitlines()
            warnings = [
                line.replace(secret, "<redacted>")
                for line in lines
                if any(
                    term in line.lower() for term in ("error", "warning", "loopback")
                )
            ]
            print(
                "Isolated TURN warnings:\n" + "\n".join(warnings[-10:]), file=sys.stderr
            )
        raise
    finally:
        if signaling_stop is not None:
            signaling_stop.set()
        if signaling is not None:
            signaling.close()
        if signaling_reader is not None:
            signaling_reader.join(timeout=3)
        if browser is not None:
            browser.close()
        for process in reversed(processes):
            process.terminate()
        for process in reversed(processes):
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if fixture is not None:
            fixture.shutdown()
            fixture.server_close()
        if server is not None:
            server.shutdown()
            server.server_close()
        for log in logs:
            log.close()
        shutil.rmtree(work)


if __name__ == "__main__":
    main()

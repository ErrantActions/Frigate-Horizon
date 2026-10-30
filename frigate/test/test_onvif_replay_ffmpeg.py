"""Optional real-FFmpeg browser replay integration using generated loopback RTP only."""

import asyncio
import contextlib
import json
import os
import shutil
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from frigate.ptz.replay import OnvifReplayManager
from frigate.ptz.replay_transport import ReplaySource, clock_range, read_message

FFMPEG = os.environ.get("ONVIF_TEST_FFMPEG") or shutil.which("ffmpeg")
FFPROBE = os.environ.get("ONVIF_TEST_FFPROBE") or (
    str(Path(FFMPEG).with_name("ffprobe.exe" if FFMPEG.endswith(".exe") else "ffprobe"))
    if FFMPEG
    else None
)


@unittest.skipUnless(
    FFMPEG, "Set ONVIF_TEST_FFMPEG to the image's existing FFmpeg binary"
)
class TestReplayRealFfmpeg(unittest.IsolatedAsyncioTestCase):
    async def test_generated_h264_rtp_becomes_decodable_browser_mp4(self):
        await self._replay(False)

    async def test_recorded_g711_audio_is_normalized_to_aac_lc_48k(self):
        await self._replay(True)

    async def _replay(self, with_audio):
        queue = asyncio.Queue(maxsize=512)
        failures = []
        senders = []
        clients = []
        tasks = set()
        frames_received = asyncio.Event()
        frame_count = 0
        now = datetime(2026, 10, 18, 6, tzinfo=UTC)
        end = now + timedelta(seconds=4)

        class Packets(asyncio.DatagramProtocol):
            def __init__(self, channel=0):
                self.channel = channel

            def datagram_received(self, data, address):
                nonlocal frame_count
                if (
                    self.channel == 0
                    and len(data) > 1
                    and data[1] & 0x80
                    and data[1] & 0x7F == 96
                ):
                    frame_count += 1
                    if frame_count >= 20:
                        frames_received.set()
                try:
                    queue.put_nowait((self.channel, data))
                except asyncio.QueueFull:
                    failures.append("Generated RTP exceeded the bounded test queue")

        udp, _protocol = await asyncio.get_running_loop().create_datagram_endpoint(
            Packets,
            local_addr=("127.0.0.1", 0),
        )
        rtp_port = udp.get_extra_info("sockname")[1]
        audio_udp = None
        audio_port = None
        if with_audio:
            audio_udp, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: Packets(2),
                local_addr=("127.0.0.1", 0),
            )
            audio_port = audio_udp.get_extra_info("sockname")[1]

        async def relay(writer):
            command = [
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=64x64:rate=5",
            ]
            if with_audio:
                command += ["-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=8000"]
            command += [
                "-map",
                "0:v:0",
                "-frames:v",
                "20",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-tune",
                "zerolatency",
                "-threads",
                "1",
                "-g",
                "5",
                "-x264-params",
                "repeat-headers=1",
                "-payload_type",
                "96",
                "-f",
                "rtp",
                f"rtp://127.0.0.1:{rtp_port}?rtcpport={rtp_port}&pkt_size=1200",
            ]
            if with_audio:
                command += [
                    "-map",
                    "1:a:0",
                    "-t",
                    "4",
                    "-vn",
                    "-c:a",
                    "pcm_mulaw",
                    "-ar",
                    "8000",
                    "-ac",
                    "1",
                    "-payload_type",
                    "0",
                    "-f",
                    "rtp",
                    f"rtp://127.0.0.1:{audio_port}?rtcpport={audio_port}&pkt_size=1200",
                ]
            sender = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            senders.append(sender)

            async def forward():
                while True:
                    base_channel, data = await queue.get()
                    try:
                        channel = base_channel + (
                            1 if len(data) > 1 and 192 <= data[1] <= 223 else 0
                        )
                        writer.write(
                            b"$"
                            + bytes([channel])
                            + len(data).to_bytes(2, "big")
                            + data
                        )
                        await writer.drain()
                    finally:
                        queue.task_done()

            forwarding = asyncio.create_task(forward())
            try:
                _stdout, stderr = await asyncio.wait_for(sender.communicate(), 15)
                if sender.returncode:
                    failures.append(stderr.decode(errors="replace"))
                await asyncio.wait_for(frames_received.wait(), 3)
                await asyncio.wait_for(queue.join(), 3)
            finally:
                forwarding.cancel()
                await asyncio.gather(forwarding, return_exceptions=True)
                writer.close()

        async def camera(reader, writer):
            clients.append(writer)
            tasks.add(asyncio.current_task())
            try:
                while True:
                    line, headers, _body = await read_message(reader)
                    method = line.split(" ", 1)[0]
                    body = b""
                    response = {"CSeq": headers["cseq"], "Session": "test-session"}
                    if method == "DESCRIBE":
                        body = (
                            b"v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=Generated test recording\r\n"
                            b"c=IN IP4 127.0.0.1\r\nt=0 0\r\na=control:*\r\n"
                            b"m=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n"
                            b"a=fmtp:96 packetization-mode=1\r\na=control:track1\r\n"
                        )
                        if with_audio:
                            body += (
                                b"m=audio 0 RTP/AVP 0\r\na=rtpmap:0 PCMU/8000/1\r\n"
                                b"a=control:audio1\r\n"
                            )
                        response["Content-Type"] = "application/sdp"
                        response["Content-Base"] = (
                            f"rtsp://camera.invalid:{camera_port}/recording/"
                        )
                    if method == "SETUP":
                        response["Transport"] = headers["transport"]
                    if method == "PLAY":
                        if headers.get("range") != clock_range(now, end):
                            failures.append("Missing fixed ONVIF clock range")
                        response["Range"] = clock_range(now, end)
                    response["Content-Length"] = str(len(body))
                    message = "RTSP/1.0 200 OK\r\n" + "".join(
                        f"{key}: {value}\r\n" for key, value in response.items()
                    )
                    writer.write(message.encode() + b"\r\n" + body)
                    await writer.drain()
                    if method == "PLAY":
                        task = asyncio.create_task(relay(writer))
                        tasks.add(task)
                    if method == "TEARDOWN":
                        break
            except asyncio.IncompleteReadError:
                pass
            finally:
                writer.close()

        server = await asyncio.start_server(camera, "127.0.0.1", 0)
        camera_port = server.sockets[0].getsockname()[1]
        configuration = object()
        manager = OnvifReplayManager(lambda: configuration)
        try:
            public = manager.create(
                "mock",
                ReplaySource(
                    f"rtsp://camera.invalid:{camera_port}/recording?private=not-for-browser",
                    "test",
                    "secret",
                    "127.0.0.1",
                    camera_port,
                    "camera.invalid",
                    recorded_audio=with_audio,
                ),
                now,
                end,
                FFMPEG,
                configuration,
            )
            session, first = await asyncio.wait_for(
                manager.prepare("mock", public["id"]), 20
            )

            async def connected():
                return True

            chunks = [
                chunk async for chunk in manager.stream(session, first, connected)
            ]
            content = b"".join(chunks)
            self.assertIn(b"ftyp", content)
            self.assertIn(b"moof", content)
            self.assertIn(b"mdat", content)
            self.assertNotIn(b"not-for-browser", content)
            self.assertEqual(session.audio, with_audio)
            self.assertEqual(
                session.audio_status, "available" if with_audio else "not_recorded"
            )
            probe = await asyncio.create_subprocess_exec(
                FFPROBE,
                "-v",
                "error",
                "-show_streams",
                "-of",
                "json",
                "-i",
                "pipe:0",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            senders.append(probe)
            probe_output, probe_error = await asyncio.wait_for(
                probe.communicate(content), 10
            )
            self.assertEqual(probe.returncode, 0, probe_error.decode(errors="replace"))
            streams = json.loads(probe_output)["streams"]
            video = next(
                stream for stream in streams if stream["codec_type"] == "video"
            )
            self.assertEqual(video["codec_name"], "h264")
            self.assertEqual(video["pix_fmt"], "yuv420p")
            audio = [stream for stream in streams if stream["codec_type"] == "audio"]
            self.assertEqual(len(audio), 1 if with_audio else 0)
            if with_audio:
                self.assertEqual(audio[0]["codec_name"], "aac")
                self.assertEqual(audio[0]["profile"], "LC")
                self.assertEqual(audio[0]["sample_rate"], "48000")
            decoder = await asyncio.create_subprocess_exec(
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-threads",
                "1",
                "-i",
                "pipe:0",
                "-f",
                "null",
                "-",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            senders.append(decoder)
            _stdout, stderr = await asyncio.wait_for(decoder.communicate(content), 10)
            self.assertEqual(decoder.returncode, 0, stderr.decode(errors="replace"))
            self.assertEqual(failures, [])
        finally:
            await manager.shutdown()
            server.close()
            await server.wait_closed()
            udp.close()
            if audio_udp is not None:
                audio_udp.close()
            for process in senders:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            for writer in clients:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

"""Probe native scheduled-file input without touching stored camera recordings."""

import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

import test_ovenmediaengine_runtime as runtime


def main() -> None:
    """Require file offset, source immutability, stream cleanup and idle recovery."""
    assert os.getuid() == 1000 and os.getgid() == 1000
    work = Path.cwd() / f"ome-recorded-source-{os.getpid()}"
    work.mkdir(mode=0o700)
    schedules = work / "schedules"
    schedules.mkdir(mode=0o700)
    process = None
    log = None
    try:
        source = work / "original.mp4"
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
                "20",
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
                str(source),
            ],
            check=True,
            timeout=30,
        )
        original_digest = hashlib.sha256(source.read_bytes()).hexdigest()
        token = "recorded-probe:" + secrets.token_hex(24)
        tree = ET.fromstring(runtime.server_xml(token))
        provider = tree.find(".//Applications/Application/Providers")
        assert provider is not None, "Synthetic application providers are missing"
        schedule = ET.SubElement(provider, "Schedule")
        ET.SubElement(schedule, "MediaRootDir").text = str(work)
        ET.SubElement(schedule, "ScheduleFilesDir").text = str(schedules)
        (work / "Server.xml").write_text(ET.tostring(tree, encoding="unicode"))
        (work / "Logger.xml").write_text(
            '<Logger version="2"><File enabled="false"/>'
            '<Tag name=".*" level="warn"/></Logger>'
        )
        log = (work / "engine.log").open("w")
        process = subprocess.Popen(
            [str(runtime.PREFIX / "bin/OvenMediaEngine"), "-c", str(work)],
            cwd=work,
            stdout=log,
            stderr=subprocess.STDOUT,
            env={**os.environ, "OMP_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "2"},
        )
        runtime.wait_for(lambda: runtime.api(token))
        assert runtime.api(token)["response"] == []
        idle = runtime.measured_interval(process.pid, 2)
        runtime.api(
            token,
            "POST",
            body={
                "stream": {
                    "name": "recorded_probe",
                    "bypassTranscoder": False,
                    "videoTrack": True,
                    "audioTrack": False,
                },
                "programs": [
                    {
                        "name": "source-offset-5000",
                        "scheduled": datetime.now(UTC).isoformat(
                            timespec="milliseconds"
                        ),
                        "repeat": False,
                        "items": [
                            {
                                "url": "file://original.mp4",
                                "start": 5000,
                                "duration": 12000,
                            }
                        ],
                    }
                ],
            },
            collection="scheduledChannels",
        )

        def running():
            channel = runtime.api(
                token, suffix="/recorded_probe", collection="scheduledChannels"
            )["response"]
            program = channel.get("currentProgram", {})
            return program if program.get("state") == "onair" else None

        program = runtime.wait_for(running)
        assert program["currentItem"]["start"] == 5000
        first_position = program["currentItem"]["currentPosition"]
        time.sleep(1)
        later = running()
        assert later is not None
        next_position = later["currentItem"]["currentPosition"]
        assert next_position > first_position, "Native file playhead did not advance"
        runtime.api(token, "DELETE", "/recorded_probe", collection="scheduledChannels")
        runtime.wait_for(lambda: runtime.api(token)["response"] == [])
        runtime.wait_for(lambda: not list(schedules.glob("*.sch")))
        after = runtime.measured_interval(process.pid, 3)
        assert after["cpu_percent"] < 20, "Deleted recorded source kept encoding"
        assert hashlib.sha256(source.read_bytes()).hexdigest() == original_digest
        assert process.poll() is None
        print(
            json.dumps(
                {
                    "result": "passed",
                    "source": "synthetic original MP4, not a stored camera recording",
                    "requested_start_ms": 5000,
                    "first_reported_position_ms": first_position,
                    "next_reported_position_ms": next_position,
                    "source_sha256_unchanged": original_digest,
                    "idle": idle,
                    "after_delete": after,
                    "qualification": "native file-source control, not decoded WebRTC seek accuracy",
                },
                indent=2,
            )
        )
    except (OSError, ValueError, AssertionError, KeyError, subprocess.SubprocessError):
        if log is not None:
            log.flush()
            diagnostics = (work / "engine.log").read_text(errors="replace")[-4000:]
            print(
                "Synthetic OME diagnostics:\n"
                + diagnostics.replace(token, "<redacted>"),
                file=sys.stderr,
            )
        raise
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if log is not None:
            log.close()
        shutil.rmtree(work)


if __name__ == "__main__":
    main()

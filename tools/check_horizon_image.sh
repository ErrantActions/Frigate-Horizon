#!/bin/bash
set -euo pipefail
image="${1:?Pass the locally built image tag}"
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
image_id="$(docker image inspect "$image" --format '{{.Id}}')"
stage="$(mktemp -d)"
name="horizon-ci-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}"
if docker container inspect "$name" >/dev/null 2>&1; then
    echo "Refusing to replace an existing qualification container: $name" >&2
    exit 1
fi
cleanup() {
    docker rm -f "$name" >/dev/null 2>&1 || true
    rm -f "$stage/config.yml" "$stage/check.py"
    rmdir "$stage" 2>/dev/null || true
}
trap cleanup EXIT

docker run --rm --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges --cpus=2 --memory=6g --pids-limit=256 \
    -e NVIDIA_VISIBLE_DEVICES=void -e PYTHONDONTWRITEBYTECODE=1 \
    -e OPENBLAS_NUM_THREADS=1 -e OMP_NUM_THREADS=1 \
    -e ONVIF_TEST_FFMPEG=/usr/lib/ffmpeg/8.0/bin/ffmpeg \
    -e ONVIF_TEST_FFPROBE=/usr/lib/ffmpeg/8.0/bin/ffprobe \
    -e PYTHONPATH=/opt/frigate:/source \
    --tmpfs /tmp:rw,exec,size=1g --tmpfs /config:rw,size=64m \
    --tmpfs /media/frigate:rw,exec,size=64m \
    --mount "type=bind,src=$root,dst=/source,readonly" \
    --entrypoint bash "$image_id" -c '
      set -euo pipefail
      cd /media/frigate
      export TMPDIR="$PWD" TMP="$PWD" TEMP="$PWD"
      ln -s /opt/frigate/migrations migrations
      python3 -u -m unittest \
        frigate.test.test_native_preferences \
        frigate.test.test_plugin_legacy_settings \
        frigate.test.test_plugins \
        frigate.test.test_plugin_api_loop \
        frigate.test.http_api.test_http_plugins \
        frigate.test.test_native_device_preferences_api \
        frigate.test.test_native_resolver \
        frigate.test.test_native_push_api \
        frigate.test.test_native_health \
        frigate.test.test_native_features_api \
        frigate.test.test_native_control_leases \
        frigate.test.test_live_hls \
        frigate.test.test_live_hls_manager \
        frigate.test.test_live_hls_api \
        frigate.test.test_live_webrtc \
        frigate.test.test_live_webrtc_api \
        frigate.test.test_recorded_webrtc \
        frigate.test.test_recorded_webrtc_api \
        frigate.test.test_ovenmediaengine_bootstrap \
        frigate.test.test_live_streams \
        frigate.test.test_preview_hls \
        frigate.test.test_media_auth \
        frigate.test.test_birdseye_hls_output \
        frigate.test.test_birdseye \
        frigate.test.test_onvif_replay \
        frigate.test.test_onvif_replay_ffmpeg \
        frigate.test.http_api.test_http_onvif \
        frigate.test.test_export_protection \
        frigate.test.test_playback_shared_lifespan \
        frigate.test.test_api_auth_spec \
        frigate.test.test_apns
      python3 -u -m unittest \
        frigate.test.test_apns_pipeline \
        frigate.test.test_recording_mirror \
        frigate.test.test_recording_mirror_linux \
        frigate.test.test_archive_store \
        frigate.test.test_archive_limits \
        frigate.test.test_archive_manager \
        frigate.test.test_archive_parallel \
        frigate.test.test_archive_selection \
        frigate.test.test_recording_policy \
        frigate.test.test_recording_protection \
        frigate.test.test_storage_usage \
        frigate.test.test_storage_snapshot \
        frigate.test.test_config_util \
        frigate.test.test_native_push_images
      python3 /source/tools/test_native_owned_media.py \
        --binary /usr/local/go2rtc/bin/go2rtc \
        --receipt /usr/local/share/go2rtc/horizon-owned-webrtc.json \
        --ffmpeg /usr/lib/ffmpeg/8.0/bin/ffmpeg
      test -s /usr/local/share/licenses/go2rtc/LICENSE
      python3 -B -m unittest discover -s /source/tools -p test_ovenmediaengine_cli.py -v
    '

docker run --rm --user 1000:1000 --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges --cpus=2 --memory=768m --pids-limit=128 \
    --entrypoint /bin/sh "$image_id" -ec '
      cd /opt/ovenmediaengine
      sha256sum -c share/manifest.sha256 >/dev/null
      /bin/sh check-cli.sh
      /usr/bin/turnserver --version
      test -s /usr/share/frigate-ovenmediaengine/runtime-packages.tsv
      python3 /opt/live-hls/verify.py --payload
    '

docker run --rm --user 1000:1000 --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges --cpus=2 --memory=2g --pids-limit=128 \
    -e NVIDIA_VISIBLE_DEVICES=void -e PYTHONDONTWRITEBYTECODE=1 \
    -e PYTHONPATH=/opt/frigate:/source \
    -e ONVIF_TEST_FFMPEG=/usr/lib/ffmpeg/8.0/bin/ffmpeg \
    -e HLS_TEST_NGINX=/usr/local/nginx/sbin/nginx \
    --tmpfs /tmp:rw,size=512m,mode=1777 \
    --mount "type=bind,src=$root,dst=/source,readonly" \
    --workdir /tmp --entrypoint python3 "$image_id" \
    -m unittest frigate.test.test_preview_hls_nginx

cat > "$stage/config.yml" <<'YAML'
version: "0.19-0"
mqtt:
  enabled: false
auth:
  enabled: true
live_webrtc:
  enabled: true
  advertised_hosts: ["192.0.2.1"]
  turn:
    enabled: false
cameras: {}
record:
  enabled: false
notifications:
  enabled: false
apns:
  enabled: false
tls:
  enabled: false
networking:
  listen:
    internal: "127.0.0.1:5000"
    external: "127.0.0.1:8971"
YAML

docker run -d --name "$name" --network none --read-only \
    --security-opt no-new-privileges --cpus=2 --memory=4g --pids-limit=512 \
    --shm-size=256m -e NVIDIA_VISIBLE_DEVICES=void \
    --tmpfs /tmp:rw,exec,size=1g --tmpfs /run:rw,exec,size=64m \
    --tmpfs /config:rw,size=64m --tmpfs /media/frigate:rw,size=64m \
    --mount "type=bind,src=$stage/config.yml,dst=/bootstrap-config.yml,readonly" \
    --entrypoint bash "$image_id" -c 'cp /bootstrap-config.yml /config/config.yml; exec /init'
ready=false
for _ in $(seq 1 60); do
    if docker exec "$name" curl --fail --silent http://127.0.0.1:5000/api/version >/dev/null; then
        ready=true
        break
    fi
    state="$(docker inspect "$name" --format '{{.State.Status}}')"
    if [ "$state" != running ]; then
        echo "Candidate startup failed; raw logs are not published because they can contain bootstrap credentials"
        exit 1
    fi
    sleep 5
done
if [ "$ready" != true ]; then
    echo "Candidate API did not become ready within five minutes"
    exit 1
fi
code="$(docker exec "$name" curl --silent --output /dev/null --write-out '%{http_code}' \
    http://127.0.0.1:8971/api/profile)"
if [ "$code" != 401 ]; then
    echo "The external profile route did not require a real authenticated session"
    exit 1
fi
docker exec "$name" python3 -c '
import ast
from pathlib import Path
code = ast.parse(Path("/opt/frigate/frigate/api/fastapi_app.py").read_text())
assert any(isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
 and n.value.value is False and any(isinstance(t, ast.Attribute)
 and t.attr == "native_export_protection" for t in n.targets)
 for n in ast.walk(code)), "Export-protection admission must default off"
print("Empty authenticated startup and fail-closed export admission checks passed")
'
docker exec -i "$name" python3 - <<'PY'
import base64
import json
import os
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

root = Path("/dev/shm/ovenmediaengine")
assert (root / "enabled").is_file()
assert not (root / "turn-enabled").exists()
for path in [root, *root.rglob("*")]:
    assert path.stat().st_uid == 1000, "Media bootstrap ownership is incorrect"
    expected = 0o700 if path.is_dir() else 0o600
    assert path.stat().st_mode & 0o777 == expected, "Media bootstrap mode is incorrect"
tree = ET.parse(root / "Server.xml")
token = tree.findtext(".//Managers/API/AccessToken")
assert token
headers = {"Authorization": "Basic " + base64.b64encode(token.encode()).decode()}
applications = tree.findall(".//Applications/Application/Name")
assert applications, "No static viewing applications were configured"
for application in applications:
    url = "http://127.0.0.1:8081/v1/vhosts/default/apps/" + application.text + "/streams"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=5) as response:
        assert json.load(response)["response"] == [], "Startup opened a media source"
processes = []
for path in Path("/proc").iterdir():
    if path.name.isdigit():
        try:
            command = (path / "cmdline").read_bytes()
        except FileNotFoundError:
            continue
        if command.startswith(b"/opt/ovenmediaengine/bin/OvenMediaEngine\x00"):
            processes.append(path)
assert len(processes) == 1, "The single-container media engine is not running"
process = processes[0]
uids = next(line for line in (process / "status").read_text().splitlines() if line.startswith("Uid:"))
assert {int(value) for value in uids.split()[1:]} == {1000}, "OME is not unprivileged"
def ticks():
    fields = (process / "stat").read_text().split(") ", 1)[1].split()
    return int(fields[11]) + int(fields[12])
before, started = ticks(), time.monotonic()
time.sleep(3)
percent = 100 * (ticks() - before) / os.sysconf("SC_CLK_TCK") / (time.monotonic() - started)
assert percent < 20, "Idle media engine kept processing"
tcp = Path("/proc/net/tcp").read_text().splitlines()[1:]
for port in (3333, 8081):
    addresses = [
        row.split()[1].split(":")[0] for row in tcp
        if int(row.split()[1].split(":")[1], 16) == port and row.split()[3] == "0A"
    ]
    assert addresses == ["0100007F"], "Media control listener is not private"
print("Single-container unprivileged media startup, zero inputs and idle checks passed")
PY

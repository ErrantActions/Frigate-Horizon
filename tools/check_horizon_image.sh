#!/bin/bash
set -euo pipefail
image="${1:?Pass the locally built image tag}"
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
image_id="$(docker image inspect "$image" --format '{{.Id}}')"
stage="$(mktemp -d)"
name="horizon-ci-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}"
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
mqtt:
  enabled: false
auth:
  enabled: true
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

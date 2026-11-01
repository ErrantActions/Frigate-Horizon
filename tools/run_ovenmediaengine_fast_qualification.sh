#!/bin/bash
set -euo pipefail
umask 077

image="${1:?Pass the verified local OME runtime image}"
expected="${2:?Pass its expected image ID}"
tools="$(realpath "${3:?Pass the uploaded synthetic test tools directory}")"
work="${4:?Pass a new private directory on the fast test pool}"
rootfs_source="${5:-}"
case "$work" in /mnt/cache/frigate-ome-qualification-*) ;; *) exit 2 ;; esac
test ! -e "$work"
test "$(docker image inspect --format '{{.Id}}' "$image")" = "$expected"
mkdir -m 700 "$work"
bundle="$work/bundle"
state="$work/state"
mkdir -m 700 "$bundle" "$state" "$bundle/rootfs"
holder="frigate-ome-export-$$"
active=""

cleanup() {
    if [[ -n "$active" ]]; then
        runc --root "$state" kill "$active" KILL >/dev/null 2>&1 || true
        runc --root "$state" delete --force "$active" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

if [[ -n "$rootfs_source" ]]; then
    rootfs_source="$(realpath "$rootfs_source")"
    case "$rootfs_source" in
        /mnt/cache/frigate-ome-qualification-*/bundle/rootfs) ;;
        *) exit 2 ;;
    esac
    cp -a --reflink=always "$rootfs_source/." "$bundle/rootfs/"
else
    docker create --name "$holder" --network none --read-only \
        --entrypoint /bin/true "$image" > "$work/export-container.id"
    docker export "$holder" --output "$work/rootfs.tar"
    docker rm "$holder" > /dev/null
    tar --numeric-owner -xf "$work/rootfs.tar" -C "$bundle/rootfs"
    rm -f "$work/rootfs.tar"
fi
chmod 755 "$bundle/rootfs"
printf '%s\n' "$expected" > "$work/source-image.id"
(cd "$bundle/rootfs/opt/ovenmediaengine" && sha256sum -c share/manifest.sha256 >/dev/null)
runc spec --bundle "$bundle"
cp "$bundle/config.json" "$work/base-spec.json"

jq --arg root "$bundle/rootfs" --arg run "$$" '
  .root.path = $root |
  .root.readonly = false |
  .process.terminal = false |
  .process.cwd = "/" |
  .process.args = ["/bin/sh", "-ec",
    "export DEBIAN_FRONTEND=noninteractive; apt-get update -qq; apt-get install -y --no-install-recommends python3 ffmpeg iproute2 util-linux chromium python3-websocket openssl libnss3-tools; rm -rf /var/lib/apt/lists"] |
  .process.env += ["DEBIAN_FRONTEND=noninteractive", "LC_ALL=C.UTF-8"] |
  .process.noNewPrivileges = true |
  .process.rlimits = [{"type":"RLIMIT_CORE","hard":0,"soft":0}] |
  .process.capabilities = {
    bounding: ["CAP_CHOWN","CAP_DAC_OVERRIDE","CAP_FOWNER","CAP_FSETID",
      "CAP_SETUID","CAP_SETGID","CAP_SETPCAP","CAP_SYS_CHROOT","CAP_MKNOD",
      "CAP_AUDIT_WRITE","CAP_SETFCAP","CAP_KILL","CAP_NET_BIND_SERVICE"],
    effective: ["CAP_CHOWN","CAP_DAC_OVERRIDE","CAP_FOWNER","CAP_FSETID",
      "CAP_SETUID","CAP_SETGID","CAP_SETPCAP","CAP_SYS_CHROOT","CAP_MKNOD",
      "CAP_AUDIT_WRITE","CAP_SETFCAP","CAP_KILL","CAP_NET_BIND_SERVICE"],
    permitted: ["CAP_CHOWN","CAP_DAC_OVERRIDE","CAP_FOWNER","CAP_FSETID",
      "CAP_SETUID","CAP_SETGID","CAP_SETPCAP","CAP_SYS_CHROOT","CAP_MKNOD",
      "CAP_AUDIT_WRITE","CAP_SETFCAP","CAP_KILL","CAP_NET_BIND_SERVICE"]
  } |
  .linux.namespaces |= map(select(.type != "network")) |
  .linux.cgroupsPath = ("frigate-ome-fast-" + $run + "-install") |
  .linux.resources.memory.limit = 2147483648 |
  .linux.resources.pids.limit = 256 |
  .mounts += [{
    destination:"/etc/resolv.conf",type:"bind",source:"/etc/resolv.conf",
    options:["rbind","ro"]
  }]
' "$work/base-spec.json" > "$bundle/config.json"

active="frigate-ome-fast-install-$$"
result=0
timeout --kill-after=30s 900s runc --root "$state" run --no-pivot --keep --bundle "$bundle" "$active" \
    > "$work/install.log" 2>&1 || result=$?
printf '%s\n' "$result" > "$work/install.exit"
runc --root "$state" delete --force "$active" > /dev/null
active=""
test "$result" = 0

overall=0
for driver in test_ovenmediaengine_runtime.py test_ovenmediaengine_recorded_source.py test_ovenmediaengine_browser.py test_ovenmediaengine_recorded_hold.py test_ovenmediaengine_recorded_audio.py; do
    stem="${driver%.py}"
    memory=805306368
    pids=128
    shm=134217728
    deadline=150
    if [[ "$driver" == test_ovenmediaengine_browser.py || "$driver" == test_ovenmediaengine_recorded_hold.py || "$driver" == test_ovenmediaengine_recorded_audio.py ]]; then
        memory=2147483648
        pids=384
        shm=536870912
        deadline=300
    fi
    jq --arg root "$bundle/rootfs" --arg tools "$tools" --arg driver "$driver" --arg run "$$" \
        --argjson memory "$memory" --argjson pids "$pids" --argjson shm "$shm" '
      .root.path = $root |
      .root.readonly = true |
      .process.terminal = false |
      .process.cwd = "/dev/shm" |
      .process.noNewPrivileges = true |
      .process.rlimits = [{"type":"RLIMIT_CORE","hard":0,"soft":0}] |
      .process.env += ["OMP_NUM_THREADS=2","OPENBLAS_NUM_THREADS=2","LC_ALL=C.UTF-8"] |
      .process.args = ["/bin/sh","-ec",
        ("ip link set lo up; exec setpriv --reuid=1000 --regid=1000 --clear-groups --bounding-set=-all --inh-caps=-all --ambient-caps=-all python3 -B /test-tools/" + $driver)] |
      .process.capabilities = {
        bounding:["CAP_NET_ADMIN","CAP_SETUID","CAP_SETGID","CAP_SETPCAP"],
        effective:["CAP_NET_ADMIN","CAP_SETUID","CAP_SETGID","CAP_SETPCAP"],
        permitted:["CAP_NET_ADMIN","CAP_SETUID","CAP_SETGID","CAP_SETPCAP"]
      } |
      .linux.cgroupsPath = ("frigate-ome-fast-" + $run + "-" + $driver) |
      .linux.resources.memory.limit = $memory |
      .linux.resources.pids.limit = $pids |
      .linux.resources.cpu = {quota:200000,period:100000} |
      .mounts |= map(if .destination == "/dev/shm"
        then .options = ["nosuid","nodev","noexec","mode=1777",("size=" + ($shm|tostring))]
        else . end) |
      .mounts += [{
        destination:"/test-tools",type:"bind",source:$tools,
        options:["rbind","ro"]
      }]
    ' "$work/base-spec.json" > "$bundle/config.json"
    active="frigate-ome-fast-${stem}-$$"
    result=0
    timeout --kill-after=10s "${deadline}s" runc --root "$state" run --no-pivot --keep --bundle "$bundle" "$active" \
        > "$work/$stem.log" 2>&1 || result=$?
    printf '%s\n' "$result" > "$work/$stem.exit"
    runc --root "$state" delete --force "$active" > /dev/null
    active=""
    printf '%s: %s\n' "$driver" "$result"
    if [[ "$result" != 0 ]]; then
        overall=1
    fi
done
printf '%s\n' "$overall" > "$work/qualification.exit"
exit "$overall"

# Viewer-only OME CPU runtime and authenticated Coturn

This packages official stable OvenMediaEngine v0.21.0, commit
`a35b09d11a82035f63f1d22e3fec9bbadb8e0aec`, into the main Frigate image.
It does not replace the recorder, go2rtc, MediaMTX, Frigate FFmpeg, nginx,
recording storage, or mirroring. No new ports are exposed by this Dockerfile.
Both optional services are disabled unless bootstrap writes their success markers.
An admitted source may need several seconds to produce its first encoded frame.
Offer negotiation retries OME's explicit not-ready response within one 12-second
deadline, closing rejected sockets. Other signaling errors fail explicitly.
The existing qualified private HLS MediaMTX implementation is separately moved
into the main image, unchanged, for old clients during migration. See
`../live-hls/README.md` for its immutable artifact, service, and amd64 restriction.

## Provenance and redistribution

- [Official release](https://github.com/OvenMediaLabs/OvenMediaEngine/releases/tag/v0.21.0)
  is stable, not a prerelease. The release tag resolves to the commit above.
- `sources.lock.json` contains the exact SHA-256 of the commit archive and every
  enabled source dependency downloaded by that release's prerequisite installer.
  Hashes were independently calculated from those official upstream downloads.
  Downloads, including cached archives, are verified before extraction.
- The source and runtime stages both use Debian 12. No Ubuntu image binaries,
  host libraries, global loader changes, GPU SDKs, or decoder upgrades are used.
  Dependency libraries remain private to `/opt/ovenmediaengine/lib`.
- The upstream OpenSSL 3.0.7 dependency is updated to the compatible 3.0.22
  release, with its own verified source digest. Both installer and dependency
  checks use the patched version declaration. No recorder libraries are replaced.
- Upstream OME is AGPLv3. Its LICENSE and dependency notices are retained under
  `/opt/ovenmediaengine/share/licenses`; the exact source URLs, checksums, upstream
  dependency versions, local build modifications and options are in `share/build`.
  `share/version.json` records the release, commit, architecture, build distribution,
  binary digest, options, and FFmpeg license. `share/manifest.sha256` verifies the
  installed payload. Distributors must satisfy the corresponding source and
  license obligations; these notices do not replace those obligations.
- Upstream's default x264 + FDK-AAC configuration enables FFmpeg's
  **nonredistributable** mode. This build disables x264 and uses **OpenH264 for CPU
  H.264**, retaining upstream Opus support. The build rejects a nonfree FFmpeg
  license at packaging time. No claim is made about patent licensing.
- Coturn comes from Debian Bookworm's signed apt repositories, including security
  updates, rather than a copied third-party executable. Its installed package
  versions and copyright are recorded in `/usr/share/frigate-ovenmediaengine`.
- Open-source OME after v0.20.5 does **not** support GPU acceleration:
  [official announcement](https://ovenmedia.com/blog/discontinuing-hardware-acceleration-ome).
  This package intentionally enables no hardware backend.

## Private listener configuration

**Stock v0.21.0 cannot separately bind HTTP and ICE using configuration alone.**
`api_server.cpp` passes `Server/GetIPList()` to its HTTP listeners. Signaling does
the same, and `IcePort::CreateIceCandidates()` originally passed that global list
to `SocketAddress::Create()`. An explicit `IceCandidate` address controls
advertisement, not the socket's bind address. `Port` is not an IP:port override.

The small, fail-closed source modification in `build.py:patch_ice_bind` adds the
optional, repeatable `BindIP` field to `IceCandidates`. It only overrides ICE
socket binding; omission preserves upstream behavior. It does not enable OME TURN.
Parent-generated configuration must use:

```xml
<Server version="8">
  <IP>127.0.0.1</IP>
  <!-- Other required server sections omitted here. -->
  <Bind>
    <Managers><API><Port>8081</Port></API></Managers>
    <Publishers><WebRTC>
      <Signalling><Port>3333</Port></Signalling>
      <IceCandidates>
        <BindIP>0.0.0.0</BindIP>
        <IceCandidate>PUBLIC_OR_REACHABLE_LAN_IP:8189/udp</IceCandidate>
        <DefaultTransport>udp</DefaultTransport>
        <IceWorkerCount>1</IceWorkerCount>
      </IceCandidates>
    </WebRTC></Publishers>
  </Bind>
</Server>
```

Advertise only reachable addresses, not `*`. Binding `0.0.0.0` accommodates NAT;
the advertised public address need not exist on a local interface. Add an explicit
IPv6 `BindIP` only if IPv6 is deployed and qualified. **Do not add `TcpRelay`:**
upstream's embedded TURN allocation path has unimplemented authentication checks.
Do not enable push providers or public API/signaling. The isolated test checks
actual `/proc/net` listener addresses, not just generated XML.

## Supervisor/bootstrap contract

`ovenmediaengine-config` is a oneshot ordered after existing `prepare`. It creates
`/dev/shm/ovenmediaengine` mode 0700 owned by **1000:1000**, clears stale markers,
then runs:

```text
python3 -m frigate.util.ovenmediaengine_bootstrap
```

The module must never log source credentials. It writes:

- `Server.xml` and optionally `Logger.xml`, private files owned by 1000:1000.
- `enabled` only after a fully valid, enabled OME configuration is persisted.
- `turnserver.conf` and `turn-enabled` only after valid authenticated Coturn setup.
  `turn-enabled` alone does not start TURN; `enabled` is also required.

Bootstrap failure removes both markers and leaves recording services independent.
OME and Coturn longruns depend on that oneshot. They call `s6-svc -O .`, so disabled
services exit normally and failures stay stopped without restart loops or sleeps.
An explicit service/container restart is required to retry a crash or configuration
change. Neither daemon is launched as root, even with Frigate's root escape hatch.
They run as numeric **1000:1000**, not a potentially remapped Frigate username.
Both have core dumps disabled and a per-process 4096-descriptor limit.

Actual foreground commands, executed from the private state directory:

```text
/opt/ovenmediaengine/bin/OvenMediaEngine -c /dev/shm/ovenmediaengine
/usr/bin/turnserver -c /dev/shm/ovenmediaengine/turnserver.conf --no-cli --no-rfc5780 --no-multicast-peers --log-file=stdout --simple-log --pidfile=/dev/shm/ovenmediaengine/turnserver.pid --userdb=/dev/shm/ovenmediaengine/turnserver.db
```

OME supports **`-h` and `-v`**, not documented GNU-style long options.
Both information flags intentionally exit with status 1 in pinned v0.21.0.
The packaged `check-cli.sh` validates that status and the expected help/version
output; other statuses, loader failures and incorrect versions remain failures.

Coturn configuration must use `use-auth-secret` with a nonempty
`static-auth-secret` and `realm` (preferred temporary TURN REST credentials), or
`lt-cred-mech` with explicit nonempty users/passwords and `realm`. The launcher
rejects anonymous/missing authentication. Parent must enforce expiration,
per-user/global quotas, bandwidth, relay worker and relay-port limits, and peer
restrictions, permitting only the intended OME endpoint where practical. Coturn
needs a bounded **UDP relay-port range** in addition to listening UDP/TCP 3478.
Routing/firewall decisions belong to deployment, not this package.

Plain TURN/TCP 3478 does not need a certificate. TLS TURN on TCP 5349 requires a
valid certificate/private key readable by UID 1000 and appropriate Coturn TLS
settings. Port 5349 does not itself imply encryption. TURN-over-TLS on 443 requires
separate listener/routing or TCP/TLS multiplexing decisions; an HTTP reverse proxy
alone is insufficient. No capabilities or privileged low-port binds are added.

## Viewer-only lifetime and CPU limits

The packaged image contains no camera URLs, startup input streams, persistent
pulls, encoder warmup or recording destinations. OME may idle; encoding must not.
Application output profiles alone do not start an encoder. The parent creates
trusted loopback RTSP pulls only for authorized active viewers and explicitly
deletes each input when its last viewer lease ends. Production source recording
must remain independent and never pass through OME.

Use REST pull properties `persistent:false`, `retryCount:0` and a finite
`unusedStreamDeletionTimeoutMs` as defense in depth. Upstream's origin property
default deletion timeout is 60000 ms, not an immediate last-viewer guarantee.
Do not use static `Origins`, `OriginMapStore`, persistent streams or always-on
producers to hide cold startup. Parent admission control must cap concurrently
active cameras, profiles, resolution, frame rate, bitrate and encoder threads.
`OMP_NUM_THREADS=2` is not a substitute for those limits. Measure the actual
production workload before permitting CPU 4K.

## Validation and exact integration commands

### Recorded viewing

The same opt-in `live_webrtc` service supports recorded viewing through
`/{camera}/recordings/webrtc`. It shares admission with live viewing, stages only
bounded protected catalog windows locally, and leaves original recordings,
retention, mirroring and exports unchanged. Historical playback does not require
the camera to be enabled or its live source to be online.

Windows are at most 30 seconds. Create, seek and resume return a `warming` peer
with a nonrenewable 20-second deadline. After decoding video from that generation,
the client posts `{"generation":N,"action":"activate","decoded_ready":true}` to
the returned control URL. Activation confirms the finite native program on the
same peer/generation. Pause releases encoding; seek and resume negotiate new
generations. Keep warmup audio muted. Quality pins and Auto use the existing peer.

The returned position is explicitly an input-read cursor, not a decoded-frame
timestamp. Do not infer exact display position from browser elapsed time or RTP
observations from a single fixture. Long playback explicitly advances finite
windows; seamless continuation and exact A/V synchronization require separate
qualification. Playback-rate controls and HLS fallback are not capabilities.
New web viewing paths never substitute the legacy HLS renderer on failure.
Still thumbnails and pre-generated small preview files remain separate.

Focused backend validation:

```sh
python3 -u -m unittest frigate.test.test_recorded_webrtc frigate.test.test_recorded_webrtc_api frigate.test.test_live_webrtc frigate.test.test_live_webrtc_api
```

Build contexts made from Windows checkouts must preserve Git executable modes
and normalize Git text files to LF, including extensionless s6 `type` files,
not only executable shell scripts.

From repository root, offline packaging/protocol tests (Python 3.11 compatible):

```sh
python3 -B -m unittest discover -s docker/main/ovenmediaengine -p 'test_*.py' -v
OME_VERIFY_UPSTREAM=1 python3 -B -m unittest discover -s docker/main/ovenmediaengine -p 'test_*.py' -v
for script in docker/main/ovenmediaengine/install-runtime.sh docker/main/rootfs/etc/s6-overlay/s6-rc.d/ovenmediaengine-config/run docker/main/rootfs/etc/s6-overlay/s6-rc.d/ovenmediaengine/run docker/main/rootfs/etc/s6-overlay/s6-rc.d/coturn/run; do bash -n "$script" || exit; done
```

Reuse the existing BuildKit builder and cache; do not prune it or rebuild unrelated
Frigate stages for the first proof. On isolated Linux amd64:

```sh
docker buildx build --platform linux/amd64 --target ovenmediaengine-qualification --build-arg OME_BUILD_JOBS=2 -f docker/main/Dockerfile -t frigate-ome-qualification:local --load .
timeout 120s docker run --rm --name frigate-ome-qualification --network none --cpus=2 --memory=768m --pids-limit=128 --read-only --cap-drop=ALL --security-opt=no-new-privileges --tmpfs /dev/shm:rw,nosuid,nodev,size=128m --user 1000:1000 --workdir /dev/shm frigate-ome-qualification:local
```

The test generates a local H.264 fixture, implements a loopback RTSP server, checks
zero startup streams, creates one nonpersistent pull via authenticated REST,
requires a **ready CPU-encoded 160x96/5fps output**, measures OME CPU/RSS/threads,
deletes the input and verifies both stream and RTSP connection disappear. It
checks API/signaling loopback and separate UDP ICE binds. It also checks actual
Coturn TCP allocation: missing credentials rejected, wrong password rejected, and
temporary HMAC-based credentials accepted. It uses no production sources,
networks, recorder or port mappings. It **does not claim browser WebRTC playback
or production CPU capacity**; the parent must qualify those separately.
Idle and post-delete OME CPU must each remain below a conservative 20 percent
of one CPU; the container also has hard CPU, memory, process and shared-memory
limits. The active phase reports measured load, not a production capacity claim.

After building the parent's full image, wire this additional payload smoke test
into the existing image checker (replace `IMAGE` with the built image):

```sh
docker run --rm --network none --user 1000:1000 --entrypoint /bin/sh IMAGE -ec 'cd /opt/ovenmediaengine; sha256sum -c share/manifest.sha256 >/dev/null; /bin/sh check-cli.sh; /usr/bin/turnserver --version; test -s /usr/share/frigate-ovenmediaengine/runtime-packages.tsv'
```

The runtime target checks manifest integrity, dynamic linking and binary `-h`/`-v`
as UID/GID 1000 during build. Native arm64 builds are not intentionally disabled,
and GGML host-native tuning is disabled, but amd64 is the production acceptance
target; other architectures require the same runtime test.

If the runtime image is already local, qualify it without resolving the main
Dockerfile's unrelated base stages (replace `IMAGE` with that local image):

```sh
docker buildx build --pull=false --platform linux/amd64 --build-arg RUNTIME_IMAGE=IMAGE -f tools/ovenmediaengine-qualification.Dockerfile -t frigate-ome-qualification:local --load .
```

This adds only the synthetic test's Python/FFmpeg prerequisites. It does not
recompile OME or publish a production image.

On an owner-approved disposable fast pool, the same verified local runtime can
also be qualified with `tools/run_ovenmediaengine_fast_qualification.sh`. Pass
the image tag, its expected image ID, the uploaded tools directory and a new
`/mnt/cache/frigate-ome-qualification-*` workspace. An optional fifth argument
reuses an earlier owned `bundle/rootfs` with Btrfs reflinks. The script does not
move Docker's global data or mount recordings. Its package-install phase uses
outbound networking; each test then runs in its own loopback-only network
namespace as UID/GID 1000 with capabilities cleared and bounded resources.

This extended harness adds a native scheduled-file probe and an actual headless
Chromium receiver. The browser test requires decoded H264 dimensions, selected
relay candidates for TURN UDP, TCP and TLS, and advancing frames across credential
expiry and allocation renewal. TLS uses a generated test CA trusted in the isolated
browser's NSS database, never a certificate-validation bypass. A separate decoded-
ready scheduled-file hold probe checks whether programs can be activated on an
already connected peer; it is not an implemented production control contract.
Browser dependencies are test-only, not production
payload. Chromium's `--no-sandbox` is restricted to this disposable, capability-free
OCI namespace; no camera data or arbitrary pages are loaded. Passing the isolated
test is not production TLS, WAN/cellular, iPhone, or full-image startup evidence.

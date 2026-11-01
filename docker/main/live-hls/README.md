# Private HLS migration into the main container

The main image copies **only** the binary, existing build receipt and license
notices from the previously qualified artifact:

```text
ghcr.io/errantactions/frigate-horizon-live-hls@sha256:e2ea553de2de7e4d4f56df53d9e2a41c7f17048044dac9e1ba403586d06e36fd
```

Its MediaMTX source is `9540cb6c1d9f1e9351fcc4ded3afc249ffff22bc` (v1.21.3),
with the existing qualified shared-clock extension, unchanged. No MediaMTX
builder, extension, Python HLS manager, player, nginx configuration, source
selection, encoder behavior or recording path is modified here.

**Architecture restriction:** the qualified artifact is linux/amd64. The main
image build explicitly rejects other target architectures instead of silently
packaging an incompatible executable. The independent OME targets do not acquire
this restriction. Supporting another main-image architecture requires qualifying
the corresponding HLS source build, not relabeling this artifact.

`live-hls-runtime` independently verifies the original receipt, binary SHA-256,
ELF amd64 architecture, absence of an ELF dynamic loader, retained licenses, and
`--version` execution as 1000:1000 on Debian 12. It copies no companion libraries,
loader, CA bundle, configuration, network settings or entrypoint. Provenance is
retained at `/opt/live-hls/share/qualified-image.txt` and `shared-clock.json`;
licenses are under `/opt/live-hls/share/licenses`.

The `live-hls` s6 service starts after Frigate's process is started. It waits up
to **120 seconds** for the existing manager's
`/config/.live-hls/mediamtx.yml`, then starts:

```text
HORIZON_HLS_SHARED_CLOCK=1 /opt/live-hls/bin/mediamtx /config/.live-hls/mediamtx.yml
```

Both config validation and the daemon run as numeric **1000:1000**. Validation
accepts the manager's existing JSON-encoded YAML without rewriting it, requires
`hlsAddress=127.0.0.1:8888`, `hlsAlwaysRemux=false`, disabled additional listeners,
and only on-demand sources through existing loopback RTSP 8554. This preserves
camera and Birdseye HLS while WebRTC is opt-in. The service has no dependency on
OME being enabled and sets no global library-path or recording-related variables.

Missing/invalid config or daemon failure leaves the optional service stopped
without a restart loop. If Frigate produces its first config after the bounded
wait, explicitly restart the HLS service (or container). It does not create
camera producers, prewarm encoders or change the manager's on-demand timers.

**Deployment must stop/remove the old HLS companion during cutover.** It uses the
same loopback port, so running both copies in one network namespace conflicts.
No port exposures, networks, firewall rules or companion lifecycle are changed
by this packaging work.

## Exact validation commands

From the repository root:

```sh
python3 -B -m unittest discover -s docker/main/live-hls -p 'test_*.py' -v
bash -n docker/main/rootfs/etc/s6-overlay/s6-rc.d/live-hls/run
docker buildx build --platform linux/amd64 --target live-hls-runtime -f docker/main/Dockerfile -t frigate-private-hls-runtime:local --load .
docker run --rm --network none --read-only --cap-drop=ALL --security-opt=no-new-privileges --cpus=1 --memory=128m --pids-limit=32 --user 1000:1000 --entrypoint /bin/sh frigate-private-hls-runtime:local -ec 'python3 /opt/live-hls/verify.py --payload; /opt/live-hls/bin/mediamtx --version'
```

After building the full parent image, wire the same payload/version command into
its image checker using that image name. Full camera/Birdseye HLS and WebRTC
integration tests remain with the parent. This task does not claim a new HLS
synthetic qualification beyond the exact already-qualified immutable artifact.

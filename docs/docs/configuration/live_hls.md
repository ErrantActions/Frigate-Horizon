---
id: live_hls
title: Bandwidth-adaptive live HLS
---

Live HLS provides a shared bitrate ladder over Frigate's existing authenticated
HTTPS connection. Unlike a real-time WebSocket feed, HLS downloads discrete
media resources, allowing the player to estimate throughput and use its buffer
to select a sustainable bitrate.

This is the only live video transport and is enabled by default. It requires the private
shared-clock MediaMTX companion in `docker/live-hls/compose.yaml`. Do not expose the
companion's HTTP server or add a router port forward.

## Renditions

The initial implementation uses advertised main-derived profiles at 720p or
higher. Cameras need at least one profile with a configured bitrate. Existing
manually configured go2rtc profiles are supported with
`live.transcode.enabled: false`; that flag controls server-side generation, not
the availability of a stream.

The packager remuxes existing streams. It does not change camera settings,
recording inputs, recording quality, retention, object detectors, or motion
filters. Producers are shared between viewers rather than created per viewer.
An unused rendition's muxer and RTSP source close after their idle timeouts.
While a multi-rung Auto video playlist is being requested, bounded background
reads keep its advertised rungs warm. Otherwise, a newly started higher rung
has no buffered history to overlap the current rung. Single-rung grid and
pinned playback do not request additional producers. Warming stops when
requests stop; there are no permanent polling timers or always-on encoders.

## Private companion

Merge the companion service into the existing Frigate Compose project. Keep the
existing Frigate networks, GPU mappings, resource settings, and storage mounts.
Create the `config/.live-hls` directory before deploying the companion, owned by
the same UID/GID that runs Frigate. The directory is private configuration, not
a recording destination.

Use the immutable image reference from the **Horizon shared-clock live HLS**
workflow's `horizon-live-hls-image-receipt` artifact, setting
`HORIZON_LIVE_HLS_IMAGE` for Compose. The image is built from pinned MediaMTX
v1.21.3 source, with an opt-in HLS-reader timestamp mapper. Its generated
receipt, binary checksum, upstream tests, synthetic cold/restarted publisher
clock checks, and license notices are part of the build.

The companion enables `HORIZON_HLS_SHARED_CLOCK=1`. All HLS reader presentation
timestamps share the Unix epoch, including after a rendition or packager
restart. Each rendition retains one leading-track offset shared by video and
audio, preserving their original timing without following packet-arrival
jitter. The muxer's normal decode-time headroom is common to all rungs.
Program dates follow that same presentation timeline instead of subsequent
packet-arrival timestamps, keeping the playlist alignment clock consistent with
the fMP4 clock during source jitter or pacing drift.
The original shared media units, encoded payloads, and recorder
inputs are not changed. Stock independent per-rendition clocks are not an
equivalent substitute for this companion.

The companion shares Frigate's network namespace and binds only
`127.0.0.1:8888`. RTSP serving, RTMP, WebRTC, SRT, MoQ, control API, metrics, and
profiling listeners are disabled. It pulls only registered go2rtc rendition
names from loopback RTSP and stores HLS segments in memory.
When replacing Frigate's container, recreate the companion with it. Its shared
network namespace belongs to that specific container instance; restarting only
the companion is not a substitute for recreating the pair.

Enable the feature only after the companion is installed:

```yaml
ui:
  live_hls: true
```

Keep HTTPS and normal certificate validation at the existing reverse proxy.
The private loopback hop does not need a publicly trusted certificate. Never
disable client TLS verification or expose the private packager to make HLS work.

## Player behavior

In Auto quality mode, all supported players use the HLS ladder. The web
player starts at the lowest advertised bitrate and delegates subsequent bitrate
selection to hls.js's throughput and buffer policy. Display size and Data Saver
cap the maximum level. Explicit quality selections pin an advertised HLS rung.
Auto can keep multiple renditions active for the viewed camera to preserve
seamless switching; this can use more encoder capacity than a pinned rung.

If HLS fails, the web player reports the error and offers an HLS retry, without
opening another transport. Offline messages, playback errors, resolution
badges, and statistics remain available; there is no separate Not live badge.

The player stops requests when its tile is inactive, allowing the shared muxer
and producer to become idle. Background and Picture in Picture ownership must
be preserved by clients.

## Authenticated client contract

All paths below are relative to the configured Frigate API base:

- `GET /{camera_name}/live/hls/status`: enabled state, availability, and configured
  rendition heights and video target bitrates.
- `GET /{camera_name}/live/hls/master.m3u8`: shared bitrate ladder, including the
  actual private packager's codec, resolution, and audio-group metadata.
- `GET /{camera_name}/live/hls/{height}p/{resource}`: authorized playlists, init
  fragments, segments, and low-latency parts.

The status response's `renditions[].bitrate` is the configured video target in
kilobits per second. Master-playlist `BANDWIDTH` and `AVERAGE-BANDWIDTH` are in
bits per second. `BANDWIDTH` includes conservative video burst and audio
headroom; these advertised values are not measurements of recent recording
usage. Read actual `RESOLUTION` and `CODECS` from the master rather than
constructing them from the requested height.

The master accepts `audio=false` to omit the audio group and audio codecs,
`height` to pin an advertised rung, and `max_height` to cap advertised heights.
It omits optional `FRAME-RATE` metadata: independent muxers' startup FPS samples
can differ slightly, causing players to reject a resolution upgrade as a
frame-rate downgrade. Encoded frame cadence is unchanged.
Height bounds are 144 through 2160; unavailable selections return 404 rather
than inventing a source. These options also apply to native HLS clients.
Birdseye uses the same routes with `birdseye` as the camera name and retains
the existing full-camera-scope authorization requirement.

Every request rechecks authentication and camera access. Responses are private
and not cacheable by shared proxies. Private packager session values are kept
server-side and removed from public playlists. Blocking reload accepts only
bounded `_HLS_msn`, `_HLS_part`, and `_HLS_skip` parameters.
Adaptive video-playlist URIs also include `warm_max_height`. It is bounded to
144 through 2160 and handled locally, never forwarded to the private packager.
It keeps only currently advertised rungs up to that height warm while video
playlists are requested. Clients should retain that query when reloading the
playlist. Slow or failed auxiliary warming is logged and cannot block the
active rendition's response.
Resolve the status response's playlist relative to the configured API base,
then resolve each child URI relative to its containing playlist. Child names
can change when an idle muxer is recreated; do not construct or persist them.
Playlists use `application/vnd.apple.mpegurl`; MP4 objects, including audio,
use `video/mp4`.

The application returns complete MP4 objects with HTTP 200 and does not
implement byte-range/206 responses or advertise byte-range playlists.
Clients must tolerate a full 200 response when requesting a Range. The API
declares GET routes; HEAD behavior can depend on the deployment's proxy.
The owner's deployed HTTPS path was verified to return HEAD 200 with MP4
content metadata. Do not assume that proxy behavior on other deployments.
Each request independently enforces authentication (401) and camera access
(403). Error bodies can come from the reverse proxy and are not necessarily
JSON.

The pinned packager is bootstrapped with `cookieCheck=1` for its HTTP
query-session mode. The proxy does not follow redirects or forward browser
cookies upstream. Requalify this private protocol before changing packager versions.
Producer shutdown also waits for the packager's upstream session inactivity
timeout, not just its muxer timeout. Allow up to 60 seconds after the last viewer
request when checking that this companion has released its RTSP readers.
When an idle muxer closes, the proxy renews a missing known media playlist once.
The first request after renewal omits old blocking-reload positions because the
new muxer starts a new media sequence. Missing fragments and unknown playlist
names remain errors instead of being replaced with unrelated media.

Native clients must send their existing authenticated session cookies and keep
same-origin and certificate checks. A native AVPlayer integration should set
its initial peak bitrate to the lowest rung, then release that startup cap
according to playback readiness and the user's maximum-quality preference.
Updating the web player does not update a native app binary.

The HLS-only release removes native talkback endpoints and advertises
`talkback.supported: false`; clients must hide those controls. PTZ remains
available under its existing authorization rules. Camera-SD replay now uses
`/onvif/cameras/{camera}/replay/sessions/{id}/hls/index.m3u8` and its manifest
children instead of the retired `stream.mp4` endpoint. Close replay sessions
with their existing DELETE operation when leaving playback.

## Qualification

Before updating an existing installation, validate a private copy of its
configuration, database, and saved plugin settings against the candidate image.
Version-1 Python plugins can load inactive default host metadata without changing
bindings or enabling subscriptions. Active newer plugin features must be
qualified separately, not silently discarded during an HLS upgrade.

Validate first decoded-frame time, variant synchronization, low/high bandwidth
transitions, buffer behavior, audio continuity, and idle producer shutdown.
Measure on the intended device and network. A working signaling request, a
downloaded playlist, or mobile-browser tests alone do not prove native iPhone
playback. Do not equate browser emulation with an actual native iPhone test.

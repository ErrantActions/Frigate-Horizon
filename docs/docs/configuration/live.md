---
id: live
title: Live View
---

Frigate Horizon uses authenticated HTTPS HLS for live and recorded viewing.
There is no WebRTC, binary WebSocket/MSE player, or JSMpeg fallback. HLS.js may
use the browser's MediaSource API internally; that is still the same HLS
transport, not the retired go2rtc MSE protocol.

## Live view technologies

The private [live HLS companion](/configuration/live_hls) packages on-demand
H264 renditions from go2rtc. Browsers and native clients use the existing HTTPS
reverse proxy. No new public listener, port forward, STUN, or TURN service is
required.

Camera ingestion, internal RTSP restreams, snapshots, and the JSON control
WebSocket remain. They are not alternate browser viewing transports.
Two-way talk and its microphone controls have been removed.

## Setting streams for Live UI

Configure a go2rtc source and enable playback-only renditions:

```yaml
go2rtc:
  streams:
    entrance:
      - rtsp://camera.example/main#backchannel=0

cameras:
  entrance:
    ffmpeg:
      inputs:
        - path: rtsp://127.0.0.1:8554/entrance
          input_args: preset-rtsp-restream
          roles: [detect, record]
    live:
      streams:
        Main: entrance
      transcode:
        enabled: true
        source: entrance
        qualities:
          - height: 720
            bitrate: 1200
          - height: 1080
            bitrate: 2500
```

Install the companion before enabling live viewing. `ui.live_hls` defaults to
`true`; setting it to `false` stops live video rather than selecting another
transport. Existing `ui.live_compatibility: false` administrator opt-outs are
preserved and also prevent camera live HLS.

Generated defaults are 720p/1200 kbps and 1080p/2500 kbps. Generation remains
opt-in for hand-written configurations. The add-camera wizard saves these two
renditions and their main-derived source together with a new camera. It does
not overwrite another camera's manually configured profiles or opt-outs.
Wizard connection testing does not open a temporary video transport. Its HLS
preview is available after saving and loading the camera configuration.

### Manually managed profiles

Existing go2rtc profiles remain supported with `live.transcode.enabled: false`.
Advertise them in `live.streams` using names such as
`entrance_transcode_720p`, and list their actual target heights and bitrates in
`live.transcode.qualities`. They must produce H264 and compatible audio. At
least one advertised rendition at 720p or higher is required. One rung works
without adaptive switching; two or more provide bandwidth adaptation.

The original main source is an ingestion source, not a bypass around HLS.
Profiles below 720p are not offered by this live player.

## Automatic main-source live quality

Auto begins with the lowest advertised bitrate and lets the HLS player adapt
to measured download throughput and its media buffer. Tile size caps quality
on the dashboard. Data Saver pins the lowest rung. Explicit quality selections
pin an advertised HLS rendition; they never change transport.

These are viewing renditions only. The camera's original recording input,
recording-time copy commands, stored files, retention, detection, and exports
are not replaced by a live transcode.

An unavailable source or failed playlist produces an explicit error and an
HLS retry button. No different protocol is opened. Hidden or inactive tiles
release their player unless background playback or Picture in Picture owns it.
Shared on-demand producers close after their idle timers, including the
packager's upstream session inactivity timer.

## Audio support

Generated live renditions normalize audio to AAC at 48 kHz for HLS. This does
not change recording audio or camera settings. Manually configured profiles
must supply their own compatible audio.

Browsers without AAC MediaSource support request a video-only HLS master.
Enabling audio there shows an explicit unsupported-audio warning. Native HLS
clients retain their platform's audio support. Autoplay may initially mute
audio because of browser policy.

## Streaming options on camera group dashboards

Per-camera group settings retain No Streaming, Smart Streaming, Continuous
Streaming, HLS quality, Data Saver, audio, and volume. Smart Streaming uses
still images without activity and starts HLS when motion or tracked objects
are active. Recording-only cameras can remain continuous.

Preferences are stored per user, group, and device. Old JSMpeg choices migrate
to Data Saver; obsolete transport and compatibility-mode fields are discarded.
Settings imports apply the same migration. Exported video files and still
images remain downloadable file operations.

### Live resolution badges

Badges show actual selected video dimensions, including native HLS changes.
Right-click a tile (long-press on mobile) and select **Show resolution badges**
to hide or restore them. This shares **Show Live Resolution** in UI Settings.
Single-camera badges retain their close action.

There is no separate Not live or Delayed badge. Offline messages and playback
errors are independent of the resolution preference.

## Recorded playback

Review, history, event previews, motion previews, completed-export viewing, and
protected-media viewing use HLS. Existing preview files and exports are
packaged without rewriting them. Original export downloads remain available.
Recording compatibility processing, when enabled, creates temporary
playback-only HLS copies; original recordings remain unchanged.

ONVIF camera-SD replay uses bounded, authorized HLS session resources. Closing
the player releases its session; idle and maximum-lifetime limits also apply.
This does not change the camera's SD recordings.

## Camera state

- **On**: normal camera processing, detection, recording, and configured viewing.
- **Off**: processing and recording pause; the camera and its historical media
  remain visible. Use the power button, camera management, or MQTT.
- **Disabled**: `enabled: false` hides the camera from normal viewing while
  retaining files. Re-enabling a disabled camera requires a restart.

Go2rtc may still have external consumers of an ingestion source. Stopping a
Frigate player is not a request to stop those independent consumers.

### Runtime toggle persistence

Camera power, detection, recording, snapshots, and audio-detection overrides
persist in `.runtime_state.json` beside the configuration. Saving the matching
configuration field clears its runtime override. Switching profiles clears
runtime overrides. A hand-edited YAML field does not override an existing
runtime value until that override is cleared.

## Migration and integrations

The old `/live/` binary media and `/api/go2rtc/webrtc` proxy paths return
HTTP 410. Integrations that used those browser paths must move to authenticated
HLS. Internal RTSP camera ingestion and control WebSockets are retained.
Native clients must follow the [HLS resource contract](/configuration/live_hls)
and authenticate every playlist and media request.

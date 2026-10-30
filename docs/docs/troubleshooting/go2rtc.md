---
id: go2rtc
title: Troubleshooting go2rtc and HLS
---

Frigate viewing follows this pipeline: camera ingestion, go2rtc playback
rendition, private HLS companion, authenticated HTTPS proxy, browser or native
HLS player. Stored recordings and original downloads are independent of the
playback renditions.

## Isolate the failing stage

1. Read the **go2rtc** logs. Camera authentication failures, connection limits,
   missing stream names, and network timeouts must be fixed at ingestion.
2. Inspect stream metadata through the authenticated `/api/go2rtc/streams` API.
   Compare the camera source with each generated playback rendition. A working
   go2rtc diagnostic player proves ingestion, not the complete Frigate HLS path.
3. Check `GET /api/{camera}/live/hls/status` with your existing authenticated
   session. An unavailable status requires a configured HLS ladder and running
   private companion. The [camera wizard](/configuration/go2rtc) saves sources
   and the ladder together; manual configurations need both.
4. Inspect the master playlist, media playlists, initialization objects, and
   fragments. All children require current camera access. A 401 means
   authentication is required; a 403 means the current account lacks access.
   Do not expose internal unauthenticated APIs or private companion ports.
5. Verify the [HLS reverse-proxy requirements](/configuration/live_hls). Disable
   caching and response buffering for live HLS. Do not configure WebRTC, TURN,
   or port 8555 as a playback workaround.

## Black video, buffering, or unavailable playback

Playback renditions use H.264. Stored recordings may retain H.265 and original
resolution without being rewritten. A raw camera source alone is not an HLS
rendition. Check that the configured source names and generated rung names
match exactly.

Use short, regular keyframe intervals and avoid smart codecs such as H.264+
or H.265+. A long interval delays the first decodable fragment. For slow
connections, use Auto quality or Data Saver, which pins the lowest configured
HLS rung. Playback failure shows an explicit Retry action, not an alternate
transport or camera-substream fallback.

## No playback audio

Generated playback profiles normalize audio to AAC-LC at 48 kHz. Verify that
the camera source actually supplies audio. Browsers without AAC MediaSource
support request video-only HLS instead of switching transport. Native HLS
clients request audio by default. Camera-native recording audio is unchanged.
Frigate no longer provides two-way talk.

## High CPU usage

On-demand playback transcoding consumes resources while renditions have readers.
Choose suitable rung sizes and hardware acceleration. Frigate's
`ffmpeg.hwaccel_args` configures its capture processes, not go2rtc transcoding.
Do not lower recording quality to reduce playback cost.

### Hardware-accelerated transcoding with FFmpeg 8

FFmpeg 8 requires an explicit hardware device for some upload/filter chains.
If logs report `A hardware device reference is required to upload frames to`,
check go2rtc's FFmpeg device configuration and container device permissions.
For VAAPI, a typical configuration is:

```yaml
go2rtc:
  ffmpeg:
    global: "-vaapi_device /dev/dri/renderD128"
```

Select the actual render device on multi-GPU systems and pass it into the
container. Other acceleration backends require their corresponding device
options. See the [go2rtc FFmpeg documentation](https://github.com/AlexxIT/go2rtc#source-ffmpeg).

## Authentication and camera-specific sources

URL-encode reserved characters in credentials written directly into go2rtc
source URLs. Avoid repeated retries against cameras with login lockouts.
Use a shared RTSP restream to reduce camera connections where appropriate.

HTTP-FLV, MJPEG, and other nonstandard sources may need go2rtc's `ffmpeg:`
module. Transcoding modifiers such as `#video=h264`, `#audio=aac`, and
`#hardware` apply to FFmpeg sources, not arbitrary bare RTSP URLs.
If another application uses camera talkback, disable Frigate's ingestion
backchannel with `#backchannel=0`.

Consult [camera-specific configuration](/configuration/camera_specific) for
Reolink, Tapo, UniFi, and other source requirements. Diagnostic go2rtc ports,
if needed locally, must not be exposed through the public reverse proxy.

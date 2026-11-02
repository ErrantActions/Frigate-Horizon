---
id: record
title: Recording
---

import ConfigTabs from "@site/src/components/ConfigTabs";
import TabItem from "@theme/TabItem";
import NavPath from "@site/src/components/NavPath";

Recordings can be enabled and are stored at `/media/frigate/recordings`. The folder structure for the recordings is `YYYY-MM-DD/HH/<camera_name>/MM.SS.mp4` in **UTC time**. These recordings are written directly from your camera stream without re-encoding. Each camera supports a configurable retention policy. Frigate chooses the largest matching retention value between the recording retention and the tracked object retention when determining if a recording should be removed.

New recording segments are written from the camera stream to cache, they are only moved to disk if they pass a validation check and match the setup recording retention policy.

:::tip

To keep a specific clip beyond your retention window, [export](/usage/exports) it rather than increasing retention for the whole camera. Exports are saved separately and are never removed by retention.

:::

Playing original H265/HEVC recordings depends on the browser, operating system,
available codecs, hardware, and the recording's profile. A current browser
version alone does not guarantee support.

Plugin cameras such as Ring default to recording-only operation. Explicitly
enabling the camera's `detect.enabled` selects normal continuous detection
capture instead of waiting for a plugin motion-event window. Configure motion
detection separately as for other cameras. The plugin reconciles this selection
at runtime and after restart; it does not change original recording quality.
Continuous cloud-camera availability remains subject to provider session gaps.
Recording-only plugin cameras are not automatically enrolled in native
compressed-vector background learning; that separate feature must not prevent
the application from starting when the native extractor is absent.

### Private HTTPS MP4 review

Set `ui.recorded_http: true` to select finite HTTPS MP4 recording windows instead
of recorded WebRTC. Live viewing, original recordings, retention, and archives
are unchanged. Authentication and the configured live WebRTC admission runtime
are required. There is no HLS or automatic failure fallback.

Each request covers at most 30 seconds. Normal Review Auto and Original request
`delivery: auto` and advertise the browser's video/audio decoders. A compatible,
closed original MP4 is served directly with byte ranges: no video encode,
remux, copy, or packet scan precedes playback. Its index may be at either end.
An original-file lease remains active until the viewer and all readers stop.
File identity is checked before reads. Incompatible codecs or fragmented MP4
indexes produce an explicit prepared derivative with `preparation_reason`;
`delivery: direct` instead rejects incompatibility.

Scaled H264/AAC outputs remain on demand at admitted 360, 720, 1080, or 2160
heights. Explicit scaled quality and Data Saver use this prepared path. For
existing API clients, omitted `delivery` still means `prepared`, and prepared
Auto selects at most 720. Prepared Original retains the verified packet-clock
remux. Shared live admission bounds viewers and scaling without changing live
viewing. Stored originals are never modified.

The earlier implementation forced normal Auto review to complete and validate
a scaled immutable window before playback. That preparation, not HTTPS itself,
caused the startup and outside-window seek regression. Direct original delivery
avoids that work; within-file seeking remains native browser byte-range seeking.

The Review client requests 20-second ranges, but direct delivery ends at the
selected original file's boundary. Near the current window's end, it
may prepare at most one upcoming MP4, only while visible and actively playing.
Pause, seek, quality changes, hiding, or stop cancel this preparation. The
upcoming window requires additional shared viewer, encoder (when scaling), and
cache admission. It does not open hidden playback streams or permanent encoding
pipelines. Hard expiries remain unchanged and nonrenewable. Capacity failures
are explicit; current video continues to its boundary.

The camera-authorized API is:

- `GET /{camera}/recordings/http/status`: availability, qualities, and `session_url`.
- `POST /{camera}/recordings/http/resolve`: `{version: 1, after, before,
  start_timestamp}` searches forward within an explicit range of at most one
  hour. Returns a source-frame-backed `effective_start_timestamp`, or null with
  `exhausted` and `reason`. It never searches or chooses a frame past `before`.
  A `search_limit` result is not exhaustion; `searched_before` identifies the
  completed bounded search and the caller may explicitly search further.
- `POST /{camera}/recordings/http/sessions`: `{version: 1, after, before,
  start_timestamp, quality, start_policy, delivery, video_codecs, audio_codecs}`
  in catalog seconds. Decoder names are `h264`/`hevc` and `aac`/`mp3`/`opus`.
  Only advertise codecs the client actually supports. `start_policy`
  defaults to `strict`; explicitly selecting `next` permits skipping a gap only
  inside that requested MP4 window. Returns `202` with
  `state: preparing` and stable `status_url`, `close_url`, and `media_url`.
- `GET /{camera}/recordings/http/sessions/{id}`: `preparing`, `ready`, `empty`, or `failed`
  status, explicit error code, dimensions, audio, and measured `timeline` spans.
  Each span maps `source_start/source_end` to `media_start/media_end`, in seconds.
  Recording gaps are omitted from media time, not filled with unrelated frames.
  `delivery` identifies `prepared` or `original_file`. Direct `before` and
  `next_timestamp` identify the selected file's playable boundary, clipped to
  the requested range. The client enforces that boundary even if the underlying
  file contains earlier or later frames.
  `effective_start_timestamp` identifies the actual usable start when ready.
  A forward window without a usable next frame is `empty`, not fake-ready.
  A prepared file's `next_timestamp` (also `continuation_timestamp`) is the exact requested `before`, not a rounded
  measured tail. Resolve from that cursor, then explicitly create the next
  bounded window. Crossing an hour or review boundary requires a new search
  range supplied by the caller.
- `GET` or `HEAD /{camera}/recordings/http/sessions/{id}/media.mp4`: immutable
  original MP4 or completed faststart derivative. Returns `409` before readiness or after preparation
  failure. Single explicit, open-ended, and suffix byte ranges return `206`;
  invalid, multiple, or unsatisfiable ranges return `416` with `bytes */size`.
  Strong ETag or date `If-Range` mismatch returns the complete `200` response.
- `DELETE /{camera}/recordings/http/sessions/{id}`: `204` only after readers and
  preparation processes have stopped and output cleanup has completed.

All routes require the current authorized account. Session routes additionally
require session ownership.
URLs are not bearer capabilities. Responses are private and non-cacheable.
Polling and byte-range requests never renew the hard expiry, which is at most
180 seconds and further limited by the authentication token and shared session
policy. Ownership mismatch returns `404`; revocation returns `401`; expiry or
disabled policy returns `410`. Cleanup failure is an explicit `503`, not a
successful close.

The backend admits at most two preparations, four 128 MiB cache reservations
(512 MiB total), eight response readers, and two readers per session. Scaled
preparation also reserves the shared live encoder budget; all sessions reserve
the shared viewer budget. Preparation times out after 60 seconds. Source leases
remain protected until actual subprocesses and reader threads have joined.
Target resolution also holds leases: at most two concurrent ten-second
resolvers query 64 rows per stream and probe at most eight source files per request.
Output is published only after finite completion, faststart validation, and
per-span packet timing checks. FFmpeg frame quantization can adjust a span's
measured duration slightly; the returned timeline is authoritative.
Muxing can extend a segment's last displayed sample at an audio boundary. The
backend attributes final packets by preserved payload hashes and measures their
actual presentation intervals without extending catalog coverage. Consumers
must map both source and media endpoint pairs, not assume equal durations.

For recording-only cameras without generated previews, Review's camera tiles
read the saved recordings and follow the main player's timestamp. These tiles
request video-only media, remain muted, load only while visible, and show
recording gaps rather than holding an unrelated frame. They prefer a recorded
substream when available.
The main player's audio settings are unaffected.
Playback uses the browser's decoder; it does not enable continuous server-side
detection or generate a separate preview recording. Viewing many original
high-resolution streams can increase browser decoding and network usage.

The main recording starts before secondary tiles load, and its hidden loading
preview does not open a duplicate recording stream. Recording-backed tiles use
60-second windows near the shared playhead instead of loading the whole hour.
New playback sources start on a two-second timestamp grid to take advantage of
short initial HLS segments. Seeking outside a tile's window loads a new window
without changing the recorded image quality.

Review keeps the capture timestamp and timestamp-link action visible. Open
**Investigation tools** below the player for In/Out selection, looping,
activity navigation, metadata, bookmarks and incident notes. This section
starts collapsed; collapsing it preserves drafts and selections while pausing
its metadata/bookmark reads. An active selection remains visible outside it.

### Permanent protected-media library

The **Protected media** library at `/protected` browses saved evidence across
the complete retained event history, without depending on semantic search or
the Explore page's recent-event view. Filter by camera and local calendar dates,
then page through thumbnails, capture times, protection reasons and indexed
local/archive locations. The library reuses the saved-event protection flag;
it does not create another protection policy.

Original playback and **Export original** use the existing camera-authorized
original-recording endpoints, preserving the recorded encoding and audio.
Export original downloads a clip; it does not create a separately managed export
job. Playback still requires browser support for the original codecs.
Saved snapshots have their own download link. In-progress events do not offer a
final original clip until the event ends.

Storage totals cover the entire filtered result, not just the visible page.
Several saved events may protect the same recording segment, which is counted
once per physical recording row. Local and archive totals are separate. Unknown
sizes are reported explicitly, and known bytes are not presented as a complete
total when some sizes are unknown. These figures describe protected recording
files, not snapshot sizes or free disk space.

Availability is based on indexed recording metadata and cached archive-worker
health. Opening the library never probes NAS files. An unavailable archive is
not treated as deleted evidence. An indexed local recording is not a guarantee
that an externally removed file still exists, or that every second of a capture
range is present. Refresh after archive health changes. Historical cameras remain
subject to the same camera-level authorization as the original-media endpoints.

Permanent saved-event protection is different from a temporary playback or
export lease. A lease reserves original media while a reader is active and
expires afterward; it does not turn that media into permanently saved evidence.
Temporary leases are not listed as saved events.

Only an administrator can remove permanent protection. The confirmation explains
that normal retention, quota eviction and future lossy compression may become
eligible. Removing protection does not immediately delete recordings, snapshots
or the event. Other saved events and active reader leases continue to protect
overlapping recordings. There is no bulk destructive action in this library.
The existing `DELETE /api/events/{event_id}/retain` endpoint requires the JSON
body `{"confirm": true, "acknowledge_retention": true}` and changes only the
event's protection flag while holding the recording storage lock.
The same confirmation dialog is available from a saved event's Explore action
menu. Deleting a saved event, a recording range containing protected segments,
or a review item covering protected recordings returns a conflict until the
relevant saved events have been explicitly unprotected. Every saved event
sharing a recording segment, including its configured pre/post-capture padding,
must be unprotected before that physical segment can be deleted. An explicit
user-deletion request does not bypass permanent protection in the archive.

### Browser-compatible playback

When the browser cannot decode a recording's video, Horizon can prepare
a temporary H264/AAC playback copy. A compatible recorded substream is preferred
when available. The main recording, its audio, exports, retention, and physical
camera settings are unchanged.

The player labels compatible playback explicitly. It is limited to 1280x720 and
15 FPS, preserving the aspect ratio, with audio normalized to AAC-LC at 48 kHz.
Use a browser that supports the original codecs, or download the original file,
when you need the original recorded resolution and encoding.

With a configured NVIDIA hardware-acceleration preset, compatibility playback
uses the GPU's `h264_nvenc` encoder. It does not change the H265 originals.
Decoding, scaling and timeline padding remain bounded software operations.
Systems without a matching NVIDIA preset retain software H264 encoding.
If NVIDIA encoding fails, the session reports a failure rather than silently
retrying on the CPU.

Compatibility conversion uses short windows and bounded local cache and encoder
quotas rather than rewriting the archive or converting an entire hour before
playback begins. Seeking cancels the superseded conversion. Paused players renew
their temporary lease while the page can make requests; a suspended browser can
let the lease expire and require an explicit retry. Every status and media request
is authenticated and checked against the camera and session owner.

Muting the player alone does not avoid an audio decoder initialization failure.
An audio-only decoder failure does not force supported H265 video through H264
conversion. The player can keep the original video, temporarily without sound,
with a visible warning. Use the audio repair workflow below to permanently
normalize eligible older audio while preserving its video.
If source data, conversion capacity, or decoding is unavailable, the player ends
loading and reports the failure instead of showing an indefinite spinner. A
retry is explicit, not an unlimited conversion loop.

### Export timestamps and investigation context

Realtime single-camera and batch recording exports can select **No timestamp
labels**, a switchable **timestamp subtitle track**, **burned-in timestamps**,
or both. The default remains no labels. Subtitle-only output copies the video
and audio codecs; burn-in creates an H.264 export copy using bounded software
encoding. Neither option changes original recordings.

Normal realtime exports retain Frigate's existing input/coverage planning.
Captions, selected investigation metadata, chapters, and MP4 faststart are
combined in the final assembly pass instead of repeatedly rewriting the whole
export. Subtitle-only video remains stream-copied. Burn-in/Both still require
software video encoding, and mixed-stream staging or custom/timelapse processing
retains its existing behavior. Export concurrency and process priority are
unchanged. The internal export manifest is not an HLS review fallback.

Labels are derived from the realized recording timeline, including collapsed
recording gaps and keyframe lead-in. They show the configured UI time zone
(UTC when unset) and its offset, including daylight-saving changes. These are
inferred Frigate capture-timeline labels, not verified camera-sensor clock
measurements. Before publishing labeled output, the exporter compares the completed
media duration with the planned timeline and fails explicitly if they disagree.
Preview and arbitrary custom-speed exports do not expose these options until
their capture-to-output mapping can be verified.

**Include investigation metadata** is separately opt-in. Its category preview
shows which information is selected before export. The available categories
include camera identity, retained detections/confidence/zones, recognized
identities, stored object geometry, environmental context, notes and provenance.
Recognized names and plates require explicit selection. Unselected categories
are excluded, and runtime credentials and source filesystem paths are not
included. Missing capture-time environmental samples are marked unavailable;
the exporter does not substitute current weather or invent notes. Selected
incident notes include the camera's shared bookmarks overlapping the export,
with their authors, edit timestamps and revisions.
Stored object summaries are not represented as frame-accurate tracking data.

Selected context is structured JSON in the exported MP4's `comment` metadata
tag, readable using tools such as ffprobe. This does not make arbitrary context
visible as subtitles or graphics in ordinary video players. Subtitle display
and whether a player initially selects the timestamp track depend on the
container/player; the track is not forced. Existing chapters and media streams
are preserved by the combined final mux.
The context includes inferred capture-to-output span mappings so retained
capture-time events can be related to media time across recording gaps.
Re-encoding or sharing through another application may strip this metadata.

The recording investigation toolbar supports In/Out selection, looping,
timestamp-link sharing, and exact-time navigation with an explicit UTC offset.
Export selection reuses the existing export dialog on desktop and mobile.
Activity skipping currently uses only the selected camera's loaded review items,
with five seconds of context; it is not an exhaustive motion search. It stops
after the last loaded activity interval and is disabled while looping.
Malformed review timestamps are reported and excluded from activity navigation.

#### Review metadata display

Open a recording from Review and expand **Review metadata** in the investigation
controls below the player. The panel follows the selected camera and overlapping
Review item, independently of whether GenAI summaries are enabled.

It displays available stored review/event times, labels, zones, indexed storage,
confidence, identities, geometry and model/detector provenance. Retained event
data is requested only while expanded, for up to 50 linked event IDs, with
explicit missing-data and retry states. Closing the panel or changing camera,
review or account cancels superseded requests. Viewing metadata does not pause,
seek, reload or modify recordings.

Times are inferred from Frigate's event/recording timeline, not a verified
camera clock. Stored geometry is not a live frame-aligned overlay, and source
units can differ between event/model versions. Capture-time weather and adaptive
grid history are marked unavailable until historically recorded; current
conditions are never substituted for past context.

#### Shared bookmarks and incident notes

Expand **Bookmarks and incident notes** in the recording investigation controls
to create an annotation at the current capture time or the marked In/Out range.
Opening the editor freezes that anchor, even while video continues playing.
Titles and notes are shared with users authorized to view the camera.
Only the creator or an administrator can edit or delete them; changing camera
permissions also changes access to existing notes.

Bookmarks are stored in SQLite, independently of video retention, and remain
until explicitly deleted. They do not protect footage or prevent archive
cleanup. The indexed-at-anchor status is a database observation, not a promise
that the archive is online or the entire annotated range is present.
Deleting a bookmark deletes only the annotation, never its recording.

The API is camera-scoped at
`/{camera_name}/recordings/bookmarks`, with bounded list pagination, creation,
revision-checked editing and revision-checked deletion. Concurrent edits return
HTTP 409 rather than overwriting notes silently. Refresh and reopen the current
note before retrying a conflicting draft. Editing does not allow changing its
camera, creator or capture-time anchor.

This feature requires database migration 044. Back up the configuration/database
before deploying the new schema. Do not copy the new bookmark/export modules
over an older production database without applying the compatible release.
Rolling back that migration removes bookmark annotations, not video files.

Metadata export is bounded to 10,000 retained event records, 10,000 timeline
records, and 2 MiB of JSON. Oversized requests fail rather than silently
truncating investigation context; shorten the export or reduce categories.
Provenance records the application version and transformation mode, not a
claim of authenticity or cryptographic capture verification.

### Playback responsiveness and diagnostics

Review reuses the current media source for a seek only when the target is inside
both the selected recording coverage and the browser's buffered media range.
Real recording gaps, unbuffered targets and camera changes still use the normal
source-loading path. Pending seeks and autoplay retries are superseded by newer
seeks, explicit pause, source replacement or controller disposal.

Metadata requests are canceled when superseded or the player leaves the page.
During timeline scrubbing, previews remain available but full-video loading is
suspended. Releasing a drag loads the final requested range; it does not queue
every intermediate position.

HLS starts with a six-second forward-buffer target, growing after ten seconds
of stable playback to a thirty-second target by default. Playback rate, data
saver preference and advertised bitrate adjust the target. The forward target
is limited to sixty media seconds and accounts for a twenty-megabyte video
budget. Fifteen seconds of back buffer support nearby reverse seeks. These are
targets, not exact memory limits: complete media segments, decoder buffers and
browser implementation details can exceed them. Native HLS buffering remains
under browser control. Existing codec recovery and automatic quality selection
remain enabled.

Recorded playback pauses and HLS fragment loading stops when the browser tab is
hidden, unless the video is in picture-in-picture or the user explicitly enables
**Keep recorded video playing in background**. Playback resumes on return only
when it was playing before suspension. This preference is separate from Live
background playback. A temporary loading overlay alone does not suspend a
source that still needs to load.

Expand **Playback diagnostics** over the recording to inspect source startup,
latest seek latency, stall counts and duration, buffered seconds, dropped frames,
source-load count, original versus compatibility playback, and local versus
archive location. Timings use presented-frame callbacks when supported and
decoded-frame events otherwise. Startup begins at media-source setup, not at the
metadata request, and suspended time is excluded. Diagnostics are local to the
player; detailed display sampling runs only while the panel is open.

### Compatible audio without video conversion

`preset-record-generic-audio-aac` copies the video bitstream and normalizes the
audio to AAC-LC, 48 kHz, stereo. It does not resize, reduce the frame rate, or
re-encode H265 video:

```yaml
ffmpeg:
  output_args:
    record: preset-record-generic-audio-aac
```

This applies only to new recordings and cannot recover audio that was never
recorded. `preset-record-generic` deliberately removes audio, while
`preset-record-generic-audio-copy` preserves the source audio configuration,
including configurations a browser may not decode.

To test the camera's original audio without normalization, use the audio-copy
preset for that camera and a direct camera source in go2rtc, rather than an
`ffmpeg:` source with `#audio=aac`. An `ffmpeg:` source with `#audio=copy` does not
re-encode audio, but still starts an FFmpeg process; a supported direct RTSP
source can avoid that startup overhead. Changing these settings affects Live
and future recording processes, not previously stored files. A camera's H264
video selection does not imply any particular AAC sample rate or channel count.

Live view needs compatible audio too. A go2rtc source can normalize audio
on demand while copying its video, for example:

```yaml
go2rtc:
  streams:
    front_door:
      - "ffmpeg:rtsp://camera.example/stream#video=copy#audio=aac#raw=-ar#raw=48000#raw=-ac#raw=2"
```

Use the camera's actual source and protected credentials. This does not change
the camera's physical encoder settings, and a browser still needs support for
the original video codec and profile.

### Repair older recorded audio

Administrators can open **Settings > Advanced system > Recording audio repair**.
Choose a camera or time range and press **Scan recordings**. Scanning lists
eligible files, existing audio formats, and skip reasons without rewriting
anything. Review the list, then explicitly start selected files or all eligible
files after confirmation.

The worker preserves verified original backups and copies video without
re-encoding it. Only the audio is normalized. It skips missing audio, protected
evidence, archived/linked files, and recordings that are incomplete or already
being deleted. Files that change during processing are revalidated rather than
blindly replaced.

**Pause** takes effect at a safe file boundary. **Stop** cancels unfinished
work; completed repairs and their backups remain. Progress and item results
are paginated and persisted. An application restart does not automatically
resume rewriting. Ensure that there is enough free storage for the backups.

For offline maintenance, the companion CLI is dry-run by default:

```bash
python fix_recording_audio.py \
  --backup-dir /media/frigate/audio-repair-backups \
  --camera front_door --limit 10
```

Actual CLI changes require both `--apply` and `--offline-maintenance`, with
Frigate and other recording maintenance stopped. Prefer the GUI for coordinated
online work. Neither workflow can restore sound to an original file that has
no audio track.

## Common recording configurations

### Tiered storage: local recordings and an NFS archive

#### Original-quality rolling local mirror

Set `storage.mode: mirror` explicitly to use one local recorder with an
asynchronous verified remote copy, rather than pressure-triggered movement.
The default remains `move`, including for existing installations. Mirror never
changes camera encoding, resolution, frame rate, bitrate, or recording quality.
Older-footage compression is incompatible with mirror mode. Verified mirrored
originals are also excluded from audio repair and compression maintenance.

```yaml
storage:
  enabled: true
  mode: mirror
  archive_path: /media/frigate/archive
  mount_id: frigate-archive
  local_budget_gib: 2793.96772384644 # 3,000,000,000,000 bytes, not 3 TiB.
  local_copy_hours: 12
  retain_days: 90
  max_archive_gib: null
  archive_full_behavior: pause
  transfer_mib_per_second: 0
  parallel_transfers: 2
  emergency_delete: true
  compression:
    enabled: false
```

Before enabling mirror, mount `/media/frigate/recordings` on its own persistent
local filesystem (ZFS, ext4, XFS, or Btrfs). NFS/CIFS, container overlay, and
memory-backed recording mounts are rejected using local mount metadata, without
probing the remote filesystem. The content budget does not provision a dataset
or enforce a filesystem quota. Provision the desired local quota/reservation
separately, disable filesystem compression if original physical storage is
required, and give the recording process permission to create files and symlinks.
Keep the database, cache, clips, and exports off NFS. The archive still requires
its separately mounted NFS share and pre-provisioned `.frigate-archive` marker.

Every completed eligible segment is copied with the existing bounded transfer
workers, fsynced, and read back with SHA-256 verification before its exact remote
length is committed. Transfers run continuously, independent of start/drain
watermarks. Committed mirrors are deduplicated on restart; interrupted transfers
retain their original and durable reservation until isolated-worker recovery.
Missing or wrong-length remote mirrors are checked in bounded maintenance passes
and requeued while their local originals remain reserved. External modification
of the archive is unsupported; a length check is not a new hash certification.
An NFS outage or blocked syscall cannot make capture, storage statistics, APIs,
or local emergency reclamation wait on an NFS operation or an NFS-held path lock.
The backlog resumes when the archive becomes available.

`local_copy_hours` defaults to 12 and cannot be less than 12. A restart-safe daily
cleanup pass removes only verified redundant local copies older than this age,
replacing them with stable archive symlinks. The first pass starts 24 hours after
mirror maintenance is initialized; later passes start at least 24 hours after
the previous completed pass. Work is bounded and resumes after interruption.
Normal occupancy is approximately 12 to 36 hours plus bounded cleanup latency,
not a hard upper lifetime. Saved evidence and active recording leases preserve
both copies and can increase local occupancy. Unverified originals are never
deleted by normal camera retention or scheduled mirror cleanup. Archive retention
and an optional maximum archive age must honor the local minimum; expiration and
explicit user deletion of a mirrored recording are handled by the archive worker,
which deletes both copies and the catalog entry without orphaning remote files.
Archive capacity eviction remains a separate explicit destructive policy.

With `emergency_delete: true`, less than one estimated hour of physical or
content-budget headroom triggers last-resort local reclamation. Verified redundant
copies are released first, even if younger than the local minimum. If that is
insufficient, oldest unverified ordinary footage is overwritten/deleted, with
unsaved event footage last. Saved evidence, active leases, and incoming transfer
reservations are never sacrificed. Normal cleanup never uses this destructive
fallback. Disable emergency deletion to pause reclamation and risk capture stopping
when no space remains. The 12-hour guarantee is intentionally overridden only by
this emergency policy or an explicit destructive archive/user policy.

The catalog uses the existing two tiers: a `local` row with non-null
`archive_bytes` owns an exact verified remote copy as well as its local original.
Both physical copies count toward their respective storage totals. After local
reclamation, the same row becomes `archive`; its recording path is a local symlink,
and its remote byte charge remains. Migration 047 adds only the
`recordingmirrorstate` daily-cleanup checkpoint and selection indexes. It does not
reclassify files, move history, populate nullable lengths, or enable mirror.

For an installation whose historical `local` rows already point to NFS bytes,
perform a guarded offline cutover before enabling mirror:

1. Stop recording, archive, cleanup, and other writers; preserve the current
   catalog and all existing remote files. Do not restore an older database.
2. Bind that existing remote tree at the configured archive path with the required
   NFS marker. Bind the empty persistent local dataset at the recordings path.
   The archive's relative `date/hour/camera/segment.mp4` paths must remain identical
   to each historical row's relative recordings path.
3. For every historical row, create an absolute local symlink at its **exact
   existing recording path** targeting the corresponding archive path. Never copy
   or unlink the historical remote originals, and never apply local emergency
   deletion to these NFS-backed rows.
4. Reclassify only the identified historical rows as `storage_tier=archive`,
   preserving IDs, paths, capture times, protection, and playback metadata.
   Populate `archive_bytes` from exact remote file lengths in an isolated,
   controlled migration process, or leave them null for the existing asynchronous
   worker backfill. Keep `max_archive_gib: null` until backfill completes; a finite
   archive budget fails closed while historical lengths are unknown.
5. Verify row/link counts, representative playback, permissions for the actual
   UID/GID, and both mount identities before starting capture with mirror enabled.
   New captures must be regular local files, not historical symlink targets.

Regenerate only these model-derived English configuration translations with
`python3 generate_config_translations.py --storage-only` when Linux detector
dependencies are unavailable. The full existing generator remains supported.

This fork supports an optional recording archive. New recordings land in
`/media/frigate/recordings`. When Frigate's local recording usage reaches its
configured start percentage of the budget, a background worker copies completed segments to NFS, oldest
first, until local usage falls below the drain target. Low filesystem free space
can trigger transfers before the recording budget is reached.

```yaml
storage:
  enabled: false # Enable only after provisioning and validating the archive.
  archive_path: /media/frigate/archive
  mount_id: frigate-archive
  local_budget_gib: 1024
  start_percent: 60
  drain_percent: 40
  retain_days: 90
  max_age_days: null
  max_archive_gib: null
  archive_full_behavior: pause
  archive_reserve_gib: 0
  transfer_mib_per_second: 0
  emergency_delete: true
```

Mount your TrueNAS NFS share on the Unraid host first, then bind its dedicated
archive directory into the container at `/media/frigate/archive`, read-write.
Do not move Frigate's database or recording cache onto NFS. Before enabling the
archive, create a `.frigate-archive` text file in that directory containing the
configured `mount_id`, followed by an optional newline. Frigate requires both an
NFS filesystem and the matching marker. It will not initialize a local fallback
directory when the mount is missing. Use a dedicated directory for this Frigate
instance; do not share the same archive directory between instances.

The container needs permission to read, write, and delete archive files, and to
create symbolic links in the local recordings directory. Provision the NFS mount
before starting the container. Adding or changing a Docker volume mapping requires
recreating the container, but subsequent storage-control edits do not.

Open **System > Storage > Storage settings** for live controls and archive health.
The two-handle archive slider runs from zero GiB to the configured local recording
budget. The left handle sets the stop target; the right handle sets the start
threshold. Current recording usage is marked on the same scale when a fresh
sample is available. Changing the budget rescales the displayed GiB values while
preserving the percentages. Stop must remain below start. For example, with a
3048-GiB budget, 60% start and 40% stop begin transfers near 1829 GiB and drain
toward 1219 GiB, without changing that budget, retention, or deletion policy.
Select **Use 60% start / 40% stop** to prepare those values, then save to apply
them live. Existing configurations without `start_percent` retain the former
100%-start behavior until edited. Physical free-space pressure can still start
transfers earlier or keep them running below the stop target. Unraid share
capacity describes the mounted share, not the free space on each underlying disk.
The dashboard offers **Clean**, following the app's existing light/dark theme, and
a dark **Control room** appearance. The appearance choice is saved per browser.
Budget, retention, rate limit, and emergency policy changes are persisted and
applied without restarting cameras or detectors. Updates interrupt an unfinished
copy safely and apply between transfers. If NFS itself is stalled, the worker
cannot apply an edit until the filesystem operation returns; the dashboard reports
an unresponsive worker rather than blocking local recording or API requests.
The dashboard distinguishes saved settings from worker acknowledgment. Archive
location changes remain locked while an earlier update is pending, preventing a
paused transfer from publishing into an obsolete archive location.
Location edits are also locked until startup link recovery has completed.
Health is polled frequently; aggregate tier usage is cached for up to one minute
to avoid repeatedly scanning a large recording database.

Archive admission commits durable reservations in groups of at most 25 files,
before any of those files are copied. Source and NFS path checks occur outside
SQLite write transactions. Each group rechecks retention, maintenance activity,
the exact configured archive quota, and physical free space, including existing
reservations. When the archive has no content quota and enough physical space,
admission reuses the worker's separately sampled accounting rather than scanning
the entire archive for every file. Copying, checksum readback, durable publication,
and source release retain their existing ordering. A higher worker count alone
cannot compensate for slow admission or metadata operations. Judge performance
by verified committed bytes over a sustained interval, not an instantaneous
copy-only rate.

The existing **Camera Storage** graph and table show local and NAS recording
usage together, rather than presenting a second camera table. They use canonical
camera IDs alongside display names. It includes historical cameras that still
have recordings, even if they were removed from the configuration. Bars show
each camera's share of known recording usage in that tier. The summary graph,
Other and Unused rows, and filesystem percentages refer only to local filesystem
capacity, not the NAS. Bandwidth and main/sub-stream details remain in the same
table. Global and per-camera totals come from the same database snapshot and
remain available when the NAS is offline. Local usage uses legacy recording
metadata. New segments use the final stored file's byte length converted to MiB
without rounding; even a clip smaller than 0.005 MiB must retain a positive size.
Historical rows may still contain older rounded estimates. Archive content
uses exact persisted file lengths. When an archive budget is configured, legacy
archive lengths are backfilled only by the archive worker, at most 256 indexed
files per pass, without walking the NAS. Unknown or unavailable lengths block
budgeted transfers rather than counting as zero. Without a budget, legacy
unknown lengths remain explicitly incomplete and do not stop unlimited transfers.
Incomplete usage is flagged. This breakdown does not include snapshots,
previews, exports, databases, filesystem overhead, or playback cache.

`local_accounting_pending` means at least one local recording has an unknown
stored size. Local usage and headroom remain unknown until that metadata is
verified; the archive worker stops at this check before checking the NFS mount.
This code alone does not mean NFS is offline. Compare the affected row with its
actual local file and repair only the measured size, preserving the recording,
protection, leases and storage policy. Do not treat unknown sizes as zero or
delete footage simply to clear the warning. After accounting is corrected,
`checking` can remain visible during normal archive recovery/retention work
before new transfers begin.

The local budget is measured in GiB of recording content, not a percentage of the Unraid
array. It is a soft target: incoming recordings, open playback file descriptors,
and transfer time can temporarily push actual local usage above it. A zero
transfer limit means unlimited bandwidth. Transfers do not transcode footage.
The rate limit accounts for both copy traffic and the verification read back.

#### Buffered transfers and capacity

Parallel archive transport is configurable with `storage.parallel_transfers`
(1 to 50, default 1). Each bounded rolling queue reserves exact bytes before
starting concurrent jobs and admits at most four files per worker. An idle
worker can start the next queued file without waiting for a slower peer.
Each admitted queue is processed in two phases: copy every selected recording
directly from HDD to temporary archive files, then read back and verify the
completed copies. Verification never overlaps copying within that queue.
No SSD staging is required. Original HDD files remain available throughout
both phases; only successfully verified copies may proceed to publication.
Cancellation or a verification error retains the originals and reservations
for recovery, and no new queue starts until the current queue finishes.
Drain planning walks the indexed oldest-first prefix only until it reaches the
estimated bytes to move, rather than sorting the entire local recording set.
Within a pinned batch, successfully flushed directory identities are reused.
Every use still validates the path; a replaced directory is flushed again.
Archive file and publication flushes remain per file.
Files retain their own checksum verification, cancellation,
source protection and atomic publication. `storage.transfer_mib_per_second` is one
aggregate copy/readback budget, not a separate allowance for each worker.
Verified copies are published under the storage lock and their tier metadata is
committed together in a SQLite FULL transaction. Each archive file and local
link is still flushed before metadata is committed. A failed metadata commit
leaves readable, recoverable local-tier links; it does not count those files as
successfully committed transfers.
Increasing concurrency can overlap storage latency, but does not automatically
create NFS TCP connections or distribute one NAS peer across LACP links.
NFS connection count and bond hashing must be verified independently; do not
remount an active archive merely to try a performance setting.

The storage dashboard follows the application's selected color scheme.
Comfortable and Compact change information density, not the light/dark theme.
Filesystem accounting and individual transfer activity can be expanded when
needed. The retention summary shows resolved camera policies alongside the
archive minimum; disabled capture and missing legacy policy information are
identified explicitly.

Storage edits submit only changed fields. The API can atomically check their
previous effective values using `expected_storage` on `/config/set`; a conflict
returns HTTP 409 without writing the configuration. Discard a conflicting draft
and review the latest values rather than overwriting another operator's changes.
Guarded saves require a backend supporting `expected_storage`; the frontend
also checks fresh settings before saving, but that alone cannot eliminate races
against older backends.

Recording-usage aggregates are refreshed by storage maintenance, not by HTTP
request threads. `/recordings/storage/tiers` includes `usage_status` with the
last successful sample time, staleness and any refresh error. The last complete
snapshot remains readable during refresh. A failed refresh is logged and marked
unavailable, not reported as fresh zero usage. Storage-tab polling pauses when
another system tab is active.

Before rolling back to a build that predates `parallel_transfers`, remove that
option from the saved configuration. Setting it to `1` selects serial transfers
in a current build, but does not make an older strict configuration schema
accept the new field.

Use **verified archival throughput** to estimate a large migration, rather than
the active-copy or readback rate alone. Committed throughput includes copying,
checksum verification, durable flushes, filesystem publication and metadata
updates. Parallel workers can overlap those waits, but short synthetic tests
do not establish sustained production throughput. Compare representative batches
and preserve verification and durable writes when tuning.
Higher concurrency can increase HDD seeking, disk queueing and NAS latency.
Measure representative batches alongside recording health before keeping a
larger value; a larger worker count does not guarantee greater throughput.

Review shows an **Archived** badge when the currently playing recording segment
is indexed on archive storage. The badge follows the selected Original or Low
stream and changes as playback crosses local/archive boundaries. It is not
inferred from recording age and does not mean a cached browser video fragment
requires another network read. Scrubbing previews are not labeled as archived
recording playback.

**Start moving at local usage** (`local_budget_gib`) is the high-water trigger.
**Stop moving at budget usage** (`drain_percent`) is the lower target. The same
existing archive worker drains oldest eligible segments and then returns to
**Idle: buffering**. New recordings are remeasured between transfers, so completing
the initial batch plan does not falsely finish a drain while ingest continues.
The existing two-hour estimated local free-space reserve can trigger an early
drain; there is no forced idle delay or new scheduling window.

The Storage page separates current-file copy bytes, archive readback bytes and
commit. Batch committed bytes/files increase only after successful SHA-256
verification, durable publication and the recording metadata update. The initial
plan uses indexed size estimates and stays fixed; additional net incoming bytes,
live usage and remaining drain are shown separately. The initial plan bar is not
an assertion that the live drain is complete. Copy and archive readback speeds
are shown separately in decimal MB/s (1 MB = 1,000,000 bytes). These live-updated
batch averages use time spent in each phase, including the copy flush, not time
spent idle or in the other phase. Samples accumulate across short recording
files and appear after at least one second of the relevant phase. Copy speed
does not imply the footage is verified or safe to release locally.
Verified archival throughput includes verification and commit and requires at least two
committed files and one second of observation. Drain ETA additionally requires
a valid aggregate ingest sample and a positive transfer-minus-ingest rate.
Insufficient samples, paused work and an unresponsive worker have no ETA. A
transfer rate that cannot outpace ingest and a lack of eligible segments are
reported rather than claiming success.

Each tier shows recording content usage and remaining Frigate budget separately
from mounted filesystem total and available space. Local content usage remains
an indexed estimate; archive usage uses verified exact file lengths. Incoming
reservations are separate from committed usage, and already-written reservation
bytes are already reflected in filesystem availability. Effective headroom is
the minimum of quota remaining and filesystem availability after safety reserve
and unwritten reservations. `archive_reserve_gib` configures the archive physical
reserve without changing its content quota. Protected bytes are included in
content usage, not added a second time; their separate size is currently unknown.
Over-budget content is visible even when headroom is zero.

Filesystem probes run only in the isolated worker. Requests use bounded cached
snapshots and never inspect NFS. Sample times and unknown/offline/stale states are
explicit; stale or offline capacity has no advertised incoming headroom. These
numbers describe the mounted filesystem or NFS export, not NAS raw disk capacity
or an inferred Unraid pool. Matching filesystem identities are labeled shared
and must not be added together; different exports can also share a provider pool.
An unset content quota does not imply unlimited physical capacity. No capacity,
quota, archive activation, or retention values are automatically selected for
an existing deployment.

Review thumbnails and detail cards display **Local**, **Archived**, or
**Local + archive** when indexed main recordings overlap the review item. Pending
deletions and substream-only recordings do not contribute to this badge.
Missing metadata has no inferred storage badge. These labels describe indexed
location, not complete time coverage or current NAS reachability; displaying
them does not access archive files.

Retention is measured from recording time, not transfer time. While archiving is
enabled, retained footage and associated review metadata are kept for at least
`retain_days`; existing longer camera retention is honored. Existing recording
modes still determine which segments are admitted from the recording cache.
For continuous long-term footage, enable recording and set continuous retention
to a positive value. Archiving does not recover segments previously discarded by
motion-only or alert-only recording policies.

`max_age_days` and `max_archive_gib` are optional and unset by default, preserving
existing retention and unlimited archive content capacity. `retain_days` still
defaults to 90, so set it to 180 alongside a 180-day maximum when 180 days of
ordinary archive retention is desired. Maximum age is measured from the segment's
end timestamp and overrides longer camera retention, but never saved evidence or
active original-media leases. A segment ending exactly on the age boundary is
not expired until that boundary is passed.

`max_archive_gib: 6144` means exactly 6 TiB (6,597,069,766,656 bytes).
It limits owned indexed recording content and incoming copy reservations, not
the entire NAS, filesystem overhead, snapshots, or other applications' files.
Use a dedicated directory and do not add or modify recordings outside Frigate.
Admission reserves the entire incoming file length before writing. A durable
reservation tracks interrupted copies until the worker reconciles publication
or deletes the known partial and orphaned copy. No recursive NAS scan is needed.
Filesystem free space is checked independently; a failed write preserves the
local source and reports an unavailable archive.

`archive_full_behavior` defaults to `pause`, even with a content budget.
Explicitly selecting `expire_oldest` allows quota or filesystem-pressure cleanup
to override minimum retention, including young footage. The worker exhausts
eligible ordinary footage oldest-first before considering unsaved event footage
as a last resort. Saved evidence and active original-media leases are never
automatically deleted. If protection, leases, or missing size metadata prevent
enough cleanup, incoming transfers pause and Storage reports an explicit blocked
status code. Lowering a budget applies the same rules to existing content.
Existing protected content can exceed the new budget; Frigate does not delete it
or admit another copy to force the budget to appear satisfied.

Original-media readers must acquire and renew `frigate.record.leases` reservations
before planning/opening their window and release them after the final read.
Derivative-cache leases alone do not protect original NAS recordings. Validate
reader integration before enabling automatic archive eviction.
Snapshot extraction, streaming clips, export staging/probing, and nginx-VOD
playback all require this protection. A raw nginx-VOD mapping can be used after
its API response completes, so a response-scoped lease alone is insufficient.
Compression remains fail-closed until complete reader coverage is certified.
The current development integration reserves originals during snapshot and Plus
frame extraction, progressive clip planning/streaming, and recording exports
(including probes, staging, and retries). Cancellation waits for the actual
reader thread and owned media processes before releasing its reservation.
Plus uploads no longer hold originals after image extraction. These integrations
do not certify nginx-VOD or compatibility playback lifetimes and do not enable
compression or automatic archive eviction.
Saved recordings must be explicitly unprotected before a deletion request can
remove them; queued user intent does not override saved protection.

Each copy and its directory entries are flushed and SHA-256 verified before its local file is atomically
replaced with a stable symbolic link to the archive. Playback, timeline, and
exports continue using the same recording paths, including previously generated
playback manifests. Back up the database and local directory structure, preserving
symbolic links, as well as the archive itself. The archive is a storage tier,
not a second backup copy.

Do not run unmodified upstream Frigate or downgrade this fork against an active
archive's database and recording directory. Older cleanup code does not understand
archive links. Restore local recording files or restore a complete pre-archive
backup before switching back.

If the archive is offline or full, local sources are preserved and transfers
retry. Missing-file synchronization does not purge archive metadata during an
outage. Archive deletion requests are queued in the database and performed when
the verified share is available again. Unless `expire_oldest` was explicitly
selected, a full archive pauses transfers rather than causing early deletion.
Export footage that must be kept separately.

With `emergency_delete: true`, the one-estimated-hour free-space threshold
reclaims ordinary **local** footage first, oldest first, then unsaved
event-associated footage only as a last resort. Event classification includes
camera-specific pre/post-capture windows and ongoing events. Protected footage
is never automatically deleted, even if its legacy `has_clip` flag is false.
Reclaimed content is measured from the actual file length, not rounded legacy
metadata. Archive files are never deleted to reclaim local space.

Emergency cleanup can violate ordinary retention and is logged explicitly.
If protected or unavailable content prevents enough space from being reclaimed,
the storage warning remains active and recording may stop. Disabling emergency
deletion is honored even when NAS archiving is off. Normal time-based retention
still runs, while preserving protected footage and its preview coverage.

Deletion claims are persisted with a reason before content is removed.
An explicit user deletion may remove protected footage; automatic retention and
emergency intents cannot bypass protection. Saving an event while an overlapping
deletion is already in progress returns a conflict instead of claiming the
footage was protected. Once an unprotected deletion has started, recovery
finishes it even if ordinary retention is subsequently increased.

Turning archiving off stops new moves; existing archived footage remains playable
and continues to expire by retention. Archive path and identity cannot change
while archiving is enabled, archived rows remain, or a settings update is pending.
Lowering retention can
permanently delete existing footage; the dashboard requires confirmation.
Previously saved events remain protected from automatic archive expiration,
including retries of older ambiguous deletion intents.

The current implementation requires Linux containers and NFS. Validate it against
a disposable share and test recordings before enabling it on production footage.

Before production use, verify playback and export across the local/archive
boundary, stop the worker during a copy and during publication, disconnect the
share, and test with an incorrect identity marker. Local recordings must remain
intact after failed transfers. Restore the share and confirm retries complete.
Use short test retention to verify expiration and saved-event protection without
waiting for the production retention period. Do not use production recordings
for these destructive expiration tests.

### Configurable older-footage compression

Compression is opt-in and independent of camera ingest, live playback and archive
transport. Select the encoder and target video bitrate in Settings. The supported
implementations are NVIDIA H.264, H.265 and AV1, CPU x264/x265, CPU SVT-AV1 and CPU
libaom AV1. NVIDIA choices require the corresponding hardware capability. An RTX
A400 cannot encode AV1 on its NVENC engine; CPU AV1 is a separate supported choice.
An unavailable encoder fails explicitly, without switching codec or processor.

```yaml
storage:
  compression:
    enabled: false
    encoder: libsvtav1
    target_mbps: 5.5
    cpu_threads: 8
    svt_preset: 12
    frame_rate: null
    min_age_days: 14
    max_files: 100
    max_batch_minutes: 30
```

Bitrate is variable, while output cadence is constant. Actual average bitrate
may differ from its target, particularly for short clips. SVT-AV1 preset mappings
depend on the installed encoder version; higher presets generally prioritize
speed. CPU subprocesses, including their decoding and validation work, are
restricted to at most the selected available logical CPU cores. This is a CPU
affinity ceiling, not a reservation of those cores or a RAM allocation.

Leaving `frame_rate` unset requires the original constant cadence and skips
variable-timed recordings. Explicitly selecting a rate, such as `16`, authorizes
normalizing timestamps and possibly duplicating or dropping frames. Resolution
and captured recording dates are preserved. A lower bitrate and fixed resolution
do not imply lossless quality.

Each batch snapshots its encoder, bitrate, CPU and cadence settings. Later
changes apply to new batches, not an in-flight conversion, and completed files
are not automatically recompressed. Saved originals and active readers are
exempt. A smaller replacement must pass codec, geometry, cadence, copied-audio
identity and complete-decode checks before publication. Temporary originals are
released only after durable commit and verified cleanup.

The reader-lease, reservation and recovery gates must pass before any batch can
start. Merely enabling the policy does not bypass those checks or authorize
converting footage using unsaved settings.

### Most conservative: Ensure all video is saved

For users deploying Frigate in environments where it is important to have contiguous video stored even if there was no detectable motion, the following configuration will store all video for 3 days. After 3 days, only video containing motion will be saved for 7 days. After 7 days, only video containing motion and overlapping with alerts or detections will be retained until 30 days have passed.

<ConfigTabs>
<TabItem value="ui">

Navigate to <NavPath path="Settings > Global configuration > Recording" />.

- Set **Enable recording** to on
- Set **Continuous retention > Retention days** to `3`
- Set **Motion retention > Retention days** to `7`
- Set **Alert retention > Event retention > Retention days** to `30`
- Set **Alert retention > Event retention > Retention mode** to `all`
- Set **Detection retention > Event retention > Retention days** to `30`
- Set **Detection retention > Event retention > Retention mode** to `all`

</TabItem>
<TabItem value="yaml">

```yaml
record:
  enabled: True
  continuous:
    days: 3
  motion:
    days: 7
  alerts:
    retain:
      days: 30
      mode: all
  detections:
    retain:
      days: 30
      mode: all
```

</TabItem>
</ConfigTabs>

### Reduced storage: Only saving video when motion is detected

To reduce storage requirements, configure recording to only retain video where motion or activity was detected.

<ConfigTabs>
<TabItem value="ui">

Navigate to <NavPath path="Settings > Global configuration > Recording" />.

- Set **Enable recording** to on
- Set **Motion retention > Retention days** to `3`
- Set **Alert retention > Event retention > Retention days** to `30`
- Set **Alert retention > Event retention > Retention mode** to `motion`
- Set **Detection retention > Event retention > Retention days** to `30`
- Set **Detection retention > Event retention > Retention mode** to `motion`

</TabItem>
<TabItem value="yaml">

```yaml
record:
  enabled: True
  motion:
    days: 3
  alerts:
    retain:
      days: 30
      mode: motion
  detections:
    retain:
      days: 30
      mode: motion
```

</TabItem>
</ConfigTabs>

### Minimum: Alerts only

If you only want to retain video that occurs during activity caused by tracked object(s), this configuration will discard video unless an alert is ongoing.

<ConfigTabs>
<TabItem value="ui">

Navigate to <NavPath path="Settings > Global configuration > Recording" />.

- Set **Enable recording** to on
- Set **Continuous retention > Retention days** to `0`
- Set **Alert retention > Event retention > Retention days** to `30`
- Set **Alert retention > Event retention > Retention mode** to `motion`

</TabItem>
<TabItem value="yaml">

```yaml
record:
  enabled: True
  continuous:
    days: 0
  alerts:
    retain:
      days: 30
      mode: motion
```

</TabItem>
</ConfigTabs>

## Pre-capture and Post-capture

The `pre_capture` and `post_capture` settings control how many seconds of video are included before and after an alert or detection. These can be configured independently for alerts and detections, and can be set globally or overridden per camera.

<ConfigTabs>
<TabItem value="ui">

Navigate to <NavPath path="Settings > Global configuration > Recording" /> for global defaults, or <NavPath path="Settings > Camera configuration > (select camera) > Recording" /> to override for a specific camera.

| Field                                          | Description                                          |
| ---------------------------------------------- | ---------------------------------------------------- |
| **Alert retention > Pre-capture seconds**      | Seconds of video to include before an alert event    |
| **Alert retention > Post-capture seconds**     | Seconds of video to include after an alert event     |
| **Detection retention > Pre-capture seconds**  | Seconds of video to include before a detection event |
| **Detection retention > Post-capture seconds** | Seconds of video to include after a detection event  |

</TabItem>
<TabItem value="yaml">

```yaml
record:
  enabled: True
  alerts:
    pre_capture: 5 # seconds before the alert to include
    post_capture: 5 # seconds after the alert to include
  detections:
    pre_capture: 5 # seconds before the detection to include
    post_capture: 5 # seconds after the detection to include
```

</TabItem>
</ConfigTabs>

- **Default**: 5 seconds for both pre and post capture.
- **Pre-capture maximum**: 60 seconds.
- These settings apply per review category (alerts and detections), not per object type.

### How pre/post capture interacts with retention mode

The `pre_capture` and `post_capture` values define the **time window** around a review item, but only recording segments that also match the configured **retention mode** are actually kept on disk.

- **`mode: all`**: Retains every segment within the capture window, regardless of whether motion was detected.
- **`mode: motion`** (default): Only retains segments within the capture window that contain motion. This includes segments with active tracked objects, since object motion implies motion. Segments without any motion are discarded even if they fall within the pre/post capture range.
- **`mode: active_objects`**: Only retains segments within the capture window where tracked objects were actively moving. Segments with general motion but no active objects are discarded.

This means that with the default `motion` mode, you may see less footage than the configured pre/post capture duration if parts of the capture window had no motion.

To guarantee the full pre/post capture duration is always retained:

```yaml
record:
  enabled: True
  alerts:
    pre_capture: 10
    post_capture: 10
    retain:
      days: 30
      mode: all # retains all segments within the capture window
```

:::note

Because recording segments are written in 10 second chunks, pre-capture timing depends on segment boundaries. The actual pre-capture footage may be slightly shorter or longer than the exact configured value.

:::

### Where to view pre/post capture footage

Pre and post capture footage is included in the **recording timeline**, visible in the History view. Note that pre/post capture settings only affect which recording segments are **retained on disk**. They do not change the start and end points shown in the UI. The History view will still center on the review item's actual time range, but you can scrub backward and forward through the retained pre/post capture footage on the timeline. The Explore view shows object-specific clips that are trimmed to when the tracked object was actually visible, so pre/post capture time will not be reflected there.

## Configuring Recording Retention

Frigate supports both continuous and tracked object based recordings with separate retention modes and retention periods.

:::tip

Retention configs support decimals meaning they can be configured to retain `0.5` days, for example.

:::

### Continuous and Motion Recording

The number of days to retain continuous and motion recordings can be configured. By default, continuous recording is disabled.

<ConfigTabs>
<TabItem value="ui">

Navigate to <NavPath path="Settings > Global configuration > Recording" />.

| Field                                     | Description                                  |
| ----------------------------------------- | -------------------------------------------- |
| **Enable recording**                      | Enable or disable recording for all cameras  |
| **Continuous retention > Retention days** | Number of days to keep continuous recordings |
| **Motion retention > Retention days**     | Number of days to keep motion recordings     |

</TabItem>
<TabItem value="yaml">

```yaml
record:
  enabled: True
  continuous:
    days: 1 # <- number of days to keep continuous recordings
  motion:
    days: 2 # <- number of days to keep motion recordings
```

</TabItem>
</ConfigTabs>

Continuous recording supports different retention modes [which are described below](#configuring-recording-retention).

### Object Recording

The number of days to retain recordings for review items can be specified for items classified as alerts as well as tracked objects.

<ConfigTabs>
<TabItem value="ui">

Navigate to <NavPath path="Settings > Global configuration > Recording" />.

| Field                                                      | Description                                 |
| ---------------------------------------------------------- | ------------------------------------------- |
| **Enable recording**                                       | Enable or disable recording for all cameras |
| **Alert retention > Event retention > Retention days**     | Number of days to keep alert recordings     |
| **Detection retention > Event retention > Retention days** | Number of days to keep detection recordings |

</TabItem>
<TabItem value="yaml">

```yaml
record:
  enabled: True
  alerts:
    retain:
      days: 10 # <- number of days to keep alert recordings
  detections:
    retain:
      days: 10 # <- number of days to keep detections recordings
```

</TabItem>
</ConfigTabs>

This configuration will retain recording segments that overlap with alerts and detections for 10 days. Because multiple tracked objects can reference the same recording segments, this avoids storing duplicate footage for overlapping tracked objects and reduces overall storage needs.

## Sub Stream Recording

In addition to the main recording stream, Frigate can record a second, lower quality stream for each camera. This serves two purposes:

- **Quality selection during playback**: A quality selector (`Auto`, `Original`, or `Low`) appears in History view for cameras with sub stream recording enabled. `Original` and `Low` play only that stream's recordings. Time ranges where the selected stream has no footage are skipped during playback, and the selector notes when the selected stream has no recordings at all in the viewed time range. With `Auto` (the default), playback prefers the original quality and automatically falls back to the low quality stream when the connection cannot keep up, or for time ranges where the original recordings have expired. The selector shows each stream's video codec and audio details beneath the options; footage recorded by older Frigate versions shows no details.
- **Quality selection when exporting**: A `Quality` selector (`Auto`, `Original`, or `Low`) is available for cameras with sub stream recording enabled. See [exporting](#exporting-a-camera-that-records-two-streams) for details on each option.
- **Extended retention**: Sub stream recordings have their own retention settings, fully independent of the main recordings. By giving the low quality recordings a longer retention period, you can keep weeks or months of low quality history using a fraction of the storage, and that history remains playable after the main recordings expire. Playback falls back to the low quality recordings automatically, and the timeline shows a muted treatment for time ranges where only low quality footage remains. Timeline previews are kept for as long as either stream still has recordings, so scrubbing works across the whole retained history.

### Configuring sub stream recording

Sub stream recording uses the `record_sub` input role. This role can be assigned to the same input as `detect`, so in the common case where detect already uses the camera's sub stream, no additional camera connection is needed. Like the main recording stream, sub stream segments are copied directly from the camera stream without re-encoding, so the recording quality is determined by the source stream.

The following examples keep 7 days of full quality continuous recordings and 60 days of low quality continuous recordings:

<ConfigTabs>
<TabItem value="ui">

Navigate to <NavPath path="Settings > Camera configuration > Streams (FFmpeg)" /> and select the camera.

- In **Camera inputs**, enable the **Record (Sub Stream)** role on the stream you want to record at low quality, commonly the same stream that has the **Detect** role. Only one stream may have this role, and it cannot be assigned to the same stream as the **Record** role.

Navigate to <NavPath path="Settings > Camera configuration > Recording" /> and select the camera.

- Set **Enable recording** to on
- Set **Continuous retention > Retention days** to `7`
- Set **Sub stream recording > Enable sub stream recording** to on
- Set **Sub stream recording > Sub stream continuous retention > Retention days** to `60`

The camera setup wizard also offers the **Record (Sub Stream)** role when assigning stream roles for a newly added camera.

</TabItem>
<TabItem value="yaml">

```yaml
cameras:
  front_door:
    ffmpeg:
      inputs:
        - path: rtsp://camera/main
          roles:
            - record
        - path: rtsp://camera/sub
          roles:
            - detect
            - record_sub
    record:
      enabled: true
      continuous:
        days: 7
      sub:
        enabled: true
        continuous:
          days: 60
```

If your camera does not provide a suitable sub stream (or the sub stream is already used at a resolution you don't want to record), you can use a go2rtc transcode as the source for `record_sub` instead:

```yaml
go2rtc:
  streams:
    front_door: rtsp://camera/main
    front_door_lq: ffmpeg:front_door#video=h264#width=854#hardware

cameras:
  front_door:
    ffmpeg:
      inputs:
        - path: rtsp://127.0.0.1:8554/front_door
          input_args: preset-rtsp-restream
          roles:
            - detect
            - record
        - path: rtsp://127.0.0.1:8554/front_door_lq
          input_args: preset-rtsp-restream
          roles:
            - record_sub
    record:
      enabled: true
      continuous:
        days: 7
      sub:
        enabled: true
        continuous:
          days: 60
```

</TabItem>
</ConfigTabs>

The `record.sub` config supports the same retention structure as the main recording config: `continuous`, `motion`, `alerts`, and `detections` each with their own `days` (and `mode` for alerts and detections). The pre-capture and post-capture windows for alerts and detections are taken from the main `record.alerts` and `record.detections` config. Extending `sub.alerts.days` or `sub.detections.days` beyond the main values also keeps those review items visible in the review timeline for the longer window, with playback falling back to the low quality stream once the main recordings expire.

:::note

Recording must be enabled (`record.enabled`) for sub stream recording to run, and Frigate will fail to start if `record.sub.enabled` is set without a `record_sub` role assigned to one of the camera's inputs.

:::

### How Auto picks a quality

`Auto` measures throughput on every segment download and compares it against the original stream's bitrate (computed from the recorded footage itself). Playback drops to the low quality stream when any of these happen:

- A freeze lasts 4 seconds (10 seconds when it starts within 2 seconds of a seek, since the seek target is rarely buffered), or freezes total 7 seconds within the last minute.
- 3 downloads in a row measure below the original bitrate plus 10%, dropping quality before a stall ever becomes visible.
- No first frame appears within 10 seconds, or loading fails outright.

Playback returns to full quality only when measured throughput exceeds the original bitrate by 50%, checked continuously while playing the low quality stream and again at each new hour. The asymmetric thresholds (1.1x to drop, 1.5x to return) keep a borderline connection from switching back and forth.

The most recent measurement is remembered on the device: a connection last measured below the original bitrate (or below 3 Mbps when the bitrate is not yet known) starts playback on the low quality stream so a first frame appears immediately, then upgrades within a few segments if the speed allows.

The quality selector shows which stream Auto is currently playing and why. A browser with Data Saver enabled stays on the low quality stream, a browser that cannot decode the original stream's codec (for example H.265 without HEVC support) plays the low quality stream for that camera, and pinning `Original` or `Low` bypasses Auto entirely.

### Sub stream output args

By default the sub stream is recorded with the same [output args](/configuration/ffmpeg_presets#output-args-presets) as the main recording stream, so it inherits any customization made to `ffmpeg.output_args.record`. Setting `ffmpeg.output_args.record_sub` gives the sub stream its own args instead. Like all `ffmpeg` config, this can be set globally or per camera.

The most common reason to set this is a pair of streams whose audio differs. Many cameras send AAC on the main stream but PCM on the sub stream, and PCM cannot be copied into an mp4 recording. Copying the main stream's audio avoids re-encoding audio that is already AAC, while the sub stream still needs to be transcoded:

```yaml
ffmpeg:
  output_args:
    # main stream audio is already AAC, so copy it
    record: preset-record-generic-audio-copy
    # sub stream audio is PCM, so transcode it to AAC
    record_sub: preset-record-generic-audio-aac
```

Other reasons to set this are recording a sub stream whose codec needs a different preset than the main stream, such as `preset-record-mjpeg`. The AAC preset normalizes both streams to 48 kHz stereo; copying AAC preserves the camera's original sample rate and may retain browser-specific audio incompatibilities.

:::warning

Avoid removing audio from only one of the two streams (for example with `-an`). When one stream has audio and the other does not, playback of time ranges that combine both qualities is silent, so stripping audio from the sub stream also silences the merged timeline.

:::

### Which stream do features use?

As a general rule, features that read recordings prefer the main stream and fall back to the sub stream for time ranges where the main recordings have expired. Analytics features use only the main stream.

| Feature                                                                                  | Stream used                                                                                                   |
| ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| Recording playback (History and Review)                                                  | Both (main preferred with sub fallback by default), or exactly one stream when a quality is selected manually |
| Tracking details and Explore clip playback                                               | Main, falling back to sub where the main recordings have expired                                              |
| Exports                                                                                  | Both (main preferred with sub fallback by default), or exactly one stream when a quality is selected in the export dialog |
| Clip downloads                                                                           | Main; sub is used when no main recordings remain in the range (streams are never mixed in one file)           |
| Frames grabbed from a recording in History (download snapshot, submit frame to Frigate+) | Main preferred, sub fallback                                                                                  |
| Audio extraction (e.g., transcription)                                                   | Main preferred, sub fallback                                                                                  |
| Motion search                                                                            | Main only                                                                                                     |
| Review timeline motion data                                                              | Main only                                                                                                     |
| Storage usage statistics                                                                 | Both streams counted, and listed separately per camera                                                        |

This table covers only features that read recordings from disk. Tracked object snapshots and thumbnails (the images shown in Explore and sent with notifications, and the images submitted to Frigate+ from a tracked object) are captured live from the `detect` stream as the object is tracked, never from recordings, so sub stream recording does not affect them.

### Trade-offs

- Recording a second stream increases overall storage use. The increase is typically small relative to the main recordings, since the low quality stream is much smaller. Both streams are cached before being written to disk, so cache use goes up as well. See [the `/tmp/cache` area is separate](#the-tmpcache-area-is-separate) if you start seeing `No space left on device` errors after enabling it.
- The go2rtc transcode approach continuously encodes the low quality stream, which uses CPU or GPU resources. This cost only applies to the transcode path; recording the camera's native sub stream does not re-encode. See the [go2rtc hardware acceleration documentation](https://github.com/AlexxIT/go2rtc?tab=readme-ov-file#source-ffmpeg) for accelerating the transcode.
- Many camera sub streams do not include audio. If the source stream has no audio, the low quality recordings will not have audio.
- **Matching video codecs and audio settings between the two streams gives the smoothest playback.** When playback combines both qualities on one timeline (the default `Auto` behavior: for example original quality during events with low quality in between, or low quality history after the original recordings expire) and the streams use different video codecs or audio settings, for example H.265 on the main stream and H.264 on the sub stream, or 16 kHz audio on one and 8 kHz on the other, playback still works: Frigate inserts a decoder reset at each quality transition, which can cause a barely-perceptible pause there. Configuring both streams in the camera's firmware to use the same video codec, audio codec, and sample rate makes transitions fully seamless, and a mismatched audio sample rate can also be corrected with [sub stream output args](#sub-stream-output-args). If one stream has audio and the other does not, combined time ranges play **without audio**; selecting a single quality with the playback selector always keeps that stream's audio.

## Can I have "continuous" recordings, but only at certain times?

Using Frigate UI, Home Assistant, or MQTT, cameras can be automated to only record in certain situations or at certain times.

## How do I export recordings?

Footage can be exported from Frigate by right-clicking (desktop) or long pressing (mobile) on a review item in the Review pane or by clicking the Export button in the History view. Exported footage is then organized and searchable through the Export view, accessible from the main navigation bar.

### Custom export with FFmpeg arguments

For advanced use cases, the [custom export HTTP API](../integrations/api/export-recording-custom-export-custom-camera-name-start-start-time-end-end-time-post.api.mdx) lets you pass custom FFmpeg arguments when exporting a recording:

```
POST /export/custom/{camera_name}/start/{start_time}/end/{end_time}
```

The request body accepts `ffmpeg_input_args` and `ffmpeg_output_args` to control encoding, frame rate, filters, and other FFmpeg options. If neither is provided, Frigate defaults to time-lapse output settings (25x speed, 30 FPS) with audio removed (`-an`). When providing your own `ffmpeg_input_args`, include `-an` if you want audio stripped from the export.

The following example exports a time-lapse at 60x speed with 25 FPS:

```json
{
  "name": "Front Door Time-lapse",
  "ffmpeg_output_args": "-vf setpts=PTS/60 -r 25"
}
```

#### CPU fallback

If hardware acceleration is configured and the export fails (e.g., the GPU is unavailable), set `cpu_fallback: true` in the request body to automatically retry using software encoding.

```json
{
  "name": "My Export",
  "ffmpeg_output_args": "-c:v libx264 -crf 23",
  "cpu_fallback": true
}
```

:::note

Non-admin users are restricted from using FFmpeg arguments that can access the filesystem (e.g., `-filter_complex`, file paths, and protocol references). Admin users have full control over FFmpeg arguments.

:::

:::tip

When `hwaccel_args` is configured, hardware encoding is used for exports. This can be overridden per camera (e.g., when camera resolution exceeds hardware encoder limits) by setting a camera-level `hwaccel_args`. Using an unrecognized value or empty string falls back to software encoding (libx264).

:::

:::tip

To reduce output file size, add the FFmpeg parameter `-qp n` to `ffmpeg_output_args` (where `n` is the quantization parameter). Adjust the value to balance quality and file size for your scenario.

:::

## Apple Compatibility with H.265 Streams

Apple devices running the Safari browser may fail to playback h.265 recordings. The [apple compatibility option](../configuration/camera_specific.md#h265-cameras-via-safari) should be used to ensure seamless playback on Apple devices.

## Syncing Media Files With Disk

Media files (event snapshots, event thumbnails, review thumbnails, previews, exports, and recordings) can become orphaned when database entries are deleted but the corresponding files remain on disk.

Normal operation may leave small numbers of orphaned files until Frigate's scheduled cleanup, but crashes, configuration changes, or upgrades may cause more orphaned files that Frigate does not clean up. This feature checks the file system for media files and removes any that are not referenced in the database.

The Maintenance pane in the Frigate UI or an API endpoint `POST /api/media/sync` can be used to trigger a media sync. When using the API, a job ID is returned and the operation continues on the server. Status can be checked with the `/api/media/sync/status/{job_id}` endpoint. Results include the disk space reclaimed, or with `dry_run: true`, the space that would be reclaimed.

Setting `verbose: true` writes a detailed report of every orphaned file and database entry to `/config/media_sync/<job_id>.txt`. For recordings, the report separates orphaned database entries (DB records whose files are missing from disk) from orphaned files (files on disk with no corresponding database record).

:::warning

This operation uses considerable CPU resources and includes a safety threshold that aborts if more than 50% of files would be deleted. Only run when necessary. If you set `force: true` the safety threshold will be bypassed; do not use `force` unless you are certain the deletions are intended.

:::

## Understanding storage usage

The storage usage Frigate reports will not exactly match what the operating system reports with `df` or `du`. This is expected, not a bug. The sections below explain how Frigate derives its storage figures and why they differ from the disk's own accounting.

### How Frigate measures recording usage

The **Recordings** value on the Storage Metrics page (<NavPath path="Health and Metrics > Storage" />), and the per-camera **Camera Storage** breakdown, is the sum of the recording segment sizes Frigate has written, taken from Frigate's database. It is **not** computed by a scan of the disk. Frigate tracks usage this way by design: repeatedly walking the entire drive to total its size would keep hard drives spun up and add unnecessary I/O.

The disk **total** shown beside it, and the free-space figure Frigate uses to decide when to delete recordings, instead come from the operating system's report for the whole filesystem mounted at `/media/frigate`. As a result, the **Unused** value on the page is _total disk capacity minus Frigate's recordings_, not the drive's real free space, which will be lower whenever anything else is stored on the disk.

### What counts toward usage, and why it won't match `df`

Only **recording segments** (`/media/frigate/recordings`) are included in the recordings storage total. Plenty of other things consume real disk space but are **not** part of that number:

- **Snapshots and thumbnails** (`/media/frigate/clips`): see [Snapshots](/configuration/snapshots). These are retained independently of recordings.
- **Preview videos** and **review thumbnails** (also under `/media/frigate/clips`).
- **Exports** (`/media/frigate/exports`): exports are never removed by retention.
- **The database, downloaded detection models, and face / license plate training images** (stored under `/config`).
- **Debug images from enrichments** (`/media/frigate/clips`): when enabled, License Plate Recognition's `debug_save_plates` and GenAI's `debug_save_thumbnails` save plate crops and request images for troubleshooting.

These files are the usual explanation for an "other" or seemingly unaccounted bucket of space: it is real, it is Frigate's, and it simply isn't part of the _recordings_ total. They are also why comparing the **Recordings** figure to `df -h` always shows a gap: `df` additionally counts any non-Frigate data on the disk, filesystem overhead and reserved blocks (ext4 reserves ~5% for root by default, so a disk can read "full" before recordings approach the total), and recently deleted recordings whose space has not yet been reclaimed.

:::tip

The Storage page is not intended to be a system-wide disk monitor: it shows how much space _Frigate's recordings_ use. To see true disk usage, use `df -h` (free space) and `du -sh` (per-directory usage) on the host.

:::

### Free space and the `/media/frigate` mount

Frigate reports the capacity and free space of whatever filesystem is actually mounted at `/media/frigate` **inside the container**. If an external drive or network share isn't truly mounted there (a missing `/etc/fstab` entry, a share that was offline when the container started, or a host that doesn't pass the path through), the container falls back to the host's OS disk, and Frigate will correctly report that smaller disk instead of the drive you intended.

If the reported capacity doesn't match your drive, the mount is the place to look, not Frigate. Verify what is actually mounted from inside the container:

```bash
docker exec -it frigate df -h /media/frigate
docker exec -it frigate mount | grep media
```

See the [storage mount layout](/frigate/installation#storage) for how the volumes are expected to be configured.

### The `/tmp/cache` area is separate

Recording segments are first written to `/tmp/cache`, a small, in-memory (`tmpfs`) area, before being checked and moved to `/media/frigate/recordings`. Because it is separate and small, `/tmp/cache` can fill up and produce `No space left on device` errors even when the recordings disk has plenty of room. They are different storage areas. See [Recordings troubleshooting](/troubleshooting/recordings) for diagnosing cache and slow-storage issues.

### When the metrics don't match what's on disk

Because usage is tracked in the database, deleting recording files directly on disk, or files left behind after an upgrade, will not update the reported usage, and can even push it above 100%. Frigate is unaware of files it didn't record and won't count or remove them automatically. Use [Syncing Media Files With Disk](#syncing-media-files-with-disk) to reconcile the database with what is actually on disk.

## Will Frigate delete old recordings if my storage runs out?

Yes. Frigate continuously checks the **free space of the disk** holding `/media/frigate/recordings`. This is different from adding up the size of every recording: free space is a single number the operating system already tracks, so Frigate can ask for it instantly without reading through your files or spinning up the disk, which is exactly why it relies on this check rather than scanning the drive. When less than roughly one hour of recording space remains (estimated from the current recording bitrate, **not** a fixed percentage), Frigate deletes the oldest recordings to reclaim space and logs a message. This emergency cleanup removes the oldest recordings first **regardless of retention settings**.

Two consequences follow from this being based on whole-disk free space:

- Because the check uses the disk's real free space, **anything** filling the drive, including non-Frigate files, can trigger deletion of your oldest recordings.
- Cleanup can run while a meaningful percentage of the disk is still free (for example, with high bitrates or many cameras), because the threshold is "less than ~1 hour of recording headroom," not "X% full."

Frequent emergency cleanups usually mean your configured retention exceeds what the disk can hold. Reduce your retention days so the normal retention cleanup keeps up and the emergency path rarely triggers.

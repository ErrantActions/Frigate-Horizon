---
id: compressed_motion
title: Compressed-motion shadow prototype
---

## Current deployment: native Reolink HTTP detection

The private experimental installation now uses image
`investigation43-derived-mse-startup` (image39 backend with static-only UI updates):
camera-side person, vehicle, and animal
alarms are authoritative, while one native RTSP recording feed remains
stream-copy. There are no continuous compressed-vector learners. All nine
camera video decoders are verified off. North's bounded 15-minute
original-detector timing reference completed and stopped its decoder
automatically; all API sources and native recordings remained healthy.
That completed automatic startup exception is now disabled, including on
routine maintenance restarts.

Native camera events follow configured Review labels rather than the
historical manual-API default-to-alert fallback. Under the current installation's
person/car alert labels and unrestricted detection labels, broad `vehicle` and
`animal` alarms appear in **Review > Detections**, while `person` remains an
Alert. A broad camera vehicle classification is not silently renamed to car.
Camera-wide alarms cannot establish required Frigate zone entry, so native
categorization does not claim those zones were satisfied. Ordinary manual
API and ONVIF event fallback behavior is unchanged.

Event/Review items retain `type: reolink` and unknown confidence. They are not
Frigate object tracks: there are no camera-provided boxes, object identities,
zones, or stationary classifications in the tested state API. `vehicle` and
`animal` remain broad camera classifications rather than fabricated narrower
labels. Camera-side sensitivity/masks remain camera settings; the connector
does not write them.

The image activation explicitly lists the nine qualified camera identities.
Other/new cameras retain the original Frigate fallback until explicitly
qualified and selected. API failures are logged and surfaced as stale/error
status; they never silently mean quiet or start continuous decoding. Native
status is included under `native_detection` in the existing authorized
camera preview-status GET response. Camera snapshots are optional JPEG stills
fetched at class-alarm onset, not decoded video frames.

Native API polling, credentials, event expiry, and Review operation were
tested separately from physical-event recall. Real vehicle alarm episodes
were saved with linked Review items. A real alarm's thumbnail and native
3840x2160 H.264 video clip were retrieved successfully. North's reference
completed but lacked enough matched object episodes, so no timing
recommendations were applied. This is not a labeled semantic-recall study.

## Native Reolink implementation details

The architecture uses one native stream-copy RTSP recording feed
and camera-side HTTP alarm states rather than routine Frigate video decoding.
Actual RLC-810A firmware probes support `GetMdState` and `GetAiState` for
person, vehicle, and animal alarms. Those are camera-level boolean states,
not tracked objects, bounding boxes, zones, or confidence estimates.
`GetEvents` was unsupported on the tested firmware.

The source implementation owns one HTTP session per explicitly selected
camera, retries an invalid session once, marks transport failures explicitly,
and bounds event lifetimes independently in the consumer. Event metadata uses
`type: reolink` and NULL confidence. Polling is nominally every half-second
plus request latency; events shorter than a polling cycle can be missed and
must not be described as lossless camera push notifications.

Optional event thumbnails use a camera-produced JPEG fetched at alarm onset.
Only that still image is decoded/resized for a thumbnail; this does not require
continuous video decoding. Failure to obtain an image is logged, and the event
does not falsely claim to have a snapshot. Native video recording stays
stream-copy. The original North detector is reserved for a bounded timing
reference and occasional explicit verification, not routine detection.
Non-selected cameras retain their original Frigate path.

## Camera Detection Reports

Administrators can open **System > Detection Reports**
(`/system#detectionReports`) to see each allowed camera's detection source,
native API health, last successful report observed by the page, report age,
current class states, recent state transitions, and worker-session alarm counts.
For fallback cameras, the page shows Frigate status and inference regions per
second rather than calling those individual object detections.

Recording progress is independent: it uses the latest completed recording
segment, not decoded video FPS. A fresh segment does not prove gap-free
historical coverage. The page also shows video decoder state from fresh cached
statistics. Disabled cameras remain visible but are not polled.

Native reports become stale after five seconds; completed recording segments
older than 60 seconds are stale, and cached decoder statistics older than
45 seconds are not presented as current. Missing or invalid server time makes
age Unknown rather than trusting the browser clock. Unavailable or stale
alarm states are never presented as quiet.

Alarm counts reset with the native worker session and do not represent unique
objects. Generic motion is a timing signal, not a person/vehicle classification.
The page reads existing cached status and recording metadata only; opening it
does not start video decoding, inference, camera snapshots, or preview streams.
Camera metadata polling pauses when the page is hidden and stops when leaving
the tab.

## Adaptive live viewing

Live viewing now has main-source 720p and 1080p renditions, with a hard 720p
automatic floor and no camera substream selection. The grid matches its
displayed size and adapts using media delivery and browser frame drops.
Expanding a camera can select a higher-quality live rendition or original
Main. These streams are independent of continuous native 4K recordings.

The deployed profiles use API-allowlisted CPU decoding and encoding only
while viewers need live video. They do not enable Frigate object inference or
the original camera detection decoders. Source-validation restrictions were
not relaxed to enable custom GPU presets. Original recordings remain native
3840x2160 H.264, verified on actual completed files for all nine cameras.

A hideable badge shows the actual live video dimensions, not recording
resolution. Use **Settings > UI > Show Live Resolution** to restore badges
after hiding them. Current viewing uses HLS-only playback renditions; the
historical Main bypass and streaming-technology choices have been removed.

## Historical deployment: original detection restored

The private installation is currently running the original decoded capture,
pixel-motion, and object-detection pipeline on all nine enabled cameras using
`investigation37-original-detection`. Native 4K recordings remain unchanged.
This is the operator-authorized fallback, not an accepted compressed or
Reolink-gated resource-saving detector. Continuous vector learners are stopped.

Reolink native-event subscriptions were tested as an alternative trigger.
The camera's subscription URL requires a bounded query parameter; subscription
bindings now accept it without relaxing camera-host or authentication guards.
A temporary North reference ran successfully, but a longer passive timing
trial was invalidated by an event subscription interruption. No learned timers
were applied, and original detection stayed running.

The admin/camera-scoped `/{camera_name}/timing-calibration` API supports
GET status, POST with a 30-900 second duration, and DELETE cancellation. Only
the image-authorized North camera can be selected. The current image's tuner
is passive: ending or cancelling the timer never disables production
detection. Initialization messages are not motion transitions, ambiguous
episodes are excluded, and at least five independent closed episodes are
required before recommending timers. Failed runs produce no recommendations.

## Historical recording-only experiment

Earlier private experimental images ran native stream-copy recordings and
one independent compressed learner per enabled camera. Legacy continuous
decoding and object inference were intentionally paused. This was not a
motion-gated object detector. Ordinary Frigate installations do not enable that
mode.

In this mode, zero decoded camera FPS is intentional. It must not generate a
global camera-offline warning merely because decoding is disabled. The live
player and global status use the same capture-mode-aware check; ordinary
decoded-capture failures still produce offline warnings.

Earlier experimental image `investigation34-noise-leases` deployed the cancellation described
below. All nine camera learners were verified running independently with ready
calibration. Live vector-activity spot checks fell by approximately 76 to
96 percent after calibration. This does not establish labeled noise rejection,
object recall, or savings with object inference restored.

### Fast compressed noise cancellation

The implementation replaces the preview's long novelty hold with
per-frame residual cancellation. Each spatial cell needs 120 seconds of usable
quiet compressed observations before its baseline can suppress anything.
Events pause this per-cell budget instead of erasing prior quiet time. Quiet here
means no large or unfamiliar compressed activity, not proof that no object is
present. A robust median plus three scaled median absolute deviations estimates
the background independently for vector activity and coded-block activity.
Statistics are sampled at most twice per second into a bounded 240-sample RAM
history; change detection examines every incoming frame.

After calibration, normal noise is subtracted immediately. A large or unfamiliar
change passes on its first reliable predictive frame. The signal returns to
baseline on the first normal frame, with no 20-frame confirmation or 20-second
output hold. A separate half-second learning guard protects affected cells and
their neighbors, without prolonging the output. Protected cells retain their
previous baseline even during a long event.

Cell occupancy of 0.25 or higher always passes raw and cannot train itself into
quiet background. Learned tolerance is capped at 0.25. These are normalized
compressed cell-area units, not object size, speed, or residual energy. Existing
minimum vector-distance filtering still applies before the estimator.
Uncertain evidence remains explicitly wake-required; it is never certified as
quiet. Source gaps, time reversal, and geometry changes discard calibration.
Ordinary keyframes do not erase learning and do not count toward quiet time.

Custom-filter automatic previews have a three-minute hard lifetime so a quiet
source can calibrate and still be inspected. They retain a 20-second viewer
idle lease. Manual previews remain limited to two minutes. Default automatic
previews share the camera's background learner instead of starting a new model.

This implementation affects the compressed inspector and per-camera learners,
not object inference or the native decode gate. Its synthetic response tests do
not establish person/vehicle recall. Both event coverage and a separate guarded
object-detection integration are required before it can control decoding.

## Historical native capture trials, not an accepted resource-saving gate

The original shadow prototype analyzes H.264 motion-vector side data and
simulates a fail-open decode policy. A separate experimental native capture
implementation can now read RTSP, buffer encoded packets, and supply selected
native 4K frames to a GPU renderer. It requires explicit experimental-image
activation; it is not installed or enabled by ordinary Frigate images.

Two guarded all-camera trials did not pass the required live pipeline checks
and rolled back. A successful native resource-saving changeover has **not**
been demonstrated. Do not activate this implementation merely because the
isolated extractor or synthetic tests pass.

## Separate selectable systems

The source configuration distinguishes:

```yaml
motion:
  backend: pixel # Existing system, default
```

The experimental alternative is `backend: compressed`. It requires a build
containing the native extractor and explicit operator activation, main-source
3840x2160 capture buffers, and a disabled intermediate dynamic analysis copy.
Changing the backend requires a restart; this is not a live stream hot-swap.
Selecting compressed in an ordinary image raises an explicit error rather
than pretending an unavailable backend is running.

The choice does not merge the existing pixel-motion tuner's histories into
the compressed system. No new backend selector is exposed on the existing
Motion Tuner page yet; the configuration/schema choice is source-only until a
passing experimental build is available.

### Compressed-data pattern tuner

The new tuner learns from **motion vectors and coded-block activity only**,
not decoded image pixels. Each spatial cell keeps independent bounded
statistics and short repeating-pattern windows. A longer RAM history (up to
300 samples at 5 Hz, approximately 60 seconds) also compares multi-second gust
patterns at several time offsets. Continuous movement can earn observation
time without having to stop and restart.

Learning has a five-minute minimum observation period. Tolerance increases
gradually and remains capped at4/255 in normalized compressed-score units.
New spatial activity, a stronger unfamiliar pattern, global coded changes,
source discontinuities, and tracked objects prevent use of the learned offsets.
Periodic probes restore the untuned baseline. The tuner does not classify an
area as a tree, shadow, or person, and familiarity is not proof that suppression
is safe. Its initial bounded history is RAM-only and resets on restart.

Decoded checks remain separate safety inputs for view alignment and object
holds, not learning features. With the compressed tuner selected, the old
pixel-motion flag does not itself control decode wake decisions. Pixel regions
are still used for the existing object-inference pipeline after frames have
been decoded. This is not a promise that all pixel processing can be eliminated.

The new tuner's source/tests pass, but it was **not enabled on the failed live
trials or deployed after rollback**. Labeled event coverage and measured
whole-system savings are still missing.

## Target: native 4K compressed-domain motion

The intended pipeline keeps the original encoded 4K stream for recording and
examines compressed motion evidence without reconstructing image planes. A
motion trigger, uncertainty or required safety probe would wake native 4K
decoding for object analysis. There is no intermediate 1080p video conversion.
Cropping a region from a decoded 4K frame and preparing the object model's
required tensor is a separate operation, not video transcoding.

Moving full reconstruction to the CPU does not meet this goal. Neither does
exporting vectors after ordinary decoding, even if the resulting pixels are
reused. The CPU frame-reuse alternative was abandoned before any live deployment.

`docker/compressed-motion/` contains an isolated, source-pinned FFmpeg H.264
experiment. It retains entropy parsing and motion state while bypassing
macroblock reconstruction and deblocking. On three native High/CABAC recordings,
all749 emitted frame records and734 vector-bearing frames exactly matched the
full reconstruction reference. The candidate reconstructed zero macroblocks
and performed zero deblocking. It still parses compressed syntax, consumes CPU,
and allocates decoder buffers.

The initial FFmpeg 7.0.2 proof was upgraded to source-pinned, signed FFmpeg
8.1.3 for live experiments. It is **not a general production FFmpeg replacement**.
Local-file mode accepts bounded recordings. Explicit RTSP entropy mode receives
the source privately on stdin, requires a timestamped IDR, and retains an
unambiguous decode-order guard. Neither mode reads/emits image planes during
entropy extraction. The source archive is checked against a fixed SHA256 and
FFmpeg's release signature. Production qualification still requires
verified compressed pre-roll and native 4K wake-up, residual/scene coverage,
and complete resource accounting. No live camera switch is supplied by this
ordinary shadow CLI; experimental capture integration is separately gated.

### Full-changeover outcome

The first all-camera native trial failed cold RTSP timestamp handling. The
second included explicit full-native-4K decoding fallback, but also failed its
real pipeline checks and automatically restored the repaired pixel baseline.
Side Yard East reported a CABAC bitstream error during the second trial.
Do not infer its entire failure was explained by that error: the required
aggregate processing check also failed, and a complete accepted performance
comparison was not obtained.

The fallback itself was tested in isolation on an actual camera:30native4K
frames at approximately4.93FPS, visible fallback status, and verified owned
process cleanup. Fallback is safety behavior, not evidence of compressed
motion savings.

With zero learned tolerance, three recorded samples produced no quiet decode
decisions even under best-case assumed view feedback: approximately99.6percent
of decisions requested full decoding. Adding an entropy parser to continuous
GPU decoding is not a resource saving. Further trials require both reliable
capture integration and safe evidence that pattern learning can reduce work.

### Native entropy-only measurements

The newer proof also exports conservative non-skipped-coded-block activity.
This covers possible changes that motion vectors alone do not describe, including
intra-coded blocks inside a predictive picture. It is **not residual energy,
pixel segmentation, or human-labeled physical-motion ground truth**.

Two alternating CPU-timing pairs on each original 4K recording, excluding vector
verification hashing:

| Native 4K sample | Full reconstruction CPU-s | Entropy-only CPU-s | CPU ratio |
| --- | ---: | ---: | ---: |
| Driveway | 6.110 | 2.821 | 0.462 |
| Side Yard East | 6.513 | 2.431 | 0.373 |
| Backyard | 6.041 | 2.865 | 0.474 |

These compare extractor implementations, not current GPU decoding, whole-server
CPU, energy use, or a complete live Frigate pipeline. Both arms export identical
vectors and coded-block grids. No source recording was resized or re-encoded.

### Native packet pre-roll and renderer

The source-only transport components keep H264 packets in a bounded IDR-aligned
RAM buffer, preserving original integer timestamps and native geometry. A
codec-free muxer changes packet/container framing only, preserving coded slices.
The on-demand renderer defaults to CUDA, outputs native I420 frames, and has no
spatial scaling filter. CPU rendering is an explicit reference-test option,
not an automatic live fallback.

A bounded recorded-source CUDA test exercised IDR probes and one synthetic
wake:49 of151 source packets were supplied to the GPU and11 selected frames
were emitted at3840x2160. Sparse-probe tests require an isolated keyframe to be
delivered without waiting for a following frame, and the real 4K replay checks
probe latency below one second. The wake schedule was synthetic, so this does
not establish motion-event recall or live whole-system savings. The production
pipeline has not been changed.

## Components

- `frigate.motion.compressed.vector_evidence`: backward-reference vector
  displacement and coarse area-weighted grid activity. Unavailable vectors,
  intra frames, sparse coverage, invalid vectors and global motion remain
  explicitly uncertain. This does not extract residual coefficients.
- `ShadowMotionPID`: independent RAM-only normalized-score tuning using the
  existing bounded adaptive PID mathematics. It never loads or overwrites the
  pixel-motion PID's learned state, changes a camera threshold, or persists
  experimental learning.
- `ShadowDecodeGate`: simulated decisions only. Unqualified extraction, missing
  signals, timestamp gaps/resets, unverified camera alignment and tracked objects
  keep decoding requested. The synthetic-qualified simulation includes periodic
  full-decode probes and a quiet hold period.
- `frigate.motion.compressed_shadow`: bounded local-file benchmark and replay.
  It measures extraction cost and compares vector activity to an adjacent-frame
  luminance-change proxy, not human-labeled event recall.

Motion vectors describe compression predictions, not guaranteed physical object
motion. They are not velocities unless reference timing is established. A grid
cell's score is a coarse block-area estimate, not pixel segmentation.

## Run an offline replay

Use an environment with the existing Frigate dependencies, PyAV, NumPy and OpenCV:

```text
python -m frigate.motion.compressed_shadow example.mp4 --seconds 10 --repeats 2 --report shadow-report.json
```

Only existing local files are accepted. The time span is limited to 1-120
seconds, with 1-3 repeats. Backend order alternates between repeats to reduce
order bias. Benchmarks must process matching frame counts. Presentation
timestamps are required; arrival time is not silently substituted.

The report includes source hash, software version, per-backend CPU/wall timings,
frame/vector counts, uncertainty, pixel-proxy disagreements, independent PID
identity and live-qualification blockers. Its input name/hash and numeric
metadata do not include video frames or stream credentials.

The reference proxy is deliberately limited. Shadows, wind or noise can trigger
pixel changes, and vector/pixel disagreement does not establish a missed person
or vehicle. Zero disagreement on a short clip does not establish reliable recall.
The replay does not certify camera alignment, so PID learning remains paused.

## Independent PID safeguards

Learning requires both independently verified quiet pixels and verified camera
alignment. Missing observations do not earn quiet history, and time gaps,
backward time or geometry changes invalidate the local controller state.
Reliable tracked-object boxes protect their overlapping cells.

The experimental gains and score offsets are separate from pixel thresholds.
The initial implementation does not import live wind/time profile selection,
pixel masks, saved adaptation histories, or current tracker objects into an
active gate. Those integrations require their own validation before production.

## Measured feasibility, October 23, 2026

Two runs per backend on the same recorded main-stream segment from each camera:

| Camera sample | Normal H.264 decode CPU-s | Decode plus vector export CPU-s | Export without deblocking CPU-s |
| --- | ---: | ---: | ---: |
| Driveway | 4.957 | 6.049 | 5.236 |
| Side Yard East | 4.631 | 5.839 | 4.646 |
| Backyard | 4.580 | 6.308 | 4.692 |

These are median CPU costs for matching frame counts within the requested
ten-second bounds, not guarantees of complete ten-second segments. Samples
yielded 46, 31 and 44 replay observations respectively. The Backyard proxy had
one vector/pixel disagreement; Driveway and Side Yard East had none. Keyframes
or sparse coverage produced unknown observations.

All three available backends still use FFmpeg frame reconstruction. Disabling
deblocking is not an entropy-only parser. Ordinary vector export added CPU cost;
no-deblocking did not show a consistent saving in repeated tests. An initial
single-sample result appeared better, but was not reproducible enough to justify
activation.

Those original PyAV measurements did not qualify live gating. The newer
entropy-only proof above resolves the local-file reconstruction-cost blocker,
but live gating is still **not qualified**:

- No production-compatible, integrated live entropy-only backend.
- No demonstrated end-to-end resource saving.
- No sufficient human-labeled event set, night coverage or small/slow-object
  validation.
- No verified compressed pre-roll/keyframe handover.

The existing motion/detection/recording pipeline stays unchanged.

## RidgeRun and other extractors

[RidgeRun libmotion](https://developer.ridgerun.com/wiki/index.php/H.264_Motion_Vector_Extractor)
is conceptually relevant: it aims to extract vectors without full decoding.
However, RidgeRun marks the product deprecated and its
[documented implementation](https://developer.ridgerun.com/wiki/index.php/H.264_Motion_Vector_Extractor/H.264_Motion_Vector_Extractor_Basics)
targets H.264 Baseline. A recorded Reolink sample was independently verified
as profile-idc100 (High), with CABAC entropy coding in its PPS.

RidgeRun's [evaluation](https://developer.ridgerun.com/wiki/index.php/H.264_Motion_Vector_Extractor/Getting_Started/Evaluating_H.264_Motion_Vector_Extractor)
must be requested from the vendor and is documented as limited to five minutes.
No compatible licensed binary or source was provided or installed. Its newer
GPU motion detector is a different product, not proof of an x86 High/CABAC
compressed-domain backend.

The inspected [mv-extractor implementation](https://github.com/LukasBommes/mv-extractor/blob/7d8860e7d276af6e6ed14b586995645cd6e7858d/src/mvextractor/video_cap.cpp)
places `decode_frames` around color conversion/output; its decoder path remains.
Do not infer absence of H.264 reconstruction from the README's "skip decoding"
wording without checking the implementation and measuring it.

No camera firmware or encoding profile was changed to fit an extractor.

## Gate qualification is necessary, not a guarantee

The policy requires a genuinely cheaper backend, at least fifty independently
labeled events with no misses in that set, day/night and small/slow-object
coverage, and verified compressed pre-roll. Its resource-ratio guard requires
measured cost at most half the chosen reference cost.

These checks are only minimum acceptance evidence. A finite test set does not
prove no future event can be missed. A production implementation must also
validate live resource totals, timestamp alignment, stream recovery, existing
masks, wind/time histories, tracker holds, stationary checks, and recording
continuity. There is deliberately no `--apply` or live-enable option.

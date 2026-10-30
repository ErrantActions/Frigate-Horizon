# Synthetic live HLS fixtures

These are generated test patterns and a sine-wave audio track, not camera
footage. Both levels share a 40-second timeline with two-second keyframe
boundaries. The low level is 1280x720 at 200 kbps and the high level is
1920x1080 at 650 kbps, with mono AAC at 32 kbps.

Generated with the project's FFmpeg 8.0 using `testsrc2`, `sine`, a two-way
video split, libx264 ultrafast/zerolatency, fixed 30-frame GOPs at 15 fps, and
the fMP4 HLS muxer. The test HTTP server maps the generated `.m4s` segment
names to the proxy's `.mp4` resource convention without changing media bytes.

The browser regression uses actual HTTP media transfers and Chromium network
throttling. It does not infer available bandwidth from a real-time WebSocket
or use delayed mocked response bodies as a bandwidth substitute.

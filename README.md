# Frigate Horizon

A modified Frigate NVR source tree, distributed under the original MIT license.
Not an official upstream Frigate release.

Images are built from source, tested on isolated Linux, and published by commit
only after the image checks pass. Use the resulting digest in a TrueNAS Custom App.
A built image is not production recording acceptance.

See [build and installation instructions](docs/docs/installation/horizon-image-builds.md).
Keep all camera configuration, databases, provider keys and credentials outside Git.
Export protection remains unavailable until protection-safe recovery is verified.

Original project: https://github.com/blakeblackshear/frigate

## Resource usage

System process and camera CPU charts show a share of Frigate's available logical
CPU capacity, honoring affinity and visible cgroup v1/v2 quotas. For example,
53% on the traditional one-core scale means 0.53 logical CPU equivalents, or
0.83% of 64 available logical CPUs. Host CPU in the status bar covers the host,
including other workloads. Raw API `cpu`/`cpu_average` readings retain their
one-core basis for compatibility; the additive `resources` field identifies
capacity and scope. Existing per-core health-notice thresholds remain unchanged.

Process memory is RSS, not virtual address space, with absolute resident bytes
and a percentage of the effective host/container memory capacity. Shared pages
can appear in more than one process, so RSS values must not be summed as total
memory consumption. GPU graphs report independent device-wide engines, not
Frigate-only usage; their percentages must not be added or CPU-normalized.

Storage uses binary MiB/GiB/TiB and distinguishes filesystem capacity from
recording budgets. Recording growth is estimated file usage, not network
traffic. Player video rates use decimal Mb/s; HLS bandwidth estimates are
labeled separately from received WebRTC video. Missing, invalid, or stale
samples are unavailable, not fabricated zero measurements.

Prometheus keeps the legacy per-core CPU metric and adds
`frigate_process_cpu_capacity_percent`, `frigate_process_cpu_cores`,
`frigate_process_resident_memory_bytes`, and CPU/RAM capacity gauges.
`frigate_host_cpu_usage_percent` is explicitly host-wide. Storage byte metrics
convert their source MiB values using 1,048,576 bytes per MiB.
The legacy nethogs `kBps` metric uses KiB/s; the additive
`frigate_bandwidth_bytes_per_second` metric exposes its unambiguous byte rate.

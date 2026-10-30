---
id: horizon-image-builds
title: Horizon source images
---

# Source build and native TrueNAS App

Horizon is a modified Frigate fork. Its image is built from checked-in source,
not copied from an existing recorder. The original Frigate MIT license and
go2rtc's MIT license must remain with their respective code and artifacts.

## Publishing

The `Horizon source image` workflow runs on the authorized repository's `main`
branch. Its actions are pinned to immutable commits. Checkout credentials are
not persisted. Registry authentication occurs only after the actual image passes
isolated Linux tests, synthetic owned-media tests, and an empty authenticated
startup check. No real camera, microphone, production database or APNs provider
is used.

Use **Actions > Horizon source image > Run workflow** and enable **cold_build**
to ignore image-layer cache and perform a full build. Manual runs default to
using cache, just like normal pushes. Both paths build the pinned go2rtc addon
from its reviewed source inputs (or reuse their matching cached result) and include
its license and build receipt. A cold build is intentionally slower.

Only a successful test run publishes
`ghcr.io/errantactions/frigate-horizon:sha-<full-commit>`. A rerun can replace
that tag; the content digest, not the tag, is immutable. Download the
`horizon-image-receipt` artifact and use its **digest reference**, not a mutable
tag, for installation. The workflow does not change package visibility, deploy
an NVR, or attest to production acceptance. If a hosted runner runs out of disk
or time, the failed run must not publish; use a larger authorized runner or
adjust the build infrastructure rather than skipping tests.

Configuration, databases, private keys, provider credentials, camera URLs and
operational receipts belong outside Git and the image. Source-path checks and
secret scanning are required before publication. A successful scan is not a
general security certification.

Linux build scripts and service entrypoints must retain their executable Git
modes, including when preparing source on Windows. The source check rejects
lost script modes before starting the image build.

## TrueNAS Custom App

Use a **Custom App**, which TrueNAS manages as Docker containers, rather than a
separate system container or a stock catalog image. Set the image to the exact
receipt digest and preserve persistent datasets across image updates.

- Use separate host paths for `/config` and `/media/frigate`. Do not mount an
  unrelated dataset or an existing recorder's writable database into a test App.
- Map only the required Coral character devices, normally `/dev/apex_0` and
  `/dev/apex_1`, and select the intended NVIDIA GPU. GPU access can be shared with
  compatible apps; resource contention and Pascal application compatibility
  still apply.
- PCIe Coral kernel modules must match the running TrueNAS kernel. Older NVIDIA
  Pascal cards require a compatible proprietary driver, not NVIDIA's open
  kernel module. Community system extensions require explicit operator approval,
  a tested recovery path and kernel-upgrade compatibility checks.
- Do not use privileged mode merely to avoid device or permission setup.
- Keep authentication enabled. Bind internal unauthenticated API and go2rtc
  control endpoints to loopback. Expose only the intended authenticated service.
- A fixed service IP requires a verified free address on the correct VLAN.
  Keep the old recorder stopped before reusing its address. Do not change the
  TrueNAS management address to the NVR's address.
- Keep provider keys in private read-only runtime mounts. Never put registry
  passwords, camera credentials or keys into a public Compose file. If GHCR
  requires authentication, configure it through TrueNAS's private registry
  credential settings.

Start with an empty camera configuration and a fresh test database when original
state is unavailable. This verifies installation, not recording. A fresh database
requires new accounts and notification registrations; existing footage is not
automatically indexed. Preserve original footage separately.

Stop the App before restoring an authorized configuration/database backup, check
schema compatibility, and snapshot the new state first. Do not overwrite newer
recording metadata with an older backup or attach the database to an arbitrary
older image.

## Admission and recovery gates

Export-protection admission remains **off** by default. Capability discovery
stays false and direct protection requests return 503 until an immutable tested
protection-compatible fallback or equivalent verified recovery exists. All
deletion guards stay active for existing protected assets regardless of admission.

After configuration, verify recording progress for every intended camera,
detector availability, current account/camera authorization and unchanged
notification rules. Do not enable alerts or send real pushes as an installation
test. Keep the prior image and consistent backups, and use only a
schema/protection-compatible rollback. Registry images are separate source
candidates, not proof of equivalence to a recovered production image.

## Routine updates

1. Push reviewed source to this repository's `main` branch, or run the workflow
   manually with `cold_build` left disabled.
2. Wait for the image build, Linux checks, owned-media checks and authenticated
   startup gate to pass. A green build is not production or native-provider readiness.
3. Use the resulting receipt's immutable digest. Update only the image reference
   in the managed TrueNAS App, preserving its current configuration/database,
   NFS mounts, network interfaces, GPUs and resource settings.
4. Schedule the App restart and verify fresh recording progress afterward.
   Never restore an older source snapshot or database just to change an image.

The initial source build can be much slower than an incremental build. The
workflow imports and exports its dedicated BuildKit layer cache. Dependency
manifests precede frontend source in the Dockerfile, so ordinary UI edits do not
reinstall frontend dependencies or rebuild unchanged native prerequisites.
Caches can expire or be evicted; leave cold rebuilding available as an explicit
diagnostic and reproducibility check. Cache reuse never skips image tests.

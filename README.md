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

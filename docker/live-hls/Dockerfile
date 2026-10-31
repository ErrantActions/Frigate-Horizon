# syntax=docker/dockerfile:1.6

FROM --platform=${BUILDPLATFORM} golang:1.27.0-bookworm AS builder
ARG TARGETARCH
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
RUN git init mediamtx \
    && git -C mediamtx remote add origin https://github.com/bluenviron/mediamtx.git \
    && git -C mediamtx fetch --depth=1 origin tag v1.21.3 \
    && git -C mediamtx checkout --detach v1.21.3 \
    && test "$(git -C mediamtx rev-parse HEAD)" = 9540cb6c1d9f1e9351fcc4ded3afc249ffff22bc
COPY tools/build_live_hls_packager.py /build/frigate/tools/build_live_hls_packager.py
COPY docker/live-hls/shared-clock/ /build/frigate/docker/live-hls/shared-clock/
RUN python3 /build/frigate/tools/build_live_hls_packager.py \
    --source /build/mediamtx --output /build/output --arch "${TARGETARCH}"

FROM builder AS tested
ARG TARGETARCH
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY tools/test_live_hls_packager.py /build/test_live_hls_packager.py
RUN --network=none python3 /build/test_live_hls_packager.py \
    --binary "/build/output/mediamtx-horizon-clock-${TARGETARCH}" \
    --ffmpeg /usr/bin/ffmpeg

FROM scratch AS live-hls
ARG TARGETARCH
COPY --from=tested /build/output/mediamtx-horizon-clock-${TARGETARCH} /mediamtx
COPY --from=tested /build/output/mediamtx-horizon-clock-${TARGETARCH}.json /usr/local/share/live-hls/shared-clock.json
COPY --from=tested /build/output/licenses/ /usr/local/share/licenses/mediamtx/
COPY --from=tested /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
ENV HORIZON_HLS_SHARED_CLOCK=1
USER 1000:1000
ENTRYPOINT ["/mediamtx"]
CMD ["/run/live-hls/mediamtx.yml"]

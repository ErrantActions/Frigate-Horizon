# syntax=docker/dockerfile:1.6@sha256:ac85f380a63b13dfcefa89046420e1781752bab202122f8f50032edf31be0021

ARG RUNTIME_IMAGE=frigate-ome-runtime:local
FROM ${RUNTIME_IMAGE}

USER root
RUN apt-get update -qq \
    && apt-get -o Dpkg::Options::=--force-unsafe-io install -y --no-install-recommends \
        python3 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY tools/test_ovenmediaengine_runtime.py /opt/ovenmediaengine/qualification.py
USER 1000:1000
WORKDIR /dev/shm
ENTRYPOINT ["python3", "/opt/ovenmediaengine/qualification.py"]

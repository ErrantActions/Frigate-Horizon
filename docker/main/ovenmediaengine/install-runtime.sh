#!/bin/sh
set -eu

# These are Debian's runtime packages, not replacements for Frigate's FFmpeg.
apt-get update -qq
apt-get -o Dpkg::Options::=--force-unsafe-io install -y --no-install-recommends \
    coturn libgomp1 libstdc++6 libuuid1 zlib1g libbz2-1.0 liblzma5
rm -rf /var/lib/apt/lists/*

mkdir -p /usr/share/frigate-ovenmediaengine
dpkg-query -W -f='${Package}\t${Version}\t${Architecture}\n' \
    coturn libgomp1 libstdc++6 libuuid1 zlib1g libbz2-1.0 liblzma5 \
    > /usr/share/frigate-ovenmediaengine/runtime-packages.tsv
cp /usr/share/doc/coturn/copyright \
    /usr/share/frigate-ovenmediaengine/coturn-copyright

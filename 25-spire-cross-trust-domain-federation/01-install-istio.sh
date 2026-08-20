#!/usr/bin/env bash
# Installs Istio demo profile on both clusters — plain standalone installs,
# no multi-primary/east-west wiring (these two clusters run different Istio
# minor versions and are never meant to join the same mesh; see README
# "跟其他目錄的關係" for why that's out of scope here).
#
# cluster1's istioctl-1.13.5 comes from ../istio_bin.tar.gz (already
# shipped in this repo). istioctl-1.29.6 is NOT in that tarball — this
# script downloads it into ../istio_bin/ the first time it's needed.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

if [ -d "../istio_bin" ]; then
  echo "✅ istio_bin already exists, skipping tar extraction"
else
  echo "🔽 Extracting istio_bin"
  tar -xzvf "../istio_bin.tar.gz"
  mv ./istio_bin ../
fi

if [ ! -f "../istio_bin/istioctl-1.29.6" ]; then
  echo "🔽 Downloading istioctl 1.29.6"
  tmp="$(mktemp -d)"
  curl -L "https://github.com/istio/istio/releases/download/1.29.6/istio-1.29.6-linux-amd64.tar.gz" \
    -o "$tmp/istio.tar.gz"
  tar -xzf "$tmp/istio.tar.gz" -C "$tmp"
  cp "$tmp"/istio-1.29.6/bin/istioctl ../istio_bin/istioctl-1.29.6
  rm -rf "$tmp"
fi

echo "🚀 Installing Istio 1.13.5 on cluster1 (k8s v1.24.17)"
../istio_bin/istioctl-1.13.5 install --context=cluster1 --set profile=demo -y

echo "🚀 Installing Istio 1.29.6 on cluster1-134 (k8s v1.34.8)"
../istio_bin/istioctl-1.29.6 install --context=cluster1-134 --set profile=demo -y

echo "✅ Done"

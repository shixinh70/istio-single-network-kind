#!/usr/bin/env bash
# Creates the two clusters this scenario needs:
#   cluster1      k8s v1.24.17  (paired with Istio 1.13.5)
#   cluster1-134  k8s v1.34.8   (paired with Istio 1.29.6)
#
# cluster1 uses the same kind config style as ../../01-kind/cluster1.yaml
# and needs no special handling.
#
# cluster1-134 needs kind >=0.32.0 (the release that published prebuilt
# v1.34.8 node images) AND, if you're running inside a nested-container
# host (cloud IDE/sandbox), a patched node image — see README.md "坑 1"
# and Dockerfile.node-1.34.8-runc1.1.14 in this directory. This script
# tries the stock image first and only falls back to the patched one if
# the plain create fails with the known runc /proc-mount error.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

KIND_0_26="${KIND_0_26:-kind}"       # kind binary/version used for cluster1 (matches repo's pinned 0.26.0)
KIND_0_32="${KIND_0_32:-kind}"       # kind binary/version used for cluster1-134 (needs >=0.32.0 for v1.34.8 images)
NODE_1_34_IMAGE="kindest/node:v1.34.8@sha256:02722c2dedddcfc00febf5d27fbeb9b7b2c14294c82109ff4a85d89ac9ba3256"
NODE_1_34_PATCHED_TAG="kindest/node:v1.34.8-runc1.1.14"

echo "🚀 Creating cluster1 (k8s v1.24.17)"
"${KIND_0_26}" create cluster --config cluster1.yaml --name cluster1 --image kindest/node:v1.24.17

echo "🚀 Creating cluster1-134 (k8s v1.34.8)"
if ! "${KIND_0_32}" create cluster --config cluster1-134.yaml --name cluster1-134 --image "${NODE_1_34_IMAGE}"; then
  echo "⚠️  Plain create failed (likely the nested-container runc /proc bug — see README 坑 1)."
  echo "🔧 Building runc-patched node image and retrying..."
  curl -Lo runc.amd64 https://github.com/opencontainers/runc/releases/download/v1.1.14/runc.amd64
  chmod +x runc.amd64
  docker build -f Dockerfile.node-1.34.8-runc1.1.14 -t "${NODE_1_34_PATCHED_TAG}" .
  "${KIND_0_32}" create cluster --config cluster1-134.yaml --name cluster1-134 --image "${NODE_1_34_PATCHED_TAG}"
fi

kubectl config rename-context kind-cluster1 cluster1 2>/dev/null || true
kubectl config rename-context kind-cluster1-134 cluster1-134 2>/dev/null || true

echo "✅ Done. Contexts: cluster1, cluster1-134"

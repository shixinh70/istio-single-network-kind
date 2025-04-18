#!/usr/bin/env bash

set -o xtrace
set -o errexit
set -o nounset
set -o pipefail

OS="$(uname)"
NUM_CLUSTERS="${NUM_CLUSTERS:-2}"

for i in $(seq "${NUM_CLUSTERS}"); do

  echo "Starting set clusterLocal=true in cluster${i}"
  "../istio_bin/istioctl-1.13.5" install --force --context="cluster${i}" -f "cluster${i}-local.yaml" -y
done

#!/usr/bin/env bash
# Usage: ./gen_custom_bootstrap.sh <kube-context> <namespace> <pod> <out-file>
#
# Dumps a running (unmodified, istiod-CA) sidecar's own bootstrap and
# rewrites the "sds-grpc" static cluster's UDS path from istio-agent's own
# local SDS server (./etc/istio/proxy/SDS) to the SPIRE Agent socket
# (/run/secrets/workload-spiffe-uds/socket, mounted by spiffe-csi-driver).
# Since every dynamic listener/cluster's SDS references already say
# cluster_name: "sds-grpc", swapping what that ONE cluster physically
# points at is enough to make Envoy fetch its "default"/"ROOTCA" secrets
# straight from SPIRE - no listener/filter-chain patching needed at all.
#
# This full-bootstrap-replace approach (via the customConfigFile ProxyConfig
# field / "-c <file>" envoy arg) was the only one of three attempts that
# actually worked on Istio 1.13.5. See README.md "安裝過程踩的坑" for why
# EnvoyFilter (CLUSTER/ADD, BOOTSTRAP/MERGE) and a plain bootstrapOverride
# merge (sidecar.istio.io/bootstrapOverride) both failed first.
set -euo pipefail

CTX="$1"; NS="$2"; POD="$3"; OUT="$4"

kubectl --context="$CTX" -n "$NS" exec "$POD" -c istio-proxy -- \
  pilot-agent request GET config_dump \
  | jq '.configs[] | select(."@type" | contains("BootstrapConfigDump")) | .bootstrap' \
  | jq '
      (.static_resources.clusters[]
       | select(.name == "sds-grpc")
       | .load_assignment.endpoints[0].lb_endpoints[0].endpoint.address.pipe.path)
      = "/run/secrets/workload-spiffe-uds/socket"
    ' \
  > "$OUT"

echo "wrote $OUT"

#!/usr/bin/env bash
# Usage: ./churn_test.sh <scope_label>
# Assumes the desired Sidecar CR is already applied and converged.
# Patches svc-1's port (a real spec change every proxy watching svc-1 must
# process), then polls until the client's Envoy cluster config reflects the
# new port, sampling istiod's and the client istio-proxy's cumulative CPU
# usage (via crictl stats, nanoseconds) throughout.
set -uo pipefail
ISTIOCTL=~/Desktop/istio-single-network-kind/istio_bin/istioctl-1.13.5
LABEL="$1"
cpu_ns_istiod() {
  kubectl --context=cluster2 -n istio-system exec istiod-5f85c6c8f7-krpdf -c discovery -- \
    sh -c "cat /sys/fs/cgroup/cpu,cpuacct/cpuacct.usage 2>/dev/null || cat /sys/fs/cgroup/cpuacct/cpuacct.usage 2>/dev/null"
}
cpu_ns_proxy() {
  kubectl --context=cluster2 -n local-ns exec client -c istio-proxy -- \
    sh -c "cat /sys/fs/cgroup/cpu,cpuacct/cpuacct.usage 2>/dev/null || cat /sys/fs/cgroup/cpuacct/cpuacct.usage 2>/dev/null"
}

# revert to port 80 first in case a previous run left it at 81, then confirm baseline propagated
kubectl --context=cluster2 -n remote-ns patch svc svc-1 --type=merge -p '{"spec":{"ports":[{"name":"http","port":80,"targetPort":80}]}}' >/dev/null
sleep 3

ISTIOD_CPU_0=$(cpu_ns_istiod)
PROXY_CPU_0=$(cpu_ns_proxy)
T0=$(date +%s.%N)

kubectl --context=cluster2 -n remote-ns patch svc svc-1 --type=merge -p '{"spec":{"ports":[{"name":"http","port":81,"targetPort":80}]}}' >/dev/null

FOUND=0
for i in $(seq 1 60); do
  PORT=$($ISTIOCTL --context cluster2 proxy-config cluster client.local-ns 2>/dev/null | grep -E '^svc-1\.remote-ns' | awk '{print $2}')
  if [ "$PORT" == "81" ]; then
    FOUND=1
    break
  fi
  sleep 0.25
done
T1=$(date +%s.%N)
PROPAGATE_S=$(echo "$T1 - $T0" | bc)

ISTIOD_CPU_1=$(cpu_ns_istiod)
PROXY_CPU_1=$(cpu_ns_proxy)

ISTIOD_DELTA_MS=$(echo "scale=2; ($ISTIOD_CPU_1 - $ISTIOD_CPU_0) / 1000000" | bc)
PROXY_DELTA_MS=$(echo "scale=2; ($PROXY_CPU_1 - $PROXY_CPU_0) / 1000000" | bc)

echo "${LABEL}: propagated=${FOUND} propagate_time=${PROPAGATE_S}s istiod_cpu_used=${ISTIOD_DELTA_MS}ms proxy_cpu_used=${PROXY_DELTA_MS}ms"
echo "${LABEL},${FOUND},${PROPAGATE_S},${ISTIOD_DELTA_MS},${PROXY_DELTA_MS}" | tee -a churn_compare.csv

# revert
kubectl --context=cluster2 -n remote-ns patch svc svc-1 --type=merge -p '{"spec":{"ports":[{"name":"http","port":80,"targetPort":80}]}}' >/dev/null

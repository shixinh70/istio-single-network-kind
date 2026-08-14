#!/usr/bin/env bash
set -uo pipefail
cd /home/xin/Desktop/istio-single-network-kind/16-sidecar-egress-hosts-scale
ISTIOCTL=~/Desktop/istio-single-network-kind/istio_bin/istioctl-1.13.5

for N in 10 50 200 500 1000 2000 4000 8000; do
  echo "=== N=$N: applying Sidecar CR ==="
  START=$(date +%s.%N)
  if ! kubectl --context=cluster2 apply -f manifests/sidecar-$N.yaml > /tmp/apply-$N.log 2>&1; then
    echo "N=$N APPLY FAILED"
    cat /tmp/apply-$N.log
    echo "apply_failed,$N,,,,,,," | tee -a results.csv
    continue
  fi
  END=$(date +%s.%N)
  APPLY_S=$(echo "$END - $START" | bc)
  echo "N=$N apply took ${APPLY_S}s"

  # wait for proxy to converge: poll cluster count until stable for 2 consecutive checks, timeout 60s
  PREV=-1
  STABLE=0
  for i in $(seq 1 30); do
    CUR=$($ISTIOCTL --context cluster2 proxy-config cluster client.local-ns 2>/dev/null | tail -n +2 | wc -l)
    if [ "$CUR" == "$PREV" ]; then
      STABLE=$((STABLE+1))
      if [ "$STABLE" -ge 2 ]; then break; fi
    else
      STABLE=0
    fi
    PREV=$CUR
    sleep 2
  done
  echo "N=$N converged at clusters=$CUR after ~$((i*2))s"

  ./measure.sh "N=$N" "$N"
done
echo "=== DONE ==="

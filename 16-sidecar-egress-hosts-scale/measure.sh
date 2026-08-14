#!/usr/bin/env bash
# Single-point measurement, adapted from 11-resource-memory-v3-largescale/measure.sh
# for cluster2 / local-ns / client.
set -uo pipefail
ISTIOCTL=~/Desktop/istio-single-network-kind/istio_bin/istioctl-1.13.5
LABEL="${1:-checkpoint}"
VAR="${2:-}"

C=$($ISTIOCTL --context cluster2 proxy-config cluster client.local-ns 2>/dev/null | tail -n +2 | wc -l)
L=$($ISTIOCTL --context cluster2 proxy-config listener client.local-ns 2>/dev/null | tail -n +2 | wc -l)
E=$($ISTIOCTL --context cluster2 proxy-config endpoint client.local-ns 2>/dev/null | tail -n +2 | wc -l)
R=$($ISTIOCTL --context cluster2 proxy-config route client.local-ns 2>/dev/null | tail -n +2 | wc -l)
ALLOC=$(kubectl --context cluster2 exec client -n local-ns -c istio-proxy -- pilot-agent request GET memory 2>/dev/null | grep -o '"allocated": *"[0-9]*"' | grep -o '[0-9]*')
CFG=$(kubectl --context cluster2 exec client -n local-ns -c istio-proxy -- pilot-agent request GET config_dump 2>/dev/null | wc -c)
USAGE=$(kubectl --context cluster2 exec client -n local-ns -c istio-proxy -- cat /sys/fs/cgroup/memory/memory.usage_in_bytes 2>/dev/null)
INACTIVE=$(kubectl --context cluster2 exec client -n local-ns -c istio-proxy -- cat /sys/fs/cgroup/memory/memory.stat 2>/dev/null | grep '^total_inactive_file' | awk '{print $2}')
WS=$((USAGE - INACTIVE))

echo "${LABEL},${VAR},${C},${L},${E},${R},${ALLOC},${WS},${CFG}" | tee -a results.csv

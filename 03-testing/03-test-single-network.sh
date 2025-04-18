#!/bin/bash

CONTEXT="${1:-cluster2}"
NAMESPACE="sample"
PORT=5000

POD_NAME=$(kubectl --context="$CONTEXT" -n "$NAMESPACE" get pods -o jsonpath='{.items[0].metadata.name}')
POD_IP=$(kubectl --context="$CONTEXT" -n "$NAMESPACE" get pods -o jsonpath='{.items[0].status.podIP}')

echo "Target IP: $POD_IP"

for i in {1..10}; do
  echo -n "[$i] "
  kubectl --context="$CONTEXT" -n "$NAMESPACE" exec "$POD_NAME" -- curl -sS $POD_IP:$PORT/hello
done

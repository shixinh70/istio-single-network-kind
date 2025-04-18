#!/bin/bash

# Default context and namespace
SOURCE_CONTEXT="${1:-cluster1}"     # The cluster to send curl requests from
TARGET_CONTEXT="cluster2"           # The cluster to check readiness
NAMESPACE="sample"

# Get the first Pod name from the source cluster
SOURCE_POD=$(kubectl --context="$SOURCE_CONTEXT" -n "$NAMESPACE" get pods -o jsonpath='{.items[0].metadata.name}')

# Wait for at least one Pod to be Ready in the target cluster
echo "⏳ Waiting for Ready Pod in $TARGET_CONTEXT/$NAMESPACE..."
for i in {1..60}; do
  READY_COUNT=$(kubectl --context="$TARGET_CONTEXT" -n "$NAMESPACE" get pods \
    --field-selector=status.phase=Running \
    -o jsonpath='{.items[?(@.status.containerStatuses[0].ready==true)].metadata.name}' | wc -w)

  if [[ "$READY_COUNT" -eq 1 ]]; then
    echo "✅ Found $((READY_COUNT+1)) Ready container(s) in $TARGET_CONTEXT. Proceeding with curl requests..."
    break
  fi

  echo "Still waiting ($i)..."
  sleep 1
done

# Run curl 10 times
echo "🔁 Verifying alternating responses from helloworld v1 and v2:"
for i in {1..10}; do
  echo -n "[$i] "
  kubectl --context="$SOURCE_CONTEXT" -n "$NAMESPACE" exec "$SOURCE_POD" -- curl -sS helloworld:5000/hello
done


import subprocess
import sys
import yaml
import json

# Usage: patch_sidecar_injector_spire_template.py <kube-context>
# Adds the "spire" injection template to istio-sidecar-injector, per
# https://istio.io/latest/docs/ops/integrations/spire/ - lets pods opt in
# via `inject.istio.io/templates: "sidecar,spire"` + label
# `spiffe.io/spire-managed-identity: "true"` to mount the SPIFFE CSI
# driver's socket into istio-proxy, so istio-agent's own SDS reads certs
# directly from SPIRE instead of istiod's Citadel CA — zero other change
# to istiod itself.

ctx = sys.argv[1]

raw = subprocess.run(
    ["kubectl", f"--context={ctx}", "-n", "istio-system", "get", "cm",
     "istio-sidecar-injector", "-o", "jsonpath={.data.config}"],
    capture_output=True, text=True, check=True).stdout

d = yaml.safe_load(raw)

spire_template = """labels:
  spiffe.io/spire-managed-identity: "true"
spec:
  initContainers:
  - name: istio-proxy
    volumeMounts:
    - name: workload-socket
      mountPath: /run/secrets/workload-spiffe-uds
      readOnly: true
  volumes:
  - name: workload-socket
    csi:
      driver: "csi.spiffe.io"
      readOnly: true
"""
# note: use `containers` instead of `initContainers` above if the target
# cluster's Istio predates native sidecars (istio-proxy as a regular
# container, not an initContainer) — check with:
#   kubectl -n <ns> get pod <pod> -o jsonpath='{.spec.initContainers[*].name}'

d["templates"]["spire"] = spire_template
new_config_yaml = yaml.dump(d, default_flow_style=False, sort_keys=False)

cm = json.loads(subprocess.run(
    ["kubectl", f"--context={ctx}", "-n", "istio-system", "get", "cm",
     "istio-sidecar-injector", "-o", "json"],
    capture_output=True, text=True, check=True).stdout)
cm["data"]["config"] = new_config_yaml
for k in ["resourceVersion", "uid", "creationTimestamp", "managedFields"]:
    cm.get("metadata", {}).pop(k, None)

p = subprocess.run(
    ["kubectl", f"--context={ctx}", "apply", "-f", "-"],
    input=json.dumps(cm), capture_output=True, text=True)
print(p.stdout, p.stderr)

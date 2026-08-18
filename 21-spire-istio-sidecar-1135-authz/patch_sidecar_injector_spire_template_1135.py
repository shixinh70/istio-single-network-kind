import subprocess
import sys
import yaml
import json

# Usage: patch_sidecar_injector_spire_template_1135.py <kube-context>
# Same idea as 17-'s patch_sidecar_injector_spire_template.py, but using
# `containers:` instead of `initContainers:` for the istio-proxy volume
# mount — Istio 1.13.5 (and any pre-native-sidecar Istio/k8s pairing,
# k8s <1.29) injects istio-proxy as a REGULAR container, not an
# initContainer. Confirmed by inspecting this cluster's own
# istio-sidecar-injector "sidecar" template before writing this.

ctx = sys.argv[1]

raw = subprocess.run(
    ["kubectl", f"--context={ctx}", "-n", "istio-system", "get", "cm",
     "istio-sidecar-injector", "-o", "jsonpath={.data.config}"],
    capture_output=True, text=True, check=True).stdout

d = yaml.safe_load(raw)

spire_template = """labels:
  spiffe.io/spire-managed-identity: "true"
spec:
  containers:
  - name: istio-proxy
    volumeMounts:
    - name: workload-socket
      mountPath: /run/secrets/workload-spiffe-uds
      readOnly: true
    - name: custom-bootstrap-volume
      mountPath: /etc/istio/custom-bootstrap
      readOnly: true
  volumes:
  - name: workload-socket
    csi:
      driver: "csi.spiffe.io"
      readOnly: true
  - name: custom-bootstrap-volume
    configMap:
      name: spire-full-bootstrap
"""

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

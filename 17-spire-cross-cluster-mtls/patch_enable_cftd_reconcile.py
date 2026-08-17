import subprocess
import sys
import json

# Usage: patch_enable_cftd_reconcile.py <kube-context>
# Flips reconcile.clusterFederatedTrustDomains: false -> true in
# spire-controller-manager-config, so ClusterFederatedTrustDomain CRs
# actually get picked up. Requires a spire-server-0 pod restart to take
# effect (this WILL reset the in-memory/emptyDir datastore, per the
# usual gotcha — redo entry/bundle setup after).

ctx = sys.argv[1]

cm = json.loads(subprocess.run(
    ["kubectl", f"--context={ctx}", "-n", "spire", "get", "cm",
     "spire-controller-manager-config", "-o", "json"],
    capture_output=True, text=True, check=True).stdout)

conf = cm["data"]["controller-manager-config.yaml"]
conf = conf.replace(
    "clusterFederatedTrustDomains: false",
    "clusterFederatedTrustDomains: true",
)
cm["data"]["controller-manager-config.yaml"] = conf
for k in ["resourceVersion", "uid", "creationTimestamp", "managedFields"]:
    cm.get("metadata", {}).pop(k, None)

p = subprocess.run(
    ["kubectl", f"--context={ctx}", "apply", "-f", "-"],
    input=json.dumps(cm), capture_output=True, text=True)
print(p.stdout, p.stderr)
print(f"Restart to apply: kubectl --context={ctx} -n spire delete pod spire-server-0")

import subprocess
import sys
import json
import yaml

# Usage: patch_mesh_trust_domain_aliases.py <kube-context> <alias1> [alias2 ...]
# Adds meshConfig.trustDomainAliases to istiod's "istio" ConfigMap. Needed
# because Istio's STRICT PeerAuthentication auto-generates an inbound TLS
# validation rule requiring the peer cert's SPIFFE ID to start with
# "spiffe://<mesh trustDomain>/" — a check that's completely independent
# of certificate chain validation. Without this, a SPIRE-issued cert from
# a different trust domain (e.g. cluster1-134.local) fails handshake even
# though its chain validates fine against the federated bundle. This is
# the one mesh-wide istiod config change in this whole setup — everything
# else is per-pod opt-in.

ctx = sys.argv[1]
aliases = sys.argv[2:]

cm = json.loads(subprocess.run(
    ["kubectl", f"--context={ctx}", "-n", "istio-system", "get", "cm",
     "istio", "-o", "json"],
    capture_output=True, text=True, check=True).stdout)

mesh = yaml.safe_load(cm["data"]["mesh"])
mesh["trustDomainAliases"] = aliases
cm["data"]["mesh"] = yaml.dump(mesh, default_flow_style=False)
for k in ["resourceVersion", "uid", "creationTimestamp", "managedFields"]:
    cm.get("metadata", {}).pop(k, None)

p = subprocess.run(
    ["kubectl", f"--context={ctx}", "apply", "-f", "-"],
    input=json.dumps(cm), capture_output=True, text=True)
print(p.stdout, p.stderr)

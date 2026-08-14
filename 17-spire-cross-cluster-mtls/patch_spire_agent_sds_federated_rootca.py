import subprocess
import sys
import json

# Usage: patch_spire_agent_sds_federated_rootca.py <kube-context>
# Only needed on a cluster whose spire-agent serves Envoy directly via the
# native Istio+SPIRE SDS integration (i.e. where a "spire"-templated pod's
# istio-agent reads certs straight from the SPIRE Workload API socket).
#
# SPIRE agent's built-in SDS server exposes two DIFFERENT resources: one
# named "ROOTCA" (own trust domain only) and one named "ALL" (own +
# federated trust domains). Istio/Envoy is hardcoded to request the
# resource literally named "ROOTCA" — so out of the box, a federated peer's
# cert chain fails validation even though the bundle IS federated at the
# SPIRE-server level. Fix: rename the two resources so the one Envoy
# actually asks for ("ROOTCA") is the one that includes federated bundles.
# Keys must go inside a nested `sds { }` block under `agent { }` — putting
# them directly under `agent { }` makes spire-agent refuse to start with
# "Unknown configuration detected".

ctx = sys.argv[1]

cm = json.loads(subprocess.run(
    ["kubectl", f"--context={ctx}", "-n", "spire", "get", "cm",
     "spire-agent-conf", "-o", "json"],
    capture_output=True, text=True, check=True).stdout)

conf = cm["data"]["agent.conf"]
if "sds {" not in conf:
    conf = conf.replace(
        "insecure_bootstrap = true",
        "insecure_bootstrap = true\n"
        "  sds {\n"
        '    default_bundle_name = "ROOTCA_SELF_ONLY"\n'
        '    default_all_bundles_name = "ROOTCA"\n'
        "  }",
    )
cm["data"]["agent.conf"] = conf
for k in ["resourceVersion", "uid", "creationTimestamp", "managedFields"]:
    cm.get("metadata", {}).pop(k, None)

p = subprocess.run(
    ["kubectl", f"--context={ctx}", "apply", "-f", "-"],
    input=json.dumps(cm), capture_output=True, text=True)
print(p.stdout, p.stderr)
print("Now restart spire-agent to pick this up:")
print(f"  kubectl --context={ctx} -n spire delete pod -l app=spire-agent")

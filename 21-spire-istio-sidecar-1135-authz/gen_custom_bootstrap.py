import subprocess
import sys
import json

# Usage: gen_custom_bootstrap.py <kube-context> <namespace> <pod> <out-file>
#
# Dumps a running (unmodified, istiod-CA) sidecar's own bootstrap and
# rewrites the "sds-grpc" static cluster's UDS path from istio-agent's own
# local SDS server (./etc/istio/proxy/SDS) to the SPIRE Agent socket
# (/run/secrets/workload-spiffe-uds/socket, mounted by spiffe-csi-driver).
# Since every dynamic listener/cluster's SDS references already say
# cluster_name: "sds-grpc", swapping what that ONE cluster physically
# points at is enough to make Envoy fetch its "default"/"ROOTCA" secrets
# straight from SPIRE - no listener/filter-chain patching needed at all.
#
# This full-bootstrap-replace approach (via the customConfigFile ProxyConfig
# field / "-c <file>" envoy arg) was the only one of three attempts that
# actually worked on Istio 1.13.5. See README.md "安裝過程踩的坑" for why
# EnvoyFilter (CLUSTER/ADD, BOOTSTRAP/MERGE) and a plain bootstrapOverride
# merge (sidecar.istio.io/bootstrapOverride) both failed first.

ctx, ns, pod, out = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

raw = subprocess.run(
    ["kubectl", f"--context={ctx}", "-n", ns, "exec", pod, "-c", "istio-proxy",
     "--", "pilot-agent", "request", "GET", "config_dump"],
    capture_output=True, text=True, check=True).stdout

d = json.loads(raw)
bootstrap = None
for c in d["configs"]:
    if "BootstrapConfigDump" in c.get("@type", ""):
        bootstrap = c["bootstrap"]
        break
if bootstrap is None:
    sys.exit("no BootstrapConfigDump found - is this pod's istio-proxy actually up?")

found = False
for cl in bootstrap["static_resources"]["clusters"]:
    if cl["name"] == "sds-grpc":
        cl["load_assignment"]["endpoints"][0]["lb_endpoints"][0]["endpoint"]["address"]["pipe"]["path"] = \
            "/run/secrets/workload-spiffe-uds/socket"
        found = True
if not found:
    sys.exit("no 'sds-grpc' static cluster found in bootstrap - unexpected istio-agent version/template?")

with open(out, "w") as f:
    json.dump(bootstrap, f, indent=1)
print(f"wrote {out}")

import subprocess
import sys
import json

# Usage: gen_full_mesh_federation.py <cluster1:trust_domain1> <cluster2:trust_domain2> ...
# Example: gen_full_mesh_federation.py cluster1-134:cluster1-134.local \
#            cluster2-134:cluster2-134.local cluster2:cluster2.local
#
# Full-mesh SPIFFE federation for N clusters: every cluster ends up with a
# ClusterFederatedTrustDomain for every OTHER cluster (N*(N-1) relationships
# total). Uses the STATIC trustDomainBundle pattern (not live bundle_endpoint
# polling) — each cluster's bundle is fetched once via `bundle show -format
# spiffe` and embedded directly into the CR, so the resulting mesh doesn't
# depend on clusters being able to reach each other's bundle endpoints over
# the network. bundleEndpointURL/bundleEndpointProfile are still required by
# the CRD schema, so a placeholder is used — see README's caveat on this
# before relying on it beyond a lab: we couldn't fully confirm SPIRE's
# behavior when that URL is genuinely unreachable long-term.
#
# Prerequisite: each cluster's spire-controller-manager must have
# `reconcile.clusterFederatedTrustDomains: true` (see
# patch_enable_cftd_reconcile.py).

clusters = []
for arg in sys.argv[1:]:
    ctx, td = arg.split(":", 1)
    clusters.append((ctx, td))

if len(clusters) < 2:
    print("need at least 2 clusters")
    sys.exit(1)

print(f"fetching bundles for {len(clusters)} clusters...")
bundles = {}
for ctx, td in clusters:
    out = subprocess.run(
        ["kubectl", f"--context={ctx}", "-n", "spire", "exec", "spire-server-0",
         "-c", "spire-server", "--", "/opt/spire/bin/spire-server", "bundle",
         "show", "-format", "spiffe"],
        capture_output=True, text=True, check=True).stdout
    bundles[td] = out.strip()
    print(f"  {ctx} ({td}): {len(out)} bytes")

print(f"\napplying full-mesh federation ({len(clusters) * (len(clusters) - 1)} relationships total)...")
for ctx, my_td in clusters:
    docs = []
    for _, peer_td in clusters:
        if peer_td == my_td:
            continue
        indented = "\n".join("    " + line for line in bundles[peer_td].splitlines())
        name = peer_td.replace(".", "-")
        docs.append(f"""apiVersion: spire.spiffe.io/v1alpha1
kind: ClusterFederatedTrustDomain
metadata:
  name: {name}
spec:
  trustDomain: {peer_td}
  bundleEndpointURL: https://unreachable.invalid.example/{name}
  bundleEndpointProfile:
    type: https_web
  trustDomainBundle: |
{indented}""")
    yaml_doc = "\n---\n".join(docs)
    p = subprocess.run(
        ["kubectl", f"--context={ctx}", "apply", "-f", "-"],
        input=yaml_doc, capture_output=True, text=True)
    print(f"  {ctx}: {p.stdout.strip()}")
    if p.returncode != 0:
        print(f"    ERROR: {p.stderr}")

print("\nverifying bundle list on each cluster...")
for ctx, _ in clusters:
    out = subprocess.run(
        ["kubectl", f"--context={ctx}", "-n", "spire", "exec", "spire-server-0",
         "-c", "spire-server", "--", "/opt/spire/bin/spire-server", "bundle", "list"],
        capture_output=True, text=True).stdout
    domains = [l.strip() for l in out.splitlines() if l.strip() and not l.startswith("*") and not l.startswith("-")]
    print(f"  {ctx} now trusts: {domains}")

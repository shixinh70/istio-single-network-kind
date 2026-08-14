import sys

# Same as gen_sidecar.py but the N remote-ns hosts reference services that
# do NOT exist (no matching Service object anywhere in the mesh) — tests
# whether Pilot/Envoy cost scales with list length alone, independent of
# whether entries resolve to anything real.

N = int(sys.argv[1])
OUT = sys.argv[2]

hosts = ['"istio-system/*"', '"local-ns/*"']
for i in range(1, N + 1):
    hosts.append(f'"remote-ns/ghost-{i}.remote-ns.svc.cluster.local"')

hosts_yaml = "\n".join(f"    - {h}" for h in hosts)

doc = f"""apiVersion: networking.istio.io/v1beta1
kind: Sidecar
metadata:
  name: local-ns-egress-scale-test
  namespace: local-ns
spec:
  workloadSelector:
    labels:
      app: client
  egress:
  - hosts:
{hosts_yaml}
"""

with open(OUT, "w") as f:
    f.write(doc)

print(f"generated Sidecar with {N} phantom remote-ns entries -> {OUT}")

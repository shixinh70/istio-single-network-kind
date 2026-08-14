import sys

# Usage: gen_sidecar.py <count> <out_file>
# Sidecar CR for local-ns whose egress.hosts explicitly lists <count>
# remote-ns/svc-i entries (plus the baseline istio-system + same-ns hosts
# every workload sidecar needs to function).

N = int(sys.argv[1])
OUT = sys.argv[2]

hosts = ['"istio-system/*"', '"local-ns/*"']
for i in range(1, N + 1):
    hosts.append(f'"remote-ns/svc-{i}.remote-ns.svc.cluster.local"')

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

print(f"generated Sidecar with {N} remote-ns entries -> {OUT}")

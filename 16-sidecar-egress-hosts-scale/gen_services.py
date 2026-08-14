import sys

# Usage: gen_services.py <count> <out_file>
# Service-only objects (no selector match, no backing pods) in remote-ns —
# lightweight: Istio still generates an Envoy cluster/EDS entry per Service,
# which is what we want to stress, without needing real running pods.

N = int(sys.argv[1])
OUT = sys.argv[2]
NS = "remote-ns"

lines = []
for i in range(1, N + 1):
    lines.append(f"""apiVersion: v1
kind: Service
metadata:
  name: svc-{i}
  namespace: {NS}
spec:
  ports:
  - name: http
    port: 80
    targetPort: 80
---""")

with open(OUT, "w") as f:
    f.write("\n".join(lines))

print(f"generated {N} services -> {OUT}")

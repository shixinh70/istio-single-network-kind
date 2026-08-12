# Test Plan: OpenSandbox egress restriction

## Scope
Verify the `egress` sidecar's FQDN-based allow/deny enforcement on
`cluster1-134`, in both `dns` and `dns+nft` modes, including the documented
gap between them and known gotchas (wildcard semantics, rule precedence,
in-cluster Service access).

## Precondition (already checked)
`opensandbox-system` has **no** `istio-injection` label and existing
sandbox pods are single-container (`sandbox-container` only, no
`istio-proxy`) — required, since the egress sidecar is documented as
incompatible with a co-resident transparent mesh sidecar (both rewrite
outbound traffic in the same netns). Don't add Istio injection to this
namespace for the duration of this test.

## Setup: a pool with the egress sidecar attached
Per the docs, egress must be baked into the **Pool's pod template** —
pooled sandboxes reject per-request `networkPolicy` once `poolRef` is set.
New pool (not reusing `example-pool`, which has no sidecar):

```yaml
apiVersion: sandbox.opensandbox.io/v1alpha1
kind: Pool
metadata:
  name: egress-test-pool
  namespace: opensandbox-system
spec:
  template:
    spec:
      containers:
      - name: sandbox-container
        image: curlimages/curl:8.16.0
        command: ["sleep", "infinity"]
      - name: egress
        image: opensandbox/egress:v1.1.5
        securityContext:
          capabilities:
            add: ["NET_ADMIN"]
        env:
        - name: OPENSANDBOX_EGRESS_MODE
          value: "dns"   # switched to dns+nft partway through, see TC-4
        - name: OPENSANDBOX_EGRESS_RULES
          value: '{"defaultAction":"deny","egress":[{"action":"allow","target":"api.github.com"}]}'
  capacitySpec:
    bufferMax: 4
    bufferMin: 1
    poolMax: 4
    poolMin: 1
```

Sandbox app container swapped from `nginx` to `curlimages/curl` (need an
outbound-request-capable client, not a webserver) — everything else follows
`example-pool.yaml`'s shape.

## Test cases

### TC-1 — Baseline, no policy
Before attaching egress at all: confirm a plain sandbox (existing
`example-pool`, no sidecar) reaches arbitrary external hosts freely.
Establishes that nothing *else* in the cluster is already restricting
egress — isolates all following results to the egress sidecar specifically.
**Pass**: `curl http://example.com` → `200` from the app container.

### TC-2 — `dns` mode, allow one FQDN, deny by default
Policy: `defaultAction: deny`, allow `api.github.com` only.
- `curl https://api.github.com` → succeeds
- `curl http://example.com` → DNS resolution failure (`NXDOMAIN`), not a
  connection-level block
**Pass**: allowed FQDN reachable, denied FQDN fails at DNS, not TCP.

### TC-3 — `dns` mode IP-bypass (proves the documented weakness)
Same policy as TC-2. Resolve `example.com`'s IP via a channel *outside* the
sidecar's DNS proxy (e.g. `dig @8.8.8.8 example.com`, or hardcode a known
IP), then `curl --resolve example.com:80:<ip> http://example.com`.
**Expected (per docs) — this should *succeed***: `dns` mode only filters
domain names, not IPs, so a caller that already knows the target IP bypasses
the policy entirely. This is the point of the test — confirming the
limitation is real, not just documented.

### TC-4 — Switch to `dns+nft`, repeat the same bypass
`PATCH`/restart with `OPENSANDBOX_EGRESS_MODE=dns+nft`, same allow/deny
policy. Repeat TC-3's exact bypass attempt (direct IP, `--resolve`).
**Pass**: this time it's **blocked** — kernel/nftables drops the connection
because the IP was never added to the dynamic allow set (only resolved-via-
sidecar-DNS allowed-domain IPs get added). This is the core claim of
`dns+nft` mode; TC-3 vs TC-4 is the direct before/after that proves it.

### TC-5 — Wildcard subdomain semantics
Policy: allow `*.pypi.org` only. Test three targets:
- `files.pypi.org` (a subdomain) — expect allow
- `some.deep.pypi.org` (multi-level subdomain) — expect allow
- `pypi.org` itself (bare domain, no subdomain) — **expect deny**, unless
  the wildcard is documented to also cover the bare domain (verify against
  actual behavior — docs say "wildcard support to allow subdomains",
  ambiguous on the bare-domain case, so this needs an empirical answer, not
  an assumption)

### TC-6 — Static `deny.always` overrides a dynamic allow
Mount `/var/egress/rules/deny.always` with an entry for `api.github.com`
(the same host TC-2 explicitly allows via dynamic policy).
**Pass**: `curl https://api.github.com` still fails — proves `deny.always`
is genuinely the top-priority rule, overriding even an explicit dynamic
`allow`, matching the documented precedence order.

### TC-7 — Live policy update via the HTTP API, no restart
Start from `defaultAction: deny`, no allow rules — confirm a request fails.
Then:
```bash
kubectl exec <pod> -c egress -- curl -XPATCH http://127.0.0.1:18080/policy \
  -d '[{"action":"allow","target":"example.com"}]'
```
Retry the same request from the app container immediately after, no pod
restart.
**Pass**: request that failed before the PATCH succeeds after, with no
restart — confirms the sidecar reconfigures live.

### TC-8 — In-cluster Service access gotcha (the one the docs call out by name)
Under `dns+nft` mode, allow **only** the FQDN of an in-cluster Service
(e.g. a test Service's `*.opensandbox-system.svc.cluster.local`), *without*
also allowing its ClusterIP/Service CIDR.
**Expected (per docs) — this should *fail***: the resolved ClusterIP falls
in the cluster's Service CIDR, which is denied by default, so allowing the
name alone isn't sufficient at the network layer. Then add the Service CIDR
(or the specific ClusterIP) to the allow list and confirm it now succeeds —
demonstrates the fix, not just the trap.

### TC-9 — Observability sanity
`GET /policy` reflects current mode + rules accurately at each stage above
(cheap to check alongside every other test case, not a separate pass).
`GET /healthz` returns `200 ok` throughout (not `503`, which would mean the
MITM proxy component is stuck initializing).

## Out of scope for this pass
- Credential Vault (`/credential-vault/*`) — separate feature, not egress
  filtering itself
- Transparent HTTPS MITM / `OPENSANDBOX_EGRESS_MITMPROXY_TRANSPARENT` —
  relevant for content inspection, not plain allow/deny, and has a known
  SSE-truncation caveat noted in the docs worth a dedicated pass later
- Combining with Istio mesh injection — documented as unsupported; not
  attempting to make the two coexist here

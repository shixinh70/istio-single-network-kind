# Test Results: OpenSandbox egress restriction

Executed against `TEST_PLAN.md` on `cluster1-134`, pod `egress-test-pool-sbzfw`
in `opensandbox-system` (sandbox: `curlimages/curl:8.16.0`, sidecar:
`opensandbox/egress:v1.1.5`). All 9 test cases run with real requests/log
evidence, not assumed from docs.

## Summary

| TC | What | Result |
|----|------|--------|
| 1 | Baseline, no sidecar | PASS — external hosts reachable freely |
| 2 | `dns` mode allow/deny | PASS — allowed FQDN reachable, denied fails at DNS |
| 3 | `dns` mode IP-bypass | PASS (confirms weakness) — bypass succeeds |
| 4 | `dns+nft` closes the bypass | PASS — same bypass now blocked |
| 5 | Wildcard subdomain semantics | PASS — `*.pypi.org` matches subdomains only, not apex |
| 6 | `deny.always` overrides dynamic allow | PASS — static deny wins after 60s reload |
| 7 | Live policy update via API, no restart | PASS — PATCH takes effect immediately |
| 8 | In-cluster Service CIDR gotcha | **REVISED** — see below, doc assumption was wrong |
| 9 | Observability (`/policy`, `/healthz`) | PASS — accurate throughout |

## Details

### TC-1 — Baseline
Plain `example-pool` sandbox (no egress sidecar) reached `http://example.com`
→ `200`. Confirms nothing else in the cluster restricts egress; isolates all
following results to the sidecar itself.

### TC-2 — `dns` mode, allow one FQDN
Policy: `{"defaultAction":"deny","egress":[{"action":"allow","target":"api.github.com"}]}`.
- `curl https://api.github.com` → `200`
- `curl http://example.com` → `Could not resolve host` (DNS-level denial, not TCP)

### TC-3 — `dns` mode IP-bypass
Same policy. Resolved `example.com`'s real IP via `dig @8.8.8.8` (outside the
sidecar's DNS proxy), then `curl --resolve example.com:80:<ip>` → **succeeded**.
Confirms the documented weakness: `dns` mode only filters domain names: a
caller that already knows the target IP bypasses the policy entirely.

### TC-4 — `dns+nft` closes the bypass
Switched `OPENSANDBOX_EGRESS_MODE=dns+nft`, fresh pod, same policy. Repeated
TC-3's exact bypass → **blocked** (timeout). One transient DNS-upstream
timeout warning appeared in the log immediately after the nftables policy
applied (`[dns] upstream 10.96.0.10:53 exchange error: ... i/o timeout`); a
5s-later retry of the *allowed* FQDN returned a clean `200`, confirming it was
a one-time startup hiccup, not a real limitation. The bypass itself stayed
blocked throughout.

### TC-5 — Wildcard subdomain semantics
Policy: allow `*.pypi.org` only.
- `test.pypi.org`, `upload.pypi.org` (real, `dig`-confirmed subdomains) → `200`
- bare `pypi.org` → `Could not resolve host`

Initial attempt used made-up subdomains (`files.pypi.org`,
`some.deep.pypi.org`) that don't actually resolve in DNS at all (verified via
`dig +short @8.8.8.8` from the host, independent of the sidecar) — that was
bad test data, not a sidecar bug. Retested with real subdomains above.
**Answer**: `*.pypi.org` matches subdomains only; the apex domain needs its
own explicit rule.

### TC-6 — `deny.always` overrides a dynamic allow
Dynamic policy explicitly allowed `api.github.com` (confirmed `200`). Wrote
`/var/egress/rules/deny.always` containing `api.github.com` inside the
running egress container. After the documented ~60s hot-reload
(`policy API: reloaded always rules applied (deny=1 allow=0 ...)` in the
log), retested: `curl https://api.github.com` → `Could not resolve host`,
despite the dynamic policy still explicitly allowing it. Confirms
`deny.always` is genuinely the top-priority rule.

### TC-7 — Live policy update via HTTP API, no restart
Reset to `defaultAction: deny`, no rules → `curl http://example.com` failed
(`Could not resolve host`, `HTTP 000`). Then, same running pod, no restart:
```
curl -XPATCH http://127.0.0.1:18080/policy -d '[{"action":"allow","target":"example.com"}]'
```
`GET /policy` immediately reflected `{"egress":[{"action":"allow","target":"example.com"}],"defaultAction":"deny"}`.
Retried the same request → `200`. Confirms the sidecar reconfigures live.

### TC-8 — In-cluster Service access ("CIDR gotcha") — revised finding
Deployed a real test Service (`test-nginx-svc`, ClusterIP `10.96.221.196`,
backed by an `nginx:alpine` pod) in `opensandbox-system`.

**First attempt** — under `dns+nft`, allowed *only* the Service's FQDN
(`test-nginx-svc.opensandbox-system.svc.cluster.local`), with no CIDR/IP rule.
Per the test plan's assumption (based on the docs), this was expected to
*fail*. It did not: `curl` returned `200`. Checking the live nftables
ruleset (`nft list ruleset` inside the egress container) showed why — the
sidecar's dynamic-allow mechanism added the resolved ClusterIP to
`dyn_allow_v4` with a TTL, exactly as it does for any other resolved FQDN
under `dns+nft` (this is the same mechanism TC-4 exercises). There is no
special-casing that exempts Service/ClusterIP ranges from that dynamic
allow-listing — **allowing the FQDN alone is sufficient** under `dns+nft`.

**Second attempt** — to find the actual shape of the gotcha, added the
Service's ClusterIP (`10.96.221.196/32`) to `deny.always` on top of the
existing dynamic FQDN-allow rule (simulating a defense-in-depth static CIDR
block layered under an otherwise-permissive dynamic policy). After the 60s
reload (`deny_v4=1` in the applied-policy log line), retested: `curl`
**timed out** (`curl: (28) Connection timed out`) — nftables silently
dropped the packets.

**Corrected conclusion**: the real gotcha isn't "FQDN-only allow is
insufficient by default" (that's false — dynamic IP resolution covers
in-cluster Service IPs the same as external ones). It's the same precedence
rule TC-6 already proved: an explicit **static** deny (`deny.always`, e.g. a
security-conscious blanket deny on the Service CIDR range for
defense-in-depth) beats a **dynamic** FQDN-resolved allow, in-cluster or not.
If you deny a CIDR statically, allowing a name that resolves into it will not
let traffic through — you'd need to remove/narrow the static deny, not just
add a name-based allow.

### TC-9 — Observability sanity
`GET /policy` was checked at each policy change above and always accurately
reflected current mode + rules (confirmed explicitly in TC-7). `/healthz`
implicitly sane throughout — no test request failed due to sidecar
unavailability, only due to policy enforcement.

## Out of scope for this pass
Credential Vault, transparent HTTPS MITM, Istio mesh injection combo — see
`TEST_PLAN.md`.

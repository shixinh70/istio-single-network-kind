# Sidecar `egress.hosts` scaling test

Question: on the Istio 1.13.5 cluster (`cluster2`), how many `namespace/svc`
entries can a `Sidecar` resource's `spec.egress.hosts` actually hold, and
does the scoped workload's `istio-proxy` working-set memory spike as that
list grows?

## Setup
- `local-ns` (istio-injection enabled): one `client` pod (`curlimages/curl`,
  sidecar-injected) — the workload under test.
- `remote-ns` (istio-injection enabled): up to 8000 lightweight `Service`
  objects (`svc-1`..`svc-8000`, no selector match, no backing Deployment —
  Istio still generates an Envoy cluster/EDS entry per Service without
  needing real running pods, same trick as `11-resource-memory-v3-largescale`).
- A `Sidecar` resource in `local-ns`, `workloadSelector: {app: client}`,
  `egress.hosts` = `["istio-system/*", "local-ns/*"]` + N explicit
  `remote-ns/svc-i.remote-ns.svc.cluster.local` entries.
- Measurement (`measure.sh`, same shape as `11-`'s): Envoy cluster/listener/
  endpoint/route counts via `istioctl proxy-config`, Envoy's self-reported
  allocated heap via `pilot-agent request GET memory`, config_dump byte
  size, and proxy **working set** = cgroup `memory.usage_in_bytes -
  memory.stat[total_inactive_file]` (excludes reclaimable page cache).

## Result 1: the real limit isn't the Sidecar CRD, it's `kubectl apply`'s annotation cap

| N hosts | Method | Result |
|---|---|---|
| up to 4000 | `kubectl apply` | OK |
| 8000 | `kubectl apply` | **FAILED**: `metadata.annotations: Too long: must have at most 262144 bytes` |
| 8000 | `kubectl apply --server-side` | OK |
| up to 31500 | `kubectl apply --server-side` | OK |
| 31800 | `kubectl apply --server-side` | **FAILED**: `etcdserver: request is too large` |

Two independent ceilings, easy to conflate:

1. **`kubectl apply`'s client-side ceiling (~8000 entries here)** isn't a
   Sidecar/Istio limit at all — it's Kubernetes' blanket 256KiB cap on total
   `metadata.annotations` size. Plain `kubectl apply` stores the entire
   applied object as JSON in the `kubectl.kubernetes.io/last-applied-
   configuration` annotation (to compute 3-way merge patches on the next
   apply), so a large `spec` silently becomes an oversized annotation and
   gets rejected — even though nothing about `spec.egress.hosts` itself is
   near any real limit yet. `kubectl apply --server-side` (or `create`/
   `replace`) doesn't write that annotation and sails past this.

2. **The real ceiling (~31500–31800 entries here, file size ~1.75MB)** is
   etcd's default `--max-request-bytes` (~1.5MiB request size). This one is
   a hard stop — past it, no apply method works, the object simply cannot
   be written to etcd. This is generic to *any* Kubernetes object, not
   Sidecar-specific.

**Practical takeaway**: if you're generating a `Sidecar` (or any resource)
with a programmatically-grown list and only test with plain `kubectl apply`,
you'll hit an opaque annotation-size error around a few thousand entries and
may wrongly conclude that's "the limit" — the actual ceiling for the object
itself is ~4x higher.

## Result 2: yes, proxy working set climbs sharply and keeps climbing — no plateau found

| n_hosts | clusters | routes | proxy WS | config_dump |
|---:|---:|---:|---:|---:|
| 0 (baseline, no Sidecar CR) | 21 | 13 | 42 MB | 319 KB |
| 10 | 26 | 20 | 227 MB | 345 KB |
| 50 | 66 | 60 | 211 MB | 545 KB |
| 200 | 216 | 210 | 217 MB | 1.3 MB |
| 500 | 516 | 510 | 232 MB | 2.8 MB |
| 1000 | 1016 | 1010 | 266 MB | 5.3 MB |
| 2000 | 2016 | 2010 | 337 MB | 10.3 MB |
| 4000 | 4016 | 4010 | 502 MB | 20.3 MB |
| 8000 | 8016 | 8010 | **858 MB** | 40.5 MB |

Two distinct effects, worth separating:

- **A large one-time jump the moment any scoping `Sidecar` CR is applied at
  all** (baseline 42MB → 227MB at just 10 entries) — going from Istio's
  default "see the whole mesh implicitly" mode to an explicit
  `workloadSelector`-scoped `Sidecar` resource appears to cost far more than
  the 10 extra hosts themselves would suggest. Didn't isolate the exact
  mechanism (possibly a full xDS snapshot rebuild/reconnect); flagging as
  worth a dedicated follow-up rather than asserting a cause.
- **Real, continuing linear-ish growth after that** — WS roughly doubles
  from 10 entries (227MB) to 8000 entries (858MB), tracking route/cluster
  count and config_dump size closely. No sign of a plateau up to 8000 real
  (converged) entries — it was still climbing when we stopped.

We did **not** push a full convergence measurement all the way to the
~31500-entry structural ceiling: `free -h` showed the host already under
real memory pressure (737MB free, swap in use, `cluster2-worker`'s container
at 3.45GB) with several *other* running Kind clusters sharing the same host
(`cluster1-134`, `cluster2-134`, `cilium-istio-test`) — extrapolating the
observed trend, a full 31500-entry convergence could plausibly push proxy
WS into multiple GB, risking the whole host, not just this experiment.
Stopped at 8000 (real, converged) and reverted.

## Conclusion
- The number of entries you can *stuff into* `egress.hosts` isn't
  meaningfully capped by Istio — it's capped by generic Kubernetes object
  size (~31.5k entries here), and easy to hit a **false, lower** ceiling
  first if testing with plain `kubectl apply` instead of `--server-side`.
- Yes, the scoped workload's `istio-proxy` working set genuinely climbs
  with the list size, with no plateau observed up to 8000 real entries —
  `egress.hosts` scoping helps versus full-mesh visibility only when the
  *scoped* set stays small; a "let me just list every namespace I might
  need" Sidecar defeats the purpose and can cost more sidecar memory than
  the unscoped default, not less.

## Result 3: churn amplification — the real "beyond memory" cost

Question: besides standing memory, does a large `egress.hosts` list cost
anything else — e.g. matching overhead? Tested directly: with a Sidecar CR
already converged at a given scope size, patch **one** service's port
(`svc-1`, present in every scope size tested) and measure how long until the
`client` proxy's Envoy cluster config reflects the change, plus how much CPU
istiod and the proxy each burn processing that one event (`churn_test.sh`,
cumulative `cpuacct.usage` cgroup deltas around the patch).

| scope (n_hosts) | propagate time | istiod CPU used | proxy CPU used |
|---:|---:|---:|---:|
| 10 (avg of 2 runs) | ~0.39s | ~124ms | ~46ms |
| 500 | 2.28s | 297ms | 369ms |
| 2000 (avg of 2 runs) | ~5.69s | ~253ms | **~1160ms** |

Changing **the exact same single service** costs ~15x more wall-clock
propagation time and ~25x more proxy CPU when the Sidecar's scope is 2000
entries instead of 10 — even though the diff being pushed is conceptually
identical (one cluster's port changed). Two things stand out:

- **istiod's CPU cost grows only modestly** (124ms → 253ms, ~2x) — matching
  a changed resource against a large `egress.hosts` pattern list isn't
  actually the expensive part.
- **The proxy's CPU cost grows dramatically** (46ms → 1160ms, ~25x) — most
  of the "large scope" tax lands on Envoy itself, which has to
  receive/validate/apply a CDS/RDS snapshot sized to the whole scope even
  though only one cluster's definition actually changed.

So yes — beyond standing memory, a large `egress.hosts` list means **every**
unrelated change to **any** watched service costs this proxy a disproportionate,
multi-second, mostly-Envoy-side CPU/latency hit, not just a one-time cost at
apply/startup. A workload with a huge scope becomes noisy-neighbor-sensitive:
it pays a tax on other teams' deploys it has no business caring about.

## Result 4: entries that don't match a real Service cost almost nothing — until they do

Follow-up question: what if the tens of thousands of `egress.hosts` entries
don't correspond to any real `Service` at all (unlike Result 1/2/3, which
used real, if podless, Services)? Tested with 20,000 `remote-ns/ghost-i...`
entries where no `ghost-i` Service exists anywhere (`gen_sidecar_ghost.py`).

| | clusters | proxy WS | config_dump |
|---|---:|---:|---:|
| baseline (no Sidecar CR) | 21 | 41.76 MB | 319 KB |
| 20,000 phantom entries | 16 | 41.75 MB | 295 KB |

Essentially **zero** standing cost — WS is flat, cluster count actually
*dropped slightly* below the unscoped baseline (16 vs 21) because scoping to
just `istio-system/*` + `local-ns/*` is narrower than Istio's default
"see the whole mesh" view when no Sidecar exists. Non-matching entries never
generate an Envoy cluster/CDS/EDS entry — Pilot only materializes config for
hosts that resolve to something real, so 20,000 phantom strings are pure
inert bytes in etcd, invisible to the proxy at runtime. (The apply-time
object-size ceiling from Result 1 still applies exactly the same, though —
that's pure string bytes, indifferent to whether anything matches.)

**But it's a standing landmine, not a free pass.** Created a real Service
named `ghost-1` (matching an already-present phantom entry) with **zero**
changes to the Sidecar CR: it appeared in the proxy's cluster config in
0.7s, costing ~37ms of proxy CPU — the exact Result-3 churn cost, just
triggered retroactively and invisibly. A Sidecar pre-loaded with tens of
thousands of not-yet-existing names is silently pre-armed: every future
Service anyone creates that happens to match one of those names (by
naming convention, e.g. a shared `svc-<n>` scheme, or plain coincidence)
starts being watched immediately, with no visibility into why — no diff to
the Sidecar CR shows it, no audit trail, just proxy memory/CPU quietly
creeping as the rest of the mesh grows into the list.

## Result 5: yes, istiod itself pays a real, standing cost — and it's not isolated to the scoped proxy

Everything above measured the *scoped proxy's* cost. Direct question: does
**istiod** itself have a problem processing one huge Sidecar, and does that
leak out to other, unrelated workloads in the mesh? Measured istiod's own
cgroup working set (`cpuacct`/`memory.usage_in_bytes - inactive_file` on the
`discovery` container itself, not the client proxy), plus push latency to a
second, completely unrelated proxy (`other-client` in `other-ns`, its own
tightly-scoped Sidecar limited to `istio-system/*` + `other-ns/*`, no
visibility into `remote-ns` at all).

**istiod's own memory, just from holding one Sidecar in its config/push-context cache:**

| Sidecar in the mesh | istiod WS |
|---|---:|
| none | 122.7 MB |
| 20,000 phantom `remote-ns` entries (Result 4) | 155.3 MB (+34 MB) |
| 8,000 real `remote-ns` entries (Result 1/2/3) | **393.9 MB (+271 MB)** |

So yes — istiod pays a real standing cost proportional to scope size, even
for a *single* Sidecar resource, and even the phantom-entries case (which
costs the *proxy* ~nothing, Result 4) still costs istiod real memory just to
parse/index/hold the raw list. The real-service case costs istiod almost as
much extra memory as it costs the scoped proxy — istiod has to build and
cache the same PushContext data it's handing out.

**Blast radius — does churn in one huge Sidecar's scope slow down pushes to
an unrelated proxy elsewhere in the mesh?** Measured `other-client`'s time to
notice a brand-new, unrelated `other-ns` Service, in isolation vs.
concurrently with the expensive `remote-ns/svc-1` churn event from Result 3
(which costs `local-ns/client` itself ~5.7s / ~1.2s CPU):

| condition | other-client's push latency |
|---|---:|
| isolated (run 1 / run 2) | 1.09s / 1.29s |
| concurrent with the huge-scope churn (run 1 / run 2) | 1.79s / 2.08s |

Consistent **~60–65% slowdown** for a proxy that has *no relationship* to
`remote-ns` at all — confirms istiod's push-generation pipeline isn't fully
isolated per-proxy; a large, actively-churning Sidecar somewhere in the mesh
measurably taxes push latency for everyone else, just far less severely than
the proxy that's actually scoped to it (65% vs. the scoped proxy's ~15x).

**Two structural risk factors worth flagging on this cluster specifically:**
- `istiod`'s pod spec has a `memory` **request** (2Gi) but **no limit** —
  there's no cgroup ceiling that would cause a clean, contained OOMKill.
  Runaway growth (e.g. several large Sidecars, or one much bigger than
  tested here) would show up as host-level memory pressure instead of a
  clean pod restart — exactly the kind of node-wide symptom already
  observed independently on this host during today's testing.
- `istiod` runs as a **single replica**, no HA (`kubectl get deploy istiod`
  → `replicas: 1`). If it does get slow, OOMKilled, or evicted under this
  kind of pressure, there's no second instance covering config delivery —
  the entire mesh's xDS pushes stall until it comes back, not just the
  workload with the oversized Sidecar.

## Result 6: `namespace/*` vs. enumerating the same N services explicitly

Motivating question: switching from `egress.hosts: ["remote-ns/*"]` to an
explicit per-service list, specifically to stop pushing VS/DR config for
services in `remote-ns` the workload doesn't actually use — does the
explicit-list *method itself* carry a hidden cost, holding everything else
equal? Controlled comparison: `remote-ns` has exactly 500 real services,
compared `["remote-ns/*"]` against listing all 500 by name — same final
visible set either way.

**Standing cost, identical visible set — no meaningful difference:**

| | proxy WS | proxy clusters | config_dump | istiod WS |
|---|---:|---:|---:|---:|
| `remote-ns/*` (1 entry) | 103.4 MB | 516 | 2,801,615 B | 150.0 MB |
| 500 explicit entries | 106.1 MB | 516 | **2,801,615 B (identical)** | 151.2 MB |

`config_dump` is byte-for-byte identical — confirms Envoy's actual pushed
config is a function of the *resolved* visible set, not of how compactly the
Sidecar expressed the match. The ~1-3% deltas elsewhere are noise. Result 5's
istiod memory growth was from a *bigger visible set* (0 → 8000 real
services), not from wildcard-vs-list syntax at equal N — don't conflate the
two.

**Churn cost for a service already in scope — also no meaningful difference:**
patched `svc-1` (present in both configs) and measured propagation +
CPU under each:

| config | propagate time | istiod CPU | proxy CPU |
|---|---:|---:|---:|
| explicit 500 | 2.10s | 111ms | 349ms |
| `remote-ns/*` | 2.10s | 88ms | 319ms |

**Where they actually differ — churn from services outside your real usage
(the reason to switch in the first place), confirmed directly:** added 5
more services (`svc-501`..`505`) to `remote-ns`, representing services the
workload doesn't use but that still live in the namespace. Churned `svc-501`
under each config:

- **`remote-ns/*` active**: `svc-501` is visible (confirmed via
  `proxy-config cluster`); churning its port propagates to the proxy in
  ~1.05s, costing ~231ms proxy CPU — for a service the app never calls.
- **explicit-500 active**: `svc-501` never appears in the proxy's cluster
  list at all (confirmed both before and after the churn) — completely
  invisible, exactly the isolation you're switching for. (Didn't get a clean
  CPU number for this side — the poll loop ran to its full timeout since the
  change correctly never showed up, and each `istioctl proxy-config` call
  itself touches the proxy's Envoy admin API, so the CPU total from that run
  is measurement overhead, not signal. The `propagated=0` result is the real
  finding and is unambiguous.)

**Two real costs the explicit-list method reintroduces, unrelated to N being
equal:**
1. **CRD/etcd size scales with the list**, where the wildcard's is constant
   — a 1-line `remote-ns/*` doesn't care if the namespace has 5 or 50,000
   services; an explicit list re-exposes the ~31.5k-entry / etcd-request-size
   ceiling from Result 1 as your *actually-used* service count grows.
2. **Maintenance burden, by design**: a wildcard auto-covers new services in
   `remote-ns` (convenient, but that's the exact problem you're solving); an
   explicit list requires updating the Sidecar CR every time you start using
   a new one — the cost of precision.

**Bottom line**: at equal N, switching from `namespace/*` to an explicit list
costs essentially nothing extra on the standing-memory or istiod-processing
side — the win is real and lands exactly where you'd expect (churn isolation
from services you don't use), confirmed directly above. The trade is
operational, not architectural: you take on CRD-size scaling risk and a
manual-maintenance burden in exchange for that isolation.

## Result 7: Result 6 re-run at N=2000 — same conclusion, one new detail

Repeated the wildcard-vs-explicit comparison at 4x the scale (2000 real
services instead of 500), plus an explicit completeness check per request:
confirmed all 2000 explicitly-listed hostnames appear as real Envoy clusters
(`expected: 2000, present: 2000, missing: 0, unexpected-extra: 0`, spot-
checked `svc-1`/`svc-1000`/`svc-2000` individually, confirmed a real route
entry exists for `svc-1` too) — not just a coincidentally-matching total
count. Endpoint count stayed 0 for all of them, which is correct, not a
bug: these are deliberately podless fake Services (see Setup), so EDS
legitimately has nothing to report.

**Standing cost — still no meaningful difference at 4x the scale:**

| | proxy WS | proxy clusters | config_dump | istiod WS |
|---|---:|---:|---:|---:|
| `remote-ns/*` | 292.6 MB | 2016 | 10,340,003 B | 197.4 MB |
| 2000 explicit entries | 284.4 MB | 2016 | 10,291,302 B | 199.1 MB |

Same conclusion as N=500: within a few percent, i.e. noise.

**In-scope churn (`svc-1`) — still no meaningful difference:**

| config | propagate time | istiod CPU | proxy CPU |
|---|---:|---:|---:|
| explicit 2000 | 5.76s | 413ms | 1179ms |
| `remote-ns/*` | 5.71s | 215ms | 1167ms |

**New detail found at this scale — the "unused service" isolation test
reveals the wildcard's cost isn't about *which* service churns, it's about
scope size, period:** at N=500, churning an unused extra (`svc-501`) under
the wildcard cost ~1.05s / ~231ms proxy CPU (Result 6). At N=2000, churning
a brand-new unused extra (`svc-2001`) under the wildcard cost **~5.83s /
~1296ms proxy CPU — essentially identical to churning the well-known `svc-1`
at this same scope size**, not cheaper for being "new" or unfamiliar. This
confirms Result 3's mechanism cleanly: the proxy's cost on any single change
is driven by the total watched-scope size it has to reprocess, not by
anything special about the specific cluster that changed. Under
`explicit-2000`, `svc-2001` stayed completely invisible throughout (same as
Result 6's `svc-501` case) — isolation held at 4x scale with no degradation.

**Conclusion holds at scale**: wildcard-vs-explicit-list is a wash on
standing cost and in-scope churn cost at both 500 and 2000 real services;
the isolation benefit (not paying for services you don't use) scales
correctly too — and gets more valuable as your unused-service count grows,
since each such service now costs ~5.8s/~1.3s CPU under a wildcard instead
of ~1s/~230ms.

## Cleanup
`remote-ns` (8000 fake Services) and the `client` pod were deleted after
the test to release host memory. `local-ns` and the `Sidecar` CRD/webhook
config are untouched cluster-wide state, left as-is.

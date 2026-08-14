# SPIRE cross-cluster mTLS, dual-track with existing istiod mTLS

## Goal
Prove that an `agent` pod in one cluster can do mTLS to the `mcp` ingress
gateway in another cluster using **SPIRE-issued SPIFFE identities**, without
disturbing any existing pod's istiod-issued-CA mTLS traffic in either
cluster. Two pairings, per user request:

- **Pairing A**: `cluster1-134` (agent/client) ↔ `cluster2-134` (mcp
  ingress gateway/server) — both k8s 1.34.8 / Istio 1.29.6
- **Pairing B**: `cluster2` (agent/client) ↔ `cluster2-134` (server) —
  k8s 1.24.17 / Istio 1.13.5 talking to k8s 1.34.8 / Istio 1.29.6

`cluster1` (the broken twin of `cluster2`, apiserver down from unrelated
cert corruption) is intentionally excluded — not needed for either pairing.

## Chosen architecture (see decision log below)
The ingress gateway gets a **second, SPIRE-only listener/port**, alongside
its existing default Istio-mTLS listener(s) — untouched. The new listener's
server cert + trusted CA bundle come from SPIRE (via `spiffe-helper`
syncing the local SPIRE Agent's Workload API output into a Kubernetes
`Secret`, which Istio's `Gateway` resource references natively via
`tls.credentialName` — no EnvoyFilter/SDS hacking needed). The agent pod
gets its own SPIRE SVID the same way, and connects directly to the new
port using SPIRE-issued client cert/key — proving mTLS authenticated purely
by SPIFFE identity, cross-trust-domain, cross-cluster.

Rejected alternatives:
- **App-level mTLS bypassing the sidecar entirely**: doesn't exercise the
  ingress gateway at all, weaker demonstration of the actual ask.
- **Rewiring istio-agent's own SDS to pull from SPIRE**: would touch the
  gateway's *existing* listeners' cert source, directly risking the
  "don't disturb existing mTLS" requirement; also the deepest, most
  fragile Envoy/xDS customization of the three options.

## Trust domain layout
Each cluster keeps its own SPIRE Server (own root CA) — federation
exchanges trust *bundles*, not a shared CA, matching how SPIFFE federation
is meant to work across genuinely independent clusters.

| Cluster | Trust domain |
|---|---|
| `cluster1-134` | `cluster1-134.local` |
| `cluster2-134` | `cluster2-134.local` |
| `cluster2` | `cluster2.local` |

Federation is set up **statically** for this lab (one-time
`spire-server bundle set` exchange) rather than live `bundle_endpoint`
polling — proves the cross-trust-domain mTLS mechanism without adding a
second moving part (bundle-endpoint reachability/refresh) that isn't what
this experiment is actually testing. Noted as a natural follow-up.

## Status

### Phase 1 — SPIRE control plane: done
- SPIRE Server (StatefulSet, sqlite datastore, self-signed CA) + SPIRE
  Agent (DaemonSet, `k8s_psat` node attestation, `insecure_bootstrap` —
  lab-only shortcut) deployed to all three clusters via
  `gen_spire_cluster.py <cluster> <trust_domain> <out.yaml>`.
- Node attestation confirmed successful on all three
  (`spiffe://<trust_domain>/spire/agent/k8s_psat/<cluster>/<uuid>` issued).
- Trust bundles exchanged both directions for both pairings
  (`spire-server bundle show/set`, static — see architecture note above):
  `cluster2-134.local` ↔ `cluster1-134.local`,
  `cluster2-134.local` ↔ `cluster2.local`. Confirmed via `bundle list` on
  all four sides.

### 安裝過程踩坑記錄

**`k8s_psat` NodeAttestor 需要明確掛一個 projected serviceAccountToken
volume，不是自動就有。** 第一次 apply 之後 `spire-agent` 立刻
CrashLoopBackOff，log：

```
level=error msg="Agent crashed" error="rpc error: code = InvalidArgument
desc = nodeattestor(k8s_psat): unable to load token from
/var/run/secrets/tokens/spire-agent: open
/var/run/secrets/tokens/spire-agent: no such file or directory"
```

原因：`k8s_psat`（Projected Service Account Token）attestor 預期在
`/var/run/secrets/tokens/spire-agent` 讀到一個指定 audience 的 projected
token，但 Pod 預設掛的是一般的 default SA token（路徑、audience 都不對），
不會自動滿足這個需求。修法（見 `gen_spire_cluster.py` 裡
DaemonSet 的 `spire-token` volume）：

```yaml
volumes:
- name: spire-token
  projected:
    sources:
    - serviceAccountToken:
        path: spire-agent
        expirationSeconds: 7200
        audience: spire-server
```

掛到 `/var/run/secrets/tokens`，然後 agent 就能正常完成 node attestation。

**Agent 在 spire-server 還沒 Ready 前啟動，會有 1～2 次自癒式 restart，
屬正常現象。** 全新安裝時 agent 和 server 幾乎同時起來，agent 第一次連線
常常打在 server 還沒開始監聽 8081 的時間點，會重啟個一兩次後自己接上，
不是 bug，看到 `RESTARTS: 2` 不用緊張，穩定後不會再增加就好。

**`insecure_bootstrap = true` 是刻意的 lab 捷徑，不是生產配置。** 這個
選項讓 agent 第一次連 server 時跳過 server 憑證驗證（沒有預先分發的
bootstrap trust bundle）。方便在 Kind lab 裡快速起機，但正式環境應該用
`spire-server bundle show` 產生的 bootstrap bundle 掛進 agent 的
ConfigMap，透過 `-trust_bundle_path` / SPIRE 的 bootstrap 機制建立初始信任，
而不是跳過驗證。

**`cluster1`（1.13.5/1.24 那組的另一半）目前不在此實驗範圍內**——它的
apiserver 因為之前一次 docker 重啟後 serving cert 遺失、目前還是壞的，
跟 SPIRE 本身無關。跨叢集配對只需要 `cluster2` 一台就夠了，故意跳過修
`cluster1`。

### Phase 2 — next
Registration entries (agent workload identity per client cluster, mcp
ingress gateway identity on `cluster2-134`), `spiffe-helper` sidecars to
materialize SVID+bundle as a k8s Secret, new Istio `Gateway` listener with
`tls.credentialName`, then end-to-end mTLS test for both pairings while
confirming existing istiod-mTLS traffic is undisturbed.

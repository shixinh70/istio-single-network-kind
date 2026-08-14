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

### Phase 2 — done: both pairings verified end-to-end

**Result: both pairings work.**
```
$ curl (agent pod, SPIRE client cert) -> mcp-ingressgateway:30443 (SPIRE server cert, MUTUAL)
hello from mcp-echo via SPIRE mTLS
```
Confirmed for both `cluster1-134 → cluster2-134` and `cluster2 → cluster2-134`
— i.e. the cross-version pairing (Istio 1.13.5/k8s 1.24 client talking to
Istio 1.29.6/k8s 1.34 server) works exactly the same as the same-version one.

**Dual-track confirmed**: `istio-ingressgateway` (the default, pre-existing
gateway) has been `Running` with **0 restarts** for its entire 5-day-plus
uptime — completely undisturbed. No `PeerAuthentication` resource was ever
touched (`kubectl get peerauthentication -A` → none, mesh-wide default still
implicit PERMISSIVE). The mesh's own trust domain is still `cluster.local`
(istiod's own CA), entirely separate from the `*.local` SPIRE trust domains
used here. Everything SPIRE-related lives in new, additive resources
(`mcp-gw` namespace, `mcp-ingressgateway` Deployment, a new `Gateway`
resource on a new port) — nothing existing was edited.

### Workload setup
- **Registration entries** (`spire-server entry create`, parent ID = the
  cluster's single schedulable node's SPIRE Agent — each Kind cluster here
  has exactly one untainted `-worker` node, so exactly one relevant agent):
  `spiffe://cluster1-134.local/agent` (ns `agent`, sa `agent`),
  `spiffe://cluster2.local/agent` (same), `spiffe://cluster2-134.local/mcp-ingress`
  (ns `mcp-gw`, sa `mcp-ingressgateway`) — each also given
  `-federatesWith` pointing at its peer trust domain(s), see gotcha below.
- **Agent pods** (`manifests/agent-pod.yaml`, same file applied to both
  `cluster1-134` and `cluster2`): `app` container (curl) + `spiffe-helper`
  sidecar writing SVID/key/bundle to a shared `emptyDir`.
- **`mcp-ingress` gateway** (`manifests/mcp-ingress.yaml` +
  `manifests/mcp-gateway.yaml`, `cluster2-134` only): a Deployment using
  Istio's **gateway injection** (`inject.istio.io/templates: gateway`
  annotation — produces a standalone `istio-proxy`-only gateway pod, no
  Helm-managed ingressgateway install needed) plus two extra sidecars:
  `spiffe-helper` (SVID → local files) and `secret-sync` (a plain `alpine`
  container doing `curl` against the K8s API with its own ServiceAccount
  token — no `kubectl` binary needed — to push those files into a
  `kubernetes.io/tls`-typed Secret every 30s, since Istio's `Gateway.tls.
  credentialName` reads from a K8s Secret via SDS, not local files). A new
  `Gateway` resource adds a second listener (port 15443, `protocol: TLS`,
  `tls.mode: MUTUAL`, `credentialName: mcp-spire-cert`) alongside whatever
  the default gateway already serves — this workload is a **separate**
  Deployment from the mesh's default `istio-ingressgateway`, so the
  dual-track separation is structural, not just configuration-level.
  Exposed as `NodePort` (30443) since Kind clusters here share one flat
  Docker network — the simplest way to get real cross-cluster L3
  reachability in this lab without standing up a proper multi-cluster
  gateway/east-west setup.

### 安裝過程踩坑記錄（續 — Phase 2）

**SPIRE server 之間交換完 bundle，不代表 workload 就自動拿得到聯邦後的信任
清單。** `spire-server bundle set` 只讓 **server** 知道對方的 root CA，個別
workload 要透過自己的 Workload API stream 拿到這份 bundle，前提是它的
registration entry 有明確宣告 `-federatesWith spiffe://<對方trust
domain>`——沒宣告的話 `ca.crt` 永遠只會有自己 trust domain 的根憑證，跨
domain 驗證 server 憑證就會失敗。用 `spire-server entry update ...
-federatesWith` 補上去解決。

**entry 加了 `-federatesWith` 還不夠，`spiffe-helper` 自己也要另外開關**：
`helper.conf` 裡要加 `include_federated_domains = true`。沒開的話，那個
「合併後的單一 bundle 檔」還是只會有自己 trust domain 的憑證——症狀跟上面
那個一模一樣，很容易誤以為是 entry 那邊沒修好，其實是另一層的開關沒開。

**`spiffe-helper` 預設寫出來的 private key 權限太嚴，同一個 pod 裡的另一個
container 讀不到。** 第二個 container（`app`，跑不同 UID，pod 沒設共用的
`fsGroup`）讀 `/svids/tls.key` 時直接 `Permission denied`。curl 自己噴出來
的錯誤訊息還很誤導人（顯示 `unable to set private key file: type PEM`，
看起來像是格式問題，其實是權限問題）。用 `helper.conf` 裡的
`key_file_mode = 0444`（全部可讀）解決——這在 lab 裡、只在單一 pod 內共用
的 emptyDir 沒關係，正式環境不該這樣設。

**Istio Gateway 用 `protocol: TLS` + `tls.mode: MUTUAL`（終止型，不是
passthrough）時，要用 `VirtualService.tcp` route，不是 `.tls`。** 一開始
用 `tls:` block 配 `sniHosts` 比對（畢竟 listener protocol 字面上就寫
「TLS」，看起來很合理）——結果 istiod 悄悄把 listener 建出來了，但 log
只留下一行不起眼的 `gateway mcp-gw/mcp-spire-gateway:15443 listener missed
network filter`，Envoy 那邊實際上從頭到尾沒開 15443 這個 port，其他地方
完全不會報錯。`tls:`/`sniHosts` 那個 routing block 是專門給
**passthrough** 模式用的（Envoy 完全不解密，只靠 SNI 做路由決定，由
backend 自己終止 TLS）；一旦 gateway 自己終止 TLS（`SIMPLE`/`MUTUAL`），
出來的東西就只是解密後的 TCP，istiod 產生 gateway 設定的邏輯在這種情況下
只會從 `.tcp` route 建 filter chain（[官方已知行為](https://github.com/istio/istio/issues/37293)）。
改成 `tcp:` 的 match/route block（只比對 port，不用 `sniHosts`——反正解密
後也沒有這個資訊了）解決。

**SPIFFE 憑證本來就不帶一般 HTTP client 拿來比對 hostname 的 DNS
SAN/CN——這是設計如此，不是 bug。** `curl`（不加 `-k`）完整走完一次真正的
TLS 1.3 雙向握手（雙方都交換了憑證，也對著聯邦後的 bundle 驗證過信任鏈），
最後才失敗在 `SSL: unable to obtain common name from peer certificate`——
也就是說信任鏈驗證是真的成功了，只有 hostname 比對這一步（SPIFFE 憑證根本
沒填這個資訊）失敗。用 `-k`（剛好只跳過這一步驗證）重跑一次、拿到完整正確
的 HTTP 回應，確認了這個判讀是對的。真正的 client 應該要去驗證對方的
SPIFFE ID（URI SAN），而不是依賴 hostname 比對——這部分不在這次 lab 的
範圍內，用 `-k` 已經足夠證明 mTLS 這一層本身是通的。

**另一個跟 SPIRE 無關的小狀況**：gateway 的 ALPN 跟 client 談成了 `h2`，但
單純的 HTTP `hashicorp/http-echo` backend 只會講 HTTP/1.1——
`curl: (16) Remote peer returned unexpected data while we expected SETTINGS
frame`。在 client 端加 `--http1.1` 解決。跟 SPIRE 或雙軌制都無關，只是
TLS 這層一通了，才浮現出 backend 能力不匹配的問題。

## Phase 3 — 改用 Istio 原生的 per-pod SPIRE 機制（參考 [istio.io 官方 SPIRE 整合文件](https://istio.io/latest/docs/ops/integrations/spire/)）

Phase 1-2 的做法（自訂 Gateway port + spiffe-helper + 手動同步 K8s Secret）
確實能動，但真的偏複雜。這裡改用 Istio 官方文件描述的原生機制：讓
`istio-agent` 自己的 SDS 直接去讀 SPIRE 提供的 Workload API socket（透過
`spiffe-csi-driver` 掛進 pod），完全不用自己寫 Secret 同步腳本，也不用開
額外的 Gateway port——**per-pod opt-in，istiod 本身零改動**（只有一個例外，
見下面 federation 那段）。

### 新架構
- 部署 `spiffe-csi-driver`（SPIRE 官方 CSI driver，讓 pod 可以用
  ephemeral inline volume 掛進 SPIRE agent 的 socket），只裝在
  `cluster2-134`（這次簡化只重做 server 端）。
- 在 `istio-sidecar-injector` 這個 ConfigMap 裡加一個新的 `spire`
  injection template（直接照官方文件的內容），讓打了
  `spiffe.io/spire-managed-identity: "true"` label +
  `inject.istio.io/templates: "sidecar,spire"` annotation 的 pod，會多掛一個
  CSI volume 到 `istio-proxy` 這個 initContainer（native sidecar 模式）。
- 新增一個乾淨的 workload `mcp-echo-spire`（沒有動 Phase 1-2 原本的
  `mcp-ingressgateway`/`mcp-echo`，兩套並存），搭配一個只 scope 到這個
  workload 的 `PeerAuthentication`（`mode: STRICT`）。
- Client 端（agent pod）完全沒動，還是 Phase 1-2 那套 spiffe-helper +
  raw curl，因為簡化的重點在 server 端「怎麼把 SPIRE 憑證餵給 Envoy」，
  client 端本來就已經很單純了。

### 踩坑記錄（Phase 3）

**istio-agent 自己想在 CSI 掛進來的同一個路徑建立它自己的 SDS
socket，撞在一起。** 一開始直接照官方文件的 template 掛，結果 log 一直噴
`SDS grpc server for workload proxies failed to set up UDS: ... read-only
file system`，Envoy 起不來（`Init:1/2` 卡住，`startupProbe` 一直失敗）。
後來才確認：這個「掛在 read-only CSI volume 上」的失敗其實是**設計上預期
會發生**的事——istio-agent 偵測到這個路徑已經是別人（SPIRE）的 socket、
自己寫不進去，就會放棄自己開 SDS server，改讓 Envoy 直接跟那個既有 socket
講話。log 裡另一行 `Existing workload SDS socket found ... Default Istio
SDS Server will only serve files` + `Workload is using file mounted
certificates. Skipping connecting to CA` 才是關鍵的成功訊號，前面那個
`error` 等級的訊息其實是誤導人的雜訊，不是真正卡住的原因。

**真正卡住 Envoy 起不來的原因，是 socket 檔名對不上。** SPIRE agent 我原本
設定寫出來的檔案叫 `agent.sock`，但 istio-agent/Envoy 這條路徑寫死是要找
一個檔名剛好叫 `socket` 的檔案（`/run/secrets/workload-spiffe-uds/socket`）。
兩個名字對不上，Envoy 那邊直接是 `No such file or directory`。改
`cluster2-134` 的 spire-agent `socket_path` 為
`/run/spire/sockets/socket`（連帶把 Phase 1-2 mcp-ingress 的
spiffe-helper 設定也一起改掉，避免它壞掉）就解決了。

**mTLS handshake 第一次真的握手成功，但被拒絕，因為 federated bundle
沒有自動包含在 Envoy 拿到的 ROOTCA 裡。** 這跟 Phase 2 遇到的
`include_federated_domains` 是同一類問題，但**修法完全不同**，因為這次是
SPIRE agent 內建 SDS server 直接餵給 Envoy，不是透過 spiffe-helper。SPIRE
的 SDS 實作預設把「只有自己 trust domain」和「自己 + 所有 federated
domain」拆成兩個不同名字的資源（預設分別叫 `ROOTCA` 和 `ALL`），而
Istio/Envoy 寫死只會去要名字叫 `ROOTCA` 的那個。解法是利用這兩個資源名稱
本身是可設定的這件事：在 spire-agent 設定的 **`sds { }` 子區塊**
（一開始沒注意到要包一層 `sds{}`，直接放在 `agent{}` 底下會被判定成
`Unknown configuration detected` 整個 crash）裡把 `default_bundle_name`
（原本叫 `ROOTCA` 的那個「只有自己」）改名讓開，再把
`default_all_bundles_name`（原本叫 `ALL` 的「自己+federated」）直接改名成
`ROOTCA`——這樣 Istio 寫死要的那個名字，實際拿到的內容就是有包含
federated bundle 的那份：

```
sds {
  default_bundle_name = "ROOTCA_SELF_ONLY"
  default_all_bundles_name = "ROOTCA"
}
```

**最後一個坎：憑證信任鏈驗證過了，連線還是被砍，是 Istio 自己一個寫死的
SAN 前綴限制。** 錯誤變成 client 端 `Send failure: Broken pipe`，看起來
像是憑證問題但其實憑證早就驗過了。查 Envoy 的 listener config 才發現：
`STRICT` PeerAuthentication 會讓 Istio 自動產生一條
`match_subject_alt_names: [{prefix: "spiffe://cluster.local/"}]` 的規則
——這是 mesh 自己的 trust domain（`cluster.local`），跟憑證是誰簽的完全
無關，是**額外一層**、獨立於信任鏈驗證的 SAN 白名單檢查。SPIRE 簽的憑證
SAN 是 `spiffe://cluster1-134.local/agent`，前綴對不上，直接被拒絕。

這是這次簡化過程中**真正跟你原本的限制（不動 istiod 預設 CA 系統）擦邊**
的地方：修法是在 istiod 的 `meshConfig` 裡加
`trustDomainAliases: [cluster1-134.local, cluster2.local]`——嚴格說沒有
換掉 CA 或 CA_ADDR，但這是 **mesh 全域**的 istiod 設定，會影響全 mesh 所有
STRICT PeerAuthentication 的 SAN 比對規則，不是只限這一個 pod。跟你確認過
（選了「加 trustDomainAliases」），才動手改的。

### 結果

兩組配對都通過，跟 Phase 1-2 的結論一致：
```
$ curl (agent pod, SPIRE client cert) -> mcp-echo-spire:30444 (native istio-agent SDS, SPIRE cert)
hello from mcp-echo-spire via native Istio+SPIRE sidecar SDS
```
`cluster1-134 → cluster2-134` 和 `cluster2 → cluster2-134` 都成功。

**雙軌制依然成立**：預設 `istio-ingressgateway` 開機 5 天多、0 次重啟；
同一個 `mcp-gw` namespace 裡 Phase 1-2 的 `mcp-echo`（沒打 SPIRE label 的
那個）憑證還是正常的 `spiffe://cluster.local/ns/mcp-gw/sa/default`，完全
沒被 SPIRE 的東西影響；`PeerAuthentication` 只 scope 到 `mcp-echo-spire`
這一個 workload，不是整個 namespace。

### Phase 1-2 vs Phase 3 比較

| | Phase 1-2（自訂 Gateway） | Phase 3（原生 CSI + injection template） |
|---|---|---|
| Server 端多開的東西 | 自訂 Gateway CR、新 port（15443）、`spiffe-helper`+`secret-sync` 兩個額外 sidecar、手寫的 K8s Secret 同步腳本 | 只有一個 label + annotation，讓現有 pod 自己的 istio-proxy 直接吃 SPIRE 憑證 |
| 憑證怎麼餵給 Envoy | 手動把檔案轉成 K8s Secret，Envoy 透過 Istio 標準 SDS 讀 Secret | Envoy 直接跟 SPIRE agent 的 socket 講 SDS 協定，中間不經過 K8s Secret |
| istiod 有沒有被動到 | 完全沒有 | 這次多了 `trustDomainAliases`（mesh 全域，但不換 CA） |
| 跨 trust domain 需要額外處理 | 不用（自己手刻的 Gateway 不吃 mesh 的 SAN 限制邏輯） | 需要（撞到 STRICT PeerAuthentication 內建的 SAN 前綴限制） |
| 複雜度來源 | 都在「我自己寫的膠水」——好懂但零件多 | 零件少，但踩坑都在「Istio/SPIRE 內部沒寫在檯面上的行為」，第一次抓錯誤點比較花時間 |

兩條路都驗證過、都能用，各有取捨，寫在這裡給你自己選要留哪一套當正式
參考。

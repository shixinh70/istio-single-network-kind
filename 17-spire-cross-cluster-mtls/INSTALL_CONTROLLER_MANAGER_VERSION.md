# 完整安裝步驟：SPIRE Server/Agent + Controller Manager + ClusterSPIFFEID + Istio 原生 SDS

這份文件是最終選定的版本——**不用**自訂 Gateway/spiffe-helper+手動同步
Secret 那條路（Phase 1-2，留在 repo 裡當對照，不在這份文件範圍內），改用：
- Istio 官方 SPIRE 整合機制（`spiffe-csi-driver` + per-pod injection
  template），讓 `istio-agent` 自己的 SDS 直接讀 SPIRE 憑證
- SPIRE Controller Manager + `ClusterSPIFFEID` CRD 宣告式管理
  registration entry，取代手動 `spire-server entry create`

從頭跑一遍需要的**每一條指令**都在這裡，照順序執行即可重現。背景脈絡、
踩過的坑、為什麼選這條路，見同目錄 `README.md` 的 Phase 1、3、4。

## 拓樸

| Cluster | 角色 | Trust domain | k8s / Istio |
|---|---|---|---|
| `cluster1-134` | agent（client） | `cluster1-134.local` | v1.34.8 / 1.29.6 |
| `cluster2` | agent（client） | `cluster2.local` | v1.24.17 / 1.13.5 |
| `cluster2-134` | mcp ingress（server） | `cluster2-134.local` | v1.34.8 / 1.29.6 |

兩組配對：`cluster1-134 → cluster2-134`、`cluster2 → cluster2-134`。
每個 cluster 各自獨立的 SPIRE Server（自己的 root CA），彼此用
**static bundle federation**（`spire-server bundle show/set`）互相信任
——不是 live `bundle_endpoint` 輪詢，這是刻意的簡化，見 README 說明。

所有指令都用同目錄下的 script，先 `cd` 進去：
```bash
cd ~/Desktop/istio-single-network-kind/17-spire-cross-cluster-mtls
```

## Step 1 — 部署 SPIRE Server + Agent（三個叢集都要）

```bash
python3 gen_spire_cluster.py cluster1-134 cluster1-134.local manifests/spire-cluster1-134.yaml
python3 gen_spire_cluster.py cluster2-134 cluster2-134.local manifests/spire-cluster2-134.yaml
python3 gen_spire_cluster.py cluster2      cluster2.local     manifests/spire-cluster2.yaml

kubectl --context=cluster1-134 apply -f manifests/spire-cluster1-134.yaml
kubectl --context=cluster2-134 apply -f manifests/spire-cluster2-134.yaml
kubectl --context=cluster2     apply -f manifests/spire-cluster2.yaml
```

等三邊的 `spire-agent` DaemonSet 都變成 `1/1 Running`：
```bash
kubectl --context=cluster1-134 -n spire get pods
kubectl --context=cluster2-134 -n spire get pods
kubectl --context=cluster2     -n spire get pods
```

**這一步做了什麼**：每個叢集各裝一個 SPIRE Server（StatefulSet，sqlite
datastore，自己簽自己的 root CA）+ SPIRE Agent（DaemonSet，`k8s_psat`
node attestation，`insecure_bootstrap=true`——lab 用的捷徑，正式環境要用
`trust_bundle_path` 走預先分發 bootstrap bundle 的正規流程）。Agent 的
Workload API socket 檔名固定叫 `socket`（不是 `agent.sock`），這是配合
Step 5 Istio 原生 SDS 整合寫死的檔名慣例。

**已知踩坑**（細節見 README）：`k8s_psat` NodeAttestor 需要一個 projected
serviceAccountToken volume（script 裡已經處理好了）；agent 剛起來時可能
自癒式重啟 1-2 次，等 server 先 ready 就會自己接上，不用管。

## Step 2 — 記錄每個 cluster 的 SPIRE Agent SPIFFE ID

之後設定 `parentID`/`ClusterSPIFFEID` selector 會用到（也可以跳過這步，
因為 Step 4 的 Controller Manager 是靠 `parentIDTemplate` 自動算出來的，
不需要手動查——這步純粹是給你自己想手動核對用）：

```bash
kubectl --context=cluster1-134 -n spire exec spire-server-0 -c spire-server -- \
  /opt/spire/bin/spire-server agent list
kubectl --context=cluster2-134 -n spire exec spire-server-0 -c spire-server -- \
  /opt/spire/bin/spire-server agent list
kubectl --context=cluster2 -n spire exec spire-server-0 -c spire-server -- \
  /opt/spire/bin/spire-server agent list
```

## Step 3 — Federation：交換 trust bundle（三邊互相 federate）

```bash
SCRATCH=/tmp/spire-bundles  # 換成你自己的暫存路徑
mkdir -p $SCRATCH

kubectl --context=cluster2-134 -n spire exec spire-server-0 -c spire-server -- \
  /opt/spire/bin/spire-server bundle show -format spiffe > $SCRATCH/bundle-cluster2-134.json
kubectl --context=cluster1-134 -n spire exec spire-server-0 -c spire-server -- \
  /opt/spire/bin/spire-server bundle show -format spiffe > $SCRATCH/bundle-cluster1-134.json
kubectl --context=cluster2 -n spire exec spire-server-0 -c spire-server -- \
  /opt/spire/bin/spire-server bundle show -format spiffe > $SCRATCH/bundle-cluster2.json

kubectl --context=cluster2-134 -n spire exec -i spire-server-0 -c spire-server -- \
  /opt/spire/bin/spire-server bundle set -format spiffe -id spiffe://cluster1-134.local < $SCRATCH/bundle-cluster1-134.json
kubectl --context=cluster1-134 -n spire exec -i spire-server-0 -c spire-server -- \
  /opt/spire/bin/spire-server bundle set -format spiffe -id spiffe://cluster2-134.local < $SCRATCH/bundle-cluster2-134.json
kubectl --context=cluster2-134 -n spire exec -i spire-server-0 -c spire-server -- \
  /opt/spire/bin/spire-server bundle set -format spiffe -id spiffe://cluster2.local < $SCRATCH/bundle-cluster2.json
kubectl --context=cluster2 -n spire exec -i spire-server-0 -c spire-server -- \
  /opt/spire/bin/spire-server bundle set -format spiffe -id spiffe://cluster2-134.local < $SCRATCH/bundle-cluster2-134.json
```

驗證（每邊都應該列出對方的 trust domain）：
```bash
kubectl --context=cluster2-134 -n spire exec spire-server-0 -c spire-server -- /opt/spire/bin/spire-server bundle list
kubectl --context=cluster1-134 -n spire exec spire-server-0 -c spire-server -- /opt/spire/bin/spire-server bundle list
kubectl --context=cluster2     -n spire exec spire-server-0 -c spire-server -- /opt/spire/bin/spire-server bundle list
```

⚠️ **`spire-data` 這個 volume 是 `emptyDir`，不是 PVC**——只要
`spire-server-0` 這個 pod 被重建（不管是手動刪除、StatefulSet 改設定、
node 重啟…），整個 sqlite datastore（bundle + entry）都會歸零，**這一步
要重做一次**。正式環境要把 `gen_spire_cluster.py` 裡的
`spire-data` 換成有 PVC 的 `volumeClaimTemplate`。

## Step 4 — 部署 SPIRE Controller Manager（三個叢集都要）

先裝 CRD：
```bash
for ctx in cluster1-134 cluster2-134 cluster2; do
  kubectl --context=$ctx apply -f manifests/crds/clusterspiffeids.yaml
  kubectl --context=$ctx apply -f manifests/crds/clusterfederatedtrustdomains.yaml
done
```

再把 controller-manager 塞進既有的 `spire-server` StatefulSet（同一個
pod，多一個 container，透過本機 admin socket
`/tmp/spire-server/private/api.sock` 跟 spire-server 溝通，不開 network
port）：
```bash
python3 gen_controller_manager.py cluster1-134 cluster1-134.local manifests/controller-manager-cluster1-134.yaml
python3 gen_controller_manager.py cluster2-134 cluster2-134.local manifests/controller-manager-cluster2-134.yaml
python3 gen_controller_manager.py cluster2      cluster2.local     manifests/controller-manager-cluster2.yaml

kubectl --context=cluster1-134 apply -f manifests/controller-manager-cluster1-134.yaml
kubectl --context=cluster2-134 apply -f manifests/controller-manager-cluster2-134.yaml
kubectl --context=cluster2     apply -f manifests/controller-manager-cluster2.yaml
```

確認每邊的 `spire-server-0` 變成 `2/2 Running`：
```bash
kubectl --context=cluster1-134 -n spire get pod spire-server-0
kubectl --context=cluster2-134 -n spire get pod spire-server-0
kubectl --context=cluster2     -n spire get pod spire-server-0
```

⚠️ **這步會讓 `spire-server-0` 重建一次 pod**（改 StatefulSet spec 觸發
rolling update）——回頭去看 Step 3，資料庫又會被清空，**Step 3 的
bundle 交換要在這步之後、Step 6 建 `ClusterSPIFFEID` 之前，重做一次**。
（這份文件的指令順序已經把這個坑排除掉了——Step 3 特意寫在 Step 4
之前只是說明先後邏輯，實際操作請照這份文件從頭到尾走一次，不要把 Step 3
的指令留到最後才跑；如果你是分階段、跨 session 操作，記得每次
`spire-server-0` 重建後都要重跑 Step 3。）

⚠️ **改 StatefulSet 一定要用 `gen_controller_manager.py` 產生的版本
（兩個 container 都有），不要再用 `gen_spire_cluster.py` 產生的版本去
`apply`**——後者不知道 controller-manager 這個 container 的存在，重新
apply 會把它整個蓋掉，變回 `1/1`。如果不小心蓋掉了，重新
`kubectl apply -f manifests/controller-manager-<cluster>.yaml` 一次就
救得回來。

## Step 5 — 部署 `spiffe-csi-driver`（只需要 server 端，`cluster2-134`）

```bash
kubectl --context=cluster2-134 apply -f manifests/spiffe-csi-driver.yaml
kubectl --context=cluster2-134 -n spire get pods -l app=spiffe-csi-driver
```

## Step 6 — Patch `istio-sidecar-injector`，加入 `spire` template（`cluster2-134`）

```bash
python3 patch_sidecar_injector_spire_template.py cluster2-134
```

驗證：
```bash
kubectl --context=cluster2-134 -n istio-system get cm istio-sidecar-injector \
  -o jsonpath='{.data.config}' | grep -A10 '^spire:'
```

⚠️ Script 裡預設用 `initContainers`（Istio native sidecar 模式，
`istio-proxy` 是 initContainer）。如果你的 Istio 版本較舊、`istio-proxy`
是一般 container 不是 initContainer，要把 script 裡 `initContainers`
改成 `containers`。判斷方式：
```bash
kubectl -n <任一個有 sidecar 的 namespace> get pod <pod名> \
  -o jsonpath='{.spec.initContainers[*].name}'
# 有印出 istio-proxy 就是 native sidecar 模式，維持 initContainers 不用改
```

## Step 7 — 讓 `spire-agent` 的 SDS 把 federated bundle 塞進 Envoy 要的 `ROOTCA`（`cluster2-134`）

```bash
python3 patch_spire_agent_sds_federated_rootca.py cluster2-134
kubectl --context=cluster2-134 -n spire delete pod -l app=spire-agent
```

等新的 `spire-agent` pod `1/1 Running` 後再繼續。

## Step 8 — 加 `meshConfig.trustDomainAliases`（`cluster2-134`，istiod 全域設定）

```bash
python3 patch_mesh_trust_domain_aliases.py cluster2-134 cluster1-134.local cluster2.local
```

驗證：
```bash
kubectl --context=cluster2-134 -n istio-system get cm istio \
  -o jsonpath='{.data.mesh}' | grep -A3 trustDomainAliases
```

這是整套設定裡**唯一**動到 istiod 全域設定的地方——不是換 CA/CA_ADDR，
只是放寬 `STRICT` PeerAuthentication 內建的 SAN 前綴比對名單。原因見
README「踩坑記錄」。

## Step 9 — 部署 server workload：`mcp-echo-spire` + `PeerAuthentication`（`cluster2-134`）

```bash
kubectl --context=cluster2-134 apply -f manifests/mcp-echo-spire.yaml
```

等 pod `2/2 Running`：
```bash
kubectl --context=cluster2-134 -n mcp-gw get pod -l app=mcp-echo-spire
```

驗證 istio-proxy 真的在用 SPIRE 憑證而不是 istiod 的（log 裡要看到這兩行）：
```bash
kubectl --context=cluster2-134 -n mcp-gw logs -l app=mcp-echo-spire -c istio-proxy | \
  grep "Existing workload SDS socket found\|Skipping connecting to CA"
```

## Step 10 — 建立 `ClusterSPIFFEID`（取代手動 `entry create`）

```bash
kubectl --context=cluster1-134 apply -f manifests/clusterspiffeids-cluster1-134.yaml
kubectl --context=cluster2     apply -f manifests/clusterspiffeids-cluster2.yaml
kubectl --context=cluster2-134 apply -f manifests/clusterspiffeids-cluster2-134.yaml
```

驗證 Controller Manager 真的照 CR 建出對應的 registration entry：
```bash
kubectl --context=cluster1-134 -n spire exec spire-server-0 -c spire-server -- /opt/spire/bin/spire-server entry show
kubectl --context=cluster2     -n spire exec spire-server-0 -c spire-server -- /opt/spire/bin/spire-server entry show
kubectl --context=cluster2-134 -n spire exec spire-server-0 -c spire-server -- /opt/spire/bin/spire-server entry show
```

⚠️ 每個歷史上「曾經匹配過」的 pod UID 都會各留一條 entry，pod 刪掉後不會
立刻消失，Controller Manager 內建的 GC（預設 10 秒一輪）會慢慢清掉——這
是正常行為，不是重複建立的 bug，log 裡看得到 `entry-reconciler Deleted
entry` 就是證據：
```bash
kubectl --context=cluster2-134 -n spire logs spire-server-0 -c spire-controller-manager | \
  grep "entry-reconciler"
```

## Step 11 — 部署 client workload：`agent` pod（`cluster1-134`、`cluster2`）

```bash
kubectl --context=cluster1-134 apply -f manifests/agent-pod.yaml
kubectl --context=cluster2     apply -f manifests/agent-pod.yaml
```

等兩邊都 `2/2 Running`：
```bash
kubectl --context=cluster1-134 -n agent get pod agent
kubectl --context=cluster2     -n agent get pod agent
```

## Step 12 — 驗證：兩組配對實測 cross-cluster mTLS

`mcp-echo-spire` 的 Service 用 NodePort `30444` 對外暴露（Kind 叢集共用
同一個 docker network，直接用 worker node 的 IP 就能跨叢集互通）：

```bash
kubectl --context=cluster2-134 -n mcp-gw get svc mcp-echo-spire
docker inspect cluster2-134-worker --format '{{.NetworkSettings.Networks.kind.IPAddress}}'
# 假設是 172.18.0.2，以下指令照這個 IP 調整
```

**Pairing A**（`cluster1-134` → `cluster2-134`）：
```bash
kubectl --context=cluster1-134 -n agent exec agent -c app -- \
  curl -sS -k --http1.1 --resolve mcp-echo-spire.mcp-gw.svc.cluster.local:30444:172.18.0.2 \
  --cert /svids/tls.crt --key /svids/tls.key --cacert /svids/ca.crt \
  https://mcp-echo-spire.mcp-gw.svc.cluster.local:30444/
```

**Pairing B**（`cluster2` → `cluster2-134`）：
```bash
kubectl --context=cluster2 -n agent exec agent -c app -- \
  curl -sS -k --http1.1 --resolve mcp-echo-spire.mcp-gw.svc.cluster.local:30444:172.18.0.2 \
  --cert /svids/tls.crt --key /svids/tls.key --cacert /svids/ca.crt \
  https://mcp-echo-spire.mcp-gw.svc.cluster.local:30444/
```

兩條都應該回：
```
hello from mcp-echo-spire via native Istio+SPIRE sidecar SDS
```

`-k` 是跳過 curl 自己的 hostname/CN 比對（SPIFFE 憑證本來就沒有 DNS
SAN/CN 給一般 HTTP client 比對，這是設計如此，不是沒驗證——實際的 mTLS
信任鏈驗證在憑證交換那一步就已經真的做過了，`-k` 只是跳過 curl 額外加碼
的 hostname 檢查）。`--http1.1` 是因為 gateway 的 ALPN 會跟 client 談
`h2`，但單純 HTTP 的 backend 只會講 HTTP/1.1，不加會斷線。

## 驗證雙軌制沒被破壞

```bash
# 預設 istio-ingressgateway 完全沒被動到
kubectl --context=cluster2-134 -n istio-system get pods -l app=istio-ingressgateway

# PeerAuthentication 只 scope 到 mcp-echo-spire 這一個 workload
kubectl --context=cluster2-134 -n mcp-gw get peerauthentication
```

## Cleanup（全部砍掉重來）

```bash
for ctx in cluster1-134 cluster2-134 cluster2; do
  kubectl --context=$ctx delete ns agent mcp-gw spire --ignore-not-found
  kubectl --context=$ctx delete crd clusterspiffeids.spire.spiffe.io clusterfederatedtrustdomains.spire.spiffe.io --ignore-not-found
  kubectl --context=$ctx delete clusterrole,clusterrolebinding -l '!kubernetes.io/bootstrapping' --field-selector metadata.name=spire-server-trust-role --ignore-not-found 2>/dev/null || true
done
# cluster2-134 專屬：CSI driver + mesh 設定（trustDomainAliases 不會自動還原，需要手動改回空陣列或整段刪除）
kubectl --context=cluster2-134 delete -f manifests/spiffe-csi-driver.yaml --ignore-not-found
```

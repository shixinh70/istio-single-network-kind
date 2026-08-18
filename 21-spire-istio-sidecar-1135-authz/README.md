# Istio 1.13.5 + 真正的 istio-proxy sidecar + SPIRE 自訂身份 + AuthorizationPolicy principal 卡控

## 目的
延續 `19-diy-shared-root-controller-manager/`（DIY 共用 root + `ClusterSPIFFEID`）
跟 `20-spire1152-istio1135/`（SPIRE 1.15.2 + Istio 1.13.5 相容性驗證），這次要
做的是使用者最終確認的完整架構：

- client、server **都掛真正的 istio-proxy sidecar**（不是 `20-` 那種繞過
  mesh 直接用 `spiffe-helper` peer-to-peer）
- SPIRE 發自訂 SPIFFE ID 給 client/server，**走 Istio 原生 mTLS**（不是
  應用層自己接 mTLS）
- server 用 `AuthorizationPolicy` 的 `principals` 卡控只有特定 client 能連
- 沒有標註要用 SPIRE 身份的 sidecar，繼續正常用 istiod 內建 CA，兩邊互不
  影響
- SPIRE 之間用共同 root 簽 intermediate（DIY shared-root，沿用 `19-`/`20-`
  的做法，不用 `bundle_set`/federation）

## 核心障礙：Istio 1.13.5 沒有原生 SPIRE socket 自動偵測

Istio **1.14+** 的 istio-agent 會自動偵測
`/run/secrets/workload-spiffe-uds/socket` 這個 UDS 存不存在，存在就直接跳過
istiod CA，讓 Envoy 直接跟這個 socket 要憑證（`istio/istio#37947`，2022-03-31
合併，`v1.14.0` 才第一次出現，`v1.13`/`v1.12`/`v1.11` 的官方文件頁面查證
都是 404，用官方 GitHub API 直接找到那條 PR 確認）。**1.13.5 的
istio-agent 完全沒有這段邏輯**——`spiffe-csi-driver` 掛好了、annotation
下對了，sidecar 還是照樣去問 istiod 要憑證，日誌完全不會提到那個 socket。

這是版本硬限制，不是設定錯誤，所以整個 `21-` 要做的事，本質上就是**手動
重現 1.14+ 自動做的那件事**。

## 走過的三條路（記錄失敗原因，避免重踩）

### 死路 1：EnvoyFilter `CLUSTER`/`ADD` 新增一個指向 SPIRE socket 的 cluster

理論上很直覺：加一個新 cluster 指到 SPIRE socket，再用 EnvoyFilter 把
listener 的 TLS context 的 `sds_config` 指過去。實測直接被 Envoy 拒絕：

```
Internal:Error adding/updating listener(s) virtualInbound: envoy.config.core.v3.ApiConfigSource
must have a statically defined non-EDS cluster: 'spire_agent' does not exist, was added via api,
or is an EDS cluster
```

原因：Envoy 規定 `ApiConfigSource`（SDS 用的那個）**只能指向 bootstrap
階段就存在的 STATIC cluster**（SDS 邏輯上早於 ADS 連線，不能依賴動態
下發的 cluster）。EnvoyFilter 的 `CLUSTER`/`ADD` 是透過 CDS（也就是
istiod 動態下發）加進去的，天生就不符合這個要求，無解。

### 死路 2：EnvoyFilter `BOOTSTRAP`/`MERGE`

EnvoyFilter 確實有 `applyTo: BOOTSTRAP` 這個選項，理論上可以在 bootstrap
產生階段就注入一個真正 STATIC 的 cluster。實測套用後完全沒有生效
（`static_resources.clusters` 裡就是沒有出現）。原因：1.13.5 的
istio-agent 產生 bootstrap 是**完全在本地、離線做的**（讀內建 Go
template + `ProxyConfig`），過程中不會去問 istiod「有沒有適用的
EnvoyFilter」——這個「先跟 istiod 要 bootstrap-relevant EnvoyFilter 再
產生」的能力，在這個版本根本沒接上（同樣是版本落差問題，只是換了個地方
出現）。

### 死路 3：直接用 `sidecar.istio.io/bootstrapOverride` 重新定義既有的 `sds-grpc` cluster

`bootstrapOverride` 這個機制是真的有效的（透過 pilot-agent 的
`--config-yaml` 把 ConfigMap 內容跟預設 bootstrap **merge** 起來），一開始
想說最省事的做法：既然所有 listener 的 SDS 都已經指向名叫 `sds-grpc` 的
cluster，那就用這個機制把 `sds-grpc` **這個名字本身**重新定義成指向 SPIRE
socket，這樣完全不用碰任何 listener/filter chain patch。結果 Envoy 啟動時
直接 crash：

```
critical envoy main] error initializing configuration: cluster manager: duplicate cluster 'sds-grpc'
```

原因：`--config-yaml` 的 merge 對 repeated field（`static_resources.clusters`
是一個 list）是**用 append 的，不是用 name 覆蓋**——同名的
`sds-grpc` 變成兩個，Envoy 對 cluster 名稱做唯一性檢查，直接拒絕啟動。

## 真正走通的路：`proxy.istio.io/config` 的 `customConfigFile`（完整替換 bootstrap，不是 merge）

pilot-agent 除了「merge 一份 overlay 上去」的 `bootstrapOverride`，還有
一個**完全替換**用來產生 bootstrap 的來源檔案的機制：`ProxyConfig` 的
`customConfigFile` 欄位（透過 `proxy.istio.io/config` annotation 設定），
對應到 pilot-agent 自己的 `--templateFile` flag。用這個機制提供一份**完整
的、已經渲染好的 bootstrap JSON**（照抄這顆 pod 原本正常運作時的
bootstrap，只把裡面 `sds-grpc` 這個 cluster 的 socket path 從
`./etc/istio/proxy/SDS`（istio-agent 自己的本地 SDS server）改成
`/run/secrets/workload-spiffe-uds/socket`（SPIRE Agent 的 socket，這個
socket 是雙協定的，除了 SPIFFE Workload API 也直接支援 Envoy 原生 SDS
協定）——因為是完整替換不是 merge，不會有 duplicate cluster 或 repeated
field append 的問題。

驗證結果（`pilot-agent request GET certs`，server 端）：
```json
"subject_alt_names": [{"uri": "spiffe://diy-1152.local/cluster/cluster2/istio-server"}]
```
Envoy 現在真的在用 SPIRE 簽的憑證，不是 istiod 的。istio-agent 自己內部
那個 SDS server 還是照常啟動、照常跟 istiod 要憑證（log 還是會看到
`generated new workload certificate`）——只是現在**沒有人接它**，因為
`sds-grpc` 已經被指到別的地方去了，那份憑證變成孤兒，不影響任何實際流量。

## 完整安裝步驟

沿用 `20-spire1152-istio1135/` 已經裝好的 SPIRE 1.15.2 控制平面
（namespace `spire-1315`、trust domain `diy-1152.local`）。

### Step 1：spiffe-csi-driver（掛 SPIRE Agent socket 進 istio-proxy）
```bash
kubectl --context=cluster1 apply -f manifests/spiffe-csi-driver.yaml
kubectl --context=cluster2 apply -f manifests/spiffe-csi-driver.yaml
```

### Step 2：patch sidecar injector，加上自訂的 "spire" injection template
```bash
python3 patch_sidecar_injector_spire_template_1135.py cluster1
python3 patch_sidecar_injector_spire_template_1135.py cluster2
```
這個 template 會掛兩個東西進 istio-proxy：SPIRE socket（CSI 驅動）跟
`spire-full-bootstrap` 這個 ConfigMap（下一步產生）。

### Step 3：`ClusterSPIFFEID`（client/server 各自的 SPIRE 身份）
```bash
kubectl --context=cluster1 apply -f manifests/clusterspiffeids.yaml
kubectl --context=cluster2 apply -f manifests/clusterspiffeids.yaml
```

### Step 4：部署 client / server，產生對應的 full-bootstrap ConfigMap
```bash
kubectl --context=cluster1 apply -f manifests/istio-client.yaml
kubectl --context=cluster2 apply -f manifests/istio-server.yaml
kubectl --context=cluster2 apply -f manifests/istio-client.yaml   # cluster2 本地也放一份，用來測 DENY

# 用「還沒套用 customConfigFile annotation」前、正常運作中的 pod 產生 full-bootstrap
python3 gen_custom_bootstrap.py cluster1 spire-istio-client istio-client manifests/custom_bootstrap_full_client1.json
python3 gen_custom_bootstrap.py cluster2 spire-istio-client istio-client manifests/custom_bootstrap_full_client.json
python3 gen_custom_bootstrap.py cluster2 spire-istio-server <server-pod-name> manifests/custom_bootstrap_full_server.json

kubectl --context=cluster1 -n spire-istio-client create configmap spire-full-bootstrap \
  --from-file=custom_bootstrap_full.json=manifests/custom_bootstrap_full_client1.json --dry-run=client -o yaml | kubectl --context=cluster1 apply -f -
kubectl --context=cluster2 -n spire-istio-client create configmap spire-full-bootstrap \
  --from-file=custom_bootstrap_full.json=manifests/custom_bootstrap_full_client.json --dry-run=client -o yaml | kubectl --context=cluster2 apply -f -
kubectl --context=cluster2 -n spire-istio-server create configmap spire-full-bootstrap \
  --from-file=custom_bootstrap_full.json=manifests/custom_bootstrap_full_server.json --dry-run=client -o yaml | kubectl --context=cluster2 apply -f -
```

`istio-client.yaml`/`istio-server.yaml` 裡已經帶有
`proxy.istio.io/config: |\n  customConfigFile: "/etc/istio/custom-bootstrap/custom_bootstrap_full.json"`
annotation，重新 apply/restart 之後 pod 就會用這份客製 bootstrap 啟動：
```bash
kubectl --context=cluster1 delete pod istio-client -n spire-istio-client
kubectl --context=cluster1 apply -f manifests/istio-client.yaml
kubectl --context=cluster2 delete pod istio-client -n spire-istio-client
kubectl --context=cluster2 apply -f manifests/istio-client.yaml
kubectl --context=cluster2 -n spire-istio-server rollout restart deployment istio-server
```

### Step 5：`DestinationRule` 讓 client 的自動 mTLS 認得自訂 SPIFFE ID
```bash
kubectl --context=cluster2 apply -f manifests/destinationrule-spire-san.yaml
```
細節看下面「坑」的部分——沒有這個，client 端的憑證驗證會直接失敗。

## 安裝過程踩的坑

### 坑：Istio 自動 mTLS 產生的驗證規則，預設只認「namespace/serviceaccount」這種 SAN 格式

`AuthorizationPolicy` 要用 `principals` 分辨 cluster1 跟 cluster2 的
client，所以 `ClusterSPIFFEID` 故意把叢集名稱編進 SPIFFE ID 路徑
（`.../cluster/{{ .ClusterName }}/istio-client`），不是 Istio 自己會猜的
`.../ns/{namespace}/sa/{serviceaccount}` 格式。這造成 client 呼叫 server
時，client 自己的出向 Envoy 直接回 `CERTIFICATE_VERIFY_FAILED`——因為
Istio 幫 client 自動產生的「應該驗證 server 憑證的哪個 SAN」清單，是根據
server 的 k8s namespace/serviceaccount 猜的：
```
spiffe://cluster2.local/ns/spire-istio-server/sa/istio-server
spiffe://diy-1152.local/ns/spire-istio-server/sa/istio-server
```
跟 server 實際拿到的憑證（`.../cluster/cluster2/istio-server`）兜不起來。
修法：加一個 `DestinationRule`（`trafficPolicy.tls.mode: ISTIO_MUTUAL` +
明確列出 `subjectAltNames`），把我們自訂的 SPIFFE ID 路徑額外告訴 client
的驗證邏輯。

## 結果

**cluster2 本地 client（錯誤 principal，預期 DENY）：**
```
HTTP_CODE:403
```
server 端 log：
```
rbac_access_denied_matched_policy[none] ... peer_uri_san="spiffe://diy-1152.local/cluster/cluster2/istio-client"
```
`AuthorizationPolicy` 正確地從**真正 mTLS 交握**拿到的 client 憑證 SAN
去判斷，且正確 DENY（因為只允許 `.../cluster/cluster1/istio-client`）。

**cluster1 client（正確 principal）：** 已確認能正確簽出對應憑證
（`spiffe://diy-1152.local/cluster/cluster1/istio-client`）；跨叢集的
ALLOW 呼叫測試需要額外的 east-west gateway/ServiceEntry 讓 cluster1 的
outbound envoy 把對 cluster2 NodePort 的呼叫辨識成 mesh 內流量（目前
`peer-client`/`peer-server`-style 純 IP:NodePort 呼叫，client 端不會觸發
Istio 自動 mTLS，這是跟本次 SDS 阻塞完全獨立、另一個關於跨叢集服務發現的
問題，非本次要驗證的範圍）。

**非標註 pod（沒有 `spiffe.io/spire-managed-identity` 的其他 sidecar）**：
不受影響，繼續使用 istiod 內建 CA——因為整個機制只透過個別 pod 自己的
`customConfigFile`/`bootstrapOverride` 生效，沒有動到全域的
`istio-sidecar-injector` 預設 "sidecar" template 或 mesh 層級設定。

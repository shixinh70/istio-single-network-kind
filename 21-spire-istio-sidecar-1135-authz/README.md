# Istio 1.13.5 + 真正的 istio-proxy sidecar + SPIRE 自訂身份 + AuthorizationPolicy principal 卡控

完整、可離線、從零開始的安裝指南。目標架構：client、server 都掛真正的
istio-proxy sidecar；SPIRE 發自訂 SPIFFE ID 給兩邊；server 用
`AuthorizationPolicy` 的 `principals` 只放行特定叢集的 client；沒有標註
的其他 pod 繼續用 istiod 內建 CA，完全不受影響；兩座叢集的 SPIRE 用同一顆
離線 root 簽 intermediate（DIY shared-root，不用 federation/`bundle_set`）。

架構原理跟為什麼要這樣設計，見同目錄下 [`AGENT-MESH-MTLS.md`](./AGENT-MESH-MTLS.md)。

## 前提

- 兩座叢集（這裡的 kubectl context 叫 `cluster1`／`cluster2`），**Istio
  1.13.5** sidecar 模式已經裝好，`istioctl`/`kubectl` 都能連得上
- `python3`（含標準函式庫即可，不需要額外套件）
- 離線環境：所有 image 要先搬進你自己的 registry，見下面「Image 清單」
  跟 [`OFFLINE_INSTALL.md`](./OFFLINE_INSTALL.md)

## Image 清單

### 基礎設施 image（這次要新裝的東西，一定要）

| Image | 用途 |
|---|---|
| `ghcr.io/spiffe/spire-server:1.15.2` | SPIRE Server |
| `ghcr.io/spiffe/spire-agent:1.15.2` | SPIRE Agent（DaemonSet） |
| `ghcr.io/spiffe/spire-controller-manager:0.7.0` | `ClusterSPIFFEID` 宣告式管理 entry |
| `ghcr.io/spiffe/spiffe-csi-driver:0.2.7` | 把 SPIRE Agent socket 掛進 istio-proxy 的 CSI driver |
| `registry.k8s.io/sig-storage/csi-node-driver-registrar:v2.6.0` | CSI driver 的 node registrar sidecar |

### 這次 lab 測試用的範例 workload image（正式環境會換成你自己的 app）

| Image | 用途 |
|---|---|
| `hashicorp/http-echo:1.0.0` | server 端最小 echo backend |
| `curlimages/curl:8.16.0` | client 端測試工具 |

### 已經跑在叢集上的前提（不是這次新裝，只是列出來確認版本）

| Image | 說明 |
|---|---|
| `docker.io/istio/pilot:1.13.5` | istiod |
| `docker.io/istio/proxyv2:1.13.5` | Istio sidecar |

## 完整安裝步驟

以下全部指令假設在 `21-spire-istio-sidecar-1135-authz/` 目錄下執行。離線
環境把 `manifests/` 換成 `manifests-offline/`（見 `OFFLINE_INSTALL.md`）。

### Step 1：CRD

```bash
for ctx in cluster1 cluster2; do
  kubectl --context=$ctx apply -f manifests/crds/clusterspiffeids.yaml
  kubectl --context=$ctx apply -f manifests/crds/clusterfederatedtrustdomains.yaml
done
```

### Step 2：離線生成 DIY 共用 root + 兩份 intermediate

```bash
mkdir -p diy-pki && cd diy-pki
openssl ecparam -name prime256v1 -genkey -noout -out root.key
openssl req -x509 -new -key root.key -sha256 -days 3650 \
  -subj "/O=spire-lab/CN=diy-1152-root" -out root.crt

for c in cluster1 cluster2; do
  openssl ecparam -name prime256v1 -genkey -noout -out int-$c.key
  openssl req -new -key int-$c.key -subj "/O=spire-lab/CN=diy-intermediate-$c-1152" -out int-$c.csr
  openssl x509 -req -in int-$c.csr -CA root.crt -CAkey root.key -CAcreateserial -days 1825 -sha256 \
    -extfile <(printf "basicConstraints=critical,CA:true\nkeyUsage=critical,keyCertSign,cRLSign") \
    -out int-$c.crt
  openssl verify -CAfile root.crt int-$c.crt
done
cd ..
```

### Step 3：Secret + SPIRE Server/Agent/Controller Manager

```bash
for ctx_c in "cluster1:cluster1" "cluster2:cluster2"; do
  ctx="${ctx_c%%:*}"; c="${ctx_c##*:}"
  kubectl --context=$ctx create namespace spire-1315
  kubectl --context=$ctx -n spire-1315 create secret generic diy-intermediate \
    --from-file=intermediate.crt=diy-pki/int-$c.crt \
    --from-file=intermediate.key=diy-pki/int-$c.key \
    --from-file=root.crt=diy-pki/root.crt
done

kubectl --context=cluster1 apply -f manifests/spire-1152-cluster1.yaml
kubectl --context=cluster2 apply -f manifests/spire-1152-cluster2.yaml
```

驗證兩邊 bundle 的 x509 root 完全一致（不需要任何 `bundle set`）：
```bash
for ctx in cluster1 cluster2; do
  kubectl --context=$ctx -n spire-1315 exec spire-server-0 -c spire-server -- \
    /opt/spire/bin/spire-server bundle show -format spiffe | \
    python3 -c "import json,sys,hashlib; d=json.load(sys.stdin); [print(hashlib.sha256(k['x5c'][0].encode()).hexdigest()[:16]) for k in d['keys'] if k['use']=='x509-svid']"
done
```

### Step 4：`meshConfig.trustDomainAliases`（讓 STRICT mTLS 認得 SPIRE 的 trust domain）

這是 mesh 層級（全域）的設定，效果是**純增量**（只加不減，不影響既有
istiod CA 簽的身份——細節見文末「為什麼這一步安全」）。兩邊都要加：

```bash
for ctx in cluster1 cluster2; do
  kubectl --context=$ctx -n istio-system get cm istio -o jsonpath='{.data.mesh}' > /tmp/mesh-$ctx.yaml
  # 確認裡面沒有 trustDomainAliases: diy-1152.local 才需要加；已經有就跳過這個 context
  grep -q "diy-1152.local" /tmp/mesh-$ctx.yaml || \
    python3 -c "
import yaml
d = yaml.safe_load(open('/tmp/mesh-$ctx.yaml'))
d.setdefault('trustDomainAliases', [])
if 'diy-1152.local' not in d['trustDomainAliases']:
    d['trustDomainAliases'].append('diy-1152.local')
yaml.dump(d, open('/tmp/mesh-$ctx.yaml', 'w'), default_flow_style=False)
"
  kubectl --context=$ctx -n istio-system create configmap istio \
    --from-file=mesh=/tmp/mesh-$ctx.yaml --dry-run=client -o yaml | kubectl --context=$ctx apply -f -
done
```

### Step 5：spiffe-csi-driver

```bash
kubectl --context=cluster1 apply -f manifests/spiffe-csi-driver.yaml
kubectl --context=cluster2 apply -f manifests/spiffe-csi-driver.yaml
```

### Step 6：patch sidecar injector，新增 "spire" injection template

```bash
python3 patch_sidecar_injector_spire_template_1135.py cluster1
python3 patch_sidecar_injector_spire_template_1135.py cluster2
```

這是**新增一個 template 選項**（`inject.istio.io/templates: "sidecar,spire"`
才會套用），不動預設的 "sidecar" template——沒選它的 pod 完全不受影響。

### Step 7：`ClusterSPIFFEID`（幫 workload 訂自訂 SPIFFE ID 規則）

```bash
kubectl --context=cluster1 apply -f manifests/clusterspiffeids.yaml
kubectl --context=cluster2 apply -f manifests/clusterspiffeids.yaml
```

### Step 8：placeholder `spire-full-bootstrap` ConfigMap

Step 6 的 "spire" template會無條件掛一個名叫 `spire-full-bootstrap` 的
ConfigMap 進 istio-proxy——這個 ConfigMap 現在還沒有「真的」內容（要從
一顆已經正常開機的 pod 才生得出來，見 Step 10），**但要先讓它存在**，
不然 pod 會卡在 `ContainerCreating`（volume 掛不到）。內容目前是什麼都
無所謂，因為要等到 Step 11 才會真的有東西去讀它：

```bash
for ns_ctx in "spire-istio-client:cluster1" "spire-istio-client:cluster2" "spire-istio-server:cluster2"; do
  ns="${ns_ctx%%:*}"; ctx="${ns_ctx##*:}"
  kubectl --context=$ctx create namespace $ns --dry-run=client -o yaml | kubectl --context=$ctx apply -f -
  echo '{}' > /tmp/placeholder_bootstrap.json
  kubectl --context=$ctx -n $ns create configmap spire-full-bootstrap \
    --from-file=custom_bootstrap_full.json=/tmp/placeholder_bootstrap.json \
    --dry-run=client -o yaml | kubectl --context=$ctx apply -f -
done
```

### Step 9：部署 client / server（第一次開機，還是用 istiod CA）

```bash
kubectl --context=cluster1 apply -f manifests/istio-client.yaml
kubectl --context=cluster2 apply -f manifests/istio-client.yaml   # cluster2 本地也放一份，用來測 DENY
kubectl --context=cluster2 apply -f manifests/istio-server.yaml
```

這時候 pod 應該正常 `2/2 Running`——因為還沒加 `customConfigFile`
annotation，sidecar 完全正常用 istiod CA 開機，只是多掛了兩個目前沒人用
的 volume（SPIRE socket、placeholder ConfigMap）。

### Step 10：產生「真的」full-bootstrap，回填進 ConfigMap

```bash
python3 gen_custom_bootstrap.py cluster1 spire-istio-client istio-client manifests/custom_bootstrap_full_client1.json
python3 gen_custom_bootstrap.py cluster2 spire-istio-client istio-client manifests/custom_bootstrap_full_client.json

SERVER_POD=$(kubectl --context=cluster2 -n spire-istio-server get pod -l app=istio-server -o jsonpath='{.items[0].metadata.name}')
python3 gen_custom_bootstrap.py cluster2 spire-istio-server $SERVER_POD manifests/custom_bootstrap_full_server.json

kubectl --context=cluster1 -n spire-istio-client create configmap spire-full-bootstrap \
  --from-file=custom_bootstrap_full.json=manifests/custom_bootstrap_full_client1.json --dry-run=client -o yaml | kubectl --context=cluster1 apply -f -
kubectl --context=cluster2 -n spire-istio-client create configmap spire-full-bootstrap \
  --from-file=custom_bootstrap_full.json=manifests/custom_bootstrap_full_client.json --dry-run=client -o yaml | kubectl --context=cluster2 apply -f -
kubectl --context=cluster2 -n spire-istio-server create configmap spire-full-bootstrap \
  --from-file=custom_bootstrap_full.json=manifests/custom_bootstrap_full_server.json --dry-run=client -o yaml | kubectl --context=cluster2 apply -f -
```

（`gen_custom_bootstrap.py` 做的事：抓這顆 pod 現在正常運作的 bootstrap，
只把裡面 `sds-grpc` 這個 cluster 的 socket path 從 istio-agent 自己的本地
SDS server 改成 SPIRE Agent 的 socket，其餘照抄——原理見文末。）

### Step 11：加上 `customConfigFile` annotation，重建 pod

Pod annotation 是「活的」metadata，直接 `kubectl annotate`/`patch` 不會
讓已經在跑的 istio-agent 重新讀取——**annotation 一定要在 pod
建立當下就存在**，所以做法是編輯 YAML 再整個重建，不是 patch 現有 pod。

在 `manifests/istio-client.yaml` 跟 `manifests/istio-server.yaml`
裡，把 `# customConfigFile annotation 是...` 那段註解換成：
```yaml
    proxy.istio.io/config: |
      customConfigFile: "/etc/istio/custom-bootstrap/custom_bootstrap_full.json"
```
然後重建 pod：
```bash
kubectl --context=cluster1 -n spire-istio-client delete pod istio-client
kubectl --context=cluster1 apply -f manifests/istio-client.yaml
kubectl --context=cluster2 -n spire-istio-client delete pod istio-client
kubectl --context=cluster2 apply -f manifests/istio-client.yaml
kubectl --context=cluster2 -n spire-istio-server rollout restart deployment istio-server
```

### Step 12：`DestinationRule`（因為用了自訂 SPIFFE ID 路徑才需要）

```bash
kubectl --context=cluster2 apply -f manifests/destinationrule-spire-san.yaml
```

## 驗證

```bash
kubectl --context=cluster2 -n spire-istio-client exec istio-client -c app -- \
  curl -sS -o /dev/null -w "HTTP_CODE:%{http_code}\n" \
  http://istio-server.spire-istio-server.svc.cluster.local:8080/
# cluster2 本地 client（principal 不對）→ 403（DENY，符合預期）

kubectl --context=cluster2 -n spire-istio-server exec <server-pod> -c istio-proxy -- pilot-agent request GET certs
# 確認 subject_alt_names.uri 是 spiffe://diy-1152.local/cluster/... 而不是 spiffe://cluster2.local/ns/.../sa/...
```

**沒標註的其他 pod**：不受任何影響，繼續用 istiod 內建 CA——整套機制完全
opt-in、per-pod、per-namespace（細節見下方「為什麼只影響有標註的 pod」）。

## 核心機制：為什麼要繞這麼一大圈

Istio **1.14+** 的 istio-agent 會自動偵測
`/run/secrets/workload-spiffe-uds/socket` 這個 UDS 存不存在，存在就直接
跳過 istiod CA，讓 Envoy 直接跟這個 socket 要憑證
（`istio/istio#37947`，2022-03-31 合併，`v1.14.0` 才第一次出現）。
**1.13.5 完全沒有這段邏輯**，`spiffe-csi-driver` 掛好、annotation 下對
了都沒用，sidecar 還是照樣去問 istiod。這是版本硬限制，不是設定錯誤。

嘗試過三條路都走不通：

1. **EnvoyFilter `CLUSTER`/`ADD`** 新增一個指向 SPIRE socket 的 cluster
   → 被 Envoy 拒絕：`ApiConfigSource must have a statically defined
   non-EDS cluster`——SDS 用的 cluster 規定要在 bootstrap 階段就存在，
   動態下發的不算
2. **EnvoyFilter `BOOTSTRAP`/`MERGE`**（理論上該幹這件事的機制）→ 完全
   沒生效，因為 1.13.5 的 istio-agent 產生 bootstrap 是純本地離線做的，
   不會去問 istiod 有沒有適用的 EnvoyFilter
3. **直接用 `bootstrapOverride` 重新定義既有的 `sds-grpc` cluster** →
   Envoy 啟動時直接 crash：`duplicate cluster 'sds-grpc'`——這個
   merge 機制對 list 類型欄位是 append 不是覆蓋

真正走通的路：pilot-agent 的 `proxy.istio.io/config` 裡有一個
`customConfigFile` 欄位，**完整替換**（不是 merge）用來產生 bootstrap 的
來源檔案。拿這顆 pod 原本正常運作時的 bootstrap 原封不動複製一份，只把
裡面 `sds-grpc` 這個 cluster 的 socket path 改指到 SPIRE Agent 的
socket——因為所有 listener 早就已經在用 `cluster_name: sds-grpc`
這個名字要憑證，改這一個地方就夠了，完全不用碰任何 listener/filter
chain patch。SPIRE Agent 的 socket 是雙協定的（除了 SPIFFE Workload
API，也直接支援 Envoy 原生 SDS 協定），所以 Envoy 可以直接跟它要
`default`/`ROOTCA`。

istio-agent 自己內部那個 SDS server 還是照常啟動、照常跟 istiod 要憑證
（log 還是會看到 `generated new workload certificate`）——只是現在
**沒有人接它**，因為 `sds-grpc` 已經被指到別的地方去了，那份憑證變成
孤兒，不影響任何實際流量。

## 為什麼需要 `DestinationRule`

`AuthorizationPolicy` 要用 `principals` 分辨 cluster1 跟 cluster2 的
client，所以 `ClusterSPIFFEID` 故意把叢集名稱編進 SPIFFE ID 路徑
（`.../cluster/{{ .ClusterName }}/istio-client`），不是 Istio 自己會猜
的 `.../ns/{namespace}/sa/{serviceaccount}` 格式（官方 1.14+ 的 SPIRE
sample 用的就是標準格式，所以完全不需要 DestinationRule——這步純粹是
我們自己選擇自訂路徑帶來的成本，跟 Istio 版本無關，就算升到 1.29 一樣
省不掉）。少了這個 DestinationRule，client 端會直接
`CERTIFICATE_VERIFY_FAILED`，因為 Istio 自動 mTLS 幫 client 猜的驗證
SAN 是根據 destination 的 k8s namespace/serviceaccount 猜的，猜不到
自訂路徑。

## 為什麼 Step 4 的 `trustDomainAliases` 改動是安全的

`trustDomainAliases` 是**純增量**的：加了 `diy-1152.local` 之後，
STRICT mTLS 的 SAN 前綴檢查、自動 mTLS 的 SAN 猜測，都是「或」邏輯——
既有 istiod CA 簽的憑證（`spiffe://cluster2.local/...`）原本的 trust
domain 依然在名單裡，不會被取代或移除。沒有任何非 SPIRE pod 會拿到
`diy-1152.local` 開頭的憑證，所以這個新增的 alias 對它們來說永遠是條
「不會被用到」的規則，不影響既有行為。

## 為什麼只影響有標註的 pod

- `inject.istio.io/templates: "sidecar,spire"` — 沒加的 pod 只吃預設
  "sidecar" template，不會碰到 SPIRE socket / custom-bootstrap volume
- `proxy.istio.io/config` 的 `customConfigFile` — 沒加的 pod，
  istio-agent 照舊用內建 template 產生 bootstrap，`sds-grpc` 還是指向
  自己本地的 SDS server（走 istiod CA）
- `spire-full-bootstrap` ConfigMap 是 **per-namespace** 的——要讓某個
  namespace 的 workload 走 SPIRE CA，該 namespace 要有這個 ConfigMap；
  沒標註的 pod 就算跟它同一個 namespace，也不會去 mount 它

## 離線安裝

見 [`OFFLINE_INSTALL.md`](./OFFLINE_INSTALL.md)。

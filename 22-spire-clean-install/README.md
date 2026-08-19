# SPIRE 自訂 SPIFFE + Istio 1.13.5 sidecar mTLS + AuthorizationPolicy——乾淨重整版

整合 `19-diy-shared-root-controller-manager/`（DIY 共用 root + `ClusterSPIFFEID`
+ Controller Manager 宣告式管理）跟 `21-spire-istio-sidecar-1135-authz/`
（Istio 1.13.5 缺少原生 SPIRE 整合，手動接通的 workaround）成一份完整、
從零開始的安裝指南。相較 `19-`/`21-` 做了兩個簡化：

1. **全部是純手寫的靜態 YAML，沒有任何 Python 產生器**——`19-`/`21-` 用
   `gen_spire_diy_cm.py`/`gen_spire_1152.py` 把 YAML 當字串模板產生，這裡
   直接給兩份完整、可直接讀懂的靜態檔案（`*-cluster1.yaml`／
   `*-cluster2.yaml`），只有內嵌設定值不同，沒有模板產生的中間層
2. **namespace 簡化、拆乾淨**：SPIRE 控制平面拆成 `spire-server`
   （Server + Controller Manager）跟 `spire-agent`（Agent DaemonSet +
   `spiffe-csi-driver`）兩個 namespace，取代 `19-`/`20-`/`21-` 那種
   `spire`/`spire-1315` 單一 namespace 塞全部東西、名字又跟版本號綁死
   的做法。展示用 workload 是 `spire-test-client`/`spire-test-server`。

架構原理（為什麼要跨叢集 mTLS、為什麼要 SPIRE + 共用 root CA）見
[`AGENT-MESH-MTLS.md`](./AGENT-MESH-MTLS.md)（沿用 `21-` 的說明，原理不變）。

## 前提

- 兩座叢集（kubectl context `cluster1`／`cluster2`），**Istio 1.13.5**
  sidecar 模式已裝好
- `kubectl`、`openssl`、[`jq`](https://jqlang.org/)（處理 JSON，取代原本
  `gen_custom_bootstrap.py` 那段）
- 離線環境：見 [`OFFLINE_INSTALL.md`](./OFFLINE_INSTALL.md)

## Image 清單

### 基礎設施 image（一定要）

| Image | 用途 |
|---|---|
| `ghcr.io/spiffe/spire-server:1.15.2` | SPIRE Server |
| `ghcr.io/spiffe/spire-agent:1.15.2` | SPIRE Agent（DaemonSet） |
| `ghcr.io/spiffe/spire-controller-manager:0.7.0` | `ClusterSPIFFEID` 宣告式管理 entry |
| `ghcr.io/spiffe/spiffe-csi-driver:0.2.13` | 把 SPIRE Agent socket 掛進 istio-proxy 的 CSI driver |
| `registry.k8s.io/sig-storage/csi-node-driver-registrar:v2.16.0` | CSI driver 的 node registrar sidecar |

### 展示用 workload image（正式環境換成你自己的 app）

| Image | 用途 |
|---|---|
| `hashicorp/http-echo:1.0.0` | server 端最小 echo backend |
| `curlimages/curl:8.16.0` | client 端測試工具 |

### 已經跑在叢集上的前提

| Image | 說明 |
|---|---|
| `docker.io/istio/pilot:1.13.5` | istiod |
| `docker.io/istio/proxyv2:1.13.5` | Istio sidecar |

## 完整安裝步驟

以下全部指令假設在 `22-spire-clean-install/` 目錄下執行。離線環境把
`manifests/` 換成 `manifests-offline/`（見 `OFFLINE_INSTALL.md`）。

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
  -subj "/O=spire-lab/CN=diy-root" -out root.crt

for c in cluster1 cluster2; do
  openssl ecparam -name prime256v1 -genkey -noout -out int-$c.key
  openssl req -new -key int-$c.key -subj "/O=spire-lab/CN=diy-intermediate-$c" -out int-$c.csr
  openssl x509 -req -in int-$c.csr -CA root.crt -CAkey root.key -CAcreateserial -days 1825 -sha256 \
    -extfile <(printf "basicConstraints=critical,CA:true\nkeyUsage=critical,keyCertSign,cRLSign") \
    -out int-$c.crt
  openssl verify -CAfile root.crt int-$c.crt
done
cd ..
```

### Step 3：Secret + SPIRE Server/Controller Manager（`spire-server` namespace）

```bash
for c in cluster1 cluster2; do
  kubectl --context=$c apply -f manifests/spire-server-$c.yaml
  # spire-server namespace 已經被上面那行建出來了，再補 Secret：
  kubectl --context=$c -n spire-server create secret generic diy-intermediate \
    --from-file=intermediate.crt=diy-pki/int-$c.crt \
    --from-file=intermediate.key=diy-pki/int-$c.key \
    --from-file=root.crt=diy-pki/root.crt
  kubectl --context=$c -n spire-server rollout status statefulset spire-server --timeout=60s
done
```

（`apply` 會先建出 StatefulSet，但 Secret 還沒建好之前 pod 會卡在
`ContainerCreating`——建完 Secret 後它會自動重試，`rollout status` 的
60 秒 timeout 會等到它成功為止，不用手動介入。）

驗證兩邊 bundle 的 x509 root 完全一致（不需要任何 `bundle set`）：
```bash
for ctx in cluster1 cluster2; do
  kubectl --context=$ctx -n spire-server exec spire-server-0 -c spire-server -- \
    /opt/spire/bin/spire-server bundle show -format spiffe | \
    python3 -c "import json,sys,hashlib; d=json.load(sys.stdin); [print(hashlib.sha256(k['x5c'][0].encode()).hexdigest()[:16]) for k in d['keys'] if k['use']=='x509-svid']" 2>/dev/null || \
  kubectl --context=$ctx -n spire-server exec spire-server-0 -c spire-server -- \
    /opt/spire/bin/spire-server bundle show -format spiffe | \
    jq -r '.keys[] | select(.use=="x509-svid") | .x5c[0]' | sha256sum
done
```
（有 `python3` 就用第一種；純 `jq`+`sha256sum` 的版本在後面 `||` 之後，
兩邊擇一都能用，純粹是為了不強制依賴 `python3` 做這個驗證步驟。）

### Step 4：SPIRE Agent（`spire-agent` namespace）

```bash
kubectl --context=cluster1 apply -f manifests/spire-agent-cluster1.yaml
kubectl --context=cluster2 apply -f manifests/spire-agent-cluster2.yaml
kubectl --context=cluster1 -n spire-agent rollout status daemonset spire-agent --timeout=60s
kubectl --context=cluster2 -n spire-agent rollout status daemonset spire-agent --timeout=60s
```

### Step 5：`meshConfig.trustDomainAliases`（讓 STRICT mTLS 認得 SPIRE 的 trust domain）

mesh 層級（全域）設定，效果是**純增量**（只加不減，不影響既有 istiod
CA 簽的身份——原理見文末）。兩邊都要加：

```bash
for ctx in cluster1 cluster2; do
  kubectl --context=$ctx -n istio-system get cm istio -o jsonpath='{.data.mesh}' > /tmp/mesh-$ctx.yaml
  grep -q "diy-1152.local" /tmp/mesh-$ctx.yaml || {
    if grep -q "^trustDomainAliases:" /tmp/mesh-$ctx.yaml; then
      sed -i '/^trustDomainAliases:/a\  - diy-1152.local' /tmp/mesh-$ctx.yaml
    else
      printf 'trustDomainAliases:\n  - diy-1152.local\n' >> /tmp/mesh-$ctx.yaml
    fi
  }
  kubectl --context=$ctx -n istio-system create configmap istio \
    --from-file=mesh=/tmp/mesh-$ctx.yaml --dry-run=client -o yaml | kubectl --context=$ctx apply -f -
done
```

### Step 6：`spiffe-csi-driver`

```bash
kubectl --context=cluster1 apply -f manifests/spiffe-csi-driver.yaml
kubectl --context=cluster2 apply -f manifests/spiffe-csi-driver.yaml
```

### Step 7：手動編輯 `istio-sidecar-injector` ConfigMap，新增 "spire" template

這一步無法純用靜態 YAML `apply` 完成——`istio-sidecar-injector` 這個
ConfigMap 已經有 istioctl 裝好時產生的預設 "sidecar"／"grpc-agent" 等
template，我們只是要在同一個 map 裡**多加一個 key**，不能整份覆蓋。
兩邊叢集都要做：

```bash
for ctx in cluster1 cluster2; do
  kubectl --context=$ctx -n istio-system get cm istio-sidecar-injector -o jsonpath='{.data.config}' > /tmp/injector-$ctx.yaml
done
```

用編輯器打開 `/tmp/injector-cluster1.yaml`（`/tmp/injector-cluster2.yaml`
同樣做一次），找到最上層的 `templates:` 那個 key，在它底下新增一個
`spire:` 條目（跟同一層級的 `sidecar:` 平行），內容如下（**整段含
`spire: |` 開頭，貼在 `templates:` 底下、跟 `sidecar:` 同一縮排層級**）：

```yaml
  spire: |
    labels:
      spiffe.io/spire-managed-identity: "true"
    spec:
      containers:
      - name: istio-proxy
        volumeMounts:
        - name: workload-socket
          mountPath: /run/secrets/workload-spiffe-uds
          readOnly: true
        - name: custom-bootstrap-volume
          mountPath: /etc/istio/custom-bootstrap
          readOnly: true
      volumes:
      - name: workload-socket
        csi:
          driver: "csi.spiffe.io"
          readOnly: true
      - name: custom-bootstrap-volume
        configMap:
          name: spire-full-bootstrap
```

（用 `containers:` 不是 `initContainers:`——這個 k8s 版本沒有 native
sidecar；如果你的 k8s ≥1.29 且用 native sidecar，要改成
`initContainers:`。）存檔後套用回去：

```bash
for ctx in cluster1 cluster2; do
  kubectl --context=$ctx -n istio-system create configmap istio-sidecar-injector \
    --from-file=config=/tmp/injector-$ctx.yaml --dry-run=client -o yaml | kubectl --context=$ctx apply -f -
done
```

這是**新增一個 template 選項**（`inject.istio.io/templates: "sidecar,spire"`
才會套用），不動預設的 "sidecar" template——沒選它的 pod 完全不受影響。

### Step 8：`ClusterSPIFFEID`（幫展示用 workload 訂自訂 SPIFFE ID 規則）

```bash
kubectl --context=cluster1 apply -f manifests/clusterspiffeids-workload.yaml
kubectl --context=cluster2 apply -f manifests/clusterspiffeids-workload.yaml
```

### Step 9：placeholder `spire-full-bootstrap` ConfigMap

Step 7 的 "spire" template 會無條件掛一個名叫 `spire-full-bootstrap` 的
ConfigMap 進 istio-proxy——這個 ConfigMap 現在還沒有「真的」內容（要從
一顆已經正常開機的 pod 才生得出來，見 Step 11），**但要先讓它存在**，
不然 pod 會卡在 `ContainerCreating`（volume 掛不到）。內容目前是什麼都
無所謂：

```bash
for ns_ctx in "spire-test-client:cluster1" "spire-test-client:cluster2" "spire-test-server:cluster2"; do
  ns="${ns_ctx%%:*}"; ctx="${ns_ctx##*:}"
  kubectl --context=$ctx create namespace $ns --dry-run=client -o yaml | kubectl --context=$ctx apply -f -
  echo '{}' > /tmp/placeholder_bootstrap.json
  kubectl --context=$ctx -n $ns create configmap spire-full-bootstrap \
    --from-file=custom_bootstrap_full.json=/tmp/placeholder_bootstrap.json \
    --dry-run=client -o yaml | kubectl --context=$ctx apply -f -
done
```

### Step 10：部署 client / server（第一次開機，還是用 istiod CA）

```bash
kubectl --context=cluster1 apply -f manifests/istio-client.yaml
kubectl --context=cluster2 apply -f manifests/istio-client.yaml   # cluster2 本地也放一份，用來測 DENY
kubectl --context=cluster2 apply -f manifests/istio-server.yaml
```

這時候 pod 應該正常 `2/2 Running`——因為還沒加 `customConfigFile`
annotation，sidecar 完全正常用 istiod CA 開機，只是多掛了兩個目前沒人用
的 volume（SPIRE socket、placeholder ConfigMap）。

### Step 11：產生「真的」full-bootstrap，回填進 ConfigMap

```bash
./gen_custom_bootstrap.sh cluster1 spire-test-client spire-test-client manifests/custom_bootstrap_full_client1.json
./gen_custom_bootstrap.sh cluster2 spire-test-client spire-test-client manifests/custom_bootstrap_full_client2.json

SERVER_POD=$(kubectl --context=cluster2 -n spire-test-server get pod -l app=spire-test-server -o jsonpath='{.items[0].metadata.name}')
./gen_custom_bootstrap.sh cluster2 spire-test-server $SERVER_POD manifests/custom_bootstrap_full_server.json

kubectl --context=cluster1 -n spire-test-client create configmap spire-full-bootstrap \
  --from-file=custom_bootstrap_full.json=manifests/custom_bootstrap_full_client1.json --dry-run=client -o yaml | kubectl --context=cluster1 apply -f -
kubectl --context=cluster2 -n spire-test-client create configmap spire-full-bootstrap \
  --from-file=custom_bootstrap_full.json=manifests/custom_bootstrap_full_client2.json --dry-run=client -o yaml | kubectl --context=cluster2 apply -f -
kubectl --context=cluster2 -n spire-test-server create configmap spire-full-bootstrap \
  --from-file=custom_bootstrap_full.json=manifests/custom_bootstrap_full_server.json --dry-run=client -o yaml | kubectl --context=cluster2 apply -f -
```

（`gen_custom_bootstrap.sh` 做的事：抓這顆 pod 現在正常運作的
bootstrap，只把裡面 `sds-grpc` 這個 cluster 的 socket path 從
istio-agent 自己的本地 SDS server 改成 SPIRE Agent 的 socket，其餘照抄
——原理見文末。）

### Step 12：加上 `customConfigFile` annotation，重建 pod

Pod annotation 是「活的」metadata，直接 `kubectl annotate`/`patch` 不會
讓已經在跑的 istio-agent 重新讀取——annotation 一定要在 pod 建立當下就
存在，所以做法是編輯 YAML 再整個重建，不是 patch 現有 pod。

在 `manifests/istio-client.yaml` 跟 `manifests/istio-server.yaml` 裡，
把 `# customConfigFile annotation 是...` 那段註解換成：
```yaml
    proxy.istio.io/config: |
      customConfigFile: "/etc/istio/custom-bootstrap/custom_bootstrap_full.json"
```
然後重建 pod：
```bash
kubectl --context=cluster1 -n spire-test-client delete pod spire-test-client
kubectl --context=cluster1 apply -f manifests/istio-client.yaml
kubectl --context=cluster2 -n spire-test-client delete pod spire-test-client
kubectl --context=cluster2 apply -f manifests/istio-client.yaml
kubectl --context=cluster2 -n spire-test-server rollout restart deployment spire-test-server
```

### Step 13：`DestinationRule`（因為用了自訂 SPIFFE ID 路徑才需要）

```bash
kubectl --context=cluster2 apply -f manifests/destinationrule-spire-san.yaml
```

## 驗證

```bash
kubectl --context=cluster2 -n spire-test-client exec spire-test-client -c app -- \
  curl -sS -o /dev/null -w "HTTP_CODE:%{http_code}\n" \
  http://spire-test-server.spire-test-server.svc.cluster.local:8080/
# cluster2 本地 client（principal 不對）→ 403（DENY，符合預期）

SERVER_POD=$(kubectl --context=cluster2 -n spire-test-server get pod -l app=spire-test-server -o jsonpath='{.items[0].metadata.name}')
kubectl --context=cluster2 -n spire-test-server exec $SERVER_POD -c istio-proxy -- pilot-agent request GET certs
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
——只是現在**沒有人接它**，因為 `sds-grpc` 已經被指到別的地方去了，那份
憑證變成孤兒，不影響任何實際流量。

## 為什麼需要 `DestinationRule`

`AuthorizationPolicy` 要用 `principals` 分辨 cluster1 跟 cluster2 的
client，所以 `ClusterSPIFFEID` 故意把叢集名稱編進 SPIFFE ID 路徑
（`.../cluster/{{ .ClusterName }}/spire-test-client`），不是 Istio 自己
會猜的 `.../ns/{namespace}/sa/{serviceaccount}` 格式（官方 1.14+ 的
SPIRE sample 用的就是標準格式，所以完全不需要 DestinationRule——這步
純粹是我們自己選擇自訂路徑帶來的成本，跟 Istio 版本無關，就算升到 1.29
一樣省不掉）。少了這個 DestinationRule，client 端會直接
`CERTIFICATE_VERIFY_FAILED`。

## 為什麼 Step 5 的 `trustDomainAliases` 改動是安全的

`trustDomainAliases` 是**純增量**的：加了 `diy-1152.local` 之後，
STRICT mTLS 的 SAN 前綴檢查、自動 mTLS 的 SAN 猜測，都是「或」邏輯——
既有 istiod CA 簽的憑證原本的 trust domain 依然在名單裡，不會被取代或
移除。沒有任何非 SPIRE pod 會拿到 `diy-1152.local` 開頭的憑證，所以這個
新增的 alias 對它們來說永遠是條「不會被用到」的規則。

## 為什麼只影響有標註的 pod

- `inject.istio.io/templates: "sidecar,spire"` — 沒加的 pod 只吃預設
  "sidecar" template，不會碰到 SPIRE socket / custom-bootstrap volume
- `proxy.istio.io/config` 的 `customConfigFile` — 沒加的 pod，
  istio-agent 照舊用內建 template 產生 bootstrap
- `spire-full-bootstrap` ConfigMap 是 **per-namespace** 的——要讓某個
  namespace 的 workload 走 SPIRE CA，該 namespace 要有這個 ConfigMap；
  沒標註的 pod 就算跟它同一個 namespace，也不會去 mount 它

## 跟 `19-`/`21-` 的差異總結

| | `19-`/`20-`/`21-` | `22-`（這份） |
|---|---|---|
| YAML 產生方式 | Python f-string 模板（`gen_spire_*.py`） | 純手寫靜態 YAML |
| SPIRE 控制平面 namespace | `spire`／`spire-1315`（單一，命名跟版本號綁死） | `spire-server` + `spire-agent`（拆開，語意清楚） |
| bootstrap 產生工具 | `gen_custom_bootstrap.py`（Python + `json`） | `gen_custom_bootstrap.sh`（`jq`） |
| 展示 workload namespace | `spire-istio-client`／`spire-istio-server` | `spire-test-client`／`spire-test-server` |

## 離線安裝

見 [`OFFLINE_INSTALL.md`](./OFFLINE_INSTALL.md)。

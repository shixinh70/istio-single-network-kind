# 跨 Istio 版本的 SPIRE mTLS：1.13.5 ↔ 1.29.6

驗證題目：兩座**完全獨立、彼此沒有 mesh federation/remote-secret 的實體
叢集**，一座 Istio **1.13.5**（k8s 1.24，`cluster1`），一座 Istio
**1.29.6**（k8s 1.34，`cluster1-134`），用**同一顆共用 root CA** 簽出的
SPIRE 憑證，能不能：

1. 讓兩邊 sidecar 真的做起 mTLS（不同 Istio 版本的 SDS/憑證交握機制不同
   ——1.13.5 要靠 `customConfigFile` workaround，1.29.6 原生支援——這兩
   套機制產生的憑證能不能被對方正確驗證，是這次測試的核心問題）
2. 讓 server 端 `AuthorizationPolicy` 用 `principals` 正確卡控

**結果：可以。** 兩邊 sidecar 各自用自己的方式（1.13.5 手動重定向
bootstrap、1.29.6 原生偵測）跟 SPIRE 要憑證，憑證本身是標準 X.509/
SPIFFE 格式，TLS 交握完全不在乎對方是被「原生偵測」還是「手動重定向」
產生的——這件事本來就是 X.509/TLS 協定層級的東西，跟 Istio 版本無關。
拿 `AuthorizationPolicy` 的 `principals` 卡控做 A/B 測試（故意改成錯的
principal → 403，改回正確的 → 200）確認是真的在做身份卡控，不是巧合。

架構原理見 [`AGENT-MESH-MTLS.md`](./AGENT-MESH-MTLS.md)；`customConfigFile`
機制本身的完整原理/為什麼需要見 `21-`/`22-` 的 README。這份文件只講
**跨版本、跨物理叢集**這次額外遇到的東西。

## 拓樸

- `cluster1`（k8s 1.24.17, Istio 1.13.5）：已經在跑 `22-` 自己的
  SPIRE（namespace `spire-server`/`spire-agent`，trust domain
  `diy-1152.local`）——**這次是完全獨立、不動它的新安裝**，新 namespace
  `spire-xver-server`/`spire-xver-agent`
- `cluster1-134`（k8s 1.34.8, Istio 1.29.6）：SPIRE 裝在
  `spire-server`/`spire-agent` namespace（這座物理叢集上没有跟它撞名的
  既有東西）
- 兩邊 SPIRE Server 的 intermediate 都是同一顆 `diy-pki/root.key` 簽的，
  trust domain 統一 `diy-1296.local`
- client（`xver-client`）在 `cluster1`，server（`xver-server`）在
  `cluster1-134`，中間沒有 east-west gateway/mesh federation——用
  `ServiceEntry`（STATIC）+ `DestinationRule`（`ISTIO_MUTUAL`）讓
  client 自己的 sidecar 對 `cluster1-134-worker` 的 docker network IP +
  server 的 NodePort 直接發起真正的 mTLS，`hostAliases` 解決兩邊沒有
  共用 DNS 的問題（同一招 `13-k8s134-istio129-sidecar-gw/` 的
  agent→mcp 場景就用過）

## 完整安裝步驟

### Step 1：CRD（兩邊）

```bash
for ctx in cluster1 cluster1-134; do
  kubectl --context=$ctx apply -f manifests/crds/clusterspiffeids.yaml
  kubectl --context=$ctx apply -f manifests/crds/clusterfederatedtrustdomains.yaml
done
```

### Step 2：DIY 共用 root + 兩份 intermediate

```bash
mkdir -p diy-pki && cd diy-pki
openssl ecparam -name prime256v1 -genkey -noout -out root.key
openssl req -x509 -new -key root.key -sha256 -days 3650 \
  -subj "/O=spire-lab/CN=diy-root-xver" -out root.crt

for c in cluster1 cluster1-134; do
  openssl ecparam -name prime256v1 -genkey -noout -out int-$c.key
  openssl req -new -key int-$c.key -subj "/O=spire-lab/CN=diy-intermediate-$c" -out int-$c.csr
  openssl x509 -req -in int-$c.csr -CA root.crt -CAkey root.key -CAcreateserial -days 1825 -sha256 \
    -extfile <(printf "basicConstraints=critical,CA:true\nkeyUsage=critical,keyCertSign,cRLSign") \
    -out int-$c.crt
  openssl verify -CAfile root.crt int-$c.crt
done
cd ..
```

### Step 3：SPIRE Server（兩邊，各自的 namespace）

```bash
kubectl --context=cluster1 apply -f manifests/spire-server-cluster1.yaml
kubectl --context=cluster1 -n spire-xver-server create secret generic diy-intermediate \
  --from-file=intermediate.crt=diy-pki/int-cluster1.crt \
  --from-file=intermediate.key=diy-pki/int-cluster1.key \
  --from-file=root.crt=diy-pki/root.crt
kubectl --context=cluster1 -n spire-xver-server rollout status statefulset spire-server --timeout=60s
```

`cluster1-134` 那邊如果還沒裝過 SPIRE（本目錄不提供
`spire-server-cluster1-134.yaml`——如果你的 `cluster1-134` 是全新的，
照抄上面這份改 namespace/cluster_name 為 `spire-server`/`cluster1-134`
即可，套用同一份 `diy-pki/int-cluster1-134.*`）。

驗證兩邊 bundle 一致：
```bash
for ctx_ns in "cluster1:spire-xver-server" "cluster1-134:spire-server"; do
  ctx="${ctx_ns%%:*}"; ns="${ctx_ns##*:}"
  kubectl --context=$ctx -n $ns exec spire-server-0 -c spire-server -- \
    /opt/spire/bin/spire-server bundle show -format spiffe | \
    jq -r '.keys[] | select(.use=="x509-svid") | .x5c[0]' | sha256sum
done
```

### Step 4：SPIRE Agent（兩邊）

```bash
kubectl --context=cluster1 apply -f manifests/spire-agent-cluster1.yaml
kubectl --context=cluster1 -n spire-xver-agent rollout status daemonset spire-agent --timeout=60s
```

### Step 5：`spiffe-csi-driver`

**`cluster1`**：這座物理叢集已經有 `22-` 的 `spiffe-csi-driver`（標準
driver 名稱 `csi.spiffe.io`）在跑。kubelet 的 CSI plugin 註冊是
**per-node 單例，用 driver 名稱當 key**（`/var/lib/kubelet/plugins/
<driver-name>/csi.sock`）——同一個 node 上跑兩個獨立的 driver 實例，
名稱一定要不同，不然會互相覆蓋對方在 kubelet 的註冊（親身測試過：
只改 `node-driver-registrar` 的 `-kubelet-registration-path` **沒用**，
實際註冊用的名稱來自 driver binary 自己回應 `GetPluginInfo` RPC 的內容，
要用 driver 自己的 `-plugin-name` flag 才能真的改掉這個名稱）：

```bash
kubectl --context=cluster1 apply -f manifests/spiffe-csi-driver-cluster1.yaml
```

（這份用 `-plugin-name csi-xver.spiffe.io`，跟 22- 的
`csi.spiffe.io` 完全分開，兩邊 driver 在同一個 node 上互不影響。）

**`cluster1-134`**：這座物理叢集沒有既有的 spiffe-csi-driver，直接用
標準名稱裝即可（照抄 `21-`/`22-` 的 `spiffe-csi-driver.yaml`，改
namespace 為 `spire-agent`）。

### Step 6：`meshConfig.trustDomainAliases`（兩邊）

**不要用 `sed`/`printf` 直接接字串**——這個 ConfigMap 的 `data.mesh`
值常常沒有結尾換行，直接 append 會把新內容黏在最後一行後面變成無效
YAML，istiod 會直接忽略整份設定（好在有 fail-safe 不會真的爆掉，但也
不會生效——親身測試過，兩邊都中招）：

```bash
for ctx in cluster1 cluster1-134; do
  kubectl --context=$ctx -n istio-system get cm istio -o jsonpath='{.data.mesh}' > /tmp/mesh-$ctx.yaml
  python3 -c "
import yaml
d = yaml.safe_load(open('/tmp/mesh-$ctx.yaml'))
aliases = d.get('trustDomainAliases') or []
if 'diy-1296.local' not in aliases:
    aliases.append('diy-1296.local')
d['trustDomainAliases'] = aliases
yaml.dump(d, open('/tmp/mesh-$ctx.yaml', 'w'), default_flow_style=False)
"
  kubectl --context=$ctx -n istio-system create configmap istio \
    --from-file=mesh=/tmp/mesh-$ctx.yaml --dry-run=client -o yaml | kubectl --context=$ctx apply -f -
done
```

### Step 7：injection template（兩邊，內容不同）

**同樣不要用 `kubectl create configmap --from-file=config=... | kubectl
apply` 這種只塞一個 key 的寫法**——`istio-sidecar-injector` 這個
ConfigMap 除了 `config` 還有 `values`（Helm values，裡面有
`clusterName`/`network` 這種跟叢集身份綁定的欄位），只塞 `config` 會把
`values` 整個砍掉（親身測試過，`cluster1` 中招，`values` 直接消失，
istiod 開始警告 `missing ConfigMap values key`）。要讀出**兩個 key**
一起改一起寫回：

```bash
kubectl --context=$ctx -n istio-system get cm istio-sidecar-injector -o jsonpath='{.data.config}' > /tmp/injector-config.yaml
kubectl --context=$ctx -n istio-system get cm istio-sidecar-injector -o jsonpath='{.data.values}' > /tmp/injector-values.json
# 用 python/yaml 修改 /tmp/injector-config.yaml 的 templates 那段（見下）
kubectl --context=$ctx -n istio-system create configmap istio-sidecar-injector \
  --from-file=config=/tmp/injector-config.yaml \
  --from-file=values=/tmp/injector-values.json \
  --dry-run=client -o yaml | kubectl --context=$ctx apply -f -
```

**`cluster1`**（1.13.5，沒有 native sidecar，要 `customConfigFile`
workaround，driver 名稱用 `csi-xver.spiffe.io`）——新增一個叫
`spire-xver` 的 template（不是 `spire`，因為 `22-` 已經用掉
`spire` 這個 key，指向它自己的標準 driver）：

```yaml
  spire-xver: |
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
          driver: "csi-xver.spiffe.io"
          readOnly: true
      - name: custom-bootstrap-volume
        configMap:
          name: spire-full-bootstrap
```

**`cluster1-134`**（1.29.6，native sidecar，原生支援不用
customConfigFile，driver 用標準名稱）——新增一個叫 `spire` 的
template：

```yaml
  spire: |
    labels:
      spiffe.io/spire-managed-identity: "true"
    spec:
      initContainers:
      - name: istio-proxy
        volumeMounts:
        - name: workload-socket
          mountPath: /run/secrets/workload-spiffe-uds
          readOnly: true
      volumes:
      - name: workload-socket
        csi:
          driver: "csi.spiffe.io"
          readOnly: true
```

（用 `initContainers:` 不是 `containers:`——k8s 1.34 支援 native
sidecar，這個 template 沒跟 `containers:` 混用的話會產生兩個同名
`istio-proxy` container，一個在 `initContainers` 一個在
`containers`，導致奇怪的錯誤。）

**如果 `cluster1-134` 是全新裝 spiffe-csi-driver**：driver binary 回應
`GetPluginInfo` 預設就是 `csi.spiffe.io`（不用額外傳
`-plugin-name`），但 K8s 的 `CSIDriver` API 物件的 `metadata.name`
**要手動建、要跟 binary 實際回應的名稱一致**——親身踩過這個坑：
`CSIDriver` 物件名稱打錯（或改了但沒同步改回 binary 真正用的名稱），
pod 會卡 `Init:0/2`，`describe pod` 會看到 `volume mode "Ephemeral" not
supported by driver csi.spiffe.io (no CSIDriver object)`，即使 driver
DaemonSet 本身是 `Running` 也一樣——這兩件事（driver 實際註冊的身份、
K8s CSIDriver API 物件的名稱）是分開的，要對齊。

### Step 8：`ClusterSPIFFEID`（兩邊）

```bash
kubectl --context=cluster1 apply -f manifests/clusterspiffeids-workload.yaml
kubectl --context=cluster1-134 apply -f manifests/clusterspiffeids-workload.yaml
```

### Step 9：部署 server（`cluster1-134`，一次到位，不用兩階段）

```bash
kubectl --context=cluster1-134 apply -f manifests/xver-server.yaml
```

native 支援不需要 `customConfigFile` 兩階段流程，annotation 直接寫死在
manifest 裡即可。

### Step 10：部署 client（`cluster1`，兩階段，同 `21-`/`22-`）

```bash
# placeholder ConfigMap（先讓 volume mount 過得去）
kubectl --context=cluster1 create namespace xver-client --dry-run=client -o yaml | kubectl --context=cluster1 apply -f -
echo '{}' > /tmp/placeholder.json
kubectl --context=cluster1 -n xver-client create configmap spire-full-bootstrap \
  --from-file=custom_bootstrap_full.json=/tmp/placeholder.json --dry-run=client -o yaml | kubectl --context=cluster1 apply -f -

# 第一次開機，還沒有 customConfigFile annotation，正常用 istiod CA
kubectl --context=cluster1 apply -f manifests/xver-client.yaml

# 產生真的 bootstrap，回填 ConfigMap
./gen_custom_bootstrap.sh cluster1 xver-client xver-client manifests/custom_bootstrap_full_client.json
kubectl --context=cluster1 -n xver-client create configmap spire-full-bootstrap \
  --from-file=custom_bootstrap_full.json=manifests/custom_bootstrap_full_client.json --dry-run=client -o yaml | kubectl --context=cluster1 apply -f -

# 在 manifests/xver-client.yaml 裡加回 customConfigFile annotation 後重建 pod
kubectl --context=cluster1 -n xver-client delete pod xver-client
kubectl --context=cluster1 apply -f manifests/xver-client.yaml
```

### Step 11：跨叢集連通性（`ServiceEntry` + `DestinationRule` + DNS）

```bash
kubectl --context=cluster1 apply -f manifests/serviceentry-xver-server.yaml
```

`manifests/serviceentry-xver-server.yaml` 裡的 `endpoints[0].address`
要換成你自己 `cluster1-134-worker` 的 docker `kind` network IP
（`docker inspect cluster1-134-worker --format '{{.NetworkSettings.Networks.kind.IPAddress}}'`），
`manifests/xver-client.yaml` 的 `hostAliases` 也要同步換成一樣的 IP
——兩座叢集之間沒有共用 DNS，`xver-server.cross-version.local` 這個
host 名稱單純是給 Envoy 內部用來匹配 `ServiceEntry`/`DestinationRule`
的字串，`hostAliases` 只是讓 client 應用層的 DNS resolve 這一步不要
直接失敗（同一招 `13-` 的 agent→mcp 場景用過）。

## 驗證

```bash
kubectl --context=cluster1 -n xver-client exec xver-client -c app -- \
  curl -sS -o /dev/null -w "HTTP_CODE:%{http_code}\n" \
  http://xver-server.cross-version.local:8080/
# 200
```

A/B 測試證明 AuthorizationPolicy 真的在依身份卡控（不是巧合通過）：
```bash
# 故意改成錯的 principal
kubectl --context=cluster1-134 -n xver-server patch authorizationpolicy xver-server-allow-cluster1-only --type=json \
  -p='[{"op":"replace","path":"/spec/rules/0/from/0/source/principals/0","value":"diy-1296.local/cluster/WRONG-cluster/xver-client"}]'
# → 403

# 改回正確的
kubectl --context=cluster1-134 -n xver-server patch authorizationpolicy xver-server-allow-cluster1-only --type=json \
  -p='[{"op":"replace","path":"/spec/rules/0/from/0/source/principals/0","value":"diy-1296.local/cluster/cluster1/xver-client"}]'
# → 200
```

實測結果（server 端 access log）：
```
200 - via_upstream ...
403 - rbac_access_denied_matched_policy[none] ...
200 - via_upstream ...
```

## 結論

Istio 1.13.5 跟 Istio 1.29.6 的 sidecar，用同一顆共用 root CA 簽出的
SPIRE 憑證，**可以互通 mTLS**，`AuthorizationPolicy` 的
principal-based 卡控也正常運作，即使兩邊：
- 拿到憑證的機制完全不同（1.13.5 手動 bootstrap 重定向 vs 1.29.6 原生
  自動偵測）
- 物理上是兩座完全獨立、沒有 mesh federation 的叢集

這證實了 mTLS/SPIFFE 身份驗證這件事的本質——它是 X.509/TLS 協定層級的
機制，只要雙方憑證鏈可驗證、trust domain 對得上，跟「哪個元件、用什麼
方式把憑證塞進 Envoy」完全無關，也不需要兩邊 Istio 版本一致。

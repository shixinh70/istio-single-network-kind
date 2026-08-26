# SPIRE 雙軌整合 + IngressGateway mTLS + AuthorizationPolicy

`cluster1`（k8s 1.24, Istio 1.13.5）跟 `cluster1-134`（k8s 1.34, Istio
1.29.6）從零重建，兩邊裝 SPIRE（同一顆 shared-root），驗證：

1. **`cluster1` 並行測試兩種讓 istio-proxy 拿到 SPIRE 身份的方法**
   - **方法 A**：`spiffe-helper` 把 SVID 寫成檔案，`istio-proxy` 完全是
     預設 sidecar 注入（不碰 SDS、不用 `customConfigFile`）——靠一份
     `DestinationRule`（`mode: MUTUAL` + 直接指定憑證檔案路徑）讓 Envoy
     自己去讀那些檔案，對「這個 destination」origin TLS，完全繞過
     istio-agent 的 SDS server
   - **方法 B**：沿用 `21-`/`22-`/`24-` 的 `customConfigFile` bootstrap
     重定向，Envoy 直接透過 SPIRE Agent 的 socket 走原生 SDS 協定要憑證
2. **`cluster1-134` 的 server 走 `istio-ingressgateway` 曝露**，igw 自己
   也裝了原生 SPIRE 整合（不是 `credentialName`+K8s Secret），
   `Gateway` 用 `tls.mode: ISTIO_MUTUAL` 自己終結 mTLS、驗證 client 憑證
3. **`AuthorizationPolicy` 卡在 igw 本身**，用 `principals` 只放行
   `cluster1` 來的兩個 client，兩種方法都要各自驗證 ALLOW/DENY

## 拓樸

```
cluster1（1.13.5）                          cluster1-134（1.29.6）
├─ spire-server / spire-agent（各自一套）    ├─ spire-server / spire-agent
├─ client-a（spiffe-helper + DR MUTUAL）     ├─ istio-ingressgateway
├─ client-b（customConfigFile）              │   （原生 SPIRE 身份，Gateway ISTIO_MUTUAL）
└─ ServiceEntry → cluster1-134 igw 的        └─ xver-server（原生 SPIRE 身份）
   MetalLB IP:443
```

兩邊 SPIRE Server 的 intermediate 都是同一顆 `diy-pki/root.key` 簽的，
trust domain 統一 `diy-25.local`。

## Image 清單

| Image | 用途 |
|---|---|
| `ghcr.io/spiffe/spire-server:1.15.2` | SPIRE Server |
| `ghcr.io/spiffe/spire-agent:1.15.2` | SPIRE Agent |
| `ghcr.io/spiffe/spire-controller-manager:0.7.0` | `ClusterSPIFFEID` 宣告式管理 |
| `ghcr.io/spiffe/spiffe-csi-driver:0.2.13` | CSI driver（掛 SPIRE socket） |
| `registry.k8s.io/sig-storage/csi-node-driver-registrar:v2.16.0` | CSI node registrar |
| `ghcr.io/spiffe/spiffe-helper:0.9.0` | 方法 A 用，把 SVID 寫成檔案 |
| `hashicorp/http-echo:1.0.0` | server 端最小 echo backend |
| `curlimages/curl:8.16.0` | client 端測試工具 |
| `docker.io/istio/pilot:1.13.5`／`proxyv2:1.13.5` | cluster1 既有前提 |
| `docker.io/istio/pilot:1.29.6`／`proxyv2:1.29.6` | cluster1-134 既有前提 |

## 完整安裝步驟

### Step 0：CRD + 共用 root

```bash
for ctx in cluster1 cluster1-134; do
  kubectl --context=$ctx apply -f manifests/crds/clusterspiffeids.yaml
  kubectl --context=$ctx apply -f manifests/crds/clusterfederatedtrustdomains.yaml
done

mkdir -p diy-pki && cd diy-pki
openssl ecparam -name prime256v1 -genkey -noout -out root.key
openssl req -x509 -new -key root.key -sha256 -days 3650 \
  -subj "/O=spire-lab/CN=diy-root-25" -out root.crt
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

### Step 1：SPIRE Server + Agent（兩邊）

```bash
for c in cluster1 cluster1-134; do
  kubectl --context=$c apply -f manifests/spire-server-$c.yaml
  kubectl --context=$c -n spire-server create secret generic diy-intermediate \
    --from-file=intermediate.crt=diy-pki/int-$c.crt \
    --from-file=intermediate.key=diy-pki/int-$c.key \
    --from-file=root.crt=diy-pki/root.crt
  kubectl --context=$c -n spire-server rollout status statefulset spire-server --timeout=60s
done

# 驗證兩邊 bundle root hash 一致（不需要 bundle set）
for ctx in cluster1 cluster1-134; do
  kubectl --context=$ctx -n spire-server exec spire-server-0 -c spire-server -- \
    /opt/spire/bin/spire-server bundle show -format spiffe | \
    jq -r '.keys[] | select(.use=="x509-svid") | .x5c[0]' | sha256sum
done

kubectl --context=cluster1 apply -f manifests/spire-agent-cluster1.yaml
kubectl --context=cluster1-134 apply -f manifests/spire-agent-cluster1-134.yaml
```

### Step 2：`trustDomainAliases`（兩邊，一定要用 python+yaml，不要用 sed）

```bash
for ctx in cluster1 cluster1-134; do
  kubectl --context=$ctx -n istio-system get cm istio -o jsonpath='{.data.mesh}' > /tmp/mesh-$ctx.yaml
  python3 -c "
import yaml
d = yaml.safe_load(open('/tmp/mesh-$ctx.yaml'))
aliases = d.get('trustDomainAliases') or []
if 'diy-25.local' not in aliases:
    aliases.append('diy-25.local')
d['trustDomainAliases'] = aliases
yaml.dump(d, open('/tmp/mesh-$ctx.yaml', 'w'), default_flow_style=False)
"
  kubectl --context=$ctx -n istio-system create configmap istio \
    --from-file=mesh=/tmp/mesh-$ctx.yaml --dry-run=client -o yaml | kubectl --context=$ctx apply -f -
done
```

### Step 3：`spiffe-csi-driver` + `ClusterSPIFFEID`

```bash
kubectl --context=cluster1 apply -f manifests/spiffe-csi-driver.yaml
kubectl --context=cluster1-134 apply -f manifests/spiffe-csi-driver.yaml
kubectl --context=cluster1 apply -f manifests/clusterspiffeids-workload.yaml
kubectl --context=cluster1-134 apply -f manifests/clusterspiffeids-workload.yaml
```

### Step 4：`cluster1-134`——server + igw 原生 SPIRE 整合

**先幫 istio-ingressgateway 自己掛 SPIRE 身份**——Istio 1.29 的 gateway
chart**已經內建**一個叫 `workload-socket` 的 volume，預設是空的
`emptyDir`（等你自己把它換成真的 CSI driver）：

```bash
kubectl --context=cluster1-134 -n istio-system patch deployment istio-ingressgateway --type=json -p='[
  {"op": "replace", "path": "/spec/template/spec/volumes/0", "value": {"name": "workload-socket", "csi": {"driver": "csi.spiffe.io", "readOnly": true}}}
]'
```

**幫一般 workload（xver-server）新增 "spire" injection template**——
Istio 1.29 的預設 "sidecar" template 也**已經內建**同一個
`workload-socket` emptyDir 佔位（volumeMount 早就有了），所以自訂
template 只要**用同名 volume 覆蓋掉**（靠 Istio 多 template 合併時的
strategic-merge-by-name 語意，不會產生 duplicate）：

```bash
kubectl --context=cluster1-134 -n istio-system get cm istio-sidecar-injector -o jsonpath='{.data.config}' > /tmp/injector.yaml
kubectl --context=cluster1-134 -n istio-system get cm istio-sidecar-injector -o jsonpath='{.data.values}' > /tmp/injector-values.json
python3 << 'EOF'
import yaml
spire_template = """labels:
  spiffe.io/spire-managed-identity: "true"
spec:
  initContainers:
  - name: istio-proxy
    volumeMounts:
    - name: workload-socket
      mountPath: /var/run/secrets/workload-spiffe-uds
      readOnly: true
  volumes:
  - name: workload-socket
    csi:
      driver: "csi.spiffe.io"
      readOnly: true
"""
with open('/tmp/injector.yaml') as f:
    d = yaml.safe_load(f)
d['templates']['spire'] = spire_template
with open('/tmp/injector.yaml', 'w') as f:
    yaml.dump(d, f, default_flow_style=False)
EOF
kubectl --context=cluster1-134 -n istio-system create configmap istio-sidecar-injector \
  --from-file=config=/tmp/injector.yaml --from-file=values=/tmp/injector-values.json \
  --dry-run=client -o yaml | kubectl --context=cluster1-134 apply -f -
```

**部署 server + Gateway/VirtualService/AuthorizationPolicy**：

```bash
kubectl --context=cluster1-134 apply -f manifests/xver-server.yaml
kubectl --context=cluster1-134 apply -f manifests/gateway-xver-server.yaml
# gateway → backend 這一跳也要一份 DestinationRule（見「踩的坑」第 4 點）
kubectl --context=cluster1-134 apply -f manifests/destinationrule-xver-server-internal.yaml
```

驗證兩邊都拿到真的 SPIRE 身份：
```bash
kubectl --context=cluster1-134 -n xver-server exec <server-pod> -c istio-proxy -- pilot-agent request GET certs
kubectl --context=cluster1-134 -n istio-system exec <igw-pod> -c istio-proxy -- pilot-agent request GET certs
```

### Step 5：`cluster1`——加一個 "spire-xver" template（給方法 B 用）

```bash
kubectl --context=cluster1 -n istio-system get cm istio-sidecar-injector -o jsonpath='{.data.config}' > /tmp/injector-c1.yaml
kubectl --context=cluster1 -n istio-system get cm istio-sidecar-injector -o jsonpath='{.data.values}' > /tmp/injector-c1-values.json
python3 << 'EOF'
import yaml
spire_template = """labels:
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
"""
with open('/tmp/injector-c1.yaml') as f:
    d = yaml.safe_load(f)
d['templates']['spire'] = spire_template
with open('/tmp/injector-c1.yaml', 'w') as f:
    yaml.dump(d, f, default_flow_style=False)
EOF
kubectl --context=cluster1 -n istio-system create configmap istio-sidecar-injector \
  --from-file=config=/tmp/injector-c1.yaml --from-file=values=/tmp/injector-c1-values.json \
  --dry-run=client -o yaml | kubectl --context=cluster1 apply -f -
```

（1.13.5 沒有 native sidecar，用 `containers:` 不是 `initContainers:`；
`22-`/`24-` 已經有 "spire" 這個 template 名稱給別的架構用時要注意不要
互相覆蓋——這裡假設是全新叢集，沒有這個問題。）

### Step 6：方法 A（`client-a`）——spiffe-helper + DestinationRule MUTUAL

```bash
kubectl --context=cluster1 apply -f manifests/client-a.yaml
```

`client-a.yaml` 重點：
- `spiffe-helper` 用**自己的** CSI volume 掛 SPIRE socket（跟 istio-proxy
  的注入機制完全無關，直接寫在 Pod spec 裡）
- `istio-proxy` 是**預設注入**（沒有自訂 template），但要讓它看得到
  `spiffe-helper` 寫出來的檔案，靠 `sidecar.istio.io/userVolumeMount`
  annotation（不用寫自訂 template 也能幫注入的 istio-proxy 多掛一個
  volumeMount，前提是那個 volume 本身已經在 Pod 自己的 `volumes:`
  裡定義過）：
  ```yaml
  annotations:
    sidecar.istio.io/userVolumeMount: '[{"name":"svids","mountPath":"/svids","readOnly":true}]'
  ```

### Step 7：方法 B（`client-b`）——customConfigFile 兩階段部署

```bash
kubectl --context=cluster1 create namespace client-b --dry-run=client -o yaml | kubectl --context=cluster1 apply -f -
echo '{}' > /tmp/placeholder.json
kubectl --context=cluster1 -n client-b create configmap spire-full-bootstrap \
  --from-file=custom_bootstrap_full.json=/tmp/placeholder.json --dry-run=client -o yaml | kubectl --context=cluster1 apply -f -
kubectl --context=cluster1 apply -f manifests/client-b.yaml   # 第一次開機，annotation 裡還沒有 customConfigFile

./gen_custom_bootstrap.sh cluster1 client-b client-b manifests/custom_bootstrap_full_client-b.json
kubectl --context=cluster1 -n client-b create configmap spire-full-bootstrap \
  --from-file=custom_bootstrap_full.json=manifests/custom_bootstrap_full_client-b.json --dry-run=client -o yaml | kubectl --context=cluster1 apply -f -

# 手動在 manifests/client-b.yaml 加回 customConfigFile annotation 後：
kubectl --context=cluster1 -n client-b delete pod client-b
kubectl --context=cluster1 apply -f manifests/client-b.yaml
```

### Step 8：跨叢集連線 + `hostAliases`

兩邊 client 都要 `hostAliases` 指向 `cluster1-134` igw 的 MetalLB IP
（`docker inspect` 或 `kubectl get svc istio-ingressgateway -o
jsonpath='{.status.loadBalancer.ingress[0].ip}'` 查）：

```bash
kubectl --context=cluster1 apply -f manifests/serviceentry-client-a.yaml
kubectl --context=cluster1 apply -f manifests/serviceentry-client-b.yaml
```

### 驗證

```bash
kubectl --context=cluster1 -n client-a exec client-a -c app -- \
  curl -sS http://xver-server.cross-version.local:15443/
kubectl --context=cluster1 -n client-b exec client-b -c app -- \
  curl -sS http://xver-server.cross-version.local:15443/
# 都應該印出 "hello from xver-server (cluster1-134) via SPIRE-issued cert"
```

`AuthorizationPolicy` A/B 測試：
```bash
kubectl --context=cluster1-134 -n istio-system patch authorizationpolicy xver-server-allow-cluster1-only --type=json \
  -p='[{"op":"replace","path":"/spec/rules/0/from/0/source/principals","value":["diy-25.local/cluster/WRONG/client-a","diy-25.local/cluster/WRONG/client-b"]}]'
# → 兩邊都變 403
kubectl --context=cluster1-134 -n istio-system patch authorizationpolicy xver-server-allow-cluster1-only --type=json \
  -p='[{"op":"replace","path":"/spec/rules/0/from/0/source/principals","value":["diy-25.local/cluster/cluster1/client-a","diy-25.local/cluster/cluster1/client-b"]}]'
# → 兩邊都變回 200（patch 後給幾秒讓設定推播到 igw，不是立即生效）
```

## 安裝過程踩的坑

### 坑 1：Istio 1.29 的預設 template 已經內建 `workload-socket` 佔位 volume

不管是 gateway 還是一般 sidecar，1.29 的 chart 都**已經**宣告一個叫
`workload-socket` 的 volume（預設 `emptyDir: {}`），對應的
volumeMount 也已經存在（`/var/run/secrets/workload-spiffe-uds`）——這是
Istio 自己為原生 SPIRE 整合做的預留位置。**不要用 JSON Patch 的
`add` 對這個路徑操作**（`kubectl patch --type=json -p='[{"op":"add",...}]'`
在 gateway 上直接報錯 `Duplicate value: "workload-socket"`）——gateway
要用 `replace` 指定 index 直接換掉；一般 workload 走注入 template
的話，Istio 自己的多 template 合併邏輯是用 volume 名稱做
strategic-merge（同名覆蓋，不會重複），自訂 "spire" template 只要
宣告同名的 `workload-socket` volume（改成 `csi:` 而不是
`emptyDir:`）就會正確覆蓋掉，不用擔心 duplicate。

### 坑 2：`ServiceEntry` 的 port `protocol` 要填 `HTTP`，不是 `TLS`

`protocol: TLS` 的意思是「app 自己已經送出 TLS bytes，Envoy 只做
SNI-based 的不透明轉發」——這跟我們要的模型（app 送 plain HTTP，
Envoy 自己用 `DestinationRule` 加 mTLS）完全相反。填錯的話，實測會發現
連線完全繞過我們設定的 cluster，掉進 `PassthroughCluster`（純轉發，
不會加任何 TLS），curl 端看到的是 `Empty reply from server`。

### 坑 3：`ServiceEntry` 的 port 號不能跟 mesh 裡其他東西共用

一開始 `ServiceEntry` 用 port 443（跟 gateway 曝露的真實 port 一致），
結果完全沒有任何路由/cluster 被產生出來——查了才發現 `cluster1` 這座
mesh 裡，port 443 早就被 `kube-system` 的 `metallb-webhook-service`
用掉了。**Istio 對同一個 port 號的協定判斷是整個 mesh 共用的，不是
per-destination**，先出現的用法會定調整個 port 號怎麼被處理，後來的
`ServiceEntry` 就算自己填了正確的 `protocol: HTTP` 也會被忽略。改用
`15443`（Istio 自己拿來給 cross-network gateway 用的慣例 port）就正常
了——`ServiceEntry` 自己宣告的 port 號可以跟真正 endpoint 的 port
不同，`endpoints[].ports` 裡照樣填實際的 443。

### 坑 4：gateway → backend 這一跳，也需要一份 `DestinationRule`

跟 client → gateway 需要 `DestinationRule` 的原因一模一樣（`21-`/
`22-`/`24-` 都講過）：`ClusterSPIFFEID` 用了自訂路徑
（`.../cluster/cluster1-134/xver-server`），Istio 的自動 mTLS 幫
**igw 自己**猜的 backend 預期 SAN 是標準 `ns/sa` 格式，猜不到自訂
路徑，igw 呼叫 backend 這一跳會直接
`CERTIFICATE_VERIFY_FAILED:...SAN_match`。這一跳容易漏掉，因為
它是「內部」的一跳（igw 本身也是 mesh 裡的一個 workload，呼叫另一個
workload），不像 client→gateway 那麼顯眼。

### 坑 5：`AuthorizationPolicy` 放錯 namespace，完全不會報錯，也完全不會生效

一開始把卡控 igw 的 `AuthorizationPolicy` 放在 `xver-server`
namespace（跟 server workload 同一個 namespace，直覺上覺得「這是
server 的存取控制」）——`selector: istio: ingressgateway` 這個
selector 完全正確，但**沒有用**：`AuthorizationPolicy` 的 selector
只會在**這個資源自己所在的 namespace**裡找 pod，`istio-ingressgateway`
其實跑在 `istio-system`。結果是：改成錯誤的 principal 測試時，兩邊
client 全部還是 200（完全沒有卡控生效），沒有任何錯誤訊息、沒有
warning、沒有 `rbac_access_denied` 的 log——因為這個 policy 從頭到尾
沒有真的套用到任何 pod 上。把 `metadata.namespace` 改成
`istio-system`（跟 igw 真正跑的 namespace 一致）才正確生效。

## 結果

兩種讓 istio-proxy（或旁邊的 helper）拿到 SPIRE 憑證的方法，都能通過
`cluster1-134` 的 `istio-ingressgateway`（`ISTIO_MUTUAL` 自己終結
mTLS）打到 backend，`AuthorizationPolicy` 用 principal 正確卡控：

```
client-a → igw → xver-server: HTTP_CODE:200
client-b → igw → xver-server: HTTP_CODE:200

# 故意改錯 principal：
client-a → igw: HTTP_CODE:403
client-b → igw: HTTP_CODE:403

# 改回正確：
client-a → igw: HTTP_CODE:200
client-b → igw: HTTP_CODE:200
```

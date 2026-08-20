# 跨 Trust Domain 的 SPIRE Federation + Istio mTLS：k8s 1.24/Istio 1.13.5 ↔ k8s 1.34/Istio 1.29.6

## 驗證題目

兩座**完全獨立的實體叢集**：

- `cluster1`：k8s **v1.24.17**、Istio **1.13.5**，SPIRE trust domain `cluster1-spire.local`
- `cluster1-134`：k8s **v1.34.8**、Istio **1.29.6**，SPIRE trust domain `cluster1-134-spire.local`

兩座的 SPIRE Server **各自發不同的 trust domain**（跟 `22-`/`24-` 刻意用同一個 trust domain 閃掉問題不同），但 intermediate CA 都是**同一顆離線 root key**簽出來的。目標：

1. client（`cluster1`）打 server（`cluster1-134`），雙方各自用 SPIRE 簽發、**trust domain 不同**的憑證，成功建立 mTLS
2. server 端 `AuthorizationPolicy` 用 `principals` 正確卡控（A/B 測試：principal 錯 → 403，改對 → 200）
3. `1.13.5` 沒有原生 SPIRE 整合（`istio/istio#37947` 是 `v1.14.0` 才合併），要用 `customConfigFile` 改寫 Envoy bootstrap 的 SDS socket path 這個 workaround（沿用 `21-`/`22-` 的機制）

**結果：都做到了**，而且做法比第一版簡單很多——關鍵是 SPIRE Agent 自己就有一個內建開關可以做這件事，完全不需要 `EnvoyFilter` 或任何手動掛靜態憑證檔案。細節見「三個關鍵坑」的坑 3。

## 跟其他目錄的關係

| | trust domain | 目的 |
|---|---|---|
| `22-spire-clean-install` | 兩邊**同一個** trust domain | 單一 Istio 版本（1.13.5）的乾淨範本 |
| `24-spire-cross-version-mtls` | 兩邊**同一個** trust domain | 驗證「Istio 版本不同不影響 mTLS」 |
| `25-`（這份） | 兩邊**不同** trust domain | 驗證「trust domain 不同也能 mTLS」——這正是 SPIRE **federation** 存在的理由，`22-`/`24-` 都刻意繞開了這題 |

`22-`/`24-` 選同一個 trust domain是為了不用碰 federation。這份目錄就是把 federation 這塊補上。

`customConfigFile` workaround本身的完整原理見 `21-`/`22-` 的 README；本文只講這次額外遇到的東西。

## 前提

- Docker、`kubectl`、`openssl`、`jq`、`python3`（含 `PyYAML`：`pip install pyyaml`）
- 這次示範環境是巢狀容器（例如雲端 IDE/sandbox 本身就跑在容器裡，`systemd-detect-virt` 會回報 `container-other`）——如果你是在一般 bare-metal/VM 上的 Docker，可以跳過「坑 1」那段 runc 修補，直接用官方 `kindest/node:v1.34.8` image

## Image 清單

### 基礎設施 image

| Image | 用途 |
|---|---|
| `ghcr.io/spiffe/spire-server:1.15.2` | SPIRE Server |
| `ghcr.io/spiffe/spire-agent:1.15.2` | SPIRE Agent（DaemonSet） |
| `ghcr.io/spiffe/spire-controller-manager:0.7.0` | `ClusterSPIFFEID` 宣告式管理 entry |
| `ghcr.io/spiffe/spiffe-csi-driver:0.2.13` | 把 SPIRE Agent socket 掛進 istio-proxy |
| `registry.k8s.io/sig-storage/csi-node-driver-registrar:v2.16.0` | CSI driver 的 node registrar sidecar |
| `docker.io/istio/pilot:1.13.5` / `docker.io/istio/proxyv2:1.13.5` | cluster1 的 istiod / sidecar |
| `docker.io/istio/pilot:1.29.6` / `docker.io/istio/proxyv2:1.29.6` | cluster1-134 的 istiod / sidecar |

### 展示用 workload

| Image | 用途 |
|---|---|
| `hashicorp/http-echo:1.0.0` | server 端最小 echo backend |
| `curlimages/curl:8.16.0` | client 端測試工具 |

## 完整安裝步驟

以下指令假設在 `25-spire-cross-trust-domain-federation/` 目錄下執行。

### Step 0：建立兩座 kind 叢集

```bash
cd kind
./create-clusters.sh
cd ..
```

這支腳本會建 `cluster1`（k8s v1.24.17，用 `kindest/node:v1.24.17`）跟 `cluster1-134`（k8s v1.34.8，用官方 `kindest/node:v1.34.8`）。**如果你是在巢狀容器環境跑**，`cluster1-134` 那段 `kind create cluster` 很可能會卡在 `Starting control-plane` 然後失敗——腳本會自動偵測失敗、build 一份把 runc 換成 1.1.14 的修補 image、重試一次（見「坑 1」）。

kind 版本需求：`cluster1` 用 kind **0.26.0**（跟主流程 `01-kind/` 一致）即可；`cluster1-134` 因為要拉 `kindest/node:v1.34.8` 的預建 image，需要 kind **>=0.32.0**（這是第一個發佈 v1.34.8 image 的 kind release）。兩個版本可以並存成兩支不同的二進位檔（`kind-0.26.0`、`kind-0.32.0`），`create-clusters.sh` 用 `KIND_0_26`/`KIND_0_32` 環境變數指定各自要用哪支：

```bash
KIND_0_26=/path/to/kind-0.26.0 KIND_0_32=/path/to/kind-0.32.0 ./create-clusters.sh
```

### Step 1：安裝 Istio

```bash
./01-install-istio.sh
```

`cluster1` 裝 Istio **1.13.5**（demo profile，用 repo 根目錄 `istio_bin.tar.gz` 裡已經有的 `istioctl-1.13.5`）；`cluster1-134` 裝 Istio **1.29.6**（demo profile，這支 istioctl 不在 `istio_bin.tar.gz` 裡，腳本會自動下載進 `../istio_bin/istioctl-1.29.6`）。

兩邊都是**獨立 standalone 安裝**，沒有 `run-01-to-03.sh` 那套 single-network multi-primary 的 metallb/east-west-gateway 串接——那套機制是設計給「兩座同版本 Istio」互連用的，這裡兩邊版本差很多，也不需要真的把兩個控制平面併成一個 mesh。

### Step 2：SPIRE CRD

```bash
for ctx in cluster1 cluster1-134; do
  kubectl --context=$ctx apply -f manifests/crds/clusterspiffeids.yaml
  kubectl --context=$ctx apply -f manifests/crds/clusterfederatedtrustdomains.yaml
done
```

### Step 3：離線生成共用 root + 兩把不同 trust domain 的 intermediate

```bash
mkdir -p diy-pki && cd diy-pki

openssl ecparam -name prime256v1 -genkey -noout -out root.key
openssl req -x509 -new -key root.key -sha256 -days 3650 \
  -subj "/O=spire-lab/CN=diy-root-xtd" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -out root.crt

for c in cluster1 cluster1-134; do
  openssl ecparam -name prime256v1 -genkey -noout -out int-$c.key
  openssl req -new -key int-$c.key -subj "/O=spire-lab/CN=diy-intermediate-$c" -out int-$c.csr
  openssl x509 -req -in int-$c.csr -CA root.crt -CAkey root.key -CAcreateserial -days 1825 -sha256 \
    -extfile <(printf "basicConstraints=critical,CA:true\nkeyUsage=critical,keyCertSign,cRLSign") \
    -out int-$c.crt
  openssl verify -x509_strict -CAfile root.crt int-$c.crt
done
cd ..
```

**注意 `root.crt` 的 `-addext keyUsage=...`——這行不能省**，見「坑 2」。`22-`/`24-` 的 root 產生方式（純 `openssl req -x509 -new`、不帶 `keyUsage`）在那两份文件的情境下能動是因为从头到尾没人拿 BoringSSL 的 `x509_strict` 去驗；這份文件因為要做跨叢集 mTLS，第一次踩到這個坑。

### Step 4：SPIRE Server（含 federation bundle endpoint）

```bash
kubectl --context=cluster1 apply -f manifests/spire-server-cluster1.yaml
kubectl --context=cluster1 -n spire-server create secret generic diy-intermediate \
  --from-file=intermediate.crt=diy-pki/int-cluster1.crt \
  --from-file=intermediate.key=diy-pki/int-cluster1.key \
  --from-file=root.crt=diy-pki/root.crt

kubectl --context=cluster1-134 apply -f manifests/spire-server-cluster1-134.yaml
kubectl --context=cluster1-134 -n spire-server create secret generic diy-intermediate \
  --from-file=intermediate.crt=diy-pki/int-cluster1-134.crt \
  --from-file=intermediate.key=diy-pki/int-cluster1-134.key \
  --from-file=root.crt=diy-pki/root.crt

kubectl --context=cluster1 -n spire-server rollout status statefulset spire-server --timeout=90s
kubectl --context=cluster1-134 -n spire-server rollout status statefulset spire-server --timeout=90s
```

`manifests/spire-server-cluster1{,-134}.yaml` 跟 `22-` 的版本比多了兩塊：

1. `server.conf` 裡的 `federation { bundle_endpoint { address = "0.0.0.0" port = 8443 } }`——讓這顆 SPIRE Server 自己在 8443 port 用它自己的 SVID 對外發布自己的 trust bundle（`https_spiffe` profile）
2. 一個額外的 `spire-server-federation` **NodePort** Service（`8443:30843`）——因為兩座 SPIRE Server 物理上在不同叢集，federation 抓 bundle 是**跨叢集**的即時 HTTPS 請求，需要真的能連得到對方，不是靠 K8s Service DNS

驗證兩邊的 intermediate 都掛在同一顆 root 下：

```bash
for ctx in cluster1 cluster1-134; do
  kubectl --context=$ctx -n spire-server exec spire-server-0 -c spire-server -- \
    /opt/spire/bin/spire-server bundle show -format spiffe | \
    jq -r '.keys[] | select(.use=="x509-svid") | .x5c[0]' | sha256sum
done
```

### Step 5：SPIRE Agent（含讓 `ROOTCA` 自動帶 federated bundle 的設定）

```bash
kubectl --context=cluster1 apply -f manifests/spire-agent-cluster1.yaml
kubectl --context=cluster1-134 apply -f manifests/spire-agent-cluster1-134.yaml
kubectl --context=cluster1 -n spire-agent rollout status daemonset spire-agent --timeout=90s
kubectl --context=cluster1-134 -n spire-agent rollout status daemonset spire-agent --timeout=90s
```

跟 `22-` 的 `agent.conf` 比多了一個 `sds` block：

```hcl
agent {
  ...
  sds {
    default_bundle_name = "ROOTCA_OWN_ONLY"
    default_all_bundles_name = "ROOTCA"
  }
}
```

**這是整份文件唯一需要的「特殊處理」**，原理跟為什麼需要它見「坑 3」——一行 config 換掉一整套原本以為要用 `EnvoyFilter` 才能解決的問題。

### Step 6：`spiffe-csi-driver`

```bash
kubectl --context=cluster1 apply -f manifests/spiffe-csi-driver.yaml
kubectl --context=cluster1-134 apply -f manifests/spiffe-csi-driver.yaml
```

兩邊都用標準 driver 名稱 `csi.spiffe.io` 就好——不像 `24-` 需要改名，因為這兩座是全新叢集，不會跟既有 SPIRE 安裝撞名。

### Step 7：`istio-sidecar-injector` 加 `spire` template

**`cluster1`**（1.13.5，沒有 native sidecar，要 `customConfigFile` workaround）：

```bash
kubectl --context=cluster1 -n istio-system get cm istio-sidecar-injector -o jsonpath='{.data.config}' > /tmp/injector-config-cluster1.yaml
kubectl --context=cluster1 -n istio-system get cm istio-sidecar-injector -o jsonpath='{.data.values}' > /tmp/injector-values-cluster1.json

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
with open('/tmp/injector-config-cluster1.yaml') as f:
    d = yaml.safe_load(f)
d['templates']['spire'] = spire_template
with open('/tmp/injector-config-cluster1.yaml', 'w') as f:
    yaml.dump(d, f, default_flow_style=False)
EOF

kubectl --context=cluster1 -n istio-system create configmap istio-sidecar-injector \
  --from-file=config=/tmp/injector-config-cluster1.yaml \
  --from-file=values=/tmp/injector-values-cluster1.json \
  --dry-run=client -o yaml | kubectl --context=cluster1 apply -f -
```

**一定要同時帶 `config` 跟 `values` 兩個 key**（哪怕原本沒有也要先讀出來一起寫回）——`istio-sidecar-injector` 這個 ConfigMap 除了 `config` 還有 `values`（Helm values，裡面有 `clusterName`/`network` 等欄位）。這裡有個容易忽略的細節：`kubectl apply` 對 `ConfigMap.data` 這種 map 欄位預設是**合併**、不是覆蓋，所以就算你只塞 `config` 通常也不會弄丟 `values`——**除非之後有東西觸發 istiod Pod 重建**（例如 `rollout restart`）。istiod **啟動時**（不是持續運行時）如果讀到 `values` key 是空的會直接 crash：`missing ConfigMap values key "values"`。踩過一次，修法是拿 `istioctl install` 重跑一次把 ConfigMap 修回預設狀態，再照本節的方式**兩個 key 一起**寫回。

**`cluster1-134`**（1.29.6，native sidecar，原生支援不用 `customConfigFile`）：

```bash
kubectl --context=cluster1-134 -n istio-system get cm istio-sidecar-injector -o jsonpath='{.data.config}' > /tmp/injector-config-cluster1-134.yaml
kubectl --context=cluster1-134 -n istio-system get cm istio-sidecar-injector -o jsonpath='{.data.values}' > /tmp/injector-values-cluster1-134.json

python3 << 'EOF'
import yaml
spire_template = """labels:
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
"""
with open('/tmp/injector-config-cluster1-134.yaml') as f:
    d = yaml.safe_load(f)
d['templates']['spire'] = spire_template
with open('/tmp/injector-config-cluster1-134.yaml', 'w') as f:
    yaml.dump(d, f, default_flow_style=False)
EOF

kubectl --context=cluster1-134 -n istio-system create configmap istio-sidecar-injector \
  --from-file=config=/tmp/injector-config-cluster1-134.yaml \
  --from-file=values=/tmp/injector-values-cluster1-134.json \
  --dry-run=client -o yaml | kubectl --context=cluster1-134 apply -f -
```

用 `initContainers:` 不是 `containers:`——k8s 1.34 支援 native sidecar，混用會產生兩個同名 `istio-proxy` container。

### Step 8：`ClusterSPIFFEID`（含 `federatesWith`）

```bash
kubectl --context=cluster1 apply -f manifests/clusterspiffeid-client-cluster1.yaml
kubectl --context=cluster1-134 apply -f manifests/clusterspiffeid-server-cluster134.yaml
```

跟 `22-` 的版本比多了一個欄位：

```yaml
spec:
  federatesWith:
    - "cluster1-134-spire.local"   # server 那份反過來寫 cluster1-spire.local
```

`FederatesWith` 決定「這個 SPIFFE ID 的持有者，可以額外拿到哪些其他 trust domain 的 bundle」——沒有這行，就算 SPIRE Server 之間已經 federate 好了，發給這個 workload 的憑證也不會帶對方的 trust anchor。

### Step 9：建立 SPIRE Federation Relationship

```bash
# 兩邊 worker 節點的 docker network IP，federation 用它們互抓 bundle
IP_CLUSTER1=$(docker inspect cluster1-worker --format '{{.NetworkSettings.Networks.kind.IPAddress}}')
IP_CLUSTER1_134=$(docker inspect cluster1-134-worker --format '{{.NetworkSettings.Networks.kind.IPAddress}}')

kubectl --context=cluster1 -n spire-server exec spire-server-0 -c spire-server -- \
  /opt/spire/bin/spire-server federation create \
  -trustDomain cluster1-134-spire.local \
  -bundleEndpointURL "https://${IP_CLUSTER1_134}:30843" \
  -bundleEndpointProfile https_spiffe \
  -endpointSpiffeID spiffe://cluster1-134-spire.local/spire/server \
  -trustDomainBundlePath /run/spire/diy-ca/root.crt

kubectl --context=cluster1-134 -n spire-server exec spire-server-0 -c spire-server -- \
  /opt/spire/bin/spire-server federation create \
  -trustDomain cluster1-spire.local \
  -bundleEndpointURL "https://${IP_CLUSTER1}:30843" \
  -bundleEndpointProfile https_spiffe \
  -endpointSpiffeID spiffe://cluster1-spire.local/spire/server \
  -trustDomainBundlePath /run/spire/diy-ca/root.crt
```

- `-trustDomainBundlePath` 給的是**啟動信任**（bootstrap trust）：`https_spiffe` profile 驗證對方 bundle endpoint 的身份時本身就需要先信任一顆 CA，這裡直接餵我們自己手上、本來就有的 `root.crt`，不用另外走 web PKI
- 確認有成功抓到（不是只有「relationship 建好」，要看到真的 fetch 成功）：

```bash
kubectl --context=cluster1 -n spire-server logs spire-server-0 -c spire-server | grep "Bundle refreshed"
# 應該看到：level=info msg="Bundle refreshed" ... trust_domain=cluster1-134-spire.local
```

Federation relationship + `federatesWith` 建好之後，**重建 SPIRE Agent**（讓它把新的 federated bundle 同步進自己的快取）：

```bash
kubectl --context=cluster1 -n spire-agent rollout restart daemonset spire-agent
kubectl --context=cluster1-134 -n spire-agent rollout restart daemonset spire-agent
```

驗證 Envoy 真的能透過 `ROOTCA` 這個資源看到兩個 trust domain（部署 workload 之後才測得出來，先接著往下走）：

```bash
kubectl --context=cluster1 -n spire-test-client exec spire-test-client -c istio-proxy -- \
  curl -s http://localhost:15000/config_dump?resource=dynamic_active_secrets \
  | jq '.configs[] | select(.name=="ROOTCA") | .secret.validation_context.custom_validator_config.typed_config.trust_domains[].name'
# 應該同時看到 "cluster1-spire.local" 跟 "cluster1-134-spire.local"
```

### Step 10：部署 client / server

```bash
# placeholder ConfigMap（cluster1 的 customConfigFile 機制要用）
kubectl --context=cluster1 create namespace spire-test-client --dry-run=client -o yaml | kubectl --context=cluster1 apply -f -
kubectl --context=cluster1 label namespace spire-test-client istio-injection=enabled --overwrite
echo '{}' > /tmp/placeholder.json
kubectl --context=cluster1 -n spire-test-client create configmap spire-full-bootstrap \
  --from-file=custom_bootstrap_full.json=/tmp/placeholder.json --dry-run=client -o yaml | kubectl --context=cluster1 apply -f -

# 第一次開機，還沒有 customConfigFile annotation，正常用 istiod CA
kubectl --context=cluster1 apply -f manifests/istio-client-cluster1.yaml

# server 是 native（1.29.6），一次到位不用兩階段
kubectl --context=cluster1-134 apply -f manifests/istio-server-cluster134.yaml
```

產生真的 bootstrap，回填 ConfigMap，補上 `customConfigFile` annotation，重建 client pod（`manifests/istio-client-cluster1.yaml` 已經內建這個 annotation，所以直接刪掉重建即可觸發）：

```bash
./gen_custom_bootstrap.sh cluster1 spire-test-client spire-test-client /tmp/custom_bootstrap_full_client.json
kubectl --context=cluster1 -n spire-test-client create configmap spire-full-bootstrap \
  --from-file=custom_bootstrap_full.json=/tmp/custom_bootstrap_full_client.json --dry-run=client -o yaml | kubectl --context=cluster1 apply -f -

kubectl --context=cluster1 -n spire-test-client delete pod spire-test-client
kubectl --context=cluster1 apply -f manifests/istio-client-cluster1.yaml
```

驗證兩邊都真的拿到 SPIRE 簽的憑證（不是 istiod CA 的）：

```bash
kubectl --context=cluster1 -n spire-test-client exec spire-test-client -c istio-proxy -- pilot-agent request GET certs \
  | grep -A1 '"uri"'
# 應該看到 spiffe://cluster1-spire.local/cluster/cluster1/spire-test-client

SERVER_POD=$(kubectl --context=cluster1-134 -n spire-test-server get pod -l app=spire-test-server -o jsonpath='{.items[0].metadata.name}')
kubectl --context=cluster1-134 -n spire-test-server exec $SERVER_POD -c istio-proxy -- pilot-agent request GET certs \
  | grep -A1 '"uri"'
# 應該看到 spiffe://cluster1-134-spire.local/cluster/cluster1-134/spire-test-server
```

### Step 11：跨叢集連通（`ServiceEntry` + `DestinationRule` + `hostAliases`）

client 跟 server 是兩座物理上分開、沒有共用網路的叢集，做法沿用 `24-`：`ServiceEntry`（STATIC）指到 server 那座叢集 worker node 的 docker network IP + NodePort，`DestinationRule` 帶 server 真正的 SPIFFE ID 當 `subjectAltNames`（因為我們的 `ClusterSPIFFEID` 模板是自訂路徑，不是 Istio 自動猜的 `ns/sa` 格式），`hostAliases` 解決兩邊沒有共用 DNS 的問題。

```bash
IP_CLUSTER1_134=$(docker inspect cluster1-134-worker --format '{{.NetworkSettings.Networks.kind.IPAddress}}')
```

把這個 IP 換進 `manifests/serviceentry-server.yaml`（`endpoints[0].address`）跟 `manifests/istio-client-cluster1.yaml`（`hostAliases[0].ip`）——本 repo 附的版本裡寫死的是我自己測試環境的 `172.18.0.4`，你的 docker network 分配到的 IP大概率不一樣。改完：

```bash
kubectl --context=cluster1 apply -f manifests/serviceentry-server.yaml
kubectl --context=cluster1 -n spire-test-client delete pod spire-test-client
kubectl --context=cluster1 apply -f manifests/istio-client-cluster1.yaml   # 讓新的 hostAliases 生效
```

### Step 12：驗證 mTLS + `PeerAuthentication`/`AuthorizationPolicy`

`PeerAuthentication`（STRICT）跟第一版 `AuthorizationPolicy`（只允許 `cluster1-spire.local/cluster/cluster1/spire-test-client`）已經包在 `manifests/istio-server-cluster134.yaml` 裡了，Step 10 就套用過。

```bash
kubectl --context=cluster1 -n spire-test-client exec spire-test-client -c app -- \
  curl -sS -o /dev/null -w "HTTP_CODE:%{http_code}\n" http://xtd-server.cross-cluster.local:8080/
# 200
```

A/B 測試證明 `AuthorizationPolicy` 真的在依身份卡控：

```bash
# 故意改成錯的 principal
kubectl --context=cluster1-134 -n spire-test-server patch authorizationpolicy spire-test-server-allow-cluster1-only --type=json \
  -p='[{"op":"replace","path":"/spec/rules/0/from/0/source/principals/0","value":"cluster1-spire.local/cluster/WRONG-cluster/spire-test-client"}]'
sleep 8   # 等 xDS push 生效
kubectl --context=cluster1 -n spire-test-client exec spire-test-client -c app -- \
  curl -sS -o /dev/null -w "HTTP_CODE:%{http_code}\n" http://xtd-server.cross-cluster.local:8080/
# 403

# 改回正確的
kubectl --context=cluster1-134 -n spire-test-server patch authorizationpolicy spire-test-server-allow-cluster1-only --type=json \
  -p='[{"op":"replace","path":"/spec/rules/0/from/0/source/principals/0","value":"cluster1-spire.local/cluster/cluster1/spire-test-client"}]'
sleep 8
kubectl --context=cluster1 -n spire-test-client exec spire-test-client -c app -- \
  curl -sS -o /dev/null -w "HTTP_CODE:%{http_code}\n" http://xtd-server.cross-cluster.local:8080/
# 200
```

實測結果（server 端 access log）：
```
200 - via_upstream ...
403 - rbac_access_denied_matched_policy[none] ...
200 - via_upstream ...
```

## 三個關鍵坑

### 坑 1：runc >=1.2.2 在巢狀容器環境的 `/proc` mount regression

建 `cluster1-134`（k8s 1.34.8）時，`kind create cluster` 卡在 `Starting control-plane` 然後失敗。查進 node container 才發現 kubelet 一直起不了任何 static pod：

```
error mounting "proc" to rootfs at "/proc": mount src=proc, dst=/proc,
dstFd=/proc/thread-self/fd/11, flags=MS_NOSUID|MS_NODEV|MS_NOEXEC: no such file or directory
```

這是 [runc#4542](https://github.com/opencontainers/runc/issues/4542) 這類 issue 描述的已知 regression：runc **1.2.2** 之後，新的（fd-based）`/proc` mount 方式在巢狀容器環境（DinD、這次的雲端 sandbox 亦同）會炸掉，`1.1.14` 是最後一個沒問題的版本。`kindest/node:v1.34.8` 內建 runc 1.4.2（有這個 bug），`kindest/node:v1.24.17` 內建 runc 1.1.12（沒有）——這就是為什麼只有 1.34 那座建不起來。

修法：拿官方 image 疊一層，把 `/usr/local/sbin/runc` 換成 runc 1.1.14 靜態二進位（見 `kind/Dockerfile.node-1.34.8-runc1.1.14`），`kind/create-clusters.sh` 已經把「先試官方 image、失敗就自動 build 修補版重試」這個邏輯寫進去了。**只有巢狀容器環境會踩到這個**——一般 bare-metal/VM 上的 Docker 不受影響，可以直接用官方 image。

### 坑 2：DIY root CA 沒有 `KeyUsage` 擴充欄位，BoringSSL 嚴格模式驗證會過不了

第一次跑 Step 3 時用的是最簡單的 `openssl req -x509 -new -key root.key ...`（不帶任何 `-addext`）。`openssl verify`（預設寬鬆模式）看這條 chain 完全 OK，但實際 mTLS 一直卡在：

```
TLS_error: 268435581:SSL routines:OPENSSL_internal:CERTIFICATE_VERIFY_FAILED
```

雙邊 log 都只有這行，沒有更多細節。後來拿 `openssl verify -x509_strict`（BoringSSL/Envoy 實際採用的嚴格驗證模式）重跑同一條 chain，才抓到真正原因：

```
error 92 at 3 depth lookup: CA cert does not include key usage extension
```

`openssl req -x509` 產生 self-signed root 預設**不會**自動加 `KeyUsage`（只會加 `basicConstraints: CA:TRUE`），而 BoringSSL 對「鏈上每一張 CA 憑證（含 root）都要有 `KeyUsage` 且要有 `keyCertSign`」這件事是嚴格要求的，OpenSSL CLI 預設模式則不管。`22-`/`24-` 沒踩到這個坑純粹是運氣——那两份文件的示範沒有人拿 `x509_strict` 去驗過。

修法：生成 root 的時候明確加 `-addext "keyUsage=critical,keyCertSign,cRLSign"`（連同 `basicConstraints`，見 Step 3）。因為 root.key 沒變，重簽 root.crt 不影響已經簽出去的 intermediate（intermediate 的簽章只依賴 root.key，不依賴 root.crt 裡的 extension），只需要把新 root.crt 重新塞進兩邊 SPIRE Server 的 Secret、重啟 StatefulSet 即可。

**如果你在別的地方（或別的 Istio/Envoy 版本）也用 DIY root CA 做 mTLS demo，先跑一次 `openssl verify -x509_strict`，不要只信預設模式的 `openssl verify` 結果。**

### 坑 3：Istio 固定跟 SPIRE 要名叫 `ROOTCA` 的資源，但 SPIRE 預設 `ROOTCA` 只有自己的 trust domain——一行 config 就能解掉，不需要 `EnvoyFilter`

Step 9 的 federation relationship + `federatesWith` 都設好、`spire-server federation show`/log 都證明 bundle 真的抓到了（`Bundle refreshed`），但一開始 client/server 的 Envoy 一樣 `CERTIFICATE_VERIFY_FAILED` / `SSLV3_ALERT_CERTIFICATE_UNKNOWN`。用 Envoy admin API 把 `ROOTCA` 這個 secret 的實際內容 dump 出來看：

```bash
kubectl --context=cluster1 -n spire-test-client exec spire-test-client -c istio-proxy -- \
  curl -s http://localhost:15000/config_dump?resource=dynamic_active_secrets \
  | jq '.configs[] | select(.name=="ROOTCA") | .secret.validation_context.custom_validator_config.typed_config.trust_domains[].name'
```

無論 federation/`federatesWith` 設得再對，這裡永遠只列出**自己的** trust domain（`"cluster1-spire.local"`），從來不包含 `federatesWith` 指定的 `cluster1-134-spire.local`。

**一開始的誤判**：以為這是 SPIRE 這個 legacy Envoy SDS 相容層本身不支援 federation，於是走了一輪 `EnvoyFilter` 把 `validation_context` 換成靜態掛進 pod 的共用 `root.crt`——這條路**可行**，但完全是繞遠路。上網查了 [SPIRE Agent 的 SDS Configuration 文件](https://github.com/spiffe/spire/blob/main/doc/spire_agent.md#sds-configuration) 才發現真正原因跟正確解法：

SPIRE Agent 的 Envoy SDS 相容層其實同時提供**兩種**資源，名字都可以自訂：

| 設定 | 預設名稱 | 內容 |
|---|---|---|
| `default_bundle_name` | `ROOTCA` | 只有**自己 trust domain** 的 CA |
| `default_all_bundles_name` | `ALL` | 自己 **+ 所有 federated** trust domain 的 CA |

Istio 的 bootstrap（包含我們 `customConfigFile` 改過的那份，跟 1.29.6 native 整合用的那份）都是寫死跟 SPIRE 要名叫 `"ROOTCA"` 的資源——剛好精準命中「只有自己」那個預設值，`ALL`（真正含 federated bundle 的那個）Istio 從來沒去要過。這不是 bug，是兩邊的預設命名剛好對不上。

**真正的修法**：把這兩個名字互換，讓 Istio 一直在要的 `"ROOTCA"` 這個名字,底下放的內容變成「全部」：

```hcl
agent {
  ...
  sds {
    default_bundle_name = "ROOTCA_OWN_ONLY"   # 改名讓出 "ROOTCA" 這個位置
    default_all_bundles_name = "ROOTCA"        # Istio 要的 "ROOTCA" 現在回傳全部（自己+federated）
  }
}
```

改完 `agent.conf`、重建 SPIRE Agent DaemonSet、重建 workload pod（重新走一次 SDS 訂閱），`ROOTCA` 立刻就同時包含兩個 trust domain，完全不用碰任何 Istio 側的 `EnvoyFilter`、不用額外掛靜態憑證檔案——雙方 identity 憑證跟驗證用的 trust bundle **從頭到尾都是 SPIRE 動態發的**，這才是真正乾淨的做法。

`22-`/`24-` 完全沒踩到這個，是因為兩邊用**同一個** trust domain，`ROOTCA`（只含自己）剛好就是全部需要的東西，這個命名對不上的問題根本不會顯現。

## 離線安裝

跟 `20-`/`21-`/`22-` 一樣，把上面所有 `docker pull`/`curl` 換成你自己的 registry/離線來源即可，沒有額外的 offline-specific 步驟需要文件化（這份目錄的 image 清單都列在最上面）。

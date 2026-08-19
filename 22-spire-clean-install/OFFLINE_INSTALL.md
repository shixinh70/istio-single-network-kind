# 離線環境安裝：image 清單 + pre-render 好的 manifest

跟 `17-`/`19-`/`20-`/`21-` 的 `OFFLINE_INSTALL.md` 同一套模式，但這次連
image 改寫都不用 Python——manifest 本來就是純靜態 YAML，改 image 位置
直接用 `sed` 對固定的幾個字串做替換即可。

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

## Step 1：在有網路的機器上拉 image、推進你的 private registry

```bash
REGISTRY="<YOUR_REGISTRY>"

IMAGES=(
  "ghcr.io/spiffe/spire-server:1.15.2"
  "ghcr.io/spiffe/spire-agent:1.15.2"
  "ghcr.io/spiffe/spire-controller-manager:0.7.0"
  "ghcr.io/spiffe/spiffe-csi-driver:0.2.13"
  "registry.k8s.io/sig-storage/csi-node-driver-registrar:v2.16.0"
  "hashicorp/http-echo:1.0.0"
  "curlimages/curl:8.16.0"
)

for img in "${IMAGES[@]}"; do
  docker pull "$img"
  path_and_tag=$(echo "$img" | sed -E 's#^[^/]*\.[^/]*/##; s#^[^/]*:[0-9]+/##')
  docker tag "$img" "$REGISTRY/$path_and_tag"
  docker push "$REGISTRY/$path_and_tag"
done
```

氣隙環境版本（先 `docker save`，搬進去再 `docker load`）：

```bash
# 有網路的機器
docker save -o spire22-images.tar "${IMAGES[@]}"
# 氣隙環境
docker load -i spire22-images.tar
for img in "${IMAGES[@]}"; do
  path_and_tag=$(echo "$img" | sed -E 's#^[^/]*\.[^/]*/##; s#^[^/]*:[0-9]+/##')
  docker tag "$img" "$REGISTRY/$path_and_tag"
  docker push "$REGISTRY/$path_and_tag"
done
```

## Step 2：把 manifest 複製一份、用 `sed` 改寫 image 位置

```bash
cd 22-spire-clean-install
REGISTRY="<YOUR_REGISTRY>"

cp -r manifests manifests-offline

sed -i "s#ghcr.io/spiffe/spire-server:1.15.2#${REGISTRY}/spire-server:1.15.2#" \
  manifests-offline/spire-server-cluster1.yaml manifests-offline/spire-server-cluster2.yaml
sed -i "s#ghcr.io/spiffe/spire-controller-manager:0.7.0#${REGISTRY}/spire-controller-manager:0.7.0#" \
  manifests-offline/spire-server-cluster1.yaml manifests-offline/spire-server-cluster2.yaml
sed -i "s#ghcr.io/spiffe/spire-agent:1.15.2#${REGISTRY}/spire-agent:1.15.2#" \
  manifests-offline/spire-agent-cluster1.yaml manifests-offline/spire-agent-cluster2.yaml
sed -i "s#ghcr.io/spiffe/spiffe-csi-driver:0.2.13#${REGISTRY}/spiffe-csi-driver:0.2.13#" \
  manifests-offline/spiffe-csi-driver.yaml
sed -i "s#registry.k8s.io/sig-storage/csi-node-driver-registrar:v2.16.0#${REGISTRY}/csi-node-driver-registrar:v2.16.0#" \
  manifests-offline/spiffe-csi-driver.yaml
sed -i "s#hashicorp/http-echo:1.0.0#${REGISTRY}/http-echo:1.0.0#" \
  manifests-offline/istio-server.yaml
sed -i "s#curlimages/curl:8.16.0#${REGISTRY}/curl:8.16.0#" \
  manifests-offline/istio-client.yaml

grep -rh "image:" manifests-offline/ | sort -u   # 檢查改寫結果
```

`manifests/` 原始檔案不動，`manifests-offline/` 兩份並存。

## Step 3：照 `README.md` 的安裝步驟走，全部改用 `manifests-offline/`

流程完全一樣，只是 `kubectl apply -f manifests/xxx.yaml` 改成
`kubectl apply -f manifests-offline/xxx.yaml`。

**注意**：`README.md` Step 12 需要手動編輯 `istio-client.yaml`／
`istio-server.yaml` 加回 `customConfigFile` annotation——如果你是在
`manifests-offline/` 裡的複本上做這個編輯，image 參照已經是改寫過的，
不用再處理一次。

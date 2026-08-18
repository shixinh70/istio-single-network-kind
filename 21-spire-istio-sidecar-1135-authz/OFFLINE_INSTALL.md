# 離線環境安裝：image 清單 + pre-render 好的 manifest

跟 `17-`/`19-`/`20-` 的 `OFFLINE_INSTALL.md` 同一套模式。全部 manifest
一樣是靜態渲染好的 YAML——沒有用 Helm，`gen_spire_1152.py` 產生的檔案可以
直接離線 `kubectl apply`，唯一要另外處理的是 image。

## Image 清單

### 基礎設施 image（一定要）

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

### 已經跑在叢集上的前提（不是這次新裝）

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
  "ghcr.io/spiffe/spiffe-csi-driver:0.2.7"
  "registry.k8s.io/sig-storage/csi-node-driver-registrar:v2.6.0"
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

如果 registry 本身也在氣隙環境內，拆成 `docker pull` + `docker save -o
images.tar` 在有網路的機器上做，搬進氣隙環境後 `docker load` + 逐一
`tag`/`push`：

```bash
# 有網路的機器
docker save -o spire21-images.tar "${IMAGES[@]}"
# 氣隙環境
docker load -i spire21-images.tar
for img in "${IMAGES[@]}"; do
  path_and_tag=$(echo "$img" | sed -E 's#^[^/]*\.[^/]*/##; s#^[^/]*:[0-9]+/##')
  docker tag "$img" "$REGISTRY/$path_and_tag"
  docker push "$REGISTRY/$path_and_tag"
done
```

## Step 2：改寫 manifest 裡的 image 參照

```bash
cd 21-spire-istio-sidecar-1135-authz
python3 rewrite_images_for_offline.py "<YOUR_REGISTRY>"
```

輸出到 `manifests-offline/`，`manifests/` 原始檔案不動，兩份並存。

## Step 3：照 `README.md` 的安裝步驟走，全部改用 `manifests-offline/`

流程完全一樣，只是 `kubectl apply -f manifests/xxx.yaml` 改成
`kubectl apply -f manifests-offline/xxx.yaml`。

**注意**：`README.md` Step 11 需要手動編輯 `istio-client.yaml`／
`istio-server.yaml` 加回 `customConfigFile` annotation——如果你是在
`manifests-offline/` 裡的複本上做這個編輯，image 參照已經是改寫過的，
不用再跑一次 `rewrite_images_for_offline.py`。

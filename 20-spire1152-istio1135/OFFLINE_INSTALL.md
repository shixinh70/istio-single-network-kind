# 離線環境安裝：image 清單 + pre-render 好的 manifest

跟 `17-spire-cross-cluster-mtls/OFFLINE_INSTALL.md` 同一套模式，這裡是
`20-spire1152-istio1135/`（SPIRE **1.15.2**、DIY 共用 root、對接
Istio **1.13.5**）專屬的版本。全部 manifest 一樣是靜態渲染好的
YAML——沒有用 Helm，`gen_spire_1152.py` 產生的檔案可以直接離線 `kubectl
apply`，唯一要另外處理的是 image。

## Image 清單

### 基礎設施 image（安裝 SPIRE + Controller Manager 一定要）

| Image | 用途 |
|---|---|
| `ghcr.io/spiffe/spire-server:1.15.2` | SPIRE Server 本體（最新版） |
| `ghcr.io/spiffe/spire-agent:1.15.2` | SPIRE Agent（DaemonSet，最新版） |
| `ghcr.io/spiffe/spire-controller-manager:0.7.0` | `ClusterSPIFFEID` 宣告式管理 entry（目前最新版，1.15.2 沒有更新的相容版本） |

這次**不需要** `spiffe-csi-driver`／`csi-node-driver-registrar`——因為這組
測試用的是純 `spiffe-helper`（檔案式 SVID）做 peer-to-peer mTLS，完全不
經過 Istio 的 sidecar/SDS，跟 `17-`/`19-` 那種要整合進 Istio mesh 的做法
不一樣，所以少兩個 image。

### 這次 lab 測試用的範例 workload image（正式環境會換成你自己的 app）

| Image | 用途 |
|---|---|
| `ghcr.io/spiffe/spiffe-helper:0.9.0` | 兩邊 pod 把 SVID 寫成本機檔案用 |
| `curlimages/curl:8.16.0` | client 端測試工具 |
| `alpine:3.20` | server 端執行 `openssl s_server` 用的最小 image（啟動時 `apk add openssl`） |

### 已經跑在叢集上的前提（不是這次新裝）

| Image | 說明 |
|---|---|
| `docker.io/istio/pilot:1.13.5` | istiod，本次要驗證的「原生 CA 不受影響」對象 |
| `docker.io/istio/proxyv2:1.13.5` | Istio sidecar，這次測試完全沒用到（peer-to-peer 走 SPIRE 憑證，不經過 mesh），列出來只是確認環境版本 |

## Step 1：在有網路的機器上拉 image、推進你的 private registry

```bash
REGISTRY="<YOUR_REGISTRY>"

IMAGES=(
  "ghcr.io/spiffe/spire-server:1.15.2"
  "ghcr.io/spiffe/spire-agent:1.15.2"
  "ghcr.io/spiffe/spire-controller-manager:0.7.0"
  "ghcr.io/spiffe/spiffe-helper:0.9.0"
  "curlimages/curl:8.16.0"
  "alpine:3.20"
)

for img in "${IMAGES[@]}"; do
  docker pull "$img"
  path_and_tag=$(echo "$img" | sed -E 's#^[^/]*\.[^/]*/##; s#^[^/]*:[0-9]+/##')
  docker tag "$img" "$REGISTRY/$path_and_tag"
  docker push "$REGISTRY/$path_and_tag"
done
```

跟 `17-` 的文件一樣，如果 registry 本身也在氣隙環境內，拆成
`docker pull` + `docker save -o images.tar` 在有網路的機器上做，搬進氣隙
環境後 `docker load` + 逐一 `tag`/`push`。

## Step 2：改寫 manifest 裡的 image 參照

```bash
cd 20-spire1152-istio1135
python3 rewrite_images_for_offline.py "<YOUR_REGISTRY>"
```

輸出到 `manifests-offline/`，`manifests/` 原始檔案不動，兩份並存。

## Step 3：照 `README.md` 的安裝步驟走，全部改用 `manifests-offline/`

流程完全一樣，只是 `kubectl apply -f manifests/xxx.yaml` 改成
`kubectl apply -f manifests-offline/xxx.yaml`。

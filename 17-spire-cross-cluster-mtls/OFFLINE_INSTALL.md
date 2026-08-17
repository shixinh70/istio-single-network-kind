# 離線環境安裝：image 清單 + pre-render 好的 manifest

目標環境沒有對外網路。這份文件涵蓋 `INSTALL_CONTROLLER_MANAGER_VERSION.md`
那 12 個 step 實際會用到的東西，改成離線可用版本：
- manifest 全部**已經是靜態、渲染好的 YAML**——這整套從頭到尾沒有用
  Helm，是直接用 Python script（`gen_spire_cluster.py`、
  `gen_controller_manager.py`）產生原生 K8s manifest，`manifests/` 底下
  的檔案本來就不需要任何線上 template render 步驟，可以直接離線 apply。
- 兩個 CRD（`clusterspiffeids.yaml`、`clusterfederatedtrustdomains.yaml`）
  之前就已經抓下來存成本機檔案，不是安裝當下才去外網抓的。
- 真正需要處理的，只有 **container image**——這些預設指向公開 registry
  （`ghcr.io`、`registry.k8s.io`、`docker.io`），離線環境的節點拉不到，
  需要你先在有網路的機器上拉好、推進你的內部 private registry，並且把
  manifest 裡的 image 參照改指到那個 registry。

## Image 清單

### 基礎設施 image（安裝 SPIRE + Controller Manager + CSI 一定要）

| Image | 用途 | 出現在哪個 manifest |
|---|---|---|
| `ghcr.io/spiffe/spire-server:1.11.2` | SPIRE Server 本體 | `spire-cluster*.yaml`, `controller-manager-cluster*.yaml` |
| `ghcr.io/spiffe/spire-agent:1.11.2` | SPIRE Agent（DaemonSet） | `spire-cluster*.yaml` |
| `ghcr.io/spiffe/spire-controller-manager:0.7.0` | `ClusterSPIFFEID` 宣告式管理 entry 的 controller | `controller-manager-cluster*.yaml` |
| `ghcr.io/spiffe/spiffe-csi-driver:0.2.7` | 讓 pod 用 ephemeral volume 掛進 SPIRE agent socket | `spiffe-csi-driver.yaml` |
| `registry.k8s.io/sig-storage/csi-node-driver-registrar:v2.6.0` | CSI driver 向 kubelet 註冊用的標準 sidecar | `spiffe-csi-driver.yaml` |

### 這次 lab 測試用的範例 workload image（正式環境會換成你自己的 app，不一定要拉這些）

| Image | 用途 | 出現在哪個 manifest |
|---|---|---|
| `ghcr.io/spiffe/spiffe-helper:0.9.0` | 把 SVID 寫成本機檔案（`agent` 測試 client 用；`mcp-echo-spire` 走原生 Istio SDS 不需要它） | `agent-pod.yaml` |
| `curlimages/curl:8.16.0` | 測試 client（模擬 agent workload） | `agent-pod.yaml` |
| `hashicorp/http-echo:1.0.0` | 測試 server 端 workload（模擬 mcp-echo） | `mcp-echo-spire.yaml` |

**未涵蓋在這份清單、但是必要前提**：Istio 本身的 image（`istio/pilot`、
`istio/proxyv2` 等）——這整套 SPIRE 整合假設目標叢集**已經裝好 Istio**
（`istio-sidecar-injector` ConfigMap 已存在，才有東西可以 patch），Istio
本身的離線安裝不在這份文件範圍內。

## Step 1：在有網路的機器上，把 image 拉下來、推進你的 private registry

```bash
REGISTRY="<YOUR_REGISTRY>"   # 換成你實際的 registry 位址，例如 registry.internal.company.com

IMAGES=(
  "ghcr.io/spiffe/spire-server:1.11.2"
  "ghcr.io/spiffe/spire-agent:1.11.2"
  "ghcr.io/spiffe/spire-controller-manager:0.7.0"
  "ghcr.io/spiffe/spiffe-csi-driver:0.2.7"
  "registry.k8s.io/sig-storage/csi-node-driver-registrar:v2.6.0"
  "ghcr.io/spiffe/spiffe-helper:0.9.0"
  "curlimages/curl:8.16.0"
  "hashicorp/http-echo:1.0.0"
)

for img in "${IMAGES[@]}"; do
  docker pull "$img"
  # 去掉原本的 registry host，保留路徑+tag，重新掛到你的 registry 下
  # （跟 rewrite_images_for_offline.py 改寫 manifest 的規則完全一致）
  path_and_tag=$(echo "$img" | sed -E 's#^[^/]*\.[^/]*/##; s#^[^/]*:[0-9]+/##')
  docker tag "$img" "$REGISTRY/$path_and_tag"
  docker push "$REGISTRY/$path_and_tag"
done
```

如果你的 image 沒辦法在同一台機器上直接 `docker push`（例如 registry
本身也在氣隙環境內、要用 `docker save` 搬 tarball 進去再 `docker load` +
手動 push），把上面迴圈拆成兩段：有網路的機器上 `docker pull` +
`docker save -o images.tar "${IMAGES[@]}"`，搬進氣隙環境後
`docker load -i images.tar`，再逐一 `tag`/`push` 到內部 registry。

## Step 2：把 manifest 裡的 image 參照改指到你的 registry

```bash
cd 17-spire-cross-cluster-mtls
python3 rewrite_images_for_offline.py "<YOUR_REGISTRY>"
```

這會產生一份新的 `manifests-offline/` 目錄，內容跟 `manifests/` 一模
一樣，唯一差別是每個 `image:` 都改指到你的 registry（例如
`ghcr.io/spiffe/spire-server:1.11.2` 變成
`<YOUR_REGISTRY>/spiffe/spire-server:1.11.2`）。**`manifests/` 原始檔案
不會被動到**——如果之後還有網路可用的環境要裝，`manifests/` 那份原封
不動可以直接照 `INSTALL_CONTROLLER_MANAGER_VERSION.md` 用。

## Step 3：照 `INSTALL_CONTROLLER_MANAGER_VERSION.md` 的 12 個 step 走，只是全部改用 `manifests-offline/`

流程、每一條 `kubectl exec`/`kubectl apply` 指令完全不變，唯一差別：
所有 `kubectl apply -f manifests/xxx.yaml` 改成
`kubectl apply -f manifests-offline/xxx.yaml`。CRD、`patch_*.py`、
`gen_full_mesh_federation.py` 這些不含 image 參照的部分不用改，直接照
原本文件操作。

## 驗證 image 已經正確改寫

```bash
grep -rh "image:" manifests-offline/*.yaml manifests-offline/crds/*.yaml 2>/dev/null | sort -u
```
應該全部都指到 `<YOUR_REGISTRY>/...`，不該再看到 `ghcr.io`、
`registry.k8s.io`、或沒有 registry host 的裸路徑。

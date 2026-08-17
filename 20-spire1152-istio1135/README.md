# SPIRE 1.15.2（最新版）+ Istio 1.13.5：跨叢集 mTLS，istiod 原生 CA 不受影響

## 目的
驗證兩件事：
1. SPIRE **1.15.2**（目前最新版，`17-`/`18-`/`19-` 用的都是 1.11.2）跟
   **Istio 1.13.5**（比 `17-`/`19-` 用的 1.29.6 舊很多）搭配沒有相容性
   問題
2. 兩座**各自獨立的**、都跑 Istio 1.13.5 的叢集（`cluster1`、`cluster2`），
   用 SPIRE 簽發的憑證做 peer-to-peer mTLS，同時確認兩邊 istiod 自己內建
   的 CA 系統完全沒被動到

架構延續 `19-diy-shared-root-controller-manager/` 驗證過的「DIY 共用
root + `ClusterSPIFFEID`」——目前測過最簡的組合。這次刻意**不**整合進
Istio mesh（不用 `spiffe-csi-driver`、不用原生 SDS injection
template）——因為要測的是「SPIRE 憑證本身」+「istiod 完全獨立」，走
`spiffe-helper`（檔案式 SVID）peer-to-peer 直接做 mTLS，比整合進 mesh
更乾淨地隔離出這兩個問題本身，不會把「Istio 版本相不相容」跟「native SDS
機制相不相容」這兩件事混在一起。

## 拓樸
`cluster1`、`cluster2`：都是 Kind + k8s 1.24.17 + **Istio 1.13.5**。獨立
`spire-1315` namespace（跟兩邊既有的 `17-`/`19-` 東西完全不衝突，命名上
特意跟它們的 namespace 不同）。Trust domain 統一：`diy-1152.local`。

## 完整安裝步驟

### Step 0（如果你的叢集也跟我一樣壞過）：修復 apiserver/etcd/controller-manager 用舊 IP 的問題

這不是這次要教的內容，是這個 lab 環境本身的技術債（Kind container 重啟
後 IP 換了，但 `/etc/kubernetes/manifests/*.yaml` 跟
`/etc/kubernetes/*.conf` 裡的 IP 沒跟著更新），如果你的目標環境是全新
乾淨的，跳過這步。修法：

```bash
NEW_IP=$(docker inspect <control-plane-container> --format '{{.NetworkSettings.Networks.kind.IPAddress}}')
docker exec <control-plane-container> sh -c "sed -i \"s/<OLD_IP>/$NEW_IP/g\" \
  /etc/kubernetes/manifests/etcd.yaml /etc/kubernetes/manifests/kube-apiserver.yaml \
  /etc/kubernetes/controller-manager.conf /etc/kubernetes/scheduler.conf"
docker exec <control-plane-container> systemctl restart kubelet
# 靜態 pod 的 kubeconfig 檔案變了，manifest 本身沒變，kubelet 不會自動重建
# container，要手動 crictl stop 逼它重建：
docker exec <control-plane-container> crictl stop <controller-manager-container-id> <scheduler-container-id>
```

### Step 1：CRD

```bash
for ctx in cluster1 cluster2; do
  kubectl --context=$ctx apply -f manifests/crds/clusterspiffeids.yaml
  kubectl --context=$ctx apply -f manifests/crds/clusterfederatedtrustdomains.yaml
done
```

### Step 2：離線生成 root + 兩份 intermediate

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

python3 gen_spire_1152.py spire-1315 cluster1 manifests/spire-1152-cluster1.yaml
python3 gen_spire_1152.py spire-1315 cluster2 manifests/spire-1152-cluster2.yaml
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

### Step 4：`ClusterSPIFFEID` + workload

```bash
kubectl --context=cluster1 apply -f manifests/clusterspiffeids.yaml
kubectl --context=cluster2 apply -f manifests/clusterspiffeids.yaml
kubectl --context=cluster1 apply -f manifests/peer-client.yaml
kubectl --context=cluster2 apply -f manifests/peer-server.yaml
```

### Step 5：驗證

```bash
docker inspect cluster2-worker --format '{{.NetworkSettings.Networks.kind.IPAddress}}'  # 找 IP

kubectl --context=cluster1 -n peer-test-client exec peer-client -c app -- \
  curl -sS -k --http1.1 --resolve peer-server.peer-test-client.svc.cluster.local:30543:<IP> \
  --cert /svids/tls.crt --key /svids/tls.key --cacert /svids/ca.crt \
  https://peer-server.peer-test-client.svc.cluster.local:30543/
```

## 安裝過程踩的坑

### 坑 1：`openssl s_server` 預設只送 leaf 憑證，不會自動帶上 PEM 檔裡的中繼憑證

`peer-server` 用 `openssl s_server -cert /svids/tls.crt`，那個檔案其實
包含完整鏈（leaf + SPIRE 內部 CA 層 + 我們的 diy-intermediate，共 3
張），照直覺以為 `-cert` 指到一個「多張憑證串起來」的檔案就會整包送出去
——**實際上不會**，`openssl s_client` 連上去只看到 1 張憑證
（`Certificate chain` 只有 index `0`），client 端因此 `unable to get
local issuer certificate`（因為 client 手上的 `ca.crt` 只有 root，中間
少了一段接不起來）。修法：額外加 `-cert_chain /svids/tls.crt`（同一個
檔案再指一次，明確告訴 openssl「這個檔案裡除了 leaf，其餘的都當中繼鏈
一起送」）：
```
openssl s_server ... -cert /svids/tls.crt -cert_chain /svids/tls.crt -key /svids/tls.key ...
```

### 坑 2：`ClusterSPIFFEID` 的 `ignoreNamespaces` 是不加錨點的字串/regex 比對，不是精確相等

一開始把測試 client/server 的 namespace 取名為 `spire-1315-client`（想
表達「這是 spire-1315 那組安裝底下的 client」），結果 Controller
Manager 的 reconcile 狀態一直是 `podsSelected: 0`,
`namespacesIgnored: 1`——完全沒有任何 entry 被建立，`spiffe-helper` 卡在
`no identity issued` 出不來。原因：`ControllerManagerConfig.ignoreNamespaces`
裡有一項是 `spire-1315`（`gen_spire_1152.py` 自動把安裝用的 namespace
名稱加進忽略清單，避免 controller 反過來去監控自己所在的 SPIRE
namespace）——而這個比對**不是精確字串相等**，`spire-1315-client` 因為
**開頭剛好是** `spire-1315`，被判定成要忽略的對象整個跳過。修法：把
workload 的 namespace 改名成不會跟 `ignoreNamespaces` 清單裡任何一項有
前綴重疊的名字（這裡改成 `peer-test-client`）。

**踩這個坑時走了一段冤枉路**：一開始誤判成是跟 `19-`（同樣在 `cluster2`
上跑、也裝了 Controller Manager 的另一套安裝）互相干擾，因為兩邊的
Controller Manager 都是 `watchClassless: true` + 沒設 `className`，理論上
確實會互相看到對方的 `ClusterSPIFFEID`。照這個方向設了 `className`
隔離，結果反而讓 entry-reconciler 完全停止動作（更嚴重的新問題）。退回
原本設定後，重新用乾淨的角度看 controller-manager 回報的
`status.stats`（`namespacesIgnored: 1`）才抓到真正的原因——**兩邊
Controller Manager 確實會互相看到對方的 CR，但因為各自的 `trustDomain`
不同，算出來的 `ParentID`/SPIFFE ID 天生就不會撞在一起，只是白做工，不是
真的故障**，不需要額外隔離。

## 結果

兩組 istio 1.13.5 叢集之間，SPIRE 1.15.2 簽發的憑證 mTLS 通過：
```
HTTP 200
```
兩邊既有、不相關的 pod（`local-ns/client`、`mtls-server/httpbin`）憑證
確認仍然是 istiod 自己發的 `spiffe://cluster1.local/...`、
`spiffe://cluster2.local/...`，完全獨立於 `diy-1152.local` 這個 SPIRE
trust domain，沒有互相影響。

## 離線安裝
見 [`OFFLINE_INSTALL.md`](./OFFLINE_INSTALL.md)。

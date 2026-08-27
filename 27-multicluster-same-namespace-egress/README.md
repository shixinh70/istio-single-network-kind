# Multicluster 同名 namespace/Service — egress 是否合併兩座叢集的 endpoint？

延續 `11-resource-memory-v3-largescale/` 的量測方法論，驗證一個沒被那份報告
涵蓋到的問題：`cluster1`、`cluster2` 是 Istio multi-primary(single network,
共用 root CA + remote secret 互相 watch 對方 API server)。如果兩邊各自都有
**同名的 namespace + 同名的 Service**，`client` 的 `Sidecar` 對這個
namespace 設 egress 時，istio-proxy 收到的 config 到底是「只有本地叢集的
endpoint」還是「兩座叢集的 endpoint 合併成同一份」？

## 結論(先講重點)

**會合併成同一個邏輯 Service。** Istio 用來識別服務的 key 是
`<service>.<namespace>.svc.cluster.local` 這個 hostname 字串，這個字串不
含叢集資訊。只要兩邊 namespace 名稱、Service 名稱都相同(且兩邊
`clusterDomain` 一致，這裡都是預設 `cluster.local`)，`istiod` 會把兩邊的
`Endpoints` 合併進**同一個 CDS cluster**，Envoy 端完全看不出這是兩個
物理叢集拼起來的——LB 演算法會把兩邊的 pod IP 當同一批候選項，預設沒有
「優先打本地叢集」這回事。

這對 `11-` 的記憶體模型有一個延伸推論：**如果你的 mesh 是 multicluster、
namespace/Service 又同名，某個叢集的 endpoint 數量會被「對方叢集的 replica
數」影響，即使你自己這座叢集完全沒有變動**——`11-` 報告量的
`~0.745 KB/endpoint (allocated)`、`~6.32 KB/endpoint (working_set)`，在
multicluster 情境下要用**兩座叢集 replica 數的總和**去算，不能只算本地。

## 實驗環境現況(重要,會影響你要不要照抄這份步驟)

這個 lab 的 `cluster1` 在本 session 稍早被整個 `kind delete` 重建過(拿去測
SPIRE 相關實驗)，重建時只重裝了 Istio 本身，**沒有重新建立跟 `cluster2`
的 multicluster 連結**——這份實驗的前半段其實是先把這條連結修好，才有辦法
往下測。步驟保留在這裡，因為裡面有兩個值得記錄的環境問題。

## Step 1：重建 pod 網路路由

```bash
bash 01-kind/02-connect_clusters.sh cluster1 cluster2
```

Kind 叢集重建後 pod CIDR/node IP 都變了，兩邊互相沒有到對方 pod network 的
route，這支腳本會用 `docker exec <node> ip route add` 補上。

## Step 2：k8s 1.24+ 不會自動幫 ServiceAccount 產生長效 token secret（踩到的坑）

`istioctl x create-remote-secret` 這個指令（即使是 1.13.5 版本）**讀的是
`ServiceAccount.secrets[]` 這個欄位**去找要用哪顆 token secret。k8s 1.24
拔掉了「自動幫每個 SA 產生一顆 secret 並填進這個欄位」的行為，就算你手動建
一顆帶正確 annotation(`kubernetes.io/service-account.name`)的
`kubernetes.io/service-account-token` 類型 secret，`.secrets[]` 陣列還是
空的，`create-remote-secret` 會直接報錯：

```
error: could not get access token to read resources from local kube-apiserver: no secret found in the service account: ...Secrets:[]ObjectReference{}...
```

**`--secret-name` 這個 flag 救不了**——它只是在陣列裡有多顆時用來指定要選
哪一顆，陣列本身是空的話一樣沒用。真正的修法是手動把 secret 名字寫進
`.secrets[]`(等於手動做 k8s 1.24 以前會自動做的事)：

```bash
kubectl -n istio-system create secret generic istio-reader-service-account-istio-remote-secret-token \
  --type=kubernetes.io/service-account-token \
  --dry-run=client -o yaml | \
  # 加上 annotation: kubernetes.io/service-account.name: istio-reader-service-account 後 apply

kubectl -n istio-system patch sa istio-reader-service-account \
  -p '{"secrets":[{"name":"istio-reader-service-account-istio-remote-secret-token"}]}'
```

補上這個 patch 之後，`istioctl x create-remote-secret` 才正常運作。兩邊都
要各自 patch 一次（cluster1、cluster2 的 SA 都缺這個欄位）。

## Step 3：互換 remote secret

```bash
istioctl x create-remote-secret --context=cluster1 --name=cluster1 | kubectl --context=cluster2 apply -f -
istioctl x create-remote-secret --context=cluster2 --name=cluster2 | kubectl --context=cluster1 apply -f -
```

驗證兩邊都 sync 成功：
```bash
kubectl --context=cluster1 -n istio-system exec deploy/istiod -- curl -s localhost:8080/debug/clusterz
# [{"id":"cluster2","secretName":"istio-system/istio-remote-secret-cluster2","syncStatus":"synced"}]
kubectl --context=cluster2 -n istio-system exec deploy/istiod -- curl -s localhost:8080/debug/clusterz
# [{"id":"cluster1","secretName":"istio-system/istio-remote-secret-cluster1","syncStatus":"synced"}]
```

## Step 4：建同名 namespace + Service，各自不同 replica 數（刻意讓兩邊數字不同，方便辨認有沒有合併）

- `cluster1`：`shared-ns/shared-svc`，2 replica，回應 `"hello from cluster1"`（`manifests/shared-svc-cluster1.yaml`）
- `cluster2`：`shared-ns/shared-svc`，3 replica，回應 `"hello from cluster2"`（`manifests/shared-svc-cluster2.yaml`）

## Step 5：`cluster1` 建 client，`Sidecar` 只放行 `shared-ns/*`（`manifests/client-egress-test.yaml`）

```bash
istioctl --context=cluster1 proxy-config endpoint curl-client.client-egress-test | grep shared-svc
```

實測結果：
```
10.10.1.13:8080   HEALTHY   OK   outbound|8080||shared-svc.shared-ns.svc.cluster.local
10.10.1.14:8080   HEALTHY   OK   outbound|8080||shared-svc.shared-ns.svc.cluster.local
10.20.1.71:8080   HEALTHY   OK   outbound|8080||shared-svc.shared-ns.svc.cluster.local
10.20.1.72:8080   HEALTHY   OK   outbound|8080||shared-svc.shared-ns.svc.cluster.local
10.20.1.73:8080   HEALTHY   OK   outbound|8080||shared-svc.shared-ns.svc.cluster.local
```

**同一個 cluster 名字底下，5 個 endpoint：`10.10.x`(cluster1 pod CIDR)2 個
+ `10.20.x`(cluster2 pod CIDR)3 個，跟兩邊各自的 replica 數對上。** 證實
endpoint 確實合併，不是各自獨立、也不是只取本地那份。

## 意外發現：合併只發生在 xDS 層，實際連線還被 mTLS trust domain 擋住（第二個坑）

endpoint 合併之後，實際打 `curl http://shared-svc.shared-ns.svc.cluster.local:8080/`
發現：打到 `cluster1` 的 pod（10.10.x）成功，打到 `cluster2` 的 pod（10.20.x）
全部失敗：

```
upstream connect error or disconnect/reset before headers. reset reason: connection failure,
transport failure reason: TLS error: 268435581:SSL routines:OPENSSL_internal:CERTIFICATE_VERIFY_FAILED
```

查 mesh config 才發現：

```
cluster1: trustDomain=cluster.local,  trustDomainAliases=[diy-25.local]        # 沒有 cluster2.local
cluster2: trustDomain=cluster2.local, trustDomainAliases=[diy-1152.local]      # 沒有 cluster.local
```

這兩座叢集本來(multi-primary 標準做法)就是用**不同的 trustDomain**互相
federate，靠 `trustDomainAliases` 讓對方的 SPIFFE ID 通過 SAN 驗證——但這個
session 稍早的其他實驗（SPIRE 相關的 `24-`/`26-`）陸續改寫過兩邊的 mesh
ConfigMap，把原本應該有的 `cluster2.local`/`cluster.local` 互相 alias 關係
覆蓋掉了，換成各自實驗需要的 `diy-25.local`/`diy-1152.local`。

**修法**：兩邊都補回對方的 trustDomain 當 alias（一定要用 `python3+yaml`
讀改寫回，不要用 `sed`，這是本 session 反覆驗證過的教訓）：

```bash
# cluster1 的 mesh ConfigMap 加上 cluster2.local
# cluster2 的 mesh ConfigMap 加上 cluster.local
```

補上**其中一邊**之後重測，變成「server 端」拒絕「client 端」的憑證（錯誤
訊息從 `CERTIFICATE_VERIFY_FAILED` 變成 `SSLV3_ALERT_CERTIFICATE_UNKNOWN`）
——這證實 **auto-mTLS 的 SAN 驗證是雙向的，兩邊都要各自把對方的
trustDomain 加進自己的 `trustDomainAliases`，缺一邊都會失敗，而且兩邊缺失
會表現成不同的錯誤訊息**(client 端驗證失敗 vs server 端驗證失敗)，可以
用錯誤訊息的方向反推是哪一邊的 alias 沒設好。兩邊都補齊後，10 次請求可以
同時看到 `"hello from cluster1"` 跟 `"hello from cluster2"`，證實流量真的
會被 Envoy 的 LB 分配到兩座實體叢集的 pod。

## 這對之前所有實驗的提醒

這個 lab 長期把 `cluster1`/`cluster2` 當成同一個 multi-primary mesh 用，但
中間插了大量各自獨立的單叢集實驗（SPIRE 系列），每次都會改寫
`istio-system/istio` 這個 ConfigMap 的 `trustDomainAliases`。**如果之後又
要做任何跨 `cluster1`/`cluster2` 的 mTLS 測試，第一步永遠先檢查兩邊
`trustDomainAliases` 有沒有互相列到對方**，不要假設連結還在——這次就是
血淋淋的例子：remote secret 連結斷了、trustDomainAliases 也被覆蓋掉，两个
獨立的斷點,順序抓出來才知道。

## 清理

```bash
kubectl --context=cluster1 delete ns shared-ns client-egress-test
kubectl --context=cluster2 delete ns shared-ns
```
（已在測試後執行，未留在叢集上；`istio-remote-secret-*`/SA patch 予以保留，
讓 multicluster 連結繼續維持給後續實驗用）

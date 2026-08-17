# 最簡安裝：DIY 共用 root CA + ClusterSPIFFEID（取代 17- 的 Federation 版本）

## 這是什麼
延續 `17-spire-cross-cluster-mtls/`、`18-diy-shared-root-nested-poc/` 的
結論，把「目前為止測過最簡的組合」實際整套搭起來、真的跑通：

- **PKI**：`18-` 驗證過的 DIY 共用 root（各叢集 SPIRE Server 各自 `UpstreamAuthority
  "disk"` 用同一把 root 簽出的不同 intermediate，`trust_domain` 全部統一）——
  完全不需要 `bundle set`/`ClusterFederatedTrustDomain`/`federatesWith`
- **Entry 管理**：`17-` Phase 4 驗證過的 SPIRE Controller Manager +
  `ClusterSPIFFEID` 宣告式管理，不用手動 CLI
- **Istio 整合**：`17-` Phase 3 驗證過的原生 Istio+SPIRE SDS
  （`spiffe-csi-driver` + injection template），不用自訂 Gateway

這次是**直接取代**現場跑的 `17-` 安裝（原本的 `spire`/`agent`/`mcp-gw`
namespace 已整個砍掉重建成這一套），不是並存。`17-`、`18-` 的 git 紀錄與
文件保留，作為過程記錄與比較基準。

## 拓樸（跟之前一致）
`cluster1-134`、`cluster2` 當 client（`agent` pod），`cluster2-134` 當
server（`mcp-echo-spire`，用 `mcp-echo` 這個 hashicorp/http-echo 測試
backend）。三邊全部使用同一個 trust domain：`diy-shared.local`。

## 完整安裝步驟

### Step 1：離線生成 root + 三份 intermediate（每個叢集各一份）

```bash
mkdir -p diy-pki && cd diy-pki
openssl ecparam -name prime256v1 -genkey -noout -out root.key
openssl req -x509 -new -key root.key -sha256 -days 3650 \
  -subj "/O=spire-lab/CN=diy-shared-root-v2" -out root.crt
# 注意：openssl req -x509 自己就會加 Basic Constraints CA:TRUE，
# 不要再手動 -addext 一次，會變成重複 extension 導致憑證整個壞掉
# （18- 踩過這個坑，細節見 18-diy-shared-root-nested-poc/README.md）

for c in cluster1-134 cluster2-134 cluster2; do
  openssl ecparam -name prime256v1 -genkey -noout -out int-$c.key
  openssl req -new -key int-$c.key -subj "/O=spire-lab/CN=diy-intermediate-$c" -out int-$c.csr
  openssl x509 -req -in int-$c.csr -CA root.crt -CAkey root.key -CAcreateserial -days 1825 -sha256 \
    -extfile <(printf "basicConstraints=critical,CA:true\nkeyUsage=critical,keyCertSign,cRLSign") \
    -out int-$c.crt
  openssl verify -CAfile root.crt int-$c.crt
done
```

### Step 2：把各自的 intermediate 存成 Secret

```bash
for ctx_c in "cluster1-134:cluster1-134" "cluster2-134:cluster2-134" "cluster2:cluster2"; do
  ctx="${ctx_c%%:*}"; c="${ctx_c##*:}"
  kubectl --context=$ctx create namespace spire
  kubectl --context=$ctx -n spire create secret generic diy-intermediate \
    --from-file=intermediate.crt=diy-pki/int-$c.crt \
    --from-file=intermediate.key=diy-pki/int-$c.key \
    --from-file=root.crt=diy-pki/root.crt
done
```

### Step 3：CRD + SPIRE Server/Agent + Controller Manager（一次到位）

```bash
for ctx in cluster1-134 cluster2-134 cluster2; do
  kubectl --context=$ctx apply -f manifests/crds/clusterspiffeids.yaml
  kubectl --context=$ctx apply -f manifests/crds/clusterfederatedtrustdomains.yaml
done

python3 gen_spire_diy_cm.py spire cluster1-134 manifests/spire-diy-cluster1-134.yaml
python3 gen_spire_diy_cm.py spire cluster2-134 manifests/spire-diy-cluster2-134.yaml
python3 gen_spire_diy_cm.py spire cluster2      manifests/spire-diy-cluster2.yaml

kubectl --context=cluster1-134 apply -f manifests/spire-diy-cluster1-134.yaml
kubectl --context=cluster2-134 apply -f manifests/spire-diy-cluster2-134.yaml
kubectl --context=cluster2 apply -f manifests/spire-diy-cluster2.yaml
```

驗證三邊 bundle 內容完全一致（都等於 root，證明不需要任何 federation）：
```bash
for ctx in cluster1-134 cluster2-134 cluster2; do
  kubectl --context=$ctx -n spire exec spire-server-0 -c spire-server -- \
    /opt/spire/bin/spire-server bundle show -format spiffe | sha256sum
done
```

### Step 4：CSI driver + Istio 原生 SDS 整合（只需要 server 端 `cluster2-134`）

```bash
kubectl --context=cluster2-134 apply -f manifests/spiffe-csi-driver.yaml
python3 ../17-spire-cross-cluster-mtls/patch_sidecar_injector_spire_template.py cluster2-134
python3 ../17-spire-cross-cluster-mtls/patch_mesh_trust_domain_aliases.py cluster2-134 diy-shared.local
```

`trustDomainAliases` 這裡只需要填**一個** `diy-shared.local`——這是 DIY
共用 root 相對 Federation 版本的另一個簡化：Federation 版本要列出每個
獨立叢集各自的 trust domain 名稱（N 個），這裡全部叢集共用同一個名字，
一行就夠。

### Step 5：`ClusterSPIFFEID`（不用 `federatesWith`）

```bash
for ctx in cluster1-134 cluster2-134 cluster2; do
  kubectl --context=$ctx apply -f manifests/clusterspiffeids.yaml
done
```

### Step 6：workload

```bash
kubectl --context=cluster1-134 apply -f manifests/agent-pod.yaml
kubectl --context=cluster2 apply -f manifests/agent-pod.yaml
kubectl --context=cluster2-134 apply -f manifests/mcp-echo-spire.yaml
```

### Step 7：驗證

```bash
kubectl --context=cluster2-134 -n mcp-gw get svc mcp-echo-spire   # 找 NodePort
docker inspect cluster2-134-worker --format '{{.NetworkSettings.Networks.kind.IPAddress}}'

kubectl --context=cluster1-134 -n agent exec agent -c app -- \
  curl -sS -k --http1.1 --resolve mcp-echo-spire.mcp-gw.svc.cluster.local:30444:<IP> \
  --cert /svids/tls.crt --key /svids/tls.key --cacert /svids/ca.crt \
  https://mcp-echo-spire.mcp-gw.svc.cluster.local:30444/
```
兩組配對都應該回 `hello from mcp-echo-spire via native Istio+SPIRE sidecar SDS`。

## 安裝過程踩的坑（這次新踩的，`17-`/`18-` 的坑不重複列）

### 坑 1：`ClusterFederatedTrustDomain` 是舊叢集殘留，`ClusterSPIFFEID` 也是——這兩個 CRD 都是 cluster-scoped，砍 namespace 砍不掉它們

砍掉 `17-` 舊安裝時，只 `kubectl delete namespace spire agent mcp-gw`，
但 `ClusterSPIFFEID`/`ClusterFederatedTrustDomain` 是**cluster-scoped**
資源，完全不受 namespace 刪除影響，三天前 `17-` 建立的物件原封不動留著。
新的 `kubectl apply` 只是「更新」了同名的舊物件，內部殘留的舊 reconcile
狀態（尤其是累積的歷史 pod-uid selector）完全沒清掉——重建整套時，砍
namespace **不夠**，`ClusterSPIFFEID`/`ClusterFederatedTrustDomain` 也要
`kubectl delete --all` 一次，才是真正的乾淨重來。

### 坑 2：`workloadSelectorTemplates` 不是篩選條件，`namespaceSelector`/`podSelector` 才是

第一次寫 `ClusterSPIFFEID` 時只填了
`workloadSelectorTemplates: [k8s:ns:agent, k8s:sa:agent]`，以為這樣就會
限定「只套用在 `agent` namespace 裡 SA 是 `agent` 的 pod」——**這個理解
是錯的**。`workloadSelectorTemplates` 只是「附加在產生出來的 entry 上的
額外 selector 字串」，不是過濾條件；真正決定「這條規則要套用在哪些 pod
上」的是 `namespaceSelector`/`podSelector`（標準 K8s label selector）。
沒設這兩個欄位時，Controller Manager 會**掃描整個叢集所有 namespace的
所有 pod**，幫每一個都掛上這些字面上寫死、但實際上文不對題的 selector
字串（例如把 `istiod`、`metallb-system` 的 pod 也建出
`k8s:ns:agent, k8s:sa:agent` 的 entry，即使它們根本不在 `agent`
namespace 也不是 `agent` SA）。這造成大量幽靈 entry，其中一部分
pod-uid 剛好跟真正的 workload 衝突撞在一起，導致真實 pod 的身份簽發
被拖累拒絕。修法：務必加上
```yaml
namespaceSelector:
  matchLabels:
    kubernetes.io/metadata.name: <namespace>
```
（K8s 1.21+ 每個 namespace 都內建這個 label，不用自己額外打標籤）。

### 坑 3（真正花最多時間排查的）：`k8s_psat` NodeAttestor 的 `cluster` 識別字串，要跟 `ControllerManagerConfig.clusterName` 用同一個值，不能各自取名

這是這次最隱蔽、最花時間的坑。設定裡有**兩個看起來很像、但完全獨立**
的「cluster」概念：

1. `NodeAttestor "k8s_psat" { plugin_data { cluster = "..." } }`
   （agent 端）／`clusters = { "..." = {...} }`（server 端）——這是
   k8s_psat 這個 node attestation 機制自己用來對 Kubernetes API 驗證
   PSAT token 的識別字串，**只是一個任意字串，不需要是真正的叢集名稱**
2. `ControllerManagerConfig.clusterName`——Controller Manager 拿來組
   `parentIDTemplate`（預設
   `spiffe://{{ .TrustDomain }}/spire/agent/k8s_psat/{{ .ClusterName }}/{{ .NodeMeta.UID }}`）
   跟 `spiffeIDTemplate` 裡 `{{ .ClusterName }}` 的那個值

第一版的 generator 裡，這兩個值我**各自填了不同東西**（第 1 個填成
namespace 名稱 `spire`，第 2 個填成真正的叢集名稱如 `cluster2-134`）。
後果：agent 實際 node attestation 拿到的身份是
`spiffe://.../k8s_psat/spire/<uid>`，但 Controller Manager 幫每條 entry
算出來的 `ParentID` 卻是 `spiffe://.../k8s_psat/cluster2-134/<uid>`——
**兩者永遠對不上**，Controller Manager 建出來的 entry 因此從來沒有真正
掛到任何一個真實存在的 agent 底下。

症狀非常誤導：`spire-server entry show` 看起來完全正常（entry 存在、
selector 也正確），但 workload 端永遠卡在
`workload is not authorized for the requested identities ["default"]`
（native SDS 路徑）或 `no identity issued`（spiffe-helper 路徑）。開
DEBUG log 才看到關鍵字 `registered=false`——agent 自己的角度看，
這個 workload「完全沒有註冊」，因為根本沒有任何一條 entry 的 `ParentID`
真的指向它。

排查過程中一併證偽了幾個一開始懷疑錯的方向，記錄一下避免以後重踩：
- ❌ 以為是 CSI driver/bind mount 用了 hostPath 導致的殘留—— 重啟 CSI
  driver、重啟整個 spire-server pod 都沒用，證明不是快取問題
- ❌ 以為是同節點兩個 `spire-agent`（`18-` 的 PoC + 這次新的）互相干擾
  cgroup attestation —— 清掉 `18-` 的 namespace 後問題依舊
- ❌ 以為是 selector 集合不夠完整（entry 只有 subset，workload 端 attest
  出 20+ 個 selector）——SPIRE 的 subset match 語意本身沒問題，加回
  `k8s:ns:...` 這個 selector 也沒解決
- ✅ 開 DEBUG log 直接看 `registered=false` + 比對 agent 實際 attest 出的
  SPIFFE ID 路徑段（`k8s_psat/spire/...`）跟 entry 的 `ParentID`
  （`k8s_psat/cluster2-134/...`），才抓到兩者對不上

修法：`gen_spire_diy_cm.py` 裡把 `NodeAttestor "k8s_psat"` 的 `cluster`/
`clusters` 值也改成 `{cluster_name}`（跟 `ControllerManagerConfig.clusterName`
用同一個變數），兩者永遠一致。

## 跟 `17-`（Federation）版本的最終對照

| | 17-（Federation） | 19-（這個，DIY 共用 root） |
|---|---|---|
| 需要 bundle 交換 | 要（O(N²) 關係，或用 `gen_full_mesh_federation.py` 腳本化） | 完全不用 |
| `trustDomainAliases` | 要列出每個獨立 trust domain（N 個） | 只要一個（全部共用同名） |
| root CA 管理 | 各叢集各自獨立、互不相干 | 集中一把，離線簽發、不需要活的 upstream server |
| 新增一座叢集的成本 | 生一組新 trust domain + 跟現有每一個都建 federation 關係 | 簽一份新 intermediate、trust_domain 沿用同一個字串 |
| SPIFFE ID 天生帶什麼資訊 | trust domain 本身就是叢集名稱 | 要自己把叢集名稱寫進路徑（`spiffeIDTemplate` 的 `/cluster/{{.ClusterName}}/`） |
| 適合情境 | 各叢集**行政上真的獨立**（不同團隊/組織各自管） | 同一組織的基礎設施，追求安裝/維運最簡 |

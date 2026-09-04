# 不同 Root CA 的跨叢集 mTLS、Linkerd/Istio 混用嘗試、雙 Control Plane 隔離世界

延續 `26-`/`27-` 的手法，但這次完全**不接 SPIRE**，只用**原生 Istio CA 系統**，
測試一系列圍繞「不同信任根/不同控制平面如何共存」的問題。`cluster1`
（Istio 1.13.5）、`cluster1-134`（Istio 1.29.6）都是**完全重建**過的乾淨環境。

---

## 1. 兩座叢集各自獨立 root CA，靠 bundle 解決跨叢集 mTLS

### 現況確認

叢集重建後，`cluster1`、`cluster1-134` 天生就用不同的 root CA（各自
`istioctl install` 時分別建立獨立的 `cacerts`，見
`pki-scripts/gen-independent-roots.sh`）：

```bash
kubectl -n istio-system get secret cacerts -o jsonpath='{.data.root-cert\.pem}' | base64 -d | openssl x509 -noout -fingerprint -sha256
```

### Step 1：baseline——沒有 bundle，確認打不通

```
upstream connect error ... TLS error: ...CERTIFICATE_VERIFY_FAILED
HTTP_CODE: 503
```

不同 root 之間物理上就是無法互信，這是預期中的失敗，不是設定問題。

### Step 2：套 bundle，打通

把兩邊 root cert 串接（`cat cluster1/root-cert.pem cluster1-134/root-cert.pem
> bundle-root.pem`），**只換 `cacerts` 的 `root-cert.pem` 欄位，簽發用的
`ca-cert.pem`/`ca-key.pem` 完全不動**，兩邊都套用、重啟 istiod：

```
hello from cross-server (cluster1-134, post multi-root)
HTTP_CODE: 200
```

### Step 3：`AuthorizationPolicy` 卡 SPIFFE ID，A/B/A 完整驗證

```
A(正確 principal)  → 200
B(錯誤 principal)  → 403, RBAC: access denied
A'(改回正確)       → 200
```

跨叢集 mTLS 場景下，principal-based ALLOW/DENY 運作跟同叢集情境一樣可靠。

### 官方 `ISTIO_MULTIROOT_MESH` / `meshConfig.caCertificates` 機制**沒有用**

Istio 官方確實有一個看起來就是為了這個情境設計的機制：

```yaml
spec:
  values:
    pilot:
      env:
        ISTIO_MULTIROOT_MESH: "true"
  meshConfig:
    caCertificates:
    - pem: |
        -----BEGIN CERTIFICATE-----...
```

`ISTIO_MULTIROOT_MESH` 是真實存在的 pilot env var（`pilot/pkg/features/pilot.go`
裡的 `MultiRootMesh`），`caCertificates` 也是真實的 proto 欄位
（`mesh/v1alpha1/config.proto` field 58）。istiod 內部的
`initWorkloadTrustBundle`（`pilot/pkg/bootstrap/server.go`）也確實會被觸發，
log 也顯示 `trustBundle` 正確初始化了 2 個 trust source。**但實測傳到
workload proxy 的 SDS `ROOTCA` 資源就是只有 1 張證書，沒有真的合併進去**——
在 Istio 1.13.5 上，這條路內部狀態管理正確，但沒有正確傳播到 proxy 端，
原因未深究（可能是版本落差，見第 5 節同類型發現）。

**最後採用、唯一驗證可靠的做法：直接改 `cacerts` 的 `root-cert.pem` 塞
bundle。** 詳見 `pki-scripts/gen-independent-roots.sh`。

**改 `cacerts` 對既有 workload 的影響**（另外驗證過的重點）：
- 同叢集內既有 mTLS 流量：不用重啟，完全不受影響（因為簽發用的 intermediate
  沒變，舊憑證還是有效的）
- **但沒重啟的 pod，它的 ROOTCA 信任內容不會自動更新**——即使 istiod 重啟、
  XDS 重連很多次也一樣，只有這個 pod **自己重啟**、建立全新 SDS 連線，才會
  拿到新的信任內容。這對「要不要重啟現有 workload」的規劃很重要：加新
  root 是安全的（舊 pod 繼續用舊信任正常運作），但要拿掉舊 root 之前，
  必須確保所有 pod 都至少重啟過一次。

---

## 2. Linkerd 塞進 `istio-ingressgateway`：官方直接拒絕

嘗試把 `linkerd.io/inject: enabled` annotation 加到 `istio-ingressgateway`
的 Deployment，讓 igw 同時有 `istio-proxy` + `linkerd-proxy`：

```
level=info msg="skipped pod/istio-ingressgateway-...: pod has a sidecar injected already"
```

查了 Linkerd 原始碼（`pkg/healthcheck/sidecar.go`），這是**刻意寫死**的
保護機制：

```go
func HasExistingSidecars(podSpec *corev1.PodSpec) bool {
    ...
    if container.Name == "istio-proxy" ||
       strings.HasPrefix(container.Image, "gcr.io/istio-release/proxyv2:") {
        return true
    }
    ...
}
```

沒有 override annotation。Linkerd 團隊很清楚兩個 mesh 的 sidecar 共存會有
問題（很可能就是類似 `21-`/`22-` 那種 iptables/probe-rewrite 衝突），直接
在程式碼層級擋死，不像某些場景放任衝突發生。**結論：無法讓
`istio-ingressgateway` 同時是兩個 mesh 的成員。**

---

## 3. Linkerd 跨叢集 mTLS：純 NodePort 打不通，Linkerd 沒有 ServiceEntry 概念

改成 client(cluster1,純 Linkerd)直接打 server(cluster1-134,純 Linkerd,
不碰 Istio)，走 NodePort + hostAlias（模仿 Istio 那次的做法）：

連線本身成功(200)，但查 Linkerd 自己的 proxy metrics：

```
tcp_open_total{...tls="no_identity",no_tls_reason="not_provided_by_service_discovery"}
```

**完全是明文 TCP passthrough，沒有 mTLS。** Linkerd 的 mTLS 決策完全依賴
自己的 service discovery（`linkerd-destination` 元件認不認識這個目的地），
**沒有 Istio `ServiceEntry` 那種「手動登記外部目的地」的機制**。要做到真
的跨叢集 mTLS，必須用官方 `linkerd-multicluster` extension（gateway +
service-mirror controller，把對方的 Service 鏡射成本地認得的物件），沒有
更輕量的手動繞過方式。這次沒有繼續往這個方向裝下去。

---

## 4. 隔離世界設計：同一個 mesh 裡，一批新 pod 用完全獨立的 CA

**需求**：一批新 pod 要活在自己的 mTLS 世界裡（只有彼此才能建立
mTLS）、強制 STRICT、**完全不影響既有用 Istio 的 pod**、**要跨兩座
叢集**。

**方案：Istio 原生的多 revision 機制**（不用 SPIRE）——每個
revision = 完全獨立的一套 istiod = 完全獨立的 CA/trust domain。

### 關鍵限制：`cacerts` 這個 Secret 名稱是寫死的

```go
// security/pkg/pki/ca/ca.go
CACertsSecret = "cacerts"
```

**兩個 revision 不能裝在同一個 namespace**（都會搶同一個 `cacerts`
名字），isolated revision 必須裝在自己獨立的 namespace（例如
`istio-isolated`），不能沿用 `istio-system`。

### 最終架構：4 套 istiod

```
cluster1:      istio-system(default,既有)       + istio-isolated(新增)
cluster1-134:  istio-system(default,既有)       + istio-isolated(新增)
```

isolated 世界的兩邊用**同一顆共用 root CA、各自簽 intermediate**
（`pki-scripts/gen-isolated-shared-root.sh`），跨叢集互信從一開始就成立，
不用像第 1 節那樣事後 bundle。

安裝範例見 `operator-configs/cluster1-isolated.yaml`、
`cluster1-134-isolated.yaml`。新 pod 的 namespace 用
`istio.io/rev: isolated` label（取代 `istio-injection: enabled`），該
namespace 開 `PeerAuthentication: STRICT`。

### 完整驗證結果

| 測試 | 結果 |
|---|---|
| isolated 內部（同叢集）mTLS | ✅ 200 |
| default 跟 isolated 完全隔離（同叢集） | ✅ `CERTIFICATE_VERIFY_FAILED`,503 |
| default 內部彼此正常，完全沒被 isolated 影響 | ✅ 200 |
| isolated 跨叢集 mTLS | ✅ 200（第一次就通，因為共用 root） |
| isolated 跨叢集打 default，同樣隔離 | ✅ 503 |

**隔離是 TLS handshake 層級天生成立，不需要額外寫 `AuthorizationPolicy`**
——default 跟 isolated 的憑證鏈根本對不上，握手就失敗。

---

## 5. Webhook 怎麼對應到正確的 revision

每個 revision 有自己獨立的 webhook 物件：

```
istio-sidecar-injector                            → default revision
istio-sidecar-injector-isolated-istio-isolated    → isolated revision
istio-validator-istio-system                       → default revision
istio-validator-isolated-istio-isolated             → isolated revision
```

`isolated` revision 的 selector 很直覺（`istio.io/rev In [isolated]`）。
`default` revision 比較繞——它自己的 `istio-sidecar-injector` 裡，通用
規則被刻意設成 `matchLabels: {istio.io/deactivated: never-match}`（等於
停用），真正負責「沒有明確指定 revision 的 namespace」的是另一個獨立物件
**`istio-revision-tag-default`**（Istio 的 **"revision tag"** 機制，讓
「誰是目前的 default」可以動態改指，不用動個別 revision 自己的 webhook）。

### VS/DR 這類 CRD 完全是另一套邏輯

Pod 注入受 K8s admission webhook 的 `namespaceSelector`/`objectSelector`
強制把關；**VirtualService/DestinationRule 不是**——每套 istiod 都有自己
的 K8s informer，直接 watch 整個叢集所有這類 CRD，**預設沒有 revision
過濾**：

```bash
# 完全沒有 istio.io/rev label 的 DR
# → default 跟 isolated 的 config store 都讀到了
```

**可以**手動在 DR/VS 上加 `labels: {istio.io/rev: isolated}` 來限定只給
特定 revision 讀取（實測有效），但這是**選擇性、要人手動記得加**，不是
K8s 強制保證的——沒加的話，兩邊會不會真的互相影響，純粹看 `host`
欄位字串有沒有剛好撞名。

---

## 6. `discoverySelectors`——官方修復方案，但有版本落差

Istio 官方文件《[Install Multiple Istio Control Planes in a Single
Cluster](https://istio.io/latest/docs/setup/install/multiple-controlplanes/)》
明確用 `meshConfig.discoverySelectors` 解決上面兩個問題：

```yaml
spec:
  revision: usergroup-1
  meshConfig:
    discoverySelectors:
      - matchLabels:
          usergroup: usergroup-1
```

> discoverySelectors can be used to configure which namespaces should
> include the `istio-ca-root-cert` config map for a particular Istio
> control plane.

### 套用前：`istio-ca-root-cert` 持續互搶（両套 istiod 的「全部同步」邏輯撞在一起)

每套 istiod 都有一個背景 controller
（`pilot/pkg/serviceregistry/kube/controller/namespacecontroller.go`），
只要 CA bundle 有變化就把 `istio-ca-root-cert` 這個 ConfigMap 同步到
**叢集裡幾乎所有 namespace**（`labels.Everything()`，完全沒有 revision
過濾）。兩套 istiod 同時存在，範圍必然重疊，持續互相覆蓋：

```
第1次: cluster1-fresh-pki
第2次: cluster1-fresh-pki
第3次: cluster1-fresh-pki
第4次: cluster1-fresh-pki
第5次: isolated-mesh        ← 開始閃爍
第6次: cluster1-fresh-pki
第7次: isolated-mesh
```

### 這個 race 不是無害的——會導致 istio-proxy 永久卡死

實測連續建 6 個新 pod，剛好卡到 ConfigMap 內容錯誤瞬間的兩個，**直接卡死，
2 分鐘內完全沒有自癒**：

```
warn sds  failed to warm certificate: ... x509: certificate signed by unknown authority
```

**原因**：istio-agent 是在容器啟動時**讀一次**掛載的 ConfigMap 檔案（這是
SDS 連線建立前的 bootstrap 信任來源，解決「要連 istiod 才能拿憑證,但要
先信任 istiod 才能連」這個雞生蛋問題），之後就算 ConfigMap 內容之後閃回
正確的那份，**已經在跑的 container 不會重新讀取，只能重啟這個 pod**才有
機會恢復（賭下次啟動時 ConfigMap 剛好是對的）。真實環境的滾動部署/頻繁
scale 場景下，這是實質的、隨機發生的故障率，不能忽略。

### 套用 `discoverySelectors` 之後：版本落差

| | cluster1（1.13.5） | cluster1-134（1.29.6） |
|---|---|---|
| proto 欄位存在(`discovery_selectors`, field 59) | ✅ | ✅ |
| `istio-ca-root-cert` race 解決 | ❌ 仍然閃爍 | ✅ 連續 5 次查詢完全穩定 |
| 沒帶 `istio.io/rev` label 的 DR 隔離 | ❌ **實測確認也沒生效** | ✅ isolated istiod 完全讀不到 |
| 既有功能(本地/跨叢集 mTLS、隔離) | ✅ 全部維持正常 | ✅ 全部維持正常，無回歸 |

**DR 隔離這項有重新嚴謹驗證過，不是憑印象推測**：一開始只確認了
`istio-ca-root-cert` 沒生效，就直接假設「CRD 隔離大概也不支援」，事後
被問到「你確定嗎」才回頭補測——用套用 `discoverySelectors`、重啟 istiod
**之後**才新建的全新 DR（排除舊物件快取殘留的可能）重測，`isolated`
istiod 仍然讀到了 `default-workload-test`(沒有 `mesh-group: isolated`
label)的 DR；`istiod-isolated` 的 log 裡完全沒有任何跟
`discoverySelectors`/namespace filter 相關的訊息（不是報錯，是**安靜到
像完全沒被處理過**）。所以 1.13.5 上不是「ConfigMap 同步這塊沒接上、CRD
隔離那塊有接上」的部分生效，是**整個 `discoverySelectors` 機制在
1.13.5 上完全被忽略**，跟第 1 節 `ISTIO_MULTIROOT_MESH`/`caCertificates`
同一種模式——proto 欄位在 1.13.5 就存在，但背後真正的功能邏輯是在
1.13.5 之後、1.29.6 之前的某個版本才補上的。1.29.6 上完全照官方文件
描述的行為運作，沒有任何落差。

**實務結論**：如果要在 Istio 1.13.5 上做這種雙 control plane 隔離架構，
`discoverySelectors` 修不好 `istio-ca-root-cert` race 這件事本身就是一個
必須認真看待的已知限制——**不是「設定寫得不夠好」，是這個版本的
namespace controller 沒有 revision 感知能力**，可能要考慮：升級到有正確
支援的版本、或改用完全分開的叢集來做真正的隔離。

---

## 檔案結構

```
pki-scripts/
  gen-independent-roots.sh    # 第 1 節:兩座叢集各自獨立 root + bundle
  gen-isolated-shared-root.sh # 第 4 節:isolated 世界共用 root、各自 intermediate
operator-configs/
  cluster1-isolated.yaml         # isolated revision,cluster1(1.13.5),沒有 discoverySelectors
  cluster1-134-isolated.yaml     # isolated revision,cluster1-134(1.29.6),沒有 discoverySelectors
  cluster1-isolated-ds.yaml      # 同上 + discoverySelectors
  cluster1-134-isolated-ds.yaml  # 同上 + discoverySelectors
  cluster1-default-ds.yaml       # default revision(cluster1)補上 discoverySelectors
  cluster134-default-ds.yaml     # default revision(cluster1-134)補上 discoverySelectors
manifests/
  native-mtls-server.yaml / native-mtls-client.yaml   # 第 1 節測試用
  isolated-workload.yaml / isolated-server-134.yaml / isolated-cross-client-route.yaml  # 第 4 節
  linkerd-server-example.yaml / linkerd-client-example.yaml  # 第 3 節(明文 passthrough 的失敗範例)
```

私鑰(`*.key`)不納入版本控制，跑 `pki-scripts/` 底下的腳本會重新產生。

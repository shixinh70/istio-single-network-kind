# DIY：一把 root CA 簽兩份 intermediate，兩座 SPIRE Server 互信不用 bundle federation

## 目的
驗證一個假設：**如果兩座 SPIRE Server 的 `trust_domain` 設成同一個名字，各自
再用同一把 root CA 簽出來的、各自不同的 intermediate 憑證（`UpstreamAuthority
"disk"` 的「join existing PKI」模式），是不是就完全不需要
`bundle set` / `ClusterFederatedTrustDomain` / `federatesWith` 這一整套
federation 機制？**

背景：在 `17-spire-cross-cluster-mtls/` 裡已經驗證過，**光是共用同一把
key 材料，但 trust domain 名稱各自獨立**——還是必須 `bundle set` 才會被
SPIRE 承認（`unable to find federated bundle`）。這次測的是不同的組合：
**trust domain 名稱也統一**，看看是不是真的能省掉 federation 這一步。

這不是官方 SPIRE 文件裡點名的「Nested SPIRE」——真正的 Nested 需要一個
活著的 upstream SPIRE Server（downstream 用 `UpstreamAuthority "spire"`
plugin，即時打 API 去要 intermediate），這裡測的是**完全離線**的手動版
本：root 的私鑰全程不用連上任何一座 SPIRE Server，簽完 intermediate 就
可以收起來。

## 為什麼另開一個獨立目錄、獨立 namespace，不動 `17-` 那套
`17-spire-cross-cluster-mtls/` 裡的 Federation 架構已經完整驗證過、寫進
`INSTALL_CONTROLLER_MANAGER_VERSION.md`、也 push 上 GitHub 了。這次測試
會改到 `trust_domain`（現有架構的核心設定），一旦動下去會直接破壞那套
已驗證的狀態。所以這次刻意用**全新、獨立的 namespace**
（`spire-nested-a` 在 `cluster1-134`、`spire-nested-b` 在
`cluster2-134`），連 hostPath socket 路徑都跟現有的 `/run/spire/sockets`
分開（用 `/run/spire-nested-a/sockets`、`/run/spire-nested-b/sockets`），
確保完全不會互相干擾，測完可以整個 namespace 砍掉，不影響 `17-` 那套。

## 完整安裝流程

### Step 1：離線生成 root + 兩份不同的 intermediate

```bash
mkdir -p diy-pki && cd diy-pki

# Root CA
openssl ecparam -name prime256v1 -genkey -noout -out root.key
openssl req -x509 -new -key root.key -sha256 -days 3650 \
  -subj "/O=spire-lab/CN=diy-nested-root" \
  -out root.crt

# Intermediate A（給 nested-a 用）
openssl ecparam -name prime256v1 -genkey -noout -out intA.key
openssl req -new -key intA.key -subj "/O=spire-lab/CN=diy-intermediate-a" -out intA.csr
openssl x509 -req -in intA.csr -CA root.crt -CAkey root.key -CAcreateserial -days 1825 -sha256 \
  -extfile <(printf "basicConstraints=critical,CA:true\nkeyUsage=critical,keyCertSign,cRLSign") \
  -out intA.crt

# Intermediate B（給 nested-b 用）
openssl ecparam -name prime256v1 -genkey -noout -out intB.key
openssl req -new -key intB.key -subj "/O=spire-lab/CN=diy-intermediate-b" -out intB.csr
openssl x509 -req -in intB.csr -CA root.crt -CAkey root.key -CAcreateserial -days 1825 -sha256 \
  -extfile <(printf "basicConstraints=critical,CA:true\nkeyUsage=critical,keyCertSign,cRLSign") \
  -out intB.crt

# 確認 intA / intB 都能追溯回 root，且彼此內容真的不同
openssl verify -CAfile root.crt intA.crt
openssl verify -CAfile root.crt intB.crt
```

⚠️ **踩坑**：第一次生成 root 時，我在 `openssl req -x509` 後面又手動加了
`-addext "basicConstraints=critical,CA:true"`。結果導致驗證整個失敗，連
**root 驗證自己**都報 `error 20: unable to get local issuer certificate`。
原因：`openssl req -x509` 這個模式**本來就會自動幫自簽憑證加上**
`Basic Constraints: CA:TRUE`，我又手動加一次，變成同一張憑證裡有
**兩個重複的 Basic Constraints extension**，成了一張畸形憑證（用
`openssl x509 -noout -text | grep -c "Basic Constraints"` 可以看到印出
`2`，正常應該是 `1`）。修法：生成 root 時不要加那個 `-addext`，讓
openssl 自己內建的預設值處理就好；`intermediate` 因為是用
`openssl x509 -req`（不是 `req -x509`）簽的，這個指令**不會**自動加
extension，所以 intermediate 那邊維持手動用 `-extfile` 加是必要且正確
的，沒有這個問題。

### Step 2：把 root/intermediate 存成兩邊各自的 K8s Secret

```bash
kubectl --context=cluster1-134 create namespace spire-nested-a
kubectl --context=cluster2-134 create namespace spire-nested-b

kubectl --context=cluster1-134 -n spire-nested-a create secret generic diy-intermediate \
  --from-file=intermediate.crt=diy-pki/intA.crt \
  --from-file=intermediate.key=diy-pki/intA.key \
  --from-file=root.crt=diy-pki/root.crt

kubectl --context=cluster2-134 -n spire-nested-b create secret generic diy-intermediate \
  --from-file=intermediate.crt=diy-pki/intB.crt \
  --from-file=intermediate.key=diy-pki/intB.key \
  --from-file=root.crt=diy-pki/root.crt
```

### Step 3：部署兩套 SPIRE Server+Agent，trust_domain 統一，用 `UpstreamAuthority "disk"` 的 join-PKI 模式

```bash
python3 gen_spire_diy.py spire-nested-a nested-a manifests/spire-nested-a.yaml
python3 gen_spire_diy.py spire-nested-b nested-b manifests/spire-nested-b.yaml
kubectl --context=cluster1-134 apply -f manifests/spire-nested-a.yaml
kubectl --context=cluster2-134 apply -f manifests/spire-nested-b.yaml
```

兩邊的 `server.conf` 核心設定只有兩處跟 `17-` 那套不同：
```hcl
server {
  trust_domain = "diy-shared.local"   # 兩邊完全一樣的名字
  ...
}
plugins {
  ...
  UpstreamAuthority "disk" {
    plugin_data {
      cert_file_path   = "/run/spire/diy-ca/intermediate.crt"  # 各自不同
      key_file_path    = "/run/spire/diy-ca/intermediate.key"  # 各自不同
      bundle_file_path = "/run/spire/diy-ca/root.crt"          # 兩邊一樣
    }
  }
}
```

驗證兩邊 server 都正確載入了各自的 intermediate（`self_signed=false`，
`local_authority_id` 兩邊應該不同）：
```bash
kubectl --context=cluster1-134 -n spire-nested-a logs spire-server-0 | grep "X509 CA prepared"
kubectl --context=cluster2-134 -n spire-nested-b logs spire-server-0 | grep "X509 CA prepared"
```

### Step 4：部署測試 client（spiffe-helper 抓 SVID），註冊身份

```bash
kubectl --context=cluster1-134 apply -f manifests/test-client-a.yaml
kubectl --context=cluster2-134 apply -f manifests/test-client-b.yaml

kubectl --context=cluster1-134 -n spire-nested-a exec spire-server-0 -c spire-server -- \
  /opt/spire/bin/spire-server entry create \
  -parentID spiffe://diy-shared.local/spire/agent/k8s_psat/spire-nested-a/<agent-id> \
  -spiffeID spiffe://diy-shared.local/nested-a-client \
  -selector k8s:ns:spire-nested-a -selector k8s:sa:test-client

kubectl --context=cluster2-134 -n spire-nested-b exec spire-server-0 -c spire-server -- \
  /opt/spire/bin/spire-server entry create \
  -parentID spiffe://diy-shared.local/spire/agent/k8s_psat/spire-nested-b/<agent-id> \
  -spiffeID spiffe://diy-shared.local/nested-b-client \
  -selector k8s:ns:spire-nested-b -selector k8s:sa:test-client
```
（`<agent-id>` 用 `spire-server agent list` 查）

### Step 5：驗證結果——全程沒跑過任何一次 bundle 交換

**第一個關鍵發現**：`bundle show` 在兩邊回傳的內容**完全一樣**，而且不是
各自的 intermediate，是**共用的 root**：

```bash
kubectl --context=cluster1-134 -n spire-nested-a exec spire-server-0 -c spire-server -- \
  /opt/spire/bin/spire-server bundle show -format spiffe
kubectl --context=cluster2-134 -n spire-nested-b exec spire-server-0 -c spire-server -- \
  /opt/spire/bin/spire-server bundle show -format spiffe
# 兩邊的 x5c[0] fingerprint 完全相同，且等於 root.crt 自己的 fingerprint
```

這其實是 SPIFFE trust bundle 設計本來就該有的行為：bundle 只發布「信任
的最頂端」（root），不含 intermediate——因為 intermediate 是對方憑證
交換時自己會附帶的（每張 SVID 的憑證鏈本身就包含 leaf + 一路到 root
前一層的所有 intermediate），驗證方只需要認得最終的 root 就夠了。

**第二個關鍵發現：實測交叉驗證，完全離線 openssl，不靠 SPIRE 自己的
mTLS 邏輯，純粹證明憑證鏈數學上就是通的**：

```bash
# 從兩邊各自的 test-client 抓出 SVID + bundle
kubectl --context=cluster1-134 -n spire-nested-a exec test-client -c app -- cat /svids/tls.crt > nested-a-leaf.crt
kubectl --context=cluster1-134 -n spire-nested-a exec test-client -c app -- cat /svids/ca.crt   > nested-a-bundle.crt
kubectl --context=cluster2-134 -n spire-nested-b exec test-client -c app -- cat /svids/tls.crt > nested-b-leaf.crt
kubectl --context=cluster2-134 -n spire-nested-b exec test-client -c app -- cat /svids/ca.crt   > nested-b-bundle.crt

# 用 nested-b 自己拿到的 bundle，去驗證 nested-a 簽出來的憑證
openssl verify -CAfile nested-b-bundle.crt -untrusted nested-a-leaf.crt nested-a-leaf.crt
# -> nested-a-leaf.crt: OK

# 反過來
openssl verify -CAfile nested-a-bundle.crt -untrusted nested-b-leaf.crt nested-b-leaf.crt
# -> nested-b-leaf.crt: OK
```

兩個方向都 `OK`。**全程沒有執行過一次 `bundle set`、沒有建立任何
`ClusterFederatedTrustDomain`、entry 上也完全沒有 `federatesWith`。**

`nested-a-leaf.crt` 實際憑證鏈是 3 層（`leaf → SPIRE Server 自己內部管理
的簽發層 → 我們的 diy-intermediate-a`），root 本身不在鏈裡（它只存在
對方的 bundle 檔案裡，當作信任錨）——這是 SPIRE 內部本來就會自動疊一層
自己管理的簽發 CA（即使只有單一 server 沒有 nested 也一樣），不是這次
測試引入的額外東西。

## 結論：假設完全成立，且比想像中乾淨

- 只要 **trust domain 名稱統一** + 各自的簽發權**真的追溯到同一把根**
  （不管是像這次離線手動簽，還是真正 Nested 的即時 API 呼叫），兩座
  SPIRE Server 從第一天就自動互信，`bundle show` 甚至連內容都天生一致
  ——完全不用管 O(N²) 的 federation 關係。
- 跟 `17-` 那套「共用同一把 key、但 trust domain 各自獨立」的失敗案例
  對照，可以清楚看出：**決定要不要 federation 的，是 trust domain
  名稱有沒有統一，不是憑證/金鑰材料像不像。**
- 跟真正的官方 Nested（`UpstreamAuthority "spire"`）比，這個 DIY 版本
  最大的差異是**沒有任何持續的網路耦合**——root 私鑰簽完 intermediate
  就可以完全離線收起來，兩座 SPIRE Server 之間永遠不需要直接通訊。
  代價是失去官方 Nested 的自動續簽機制：intermediate 快過期時要手動
  重跑一次簽發流程，沒有 live upstream server 幫你自動處理。

## Cleanup

```bash
kubectl --context=cluster1-134 delete namespace spire-nested-a
kubectl --context=cluster2-134 delete namespace spire-nested-b
```
（`17-spire-cross-cluster-mtls/` 的 `spire` namespace 完全沒被這次測試
動到，不用管。）

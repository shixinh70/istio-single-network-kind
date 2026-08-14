# Sidecar egress.hosts：逐條列舉 service vs. 整個 namespace 通配

比較兩種 `Sidecar.spec.egress.hosts` 寫法，在**引用的 service 數量相同**的前提下
（即最終 Envoy 實際可見的 service 集合完全一樣），限制與效能上的差異：

- **方式 A（namespace 通配）**：`"remote-ns/*"` — 一條 entry，涵蓋整個 namespace
- **方式 B（svc 列舉）**：`"remote-ns/svc-1.remote-ns.svc.cluster.local"`, `"remote-ns/svc-2...."`, ... — 每個要用的 service 各寫一條

實測環境：`cluster2`（Istio 1.13.5），`remote-ns` 放置真實（但不接後端 pod 的輕量）
Service，`local-ns/client` 是被 Sidecar 限定 scope 的目標 workload。完整原始數據見同目錄
`README.md` 的 Result 1、2、3、5、6、7；本篇只抽取「A vs B 直接對照」的部分。

## 結論先講：兩者在相同 svc 數量下，standing cost 幾乎沒有差異

無論 svc 數量是 500 還是 2000，只要最終可見的 service 集合相同，**A、B 兩種寫法對
proxy 記憶體、istiod 記憶體、實際推送的 config 大小幾乎沒有差別**（誤差都在幾個百分點內，
屬於量測雜訊）。換句話說：「哪個 service 可見」才是決定成本的關鍵，「你用什麼語法表達
可見性」幾乎不影響 runtime 成本。

| 指標 | N=500，A（`*`） | N=500，B（列舉） | N=2000，A（`*`） | N=2000，B（列舉） |
|---|---:|---:|---:|---:|
| proxy 可見 cluster 數 | 516 | 516 | 2016 | 2016 |
| proxy working set | 103.4 MB | 106.1 MB | 292.6 MB | 284.4 MB |
| config_dump 大小 | 2,801,615 B | 2,801,615 B（逐 byte 相同） | 10,340,003 B | 10,291,302 B |
| istiod working set | 150.0 MB | 151.2 MB | 197.4 MB | 199.1 MB |

N=500 那組的 config_dump 甚至逐 byte 完全一致 — 直接證明 Envoy 拿到的 config 是
「解析後的可見集合」決定的，跟 Sidecar CR 裡怎麼寫無關。

**已在 scope 內的 service 發生變動（churn）時，處理成本也一樣：**

| | N=500，A | N=500，B | N=2000，A | N=2000，B |
|---|---:|---:|---:|---:|
| 傳播時間 | 2.10s | 2.10s | 5.71s | 5.76s |
| istiod CPU | 88ms | 111ms | 215ms | 413ms |
| proxy CPU | 319ms | 349ms | 1167ms | 1179ms |

## 真正的差異：三個面向

### 1. CRD/etcd 物件大小上限 —— 只有 B 會撞到

- **A（`namespace/*`）**：固定 1 條、幾十 bytes，跟 namespace 裡有 5 個還是 5 萬個
  service 完全無關，**永遠不會因為 svc 數量而撞到任何大小限制**。
- **B（svc 列舉）**：CR 大小隨列舉數量線性成長。實測撞到兩層限制：
  1. 用一般 `kubectl apply`：約 **8,000 條**左右失敗 —— 不是 Sidecar/etcd 的限制，是
     Kubernetes 對單一物件 `metadata.annotations` 總大小 256 KiB 的限制（`kubectl apply`
     會把整份物件序列化寫進 `kubectl.kubernetes.io/last-applied-configuration` 這個
     annotation 裡）。改用 `kubectl apply --server-side` 可以繞過這層（不寫這個
     annotation）。
  2. 換成 `--server-side` 後，真正的硬上限出現在約 **31,500～31,800 條**之間 ——
     `etcdserver: request is too large`，這是 etcd 預設 `--max-request-bytes`
     （~1.5MB）造成的，**跟 Sidecar 這個 CRD 本身無關，是任何 K8s 物件都會撞到的通用限制**。

**結論**：如果你的 svc 列舉清單長期會成長到幾千條以上，B 方式需要考慮：
1. 是否用 `--server-side` apply（GitOps 工具鏈要注意是否支援）
2. 清單成長的長期趨勢是否有機會逼近 3 萬條這個絕對上限

### 2. 「沒在用」的 service churn —— B 的核心優勢，且優勢隨規模放大

這是兩者唯一在效能面真正分道揚鑣的地方。namespace 裡如果存在「這個 workload 其實
用不到」的 service（例如同 namespace 裡別的團隊的 service），A 方式會把它們一起納入
watch 範圍；B 方式因為明確列舉，天然排除掉它們。

實測：在 remote-ns 裡加入幾個「沒被列舉、沒被使用」的額外 service，改動其中一個
(churn)，觀察 `local-ns/client` 的反應：

| 規模 | A（`*`）：未列舉 service 的 churn 代價 | B（列舉）：未列舉 service 的 churn 代價 |
|---|---|---|
| N=500 | 傳播 1.05s，proxy CPU ~231ms | **完全不可見，從未出現在 proxy config 裡** |
| N=2000 | 傳播 5.83s，proxy CPU ~1296ms | **完全不可見，從未出現在 proxy config 裡** |

關鍵細節：在 N=2000 規模下，A 方式改動一個「全新、沒在用」的 service，代價
（~5.83s / ~1296ms）幾乎跟改動一個「早就在 scope 內、大家都認識」的 service
（~5.71s / ~1167ms）**一模一樣**。也就是說 A 方式付出的代價，不是因為那個 service
「陌生」，而是單純因為**整個 scope 的規模大**——proxy 每次都要重新處理跟 scope
等大的一份 config diff，不管實際變動的是不是 workload 真正在乎的那個 service。

這代表：如果 remote-ns 裡「沒被這個 workload 使用」的 service 數量越多、或那些
service 的變動越頻繁（例如別的團隊常態性部署），A 方式付出的無謂成本就越高；B 方式
則完全不受影響，因為那些 service 根本不在它的 watch list 裡。

### 3. 維運負擔 —— B 的代價，換取上面第 2 點的隔離效果

- **A**：新增的 service 自動被涵蓋，不用改 Sidecar CR ——方便，但這正是「沒在用的
  service 也被推送」這個問題的根源。
- **B**：每次要開始用一個新的 remote-ns service，都要手動把它加進 Sidecar CR 的
  `egress.hosts` 清單——多一道維護動作，換取「不會被無關變動打擾」的隔離性，以及
  「scope 完全由你自己宣告、不會隨 mesh 成長而悄悄擴大」的可預期性。

## 快速決策表

| 情境 | 建議 |
|---|---|
| remote-ns 裡的 service 幾乎都會被這個 workload 用到 | **A（`*`）**——B 只會多付出維護成本和 CRD 大小風險，換不到什麼隔離效益 |
| remote-ns 裡有明顯用不到、且會頻繁變動的 service（如别的團隊共用 namespace） | **B（列舉）**——隔離效益真實存在，且規模越大受益越明顯 |
| 預期實際會用到的 service 數量未來可能上看數千甚至上萬 | 用 B 前先評估 CRD 大小（`--server-side` apply、~3萬條硬上限），必要時搭配自動化工具維護清單，而非手動維護 |
| 只是想讓 config 精簡、但用到的 service 集合本身就很小（幾十條內） | A、B 皆可，效能差異可忽略，選維護起來順手的即可 |

## 參考

完整原始實測數據、方法論、以及額外面向（istiod 自身記憶體成長、對 mesh 其他無關
proxy 的 blast radius、不存在 service 的幽靈 entry 影響）見同目錄 `README.md`
Result 1–7。

# istio-proxy CPU limit throttling + `holdApplicationUntilProxyStarts`(1.13.5 vs 1.29.6）

## 問題描述

正式環境中的 Istio 1.13.5 叢集,`istio-proxy` 的 CPU limit 設得很低（100m）,同時開了
`holdApplicationUntilProxyStarts`（讓 app container 一直等到 sidecar 真的 ready 才啟動)。
現象是 sidecar 啟動很容易因為 CPU throttling 拖慢,進而讓 app container 被
`holdApplicationUntilProxyStarts` 卡住、觸發 timeout。懷疑跟 `GOMAXPROCS` 有關。

本篇記錄：先查證相關的 upstream issue，再用 kind cluster 實際重現、量化、找出真正的
瓶頸，最後直接在 Istio 1.29.6 上重跑同一組測試做版本對照。

## 背景 issue（已查證，非猜測）

- [`istio/istio#20264`](https://github.com/istio/istio/issues/20264) — Pilot（istiod）因為
  Go runtime 沒有 cgroup-aware，`GOMAXPROCS` 預設抓 node 全部核心數而非 pod 的 CPU limit，
  被 CFS 持續 throttle。建議用 `uber-go/automaxprocs` 或手動設 `GOMAXPROCS` env var。
- [`istio/istio#41351`](https://github.com/istio/istio/issues/41351) — 同樣的訴求延伸到
  "pilot agent and istiod（以及任何其他 Istio 的 Go process）"，建議 `GOMAXPROCS` 該設成接近
  CPU request/limit。
- [`fix-concurrency.yaml`](https://github.com/istio/istio/blob/master/releasenotes/notes/fix-concurrency.yaml) —
  Envoy 的 `--concurrency`（worker thread 數）改成「預設就根據 CPU limit 換算，除非明確指定」，
  約每 833m 一條 thread；此修法在 **Istio 1.18** 上線。
- Istio **1.19.0** release note："Added an automatically set `GOMEMLIMIT` and `GOMAXPROCS`
  to all deployments to improve performance."

## 環境

- `cluster1`：k8s v1.24.17 + Istio **1.13.5**（kind，host 6 核心，`docker exec cluster1-worker nproc` = 6）
- `cluster1-134`：k8s v1.34.8 + Istio **1.29.6**（kind，用來對照新版本行為）
- 測試用 workload：`hashicorp/http-echo`，annotation 設 `sidecar.istio.io/proxyCPU` /
  `proxyCPULimit: "100m"`，`proxy.istio.io/config: '{"holdApplicationUntilProxyStarts": true}'`
- 用 Python 產生 600 組假 `Service`+`Endpoints`（無對應真實 pod），灌進同一個 namespace，
  用來放大 xDS（CDS/EDS/LDS/RDS）在啟動當下要處理的資料量，模擬「大 mesh」情境
- 量測方式：`time kubectl wait --for=condition=Ready`（實際啟動耗時,即
  `holdApplicationUntilProxyStarts` 會讓 app container 卡多久)，加上直接讀
  `/sys/fs/cgroup/cpu/cpu.stat`（cgroup v1，`nr_periods`/`nr_throttled`/`throttled_time`)
  確認是不是真的被 CFS throttle，而不是純粹用啟動時間去猜

## 一、`--concurrency` 與 `GOMAXPROCS` 是兩條完全獨立的線

`istio-proxy` container 裡其實是兩個獨立行程：

```
istio-proxy container
├── pilot-agent（Go process，PID 1）
│   └── GOMAXPROCS ← 只受 GOMAXPROCS env var 影響；Istio 1.13.5 不會自動設，
│                     沒設的話 = runtime.NumCPU() = host 可見核心數（跟 CPU limit 無關）
└── envoy（C++ process，pilot-agent 的子行程)
    └── --concurrency ← 只受 EstimatedConcurrency 影響；決定 Envoy 自己開幾條 worker thread
```

查證 `pkg/envoy/proxy.go`（`release-1.13` branch）：

```go
if e.Concurrency > 0 {
    startupArgs = append(startupArgs, "--concurrency", fmt.Sprint(e.Concurrency))
}
```

整個檔案沒有任何 `runtime.GOMAXPROCS` 呼叫。`GOMAXPROCS` 只影響 pilot-agent 自己的
Go scheduler 執行緒數，對 Envoy 的 `--concurrency`／實際 worker thread 數**沒有任何影響**。

**實測佐證**：把 `--concurrency` 從 2 調到 8、16，pilot-agent 的執行緒數全部維持在 13 條，
完全沒有跟著變動；只有手動加 `GOMAXPROCS=1` env var 之後，pilot-agent 執行緒數才從 13
降到 8。兩者互不影響，修其中一個不會影響另一個的行為。

## 二、Istio 1.13.5 的 `estimateConcurrency` 演算法（讀原始碼，非猜測）

`pkg/kube/inject/inject.go`（`release-1.13` branch）：

```go
func estimateConcurrency(cfg *meshconfig.ProxyConfig, annotations map[string]string, valuesStruct *opconfig.Values) int {
    if cfg != nil && cfg.Concurrency != nil {
        concurrency := int(cfg.Concurrency.Value)
        if concurrency > 0 {
            return concurrency // mesh 明確設正數 → 直接用
        }
        // 只有明確設成 0（不是「沒設」），才會走這段自動偵測
        if limit, ok := annotations[annotation.SidecarProxyCPULimit.Name]; ok {
            out, err := quantityToConcurrency(limit)
            if err == nil { return out }
        } else if request, ok := annotations[annotation.SidecarProxyCPU.Name]; ok {
            ...
        } else if resources := valuesStruct.GetGlobal().GetProxy().GetResources(); resources != nil {
            ... // mesh 層級的預設 resources.limits/requests["cpu"]
        }
    }
    return 2 // cfg.Concurrency 是 nil（完全沒設）→ 固定回傳寫死的 2
}

func quantityToConcurrency(quantity string) (int, error) {
    q, err := resource.ParseQuantity(quantity)
    return int(math.Ceil(float64(q.MilliValue()) / 1000)), nil // 無條件進位到整數核心
}
```

**關鍵陷阱**：`sidecar.istio.io/proxyCPULimit` annotation，**只有在 mesh-wide
`ProxyConfig.Concurrency` 被明確設成特殊值 `0`（不是「沒設」)時才會被讀取**。如果
`Concurrency` 欄位完全沒配置（最常見的情境，包含我們一開始的測試),函式會直接跳過所有
annotation／CPU 判斷，回傳寫死的 **2**——不管你的 CPU limit 是 100m 還是 100 核，
Envoy 拿到的都是同一個數字。這是很容易被誤解的地方：以為裝了 `proxyCPULimit`
annotation 就會自動反映到 concurrency，實際上 1.13.5 預設完全不會。

### 意外發現：istiod 的 mesh config 快取，重啟前不會反映 ConfigMap 的最新內容

實測過程中一度量到 concurrency=16（不是預期的 2），排查後發現：mesh ConfigMap 的
`defaultConfig.concurrency` 在這顆叢集先前的某個時間點被設過 16，後來又移除，但**已經在跑
的 istiod process 從頭到尾沒有重新讀取這個變更**，一直用記憶體裡的舊值，直到手動
`rollout restart istiod` 之後才變回 2。這代表：**改 mesh ConfigMap 的內容，不保證正在跑的
istiod 會即時生效**，這件事本身也是個操作面陷阱，值得記錄。

## 三、實測：CPU limit 100m 下的 throttling（1.13.5）

baseline（少量既有 service）：13.3s 啟動，`nr_throttled/nr_periods` = 52/104 = 50%。

加入 600 組假 Service/Endpoint 之後：

| 測試 | 啟動時間 | throttle 比例 | pilot-agent 執行緒 | Envoy concurrency |
|---|---|---|---|---|
| 小 mesh | 13.3s | 50%（52/104） | 13 | 2（固定預設） |
| +600 假 Service | 24.8s | 86%（308/358） | 13 | 2（固定預設） |
| 同上 + `GOMAXPROCS=1` | 33s（無改善） | 88%（316/359） | 8（下降） | 2（不變） |

**結論**：`GOMAXPROCS` 修正確實讓 pilot-agent 執行緒數下降，但對 throttle 比例／啟動時間
幾乎沒有幫助——因為真正在啃 CPU、解析套用 xDS 的是 Envoy（C++），不是 pilot-agent。

## 四、實測：`--concurrency` 對 throttle 的影響（同一 CPU limit）

用 `proxy.istio.io/config` annotation 強制指定 concurrency，CPU limit 維持 100m 不變，
mesh 維持 600 個假 Service：

| concurrency | 平均啟動時間（3 次) | 平均 throttle 比例 |
|---|---|---|
| 1 | 36.2s | 98.7% |
| 16 | 39.0s | 98.2% |

**結論**：concurrency 調小（1 vs 16），throttle 比例幾乎沒變。原因是 **cgroup 的 CPU
quota 是整個 container 的總量上限，不是分給每條 thread 各自的額度**——不管幾條 thread
分著跑，加總起來能用的 CPU-time 就是那 100m。如果啟動當下需要的 CPU 運算量本身就超過
100m 能供應的量，調 thread 數幫助有限。

### 為什麼「host 核心數更多」理論上仍然值得擔心

雖然 concurrency 從 1 調到 16 對*這次*的 throttle 比例影響不大，但 `--concurrency`
從 2 調到 8（同一批測試更早的一輪）時啟動時間確實從 24.8s 惡化到 32.5s、throttled_time
從 39.7s 增加到 44.5s——真正的風險不是「thread 數量要一路線性往上加才會一路惡化」，
而是**一旦 thread 數從「正確對應 CPU limit 的低值」跳到「明顯超額的高值」，傷害就已經
產生**。Envoy 官方自己在 `fix-concurrency.yaml` 承認"CPU limit 偵測有時候不準"——host
核心數越多，一旦這個偵測失準、退回抓 `hardware_concurrency()`（host 核心數），
Envoy 就會開出對應 host 核心數那麼多的 worker thread，而不是我們這次多數情況下看到的
2～16 這種量級。不過在 Istio 1.13.5 的 injection template 邏輯下（`estimateConcurrency`
結構上幾乎不會回傳 0 或不回傳），這個最極端情境理論上很難透過正常 injection 流程觸發。

## 五、實測：node 資源與 throttle 的關係

### node 完全被鄰居打滿飽和（無 CPU limit 的鄰居，6 個 pod × 4 條無限迴圈 = 24 條）

- node load average 飆到 19.2（6 核心機器）
- 啟動時間：**73 秒**（比乾淨環境慢 2~3 倍）
- throttle 比例反而**下降**到 27%（179/673)，throttled_time 只有 8.4 秒（比乾淨環境的
  40~48 秒還低）

**這是本次調查最重要的反直覺發現**：啟動時間大幅惡化，但代表「自己 quota 用完被卡住」
的 `nr_throttled` 指標反而變低。原因是兩種完全不同的機制：

1. **CFS quota throttling**（`nr_throttled`）：node 有閒置容量時，container 幾乎立刻能把
   自己的額度燒完，然後被卡到下一週期——週期數多、throttle 比例高，但每個週期實際耗時
   接近標準的 100ms，累積起來的總延遲有限
2. **排程搶佔／餓死**（完全不會反映在 `cpu.stat` 裡）：鄰居沒有 CPU limit、瘋狂搶核心時，
   我們的 process 連被排到、拿到那份配額的機會都變少了——不是「額度用完被卡」，是「根本
   輪不到你跑」，這種延遲只會反映在 wall clock 上，`cpu.stat` 的 throttle 計數器完全看不到

**操作面意義**：只監控 `container_cpu_cfs_throttled_seconds_total` 這類 cgroup throttle
指標，在「node 被鄰居嚴重搶占」的情境下反而會誤判成「情況變好了」，實際上是當下最糟的
情況。必須同時看**實際 pod Ready 時間**，throttle 指標只能反映其中一種成因。

### node 整體不忙、只有中等的單一鄰居（500m，未飽和）

同一 session 前後緊接測試：

| 情境 | 啟動時間 | throttle 比例 |
|---|---|---|
| 有一個 500m 中等鄰居 | 42.4s | 92.5% |
| 完全安靜 | 38.9s | 95.9% |

差異在雜訊範圍內。**結論**：只要 node 整體還有閒置容量，中等程度的鄰居幾乎不會加劇
throttle；真正造成傷害的是 node 被推向**整體飽和**，不是「有沒有鄰居在用 CPU」。

## 六、實測：CPU limit 拉高能否解決（同一 600-service mesh）

| CPU limit | 啟動時間 | throttle 比例 | 累積 throttled_time |
|---|---|---|---|
| 100m | 36.2s | 97%（306/316） | 47.3s |
| 250m | 16.8s | 87%（122/141） | 13.7s |
| 500m | 9.8s | 74%（56/76） | 5.2s |
| 1000m | 6.1s | 24%（10/41） | 0.87s |
| 2000m | 6.2s | **0%（0/42）** | **0s** |

**結論**：拉高 CPU limit 是唯一被證實能徹底解決（甚至歸零）throttle 的槓桿，而且是乾淨的
單調關係。但注意**報酬遞減**：1000m→2000m throttle 比例從 24% 降到 0%,但啟動時間幾乎
沒變（6.1s→6.2s）——不需要追求「零 throttle」，抓到「啟動時間開始打平」的那個 limit
值即可（此案例約 1000m）。這個門檻值**跟 mesh 規模綁定**，不是通用常數，mesh 越大門檻
越高。

## 七、為什麼 CPU limit 沒吃滿、node 還有資源，也會被 throttle

CPU limit 是 **cgroup 層級的「配額」機制**，跟 node 剩不剩資源是兩件完全獨立的事：

Linux CFS bandwidth controller 把時間切成週期（本環境 100ms 一個週期，`cfs_period_us`
=100000）。container 在每個週期裡最多消耗 `cfs_quota_us`（100m 對應 10ms)的 CPU 時間，
用完就會被**強制擋下來，不管其他核心是不是完全閒置**。這就像速限：路上其他車道空不
空,跟你的車被限速在 10 km/h 完全無關——限制你的是自己車上的限速器,不是路況。

`docker exec cluster1-worker top` 顯示 CPU 大部分是閒的，但 throttle 比例還是 86~99%,
正好證實了兩者無關：node 閒不閒只決定「你多快能被排進去跑」；配額用不用得完，只跟
「自己被分配到的配額 vs 實際需要跑多久」有關。

## 八、1.13.5 vs 1.29.6 版本對照（同一組 600-service mesh、同 100m limit）

| | 1.13.5 | 1.29.6 |
|---|---|---|
| GOMAXPROCS | 沒設，預設抓 host 核心數（6） | **自動設成 1**（Istio 1.19 起，所有 deployment 自動注入） |
| Envoy concurrency | annotation 被忽略，固定拿到 2（除非明確設 `concurrency:0`） | **annotation 直接生效，自動算出 1**（Istio 1.18 起） |
| throttle 比例 | ~86~99%（多次平均） | 78%（195/249） |
| 累積 throttled_time | ~40~48s | 20.2s（大約砍半） |
| 啟動時間 | 24~42s（多次平均約 30s） | 23s |

**兩個 upstream 修法都已查證、也已直接在叢集上實測確認生效**：

- [Istio 1.18](https://istio.io/latest/news/releases/1.18.x/announcing-1.18/change-notes/) —
  concurrency 預設就根據 CPU limit 自動換算，不用再明確設 `concurrency: 0`
- [Istio 1.19](https://istio.io/latest/news/releases/1.19.x/announcing-1.19/change-notes/) —
  `GOMAXPROCS`/`GOMEMLIMIT` 自動注入到所有 deployment（istiod 與 sidecar 皆是)

### 同 concurrency、同 CPU limit、同 mesh 規模下，1.29 仍然比較快——原因不只是這兩個修法

刻意把 1.13.5 的 concurrency 手動設成跟 1.29 自動算出來的**同一個值（1）**做直接對照：

| | 1.13.5（concurrency 手動設 1） | 1.29.6（concurrency 自動算出 1） |
|---|---|---|
| throttle 比例 | 96.5~99% | 78% |
| 啟動時間 | 32~41s | 23s |

concurrency、CPU limit、mesh 規模完全相同，結果仍有明顯差距——證明 1.29 效能較好**不只是
因為 thread 數配置得更聰明**，而是處理同樣工作量所需的絕對 CPU-seconds 本身變少了。
可能原因（皆非能靠參數調校取得，需要版本升級）：

1. Envoy 本體版本差距極大（1.13.5 綁定 ~2022 年的 Envoy，其間有大量 xDS 解析/config
   套用效率優化)
2. istiod 產生的 xDS payload 內容本身可能更精簡（同樣宣告的 Service 數量，不同版本
   產生的 filter/config 內容量不同)
3. Delta xDS 逐漸成為預設路徑，處理方式效率不同

## 九、1.13.5 能調、不能調的整理

**能調、且已證實有效或有幫助**：
- 拉高 CPU limit（唯一證實能徹底解決、甚至歸零 throttle 的槓桿，見第六節）
- 手動設 `GOMAXPROCS` env var（減少 pilot-agent 浪費的執行緒，但對整體 throttle 幫助有限）
- 手動設 `concurrency`（同上，效果有限，因為瓶頸是總量不是分配）
- 用 `Sidecar` CRD 縮小 workload 的 egress 範圍，減少啟動當下要處理的 xDS 資料量（理論上
  最有機會拉近版本差距的槓桿，因為是直接減少「要做的事」而非重新分配資源；本次未實測)

**調不到，只能靠升級版本**：
- Envoy binary 本身的處理效率
- istiod 產生 config 的內部邏輯效率
- concurrency／GOMAXPROCS 自動偵測的預設行為（1.13.5 需要明確配置才會生效，1.18/1.19+
  預設就對）

## 測試腳本

生成假 Service/Endpoint 灌入 mesh（放大 xDS 資料量用）：

```python
N = 600
for i in range(N):
    print(f"""apiVersion: v1
kind: Service
metadata:
  name: fake-svc-{i}
  namespace: cpu-throttle-test
spec:
  ports: [{{port: 8080, targetPort: 8080}}]
---
apiVersion: v1
kind: Endpoints
metadata:
  name: fake-svc-{i}
  namespace: cpu-throttle-test
subsets:
- addresses: [{{ip: "10.244.{{(i // 250) + 1}}.{{(i % 250) + 2}}"}}]
  ports: [{{port: 8080}}]
---""")
```

量測單一 pod 啟動耗時與 throttle 狀態：

```bash
time kubectl -n cpu-throttle-test wait --for=condition=Ready pod/<name> --timeout=300s
kubectl -n cpu-throttle-test exec <name> -c istio-proxy -- cat /sys/fs/cgroup/cpu/cpu.stat
kubectl -n cpu-throttle-test exec <name> -c istio-proxy -- ps aux | grep -o "concurrency [0-9]*"
```

模擬吵鬧鄰居（無 CPU limit，飽和整個 node）：

```yaml
resources:
  requests: {cpu: "900m"}
command: ["sh", "-c", "for i in 1 2 3 4; do (while true; do :; done) & done; wait"]
```

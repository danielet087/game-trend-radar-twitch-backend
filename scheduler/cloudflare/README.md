# Game Trend Radar 排程控制器（Cloudflare Workers）

七項定時工作統一由既有 Cloudflare Worker 觸發，使用單一 Cron `0,5,15,17,30 * * * *`，每天 120 個 tick。每次只檢查原定 Cron 分鐘到期的工作：Twitch 每小時第 05 分檢查當小時是否缺少資料，其餘工作依下表執行。Python 與資料發布繼續在 GitHub Actions 執行；Worker 只查狀態與派發 workflow，不需要 KV、D1 或常駐伺服器，也沒有可由網頁呼叫的執行端點。

## 正式排程與設定

統一控制器限定 `danielet087`、`main` 與下列目的 workflow，不能透過環境變數指定其他儲存庫。既有 Twitch 回條驗證、每日資料與收集前 guard 維持原邏輯。

| 工作 ID | 台灣時間 | 儲存庫 | Workflow | 額外輸入 |
| --- | --- | --- | --- | --- |
| `twitch` | 每小時第 05 分 | `game-trend-radar-twitch-backend` | `collect.yml`（ID `369223512`） | `force=false` |
| `steam_daily` | 每日 00:00／06:00／12:00／18:00 檢查，當日成功後略過 | `game-trend-radar-backend` | `steam-two-phase.yml` | `refresh_today=true` |
| `steam_catchup` | 每日 03:00–23:00，每小時整點 | `game-trend-radar-backend` | `steam-official-daily-catchup-250.yml` | — |
| `steam_growth` | 每日 01:15／07:15／13:15／19:15 檢查，當日成功後略過 | `game-trend-radar-backend` | `steam-public-growth.yml` | — |
| `steam_content` | 每日 07:30、19:30 | `game-trend-radar-content-backend` | `steam-catalog-reconcile.yml` | — |
| `nintendo_daily` | 每日 08:30 | `game-trend-radar-twitch-backend`（憑證執行入口） | `collect-nintendo.yml` | — |
| `frontend_insights` | 每小時第 17 分 | `game-trend-radar` | `radar-insights.yml` | — |

IGDB（NS／NS2／PS5）的收集程式與候選資料位於獨立的 `game-trend-radar-igdb-backend`；`collect-nintendo.yml` 留在既有 Twitch 儲存庫，只負責在 runner 安全沿用現有 IGDB／Twitch 憑證並執行 IGDB 主機收集器。IGDB 主機收集使用獨立 workflow 與 concurrency，不經過 Twitch 直播或 Steam Followers 佇列。每日 08:30 重用原有 30 分 Cron tick，因此不新增 Cron，也不增加每天 120 個 tick；Cloudflare PAT 仍只需原先四個儲存庫，不必新增 IGDB 後端儲存庫權限。

Twitch 以外的六個 workflow 接受 `target_slot` 與 `trigger_source=cloudflare`；`target_slot` 使用原定到期時刻的 UTC ISO 字串，包含分鐘，並且 `run-name` 須含獨立 `slot=<同一時刻>` 欄位。例如 `Steam growth | slot=2026-10-02T17:15:00Z | cloudflare`。Twitch 仍使用 UTC 整點小時作為 slot。

正式設定如下，`wrangler.toml` 是 Git 連接及 CLI 部署時的設定來源：

- `RADAR_ENABLED_JOBS = "steam_daily,steam_catchup,steam_growth,steam_content,frontend_insights,nintendo_daily"`；Twitch 保持內建啟用。
- `crons = ["0,5,15,17,30 * * * *"]`，只保留這一個 Cron。
- 四個儲存庫的 workflow 已支援相應輸入、run-name 與既有 concurrency。每日刷新在真正重設進度前檢查 slot，避免相同時段重複重設。
- 七種工作的 GitHub 原生 `schedule` 已停用，保留 `workflow_dispatch` 與必要的 push/CI 入口；不恢復舊的一次性手動工作排程。Cloudflare 派發後仍需對照 Actions 與資料發布結果，不能只憑 Cron 存在判定收集成功。

控制器需要 GitHub fine-grained PAT，Resource owner 為 **`danielet087`**，Repository access 選擇 **`game-trend-radar-twitch-backend`、`game-trend-radar-backend`、`game-trend-radar-content-backend`、`game-trend-radar` 四個儲存庫**，Repository permissions 設為 **Actions → Read and write**。控制器不需要 Contents write；資料發布仍由 GitHub 既有 Secrets 處理。

**只修改同一個 PAT 的儲存庫範圍或權限，token 值不變，就不需要替換 Cloudflare Secret。** 新建、重新產生或更換 PAT，才需要更新既有 Worker 的執行時 Secret `GITHUB_ACTIONS_TOKEN`。不要把 token 提交到儲存庫或貼到聊天訊息。

可使用 `probeSchedulerPermissions(env)` 做 read-only 檢查：它只 GET 固定 workflow，對每個目的回報讀取結果，完全不 dispatch。GET 成功只能確認 Actions 讀取與 workflow 存在，**不能證明 Actions write 權限**；實際派發被拒絕時仍會以 403 停止該工作，不會改派其他目的地。

單一 Cron 每天產生 120 個 tick，每次只查原定 Cron 分鐘到期的工作，Twitch 不會因 00、15、17、30 分的 tick 額外重試。延遲到同一小時內的 Twitch、Followers 補漏與前端分析仍可執行；跨小時則略過，避免補造舊時段觀測。每日刷新、成長與內容工作僅接受原定台灣日期內的延遲，不跨日補跑。傳入 workflow 的 slot 始終是原定到期時刻；Twitch 收集仍記錄實際量測時間。

Twitch 以外的工作會查 active 狀態與分頁執行紀錄。同 slot 已嘗試不重送；每日候選與 Steam 成長另每六小時檢查，當天任一完整成功便跳過後續時段，失敗／取消／未執行則在下一檢查時段重試，仍在排隊或執行時等待。日期以 Asia/Taipei 計算，讀取範圍從當日午夜開始，不把前日成功沿用到今天。每日候選只採信當日有效排程 slot，手動的一個批次續跑不算每日刷新成功；同日重试續用已保存的候選進度，不再次重設。成長需該次 run 的 `Require complete growth coverage` 步驟成功，429、部分量測、錯誤或覆蓋不足會在保存／發布成果後使 workflow 失敗，舊版顯示成功卻沒有此驗證的 run 不阻止重試。成長／Followers 另查同 concurrency 群組的已知工作；候選重試與整點 Followers 同時到期時先檢查候選，候選需執行或等待時該輪 Followers 等待，避免尚未可見的派發競爭。三個相關手動一次性 workflow 僅列為 active blockers，不列入排程或 dispatch 清單。

缺少 Secret、目的地設定不符、403、重新導向或不完整回應都會阻擋該工作；某個儲存庫的權限問題不會停用既有 Twitch 工作。Worker 在同一 isolate 內另有進行中檢查與已派發 slot 保護，HTTP timeout 也不盲目重送。這是記憶體保護，不能代替 workflow 持久 slot guard，也不宣稱跨 isolate 的 exactly-once。

## Twitch 判斷流程

1. 原定每小時第 05 分的 Cron 才檢查 Twitch，以實際執行時間取得目前 UTC 小時。同小時內延遲數分鐘仍可檢查，跨小時的舊 Cron 會略過；若提早於原定時刻收到事件則等待。台灣也是第 05 分，UTC 時段只用於內部識別。
2. 讀取前端儲存庫的 `data/twitch_collection_status.json`。只有 `collection_complete: true`、時間欄位一致且 `observed_slot` 是目前小時，才視為已完成。
3. 分別查詢 `queued`、`in_progress`、`waiting`、`pending`、`requested` 工作，任何一種存在就等待。即使是前一天尚未解除的工作，也不會被最近幾筆歷史遮住。
4. 分頁讀取最近兩小時內建立的執行紀錄。當前小時建立的工作，或標題含 `slot=<目前 UTC 時段>` 的工作，計入嘗試次數。GitHub `run_attempt` 也計入；已完成但沒有發布回條的工作同樣不視為成功。
5. 保留事件重送時的防重複保護：本小時已看到 2 次嘗試，或最近一次完成未滿 10 分鐘，就不觸發。狀態讀取失敗、內容異常或超過讀取上限時停止本次觸發；Cloudflare 不會另外安排本小時重試，下次定期檢查在下一小時第 05 分。
6. 透過 `workflow_dispatch` 傳入 `target_slot`、`trigger_source=cloudflare`、`force=false`。HTTP 成功只代表 GitHub 接受觸發；是否發布成功仍須對照 Actions 與回條。下一輪排程只檢查屆時的目前小時。

GitHub 原本第 17 分的 Twitch 定時排程已移除。手動及必要的 push 入口仍進入同一 concurrency 群組，真正開始收集前再次確認目前小時是否已發布。跨小時才開始的 Cloudflare 舊時段工作會跳過，不會把現在的直播資料寫成過去的觀測。

### 回條代表什麼

回條與 `twitch_live.json`、台灣日期的逐時歷史檔一同提交到前端儲存庫；它表示資料已提交成功，**不表示 GitHub Pages 建置已完成**。`collection_complete` 表示本次有效 census 已完成並發布，並不表示所有低優先序類別的追隨者查詢都已完成。未查完的篩選觀眾中位數仍保留 `null`，不能為了這些缺值反覆觸發整批收集。

| 欄位 | 意義 |
| --- | --- |
| `schema_version` | `1` |
| `target_slot` | 原定收集時段，UTC 整點 ISO 字串 |
| `observed_slot` | 實際開始收集所在小時，UTC 整點 ISO 字串 |
| `collection_started_at` | 實際開始時間 |
| `completed_at` / `generated_at` | 收集完成時間；兩者相同 |
| `collection_complete` | 有效資料是否已完成並提交 |
| `run_id` | 對應 GitHub Actions 執行 ID |
| `history_path` | 依實際觀測台灣日期產生的歷史檔 |

## 部署前提

部署時，四個儲存庫的七個 workflow 必須已包含控制器需要的輸入與 run-name；Twitch `collect.yml` 另需 `force` 輸入、收集前檢查與發布回條。正式排程表與六項 enable 清單需同時保持一致，否則可能出現未知輸入被拒絕、工作停用或無法判定發布成功。

需要 Cloudflare 帳號與一個 GitHub fine-grained personal access token：

- Resource owner：`danielet087`。
- Repository access：選 `game-trend-radar-twitch-backend`、`game-trend-radar-backend`、`game-trend-radar-content-backend`、`game-trend-radar`。
- Repository permissions：`Actions` → `Read and write`；Metadata 的必要讀取權限會自動包含。
- 不需要前端 Contents 權限；回條從公開網址讀取。
- 設定適合的到期日；更換 token 值時更新 Cloudflare 執行時 Secret。只擴充同一 PAT 權限而未更換值時，不必替換 Secret。失效時 Worker 會停止觸發並記錄 `github_runs_http_401` 或 `403`。

不要把 token 放進程式、`wrangler.toml`、GitHub commit 或聊天訊息。

### 方法 A：Cloudflare 連接 GitHub 自動部署（目前採用）

已建立的 Cloudflare Worker 名稱是 `game-trend-radar-twitch-backend`，必須與 `wrangler.toml` 的 `name` 相同；它仍然只是排程監控器，Python 收集器繼續在 GitHub Actions 執行。

連接 `danielet087/game-trend-radar-twitch-backend` 儲存庫，設定如下：

| 設定 | 值 |
| --- | --- |
| Worker 名稱 | `game-trend-radar-twitch-backend` |
| Production branch | `main` |
| Root directory / Path | `scheduler/cloudflare` |
| Build command | `node --test test/*.test.mjs` |
| Deploy command | `npx wrangler deploy` |

首次部署後，在 Worker 的 **Settings → Variables and Secrets** 新增執行時 **Secret** `GITHUB_ACTIONS_TOKEN`，填入上述 GitHub token 並套用變更。既有 Secret 使用同一 PAT 且值未變時直接保留；新建或重新產生 token 才更新它。不要填在 Build variables and secrets；建置用 Secret 不會自動成為執行時的 Secret。

Git 連接方式會執行 Wrangler 並套用設定檔，包括單一 `0,5,15,17,30 * * * *` Cron、六項 enable 清單、一般變數、日誌與關閉 HTTP 路由。`No URLs enabled` 是預期設定，不需要新增網域。往後推送 `main` 會依 Cloudflare Builds 的分支與路徑設定觸發部署；程式成功部署不等於已成功觸發採集，仍需確認 Secret、Cron 與執行紀錄。

若出現 `Latest build failed`，開啟該次建置日誌確認錯誤。Worker 名稱與設定檔不一致會造成 Git 連接部署失敗；修正後應部署最新 commit，不要只重試舊 commit。成功後從 Observability 查看 `dispatch`、`already_published` 或 `workflow_active` 等結果，再對照 GitHub Actions 與前端回條。新 Cron 的設定傳播可能需要最多 15 分鐘。

### 方法 B：Cloudflare 控制台貼上程式

1. 在 Workers & Pages 建立 Worker，名稱使用 `game-trend-radar-twitch-backend`。若已建立或連接 GitHub，直接使用既有 Worker，不要另建第二個監控器。
2. 將 `src/worker.mjs` 的完整內容貼入編輯器並部署。程式無外部依賴，可以直接使用；尚未加入 Cron 時不會收集。
3. 在 Worker 設定的 Variables and Secrets 加入執行時 **Secret** `GITHUB_ACTIONS_TOKEN`，值為上述四個儲存庫 Actions RW 的 GitHub PAT；已有且未換值的 Secret 直接保留。同時新增一般變數 `RADAR_ENABLED_JOBS`，值為 `steam_daily,steam_catchup,steam_growth,steam_content,frontend_insights,nintendo_daily`，並套用變更。只貼程式而沒設定這個 enable 清單，六個非 Twitch 工作仍會停用。
4. 在 Domains & Routes 停用 `workers.dev` 路由與 Preview URLs；這個 Worker 只需要排程，即使誤開網址也只會回傳 404。
5. 在 Triggers / Cron Triggers 將原項目改為單一 `0,5,15,17,30 * * * *`，移除舊的 `5 * * * *`、`*/5 * * * *` 或重複項目。Cron 使用 UTC，新增或修改可能需要最多 15 分鐘傳播；台灣的分鐘數相同，每日時段由程式依 UTC+8 選擇。
6. 確認七個 GitHub 原生定時 `schedule` 已停用，再從 Worker Logs / Observability 查看對應 `job_id` 的下一次檢查結果；搭配 Actions 的 `slot=` 標題確認工作開始與完成。Twitch 另須確認前端回條與歷史 JSON 一起更新。

僅貼上程式的方式不會自動套用儲存庫的 `wrangler.toml`，因此需要手動設定執行時 Secret、enable 清單、Cron 與路由；若日後改用 Git 連接或 CLI 部署，會以該設定檔為準。已採用 Git 連接時，請修改儲存庫宣告設定來變更 Cron 或一般變數，避免只在控制台手改而被下一次 Wrangler 部署覆寫。

### 方法 C：Wrangler CLI

首次建立時，從此目錄執行（Node.js 22 或更新版本）：

```sh
node --test test/*.test.mjs
npx wrangler login
npx wrangler deploy
npx wrangler secret put GITHUB_ACTIONS_TOKEN
npx wrangler tail
```

`secret put` 會在本機互動式詢問 token。第一次 `deploy` 至 `secret put` 完成之間，即使 Cron 已觸發也會因缺少 Secret 而停止，不會呼叫 GitHub。已有同一 token 的執行時 Secret 時，可略過 `secret put`。設定檔關閉 `workers.dev` 與預覽網址，包含單一正式 Cron 與六項 enable 清單；不需要自行新增 HTTP 路由。部署需要你自己的 Cloudflare 帳號授權。

現有 `TWITCH_CLIENT_ID`、`TWITCH_CLIENT_SECRET`、`FRONTEND_REPO_TOKEN` 繼續留在 GitHub，**不必複製到 Cloudflare**。使用控制台或本機登入部署，也不必新增 Cloudflare API token 到 GitHub。

## 觀察與測試

```sh
# 無網路、無 Secret 的自動測試
node --test test/*.test.mjs
```

測試涵蓋七項工作時段、Nintendo 每日一次與獨立 workflow、台灣午夜與 UTC 跨年、03:00–23:00 窗口、原定分鐘 slot、同小時延遲與跨小時／跨日拒絕、同 slot 成功或失敗不重送、共用 concurrency blockers、權限不足時隔離各工作，以及原有 Twitch 回條、404 首次啟動、五種 active 狀態、冷卻與上限、分頁、觸發回應、異常或巨大回應、秘密不送到公開網址及 HTTP 端點不執行工作。

GitHub CI 另以 Miniflare／workerd 執行真實 Workers 請求相容性測試，所有外部請求都由本機 fixture 回應，不使用真實 token 或觸發 GitHub。測試工具獨立放在 `test/runtime`，不增加 Worker 執行時依賴，也不需要修改 Cloudflare 的 Build command。

```sh
npm ci --prefix test/runtime --no-audit --no-fund
npm test --prefix test/runtime
```

對外請求使用 `redirect: "manual"`，明確拒絕所有 3xx 回應。不要改成 `redirect: "error"`：Node.js 接受這個值，但 workerd 會在發出請求前拒絕它，原本會因此記錄 `receipt_unavailable`。亦不可改成自動跟隨重新導向，避免將 GitHub token 送到其他位置。

Worker 結構化日誌只包含結果代碼、時段與執行 ID，不輸出 token、請求標頭或 API 回應內容。常見結果：

| `action` / `reason` | 說明 |
| --- | --- |
| `skip` / `already_published` | 目前小時已完成 |
| `wait` / `before_collection_window` | 尚未到第 05 分 |
| `wait` / `workflow_active` | 已有工作排隊或執行 |
| `wait` / `retry_cooldown` | 等候 10 分鐘重試間隔 |
| `wait` / `hourly_attempt_limit` | 已看見 2 次嘗試，不再自動觸發 |
| `dispatch` / `missing_published_collection` | GitHub 已接受 Twitch 觸發；尚未代表資料已更新 |
| `dispatch` / `scheduled_slot_due` | GitHub 已接受其他工作到期時段的觸發 |
| `skip` / `slot_completed` | 同 slot 已有成功執行紀錄 |
| `skip` / `day_already_completed` | 每日候選或成長在同一台灣日期已完整成功，後續六小時檢查略過 |
| `wait` / `daily_refresh_priority` | 同分鐘候選需重試／等待，Followers 補漏等待下一輪 |
| `skip` / `slot_already_attempted` | 同 slot 已有執行紀錄，即使失敗也不自動重送 |
| `wait` / `slot_dispatch_attempted` | 本 isolate 已嘗試派發同 slot，避免 timeout 後盲目重送 |
| `skip` / `stale_cron_delivery` | 延遲事件已跨過工作允許的小時或台灣日期 |
| `skip` / `no_job_due` | 該 Cron 分鐘沒有到期工作 |
| `skip` / `job_staged` | 該非 Twitch 工作不在 enable 清單；正式七項開啟時不應出現 |
| `blocked` / `receipt_redirect_rejected`、`github_runs_redirect_rejected` 或 `github_dispatch_redirect_rejected` | 來源回傳重新導向；沒有跟隨目標網址，也不視為觸發成功 |
| `blocked` / `receipt_*` 或 `github_*` | 讀取失敗或內容不可信；此次沒有盲目補跑 |

## 免費額度與限制

單一 Cron 每小時 00、05、15、17、30 分執行，共 120 個 tick／日，每次只查原定分鐘到期的工作，沒有工作到期就直接略過。Twitch 當前小時回條已完成時只讀一次小檔案；待收集時多讀 GitHub 狀態。這個設計使用 Workers 免費方案，不把 20～25 分鐘的 Python 工作搬到 Worker；仍須查看 CPU 與請求用量，確認符合免費方案限制。

- 七項工作使用 Cloudflare 作為唯一固定排程來源；GitHub 原生定時備援已移除。單一 Cron 的其他分鐘不會額外檢查 Twitch，也不會在同 slot 安排定期重試。這不是平台可用性保證，Cloudflare Cron、GitHub API、GitHub runner 或資料來源仍可能延遲／失敗。
- Worker 記憶體中的進行中檢查與已派發 slot 紀錄只能防護同一 isolate；未使用跨 isolate 的持久鎖。兩個幾乎同時到達不同 isolate 的 Cron 仍可能在 GitHub run 可見前重複 dispatch，不能宣稱 exactly-once。後端 concurrency、執行前回條／slot 檢查及每日重設的持久狀態是必要的防護。
- Twitch「2 次」限制是 **Worker 依已看見的紀錄決定是否再觸發**，不是所有來源的硬性全域上限；手動強制執行或必要 push 入口仍可能額外啟動。跳過收集的同小時工作也會保守計入上限，狀態 API 短暫延後顯示不能視為交易鎖。其他六種工作只要已看見同 slot run，就不會自動重送。
- 只檢查目前小時，不會補造過去漏掉的直播觀眾數。現在重跑只能獲得現在的資料；過去缺少的歷史量測仍保留為缺值。
- Twitch workflow 顯示成功但沒有回條，不代表資料已發布；同小時內可人工檢查與重跑，Cloudflare 不會額外排定重試。本版不會自動下載舊 artifact 重發；有效 artifact 可人工恢復發布，不能冒充新的觀測時間。
- 長期卡在排隊／等待核准的 GitHub 工作不會被自動取消，需要查看原因。Worker 選擇等待，避免以更多排隊工作淹沒執行器。

官方文件：[Cron Triggers](https://developers.cloudflare.com/workers/configuration/cron-triggers/)、[Wrangler 設定](https://developers.cloudflare.com/workers/wrangler/configuration/)、[Workers Secrets](https://developers.cloudflare.com/workers/configuration/secrets/)、[GitHub workflow runs](https://docs.github.com/en/rest/actions/workflow-runs)、[GitHub workflow dispatch](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event)。


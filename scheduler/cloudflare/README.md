# Game Trend Radar 排程控制器（Cloudflare Workers）

目前維持每小時第 05 分檢查 Twitch 收集工作（Cron：`5 * * * *`），缺少當小時資料時才觸發收集。其餘五種工作已準備好但預設停用，`RADAR_ENABLED_JOBS` 為空字串；在權限與 workflow 輸入完成前，不會觸發新的工作。Python 仍在 GitHub Actions 執行，Worker 只查狀態與觸發 workflow，不需要 KV、D1 或常駐伺服器，也沒有可由網頁呼叫的執行端點。

## 全部排程切換準備

統一控制器限定 `danielet087`、`main` 與下列目的 workflow，不能透過環境變數指定其他儲存庫。既有 Twitch 回條驗證、每日資料與收集前 guard 維持原邏輯。

| 工作 ID | 台灣時間 | 儲存庫 | Workflow | 額外輸入 |
| --- | --- | --- | --- | --- |
| `twitch` | 每小時第 05 分 | `game-trend-radar-twitch-backend` | `collect.yml`（ID `369223512`） | `force=false` |
| `steam_daily` | 每日 00:00 | `game-trend-radar-backend` | `steam-two-phase.yml` | `refresh_today=true` |
| `steam_catchup` | 每日 03:00–23:00，每小時整點 | `game-trend-radar-backend` | `steam-official-daily-catchup-250.yml` | — |
| `steam_growth` | 每日 01:15 | `game-trend-radar-backend` | `steam-public-growth.yml` | — |
| `steam_content` | 每日 07:30、19:30 | `game-trend-radar-content-backend` | `steam-catalog-reconcile.yml` | — |
| `frontend_insights` | 每小時第 17 分 | `game-trend-radar` | `radar-insights.yml` | — |

五個新 workflow 必須接受 `target_slot` 與 `trigger_source=cloudflare`；`target_slot` 使用原定到期時刻的 UTC ISO 字串，包含分鐘，並且 `run-name` 須含獨立 `slot=<同一時刻>` 欄位。例如 `Steam growth | slot=2026-10-02T17:15:00Z | cloudflare`。Twitch 仍使用 UTC 整點小時作為 slot。

尚待完成的設定：

1. 先確認四個儲存庫的 workflow 都已支援上述輸入、run-name、既有 concurrency，以及在真正修改資料前的 slot guard。每日刷新尤其須避免重複 slot 再次重設進度。
2. 將既有 GitHub fine-grained token 的 Repository access 擴充至 **`game-trend-radar-twitch-backend`、`game-trend-radar-backend`、`game-trend-radar-content-backend`、`game-trend-radar` 四個儲存庫**，Repository permissions 的 **Actions → Read and write**。控制器不需要 Contents write；資料發布仍由 GitHub 既有 Secrets 處理。將更新後 token 套用到現有 Cloudflare Secret `GITHUB_ACTIONS_TOKEN`，不要提交或公開 token。
3. 可使用 `probeSchedulerPermissions(env)` 做 read-only 檢查：它只 GET 固定 workflow，對每個目的回報讀取結果，完全不 dispatch。GET 成功只能確認 Actions 讀取與 workflow 存在，**不能證明 Actions write 權限**；實際派發被拒絕時仍會以 403 停止該工作，不會改派其他目的地。
4. 權限與 workflow 就緒後，將 `RADAR_ENABLED_JOBS` 設為 `steam_daily,steam_catchup,steam_growth,steam_content,frontend_insights`，將 `[triggers]` 改為單一 Cron `crons = ["0,5,15,17,30 * * * *"]`。目前 staging 設定檔刻意保留空的 enable 清單與原本 `5 * * * *`，不可只更新 Cron 就宣稱全部工作已切換。
5. 正式切換時停用這六種工作的 GitHub 原生 `schedule`，保留 `workflow_dispatch` 與必要的 push/CI 入口；不要讓兩套定時來源長期重複觸發，也不恢復舊的一次性手動工作排程。對照 Cloudflare 日誌與每個 workflow 的 slot 執行紀錄確認切換結果。

單一最終 Cron 每天產生 120 個 tick，每次只查原定 Cron 分鐘到期的工作，Twitch 不會因 00、15、17、30 分的 tick 額外重試。延遲到同一小時內的 Twitch、Followers 補漏與前端分析仍可執行；跨小時則略過，避免補造舊時段觀测。每日刷新、成長與內容工作僅接受原定台灣日期內的延遲，不跨日補跑。傳入 workflow 的 slot 始終是原定到期時刻；Twitch 收集仍記錄實際量測時間。

新工作會分別查五種 active 狀態，再分頁檢查最近執行紀錄。找到同 slot 的成功 run 就略過；同 slot 已失敗、取消或略過也不自動重送，避免每日 reset 被重複執行。成長／Followers 另查同 concurrency 群組的已知工作；Followers 也等待每日發現工作完成，避免新派發替換另一條流程已排隊的工作。三個相關手動一次性 workflow 僅列為 active blockers，不列入排程或 dispatch 清單。

缺少 Secret、目的地設定不符、403、重新導向或不完整回應都會阻擋該工作；某個新儲存庫的權限問題不會停用既有 Twitch 工作。Worker 在同一 isolate 內另有進行中檢查與已派發 slot 保護，HTTP timeout 也不盲目重送。這是記憶體保護，不能代替 workflow 持久 slot guard，也不宣稱跨 isolate 的 exactly-once。

以下 Twitch 專用流程與 GitHub 第 17 分備援敘述，適用於目前尚未切換的 staging 狀態；正式切換後依上表與單一 Cloudflare Cron 運作。

## 判斷流程

1. 每小時第 05 分由 Cron 執行，以實際執行時間取得目前 UTC 小時；若提早於第 05 分收到事件則略過本次檢查。台灣也是第 05 分，UTC 時段只用於內部識別。
2. 讀取前端儲存庫的 `data/twitch_collection_status.json`。只有 `collection_complete: true`、時間欄位一致且 `observed_slot` 是目前小時，才視為已完成。
3. 分別查詢 `queued`、`in_progress`、`waiting`、`pending`、`requested` 工作，任何一種存在就等待。即使是前一天尚未解除的工作，也不會被最近幾筆歷史遮住。
4. 分頁讀取最近兩小時內建立的執行紀錄。當前小時建立的工作，或標題含 `slot=<目前 UTC 時段>` 的工作，計入嘗試次數。GitHub `run_attempt` 也計入；已完成但沒有發布回條的工作同樣不視為成功。
5. 保留事件重送時的防重複保護：本小時已看到 2 次嘗試，或最近一次完成未滿 10 分鐘，就不觸發。狀態讀取失敗、內容異常或超過讀取上限時停止本次觸發；Cloudflare 不會另外安排本小時重試，下次定期檢查在下一小時第 05 分。
6. 透過 `workflow_dispatch` 傳入 `target_slot`、`trigger_source=cloudflare`、`force=false`。HTTP 成功只代表 GitHub 接受觸發；是否發布成功仍須對照 Actions 與回條。下一輪排程只檢查屆時的目前小時。

GitHub 原本第 17 分的排程保留為備援。所有入口進入同一 concurrency 群組，真正開始收集前再次確認目前小時是否已發布。跨小時才開始的 Cloudflare 舊時段工作會跳過，不會把現在的直播資料寫成過去的觀測。

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

先將支援 `target_slot`、`trigger_source`、`force` 輸入、收集前檢查與發布回條的新版 `collect.yml` 合併到後端 `main`，再啟用 Cloudflare Cron。否則 GitHub 會拒絕未知輸入，Worker 也無法判定發布是否成功。

需要 Cloudflare 帳號與一個 GitHub fine-grained personal access token：

- Resource owner：`danielet087`。
- Repository access：只選 `game-trend-radar-twitch-backend`。
- Repository permissions：`Actions` → `Read and write`；Metadata 的必要讀取權限會自動包含。
- 不需要前端 Contents 權限；回條從公開網址讀取。
- 設定適合的到期日，並在到期前更新 Cloudflare Secret；失效時 Worker 會停止觸發並記錄 `github_runs_http_401` 或 `403`。

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

部署成功後，在 Worker 的 **Settings → Variables and Secrets** 新增 **Secret** `GITHUB_ACTIONS_TOKEN`，填入上述 GitHub token 並套用變更。不要填在 Build variables and secrets；建置用 Secret 不會自動成為執行時的 Secret。

Git 連接方式會執行 Wrangler 並套用設定檔，包括每小時第 05 分 Cron、一般變數、日誌與關閉 HTTP 路由。`No URLs enabled` 是預期設定，不需要新增網域。往後推送 `main` 會依 Cloudflare Builds 的分支與路徑設定觸發部署；程式成功部署不等於已成功觸發採集，仍需確認 Secret、Cron 與執行紀錄。

若出現 `Latest build failed`，開啟該次建置日誌確認錯誤。Worker 名稱與設定檔不一致會造成 Git 連接部署失敗；修正後應部署最新 commit，不要只重試舊 commit。成功後從 Observability 查看 `dispatch`、`already_published` 或 `workflow_active` 等結果，再對照 GitHub Actions 與前端回條。新 Cron 的設定傳播可能需要最多 15 分鐘。

### 方法 B：Cloudflare 控制台貼上程式

1. 在 Workers & Pages 建立 Worker，名稱使用 `game-trend-radar-twitch-backend`。若已建立或連接 GitHub，直接使用既有 Worker，不要另建第二個監控器。
2. 將 `src/worker.mjs` 的完整內容貼入編輯器並部署。程式無外部依賴，可以直接使用；尚未加入 Cron 時不會收集。
3. 在 Worker 設定的 Variables and Secrets 加入 **Secret** `GITHUB_ACTIONS_TOKEN`，值為上述 GitHub token，套用變更。其餘設定已有程式預設值。
4. 在 Domains & Routes 停用 `workers.dev` 路由與 Preview URLs；這個 Worker 只需要排程，即使誤開網址也只會回傳 404。
5. 在 Triggers / Cron Triggers 將排程設為 `5 * * * *`。若已有 `*/5 * * * *`，請修改原項目，避免保留兩個排程。Cron 使用 UTC，新增或修改可能需要最多 15 分鐘傳播。
6. 從 Worker Logs / Observability 查看下一次檢查結果；搭配 GitHub Actions 確認開始執行，再確認前端回條出現並與歷史 JSON 一起更新。

僅貼上程式的方式不會自動套用儲存庫的 `wrangler.toml`，因此需要手動設定 Secret、Cron 與路由；若日後改用 Git 連接或 CLI 部署，會以該設定檔為準。

### 方法 C：Wrangler CLI

從此目錄執行（Node.js 22 或更新版本）：

```sh
node --test test/*.test.mjs
npx wrangler login
npx wrangler deploy
npx wrangler secret put GITHUB_ACTIONS_TOKEN
npx wrangler tail
```

`secret put` 會在本機互動式詢問 token。第一次 `deploy` 至 `secret put` 完成之間，即使 Cron 已觸發也會因缺少 Secret 而停止，不會呼叫 GitHub。設定檔關閉 `workers.dev` 與預覽網址，包含每小時第 05 分 Cron；不需要自行新增 HTTP 路由。部署需要你自己的 Cloudflare 帳號授權。

現有 `TWITCH_CLIENT_ID`、`TWITCH_CLIENT_SECRET`、`FRONTEND_REPO_TOKEN` 繼續留在 GitHub，**不必複製到 Cloudflare**。使用控制台或本機登入部署，也不必新增 Cloudflare API token 到 GitHub。

## 觀察與測試

```sh
# 無網路、無 Secret 的自動測試
node --test test/*.test.mjs
```

測試涵蓋已發布、404 首次啟動、跨日 UTC 時段、舊觀測延後發布、五種執行中狀態、冷卻與上限、分頁、觸發回應、API 失敗、異常回條、巨大回應、秘密不送到公開網址，以及 HTTP 端點不執行工作。

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
| `dispatch` / `missing_published_collection` | GitHub 已接受觸發；尚未代表資料已更新 |
| `blocked` / `receipt_redirect_rejected`、`github_runs_redirect_rejected` 或 `github_dispatch_redirect_rejected` | 來源回傳重新導向；沒有跟隨目標網址，也不視為觸發成功 |
| `blocked` / `receipt_*` 或 `github_*` | 讀取失敗或內容不可信；此次沒有盲目補跑 |

## 免費額度與限制

每小時一次，共 24 次定期檢查／日。當前小時回條已完成時只讀一次小檔案；待收集時多讀 GitHub 狀態。這個設計使用 Workers 免費方案，不把 20～25 分鐘的 Python 工作搬到 Worker，但正式啟用後仍須查看 CPU 與請求用量，確認符合免費方案限制。

- 每小時排程搭配 GitHub 原生備援可降低漏觸發，但 Cloudflare 不會每 5 分鐘檢查或在同小時定期重試，也不是平台可用性保證。Cloudflare Cron、GitHub API、GitHub runner 或 Twitch 都可能延遲／失敗。
- 不使用持久鎖，因此兩個幾乎同時到達的 Cron 可能都接受到「尚無工作」並重複 dispatch。後端 concurrency 與執行前回條檢查是必要的防重複收集措施。
- 「2 次」限制是 **Worker 依已看見的紀錄決定是否再觸發**；不是所有來源的硬性全域上限。第 17 分的備援或手動強制執行仍可能額外啟動；跳過收集的同小時工作也會保守計入上限。狀態 API 短暫延後顯示也不能視為交易鎖。
- 只檢查目前小時，不會補造過去漏掉的直播觀眾數。現在重跑只能獲得現在的資料；過去缺少的歷史量測仍保留為缺值。
- Workflow 顯示成功但沒有回條，不代表資料已發布；同小時內可能由第 17 分備援或人工重跑處理，Cloudflare 不會額外排定重試。本版不會自動下載舊 artifact 重發；有效 artifact 可人工恢復發布，不能冒充新的觀測時間。
- 長期卡在排隊／等待核准的 GitHub 工作不會被自動取消，需要查看原因。Worker 選擇等待，避免以更多排隊工作淹沒執行器。

官方文件：[Cron Triggers](https://developers.cloudflare.com/workers/configuration/cron-triggers/)、[Wrangler 設定](https://developers.cloudflare.com/workers/wrangler/configuration/)、[Workers Secrets](https://developers.cloudflare.com/workers/configuration/secrets/)、[GitHub workflow runs](https://docs.github.com/en/rest/actions/workflow-runs)、[GitHub workflow dispatch](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event)。

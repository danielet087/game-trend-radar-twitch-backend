# Twitch 排程監控器（Cloudflare Workers）

每 5 分鐘檢查 Twitch 收集工作。正常每小時收集一次；Python 仍在 GitHub Actions 執行，這裡只查狀態與觸發 workflow。Worker 不需要 KV、D1 或常駐伺服器，也沒有可由網頁呼叫的執行端點。

## 判斷流程

1. 以實際執行時間取得目前 UTC 小時；每小時第 05 分起才嘗試收集。台灣也是第 05 分，UTC 時段只用於內部識別。
2. 讀取前端儲存庫的 `data/twitch_collection_status.json`。只有 `collection_complete: true`、時間欄位一致且 `observed_slot` 是目前小時，才視為已完成。
3. 分別查詢 `queued`、`in_progress`、`waiting`、`pending`、`requested` 工作，任何一種存在就等待。即使是前一天尚未解除的工作，也不會被最近幾筆歷史遮住。
4. 分頁讀取最近兩小時內建立的執行紀錄。當前小時建立的工作，或標題含 `slot=<目前 UTC 時段>` 的工作，計入嘗試次數。GitHub `run_attempt` 也計入；已完成但沒有發布回條的工作同樣不視為成功。
5. 已看到 2 次嘗試就不再由 Worker 觸發；最近一次完成後等待至少 10 分鐘才重試。狀態讀取失敗、內容異常或超過讀取上限時停止本次觸發，在下次檢查重試狀態讀取。
6. 透過 `workflow_dispatch` 傳入 `target_slot`、`trigger_source=cloudflare`、`force=false`。HTTP 成功只代表 GitHub 接受觸發，下一輪仍會驗證工作與發布回條。

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

Git 連接方式會執行 Wrangler 並套用設定檔，包括每 5 分鐘 Cron、一般變數、日誌與關閉 HTTP 路由。`No URLs enabled` 是預期設定，不需要新增網域。往後推送 `main` 會依 Cloudflare Builds 的分支與路徑設定觸發部署；程式成功部署不等於已成功觸發採集，仍需確認 Secret、Cron 與執行紀錄。

若出現 `Latest build failed`，開啟該次建置日誌確認錯誤。Worker 名稱與設定檔不一致會造成 Git 連接部署失敗；修正後應部署最新 commit，不要只重試舊 commit。成功後從 Observability 查看 `dispatch`、`already_published` 或 `workflow_active` 等結果，再對照 GitHub Actions 與前端回條。新 Cron 的設定傳播可能需要最多 15 分鐘。

### 方法 B：Cloudflare 控制台貼上程式

1. 在 Workers & Pages 建立 Worker，名稱使用 `game-trend-radar-twitch-backend`。若已建立或連接 GitHub，直接使用既有 Worker，不要另建第二個監控器。
2. 將 `src/worker.mjs` 的完整內容貼入編輯器並部署。程式無外部依賴，可以直接使用；尚未加入 Cron 時不會收集。
3. 在 Worker 設定的 Variables and Secrets 加入 **Secret** `GITHUB_ACTIONS_TOKEN`，值為上述 GitHub token，套用變更。其餘設定已有程式預設值。
4. 在 Domains & Routes 停用 `workers.dev` 路由與 Preview URLs；這個 Worker 只需要排程，即使誤開網址也只會回傳 404。
5. 在 Triggers / Cron Triggers 加入 `*/5 * * * *`。Cron 使用 UTC，新增或修改可能需要最多 15 分鐘傳播。
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

`secret put` 會在本機互動式詢問 token。第一次 `deploy` 至 `secret put` 完成之間，即使 Cron 已觸發也會因缺少 Secret 而停止，不會呼叫 GitHub。設定檔關閉 `workers.dev` 與預覽網址，包含每 5 分鐘 Cron；不需要自行新增 HTTP 路由。部署需要你自己的 Cloudflare 帳號授權。

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

每 5 分鐘共 288 次檢查／日。當前小時回條已完成時只讀一次小檔案；待收集時多讀 GitHub 狀態。這個設計使用 Workers 免費方案，不把 20～25 分鐘的 Python 工作搬到 Worker，但正式啟用後仍須查看 CPU 與請求用量，確認符合免費方案限制。

- 這是降低漏觸發、偵測失敗並重試，不是平台可用性保證。Cloudflare Cron、GitHub API、GitHub runner 或 Twitch 都可能延遲／失敗。
- 不使用持久鎖，因此兩個幾乎同時到達的 Cron 可能都接受到「尚無工作」並重複 dispatch。後端 concurrency 與執行前回條檢查是必要的防重複收集措施。
- 「2 次」限制是 **Worker 依已看見的紀錄決定是否再觸發**；不是所有來源的硬性全域上限。第 17 分的備援或手動強制執行仍可能額外啟動；跳過收集的同小時工作也會保守計入上限。狀態 API 短暫延後顯示也不能視為交易鎖。
- 只檢查目前小時，不會補造過去漏掉的直播觀眾數。現在重跑只能獲得現在的資料；折線圖過去缺口保留。
- Workflow 顯示成功但沒有回條時，仍會受冷卻與次數限制地重試整批收集。本版不會自動下載舊 artifact 重發；有效 artifact 可人工恢復發布，不能冒充新的觀測時間。
- 長期卡在排隊／等待核准的 GitHub 工作不會被自動取消，需要查看原因。Worker 選擇等待，避免以更多排隊工作淹沒執行器。

官方文件：[Cron Triggers](https://developers.cloudflare.com/workers/configuration/cron-triggers/)、[Wrangler 設定](https://developers.cloudflare.com/workers/wrangler/configuration/)、[Workers Secrets](https://developers.cloudflare.com/workers/configuration/secrets/)、[GitHub workflow runs](https://docs.github.com/en/rest/actions/workflow-runs)、[GitHub workflow dispatch](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event)。

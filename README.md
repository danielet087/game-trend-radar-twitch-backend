# Game Trend Radar — Twitch 獨立後端

找出總觀眾數達 **7,000 人**且有新作線索的遊戲，收錄後持續追蹤到發售滿 **30 天**。7,000 人只用於首次收錄，之後跌破門檻、退出熱門排行或沒人直播都不會移除。紀錄總觀眾數、開台實況主人數及**每台觀眾數的中位數**，並獨立管理 Twitch「全新」標記的驗證狀態。顯示用的**篩選中位數**只納入免費追隨者 **> 1,000**、且當次觀眾 **≥ 10** 的直播台。

## 執行狀態

2026-09-30 新增 [Cloudflare 每小時排程](scheduler/cloudflare/README.md)：每小時第 05 分檢查本小時是否已發布完整基礎量測，缺資料且沒有執行中的工作時才觸發既有 Python workflow。Cloudflare 每小時只檢查一次，不在同一小時每 5 分鐘重試；GitHub 原生第 17 分排程保留為備援。Cloudflare 程式與設定提交到 GitHub **不代表外部排程已啟用**，需先依部署說明設定 Worker 與 `GITHUB_ACTIONS_TOKEN`，再以 Cloudflare 日誌、Actions 的 `cloudflare` 執行名稱與發布 receipt 三者驗證。

所有入口共用 workflow concurrency；工作開始後會解析前端最新 git HEAD，讀取該不可變版本的成功紀錄，再判斷是否需要收集。同一小時已有成功發布時直接略過；舊時段延遲排隊的外部請求略過，不回填歷史直播數。手動 Run workflow 預設也防重複，需要重新取樣時才勾選 `force`。原生 GitHub cron 留作備援。

**Cloudflare 設定於每小時 05 分觸發，GitHub Actions 於 17 分備援**（台灣時間，例如 11:05 與 11:17）。兩者的 cron 分別為 `5 * * * *` 與 `17 * * * *`；UTC 與台灣分鐘相同，不需另外平移小時。備援仍會檢查本小時回條，已發布就略過收集。排程可能延遲，因此資料保存實際收集與量測時間，不將延遲結果標成準點觀測。設定已提交不等於排程已觸發，實際狀態須看 Actions 的 `cloudflare` 名稱或 `schedule` 執行紀錄，不能以手動成功代替。

仍可到 Actions → Collect Twitch live data → Run workflow 手動執行。同一時間只允許一個收集／發布工作，不取消正在執行的工作；沒有 push 或 workflow_run 收集觸發器。Test standalone collector 的 push CI 只跑離線測試，不查 API、不發布資料。

舊版收集器已成功連線及發布；這不代表新版候選流程已通過實際 API 測試。2026-09-29 01:08（台灣）啟動的新版收集在 Helix 呼叫預算耗盡後中止，沒有發布。當時第 2～4 頁各有 99 個新量測分類且皆低於門檻，但跨頁重複分類使停止條件一直不成立。本版改為重新量測重複分類，再判斷門檻，不增加 API 呼叫預算。每次執行會在 Actions Summary 顯示候選數、待驗證數、日期實驗及 Helix 呼叫次數。

## 收集與驗證流程

1. 用 Helix `Get Top Games` 取得依觀眾數排列的分類。這個 API 沒有回傳總觀眾數，也沒有「全新」欄位。
2. 非遊戲分類（Just Chatting、IRL、Special Events、ASMR、Music）依 Twitch 類別 ID 排除，略過 IGDB、直播分頁與追隨數查詢。每頁其餘分類先批次查詢 IGDB 首次發售日期；可判定且日期差 **≥ 30 × 24 小時**的遊戲，停止本輪直播分頁與追隨數查詢。尚未收錄、有效直接觀察為沒有「全新」標記的分類也略過量測；已收錄遊戲不因標記消失而退出。
3. IGDB 30 天推算命中，或日期仍未知的分類，逐一查詢 `Get Streams`，跟隨游標翻完直播分頁，以實況主 ID 去重，再計算總觀眾數與中位數。保留總觀眾數 **≥ 7,000** 的候選。
4. 每頁所有符合日期收集條件的分類都會檢查；若該頁至少有一款首次量測，且量測結果全低於門檻，依目錄排名停止往後翻頁。僅包含排除項或跨頁重複項的頁面繼續掃描。此排名式停止條件不能證明所有被略過分類低於門檻，也不能視為同時刻的完整全球普查。
5. 有有效「全新」觀察紀錄的候選列為 `new`；其他候選列為 `pending`。IGDB 篩選不改寫官方觀察狀態；日期與官方觀察有分歧的排除項仍保留雙方證據及對照。
6. 達門檻且有新作線索時加入持續追蹤名單。每輪另外依類別 ID 查詢已收錄遊戲，包含不在熱門目錄、跌破門檻的遊戲。候選與持續追蹤遊戲依 ID 去重後計算篩選指標：先排除當次觀眾 0～9 人的台，再查 ≥10 人頻道的公開追隨總數；恰好 1,000 名追隨者不符合。同一實況主跨遊戲共用快取與本輪查詢結果。

**IGDB 發售日期不能證明 Twitch 的「全新」標記。** 30 天規則僅控制本網站的資料收集範圍。IGDB 失敗、沒有 IGDB ID、日期缺少／失效都維持未知並繼續收集；不把未知當作未命中。每小時仍重新掃描分類與日期，遊戲資料修正後可以重新入列。既有歷史不刪除，不填補停止收集後的空白時段。

### 持續追蹤與歷史補回

`data/twitch_tracking.json`（前端儲存庫）保存首次收錄、收錄依據、最後觀測、已知發售日期與退出時間。首次收錄須同時達觀眾門檻及有新作證據：有效官方全新觀測、Twitch 日期推算命中或 IGDB 30 天命中；只有人氣、沒有新作證據的未知候選不會自動加入。

收錄後保留至 `release_at + 30 × 24 小時`。未知日期不以首次收錄日代替發售日，保留追蹤待確認；暫時無法查日期時沿用已確認的發售日判斷退出。非遊戲分類仍排除。只有直播分頁完整且實際查無開台時，當輪總觀眾與開台數才記 0；查詢失敗或未量測時保留缺測。

排程收集前先下載前端最新不可變 Git 版本的追蹤名單。檔案缺少、讀取失敗或格式損毀時停止收集，不用空名單覆蓋既有收錄。追蹤狀態與 latest、逐時歷史及成功紀錄一同發布；遇到並行更新保留較新的狀態及最早的收錄依據。

歷史補回使用已保存的人數與日期證據重播首次收錄條件；舊 IGDB 14 天快照可依原始日期補算 30 天資格，但不改寫舊快照的命中結果、原始量測時間或逐時數值。前端將尚未重新量測的恢復項目標示為上次觀測，不混入目前總數。

```bash
python -m scripts.recover_twitch_tracking /path/to/frontend --output recovered_tracking.json --source-commit FRONTEND_COMMIT
```

### 直接觀察紀錄

`data/twitch_category_verification.json` 保存直接在 Twitch 目錄看見的標記結果。每筆包含 `status`、`observed_at`、`expires_at`、`source` 與 `source_url`，有效期最多 24 小時，過期即回到待驗證。

目前種子資料來自 **台灣時間 2026-09-29 00:11:31** 的目錄觀察：30 個分類，其中 6 個有「全新」標記、24 個沒有；僅代表當時實際載入的分類，並非完整目錄。種子資料在 **2026-09-30 00:11:31** 到期。

**目前沒有自動刷新這份標記紀錄的來源。** Twitch 網頁分頁實驗遇到 integrity check，未完成 7,000 人門檻的目錄掃描；本收集器不依賴該網頁爬取。後續可直接核對待驗證候選，在確認卡片標記後更新觀察紀錄；不能把未載入、查不到或缺少資料填成 `not_new`。`new` 與 `not_new` 都必須有直接觀察來源，不接受永久標記。

### Twitch 14 天實驗與 IGDB 30 天收集篩選

採用 [Glance 的實作條件](https://github.com/glanceapp/glance/blob/372466c6d75318670dc66e4e452179350fc50c97/internal/glance/widget-twitch-top-games.go)：`評估時間 − 發售時間 < 14 × 24 小時`。滿 14 天不符合；未來日期也符合，但另標記 `release_phase: upcoming`，不顯示成已上市。

Twitch 原始日期實驗保留上述 14 天條件。IGDB 另改為 `評估時間 − 首次發售時間 < 30 × 24 小時`；恰好滿 30 天即未命中，未來日期仍命中。每筆推算及每種來源的報告均保存 `window_days` 與 `rule`，舊的 14 天歷史保持原樣。

本版隨每次排程或手動收集產生 `newness_experiment`，並在每款遊戲的 `release_experiment` 中保留兩條獨立結果：

| 來源 | 日期 | 可用狀況 |
|---|---|---|
| `twitch_original_release_date` | Twitch 的 `originalReleaseDate` | 目前缺資料；需有來源、擷取時間的既有回應或第三方資料集匯出 |
| `igdb_first_release_date` | 官方 IGDB API 的 `first_release_date` | 沿用現有 Twitch 憑證批次查詢，獨立的 30 天推算及收集篩選，不補成 Twitch 原始日期 |

`predicted_new` 為 `true` / `false` / `null`；缺資料、日期不合法、擷取時間在未來，或資料擷取超過 24 小時，都回傳未知 `null`。所有推算都標記 `confirms_twitch_new_badge: false`，不改寫 `verification`。IGDB 的有效 `false` 會將分類列入 `excluded_games`，原因為 `igdb_release_outside_window`，附上日期證據、30 天窗口與完整推算。因為已略過直播查詢，其 `metrics_collected` 為 `false`、`viewer_threshold_met` 為 `null`，不能稱它已達 7,000 人，也不將未量測人數填 0。`top_games` 僅包含仍在收集範圍且達觀眾門檻、官方觀察為 `new` 的候選。

`newness_experiment` 列出每種來源可判定／未知的候選數及排除項數、推算 NEW 的遊戲 ID、尚未上市 ID，以及與有效官方觀察的 `reference_checks`。對照使用**官方標記當時**的時間及該來源窗口計算；若日期資料是在較晚時間取得，會標示 `retrospective`，不能解讀成官方規則已驗證。IGDB 與 Twitch 日期的對照分開計數，不能把兩個來源當成兩款遊戲。`coverage.excluded_by_igdb_date_count` 紀錄本輪因日期未命中而跳過量測的分類數；`igdb_categories_evaluated`／`igdb_categories_unknown` 包含已查日期但最後未達觀眾門檻的分類。

#### 匯入 Twitch 原始日期

`data/twitch_release_dates.json` 目前為空，刻意保留缺資料的真實狀態。匯入器只處理已保存的 JSON，不連線抓取 Twitch，不啟用代理或私有 GraphQL 呼叫。支援回應本文的 `data.directoriesWithTags.edges[].node`／`data.game`，以及包含 `categoryId`、`originalReleaseDate`、`scrapedAt` 的第三方資料集陣列。IGDB 的 `first_release_date` 不會被此匯入器接受。

```bash
python -m scripts.import_twitch_release_dates saved-response.json \
  --source-name "Twitch response export" \
  --source-url https://www.twitch.tv/directory \
  --observed-at 2026-09-29T12:00:00Z
```

`--observed-at` 必須改成實際擷取時間，不能用匯入時間。第三方每筆已有 `scrapedAt` 時可以省略。來源網址只記公開網址，不含查詢字串；匯入器只保存日期與來源欄位。重複 ID 只接受較新紀錄，同一時間有衝突日期則拒絕。匯入資料提交到 Repo 後，下次排程或手動 workflow 即會使用。

**實際資料取得仍未完成：** 本版能自動做 IGDB 試算；沒有 Twitch 日期匯出時，Twitch 日期實驗的可判定數會是 0。不能把離線測試通過、IGDB 試算完成或先前 6 筆標記觀察，說成已完成官方 NEW 自動確認。

## 指標定義與範圍

| 欄位 | 意義 |
|---|---|
| `viewer_count` | 同一分類所有取得的直播台觀眾數總和 |
| `streamer_count` | 以 `user_id` 去重後的直播實況主人數，包含 0 觀眾台 |
| `median_viewer_count` | 所有這些台的觀眾數排序後的中位數；偶數台取中央兩個值的平均，並非全部台的平均觀眾數 |
| `filtered_audience.median_viewer_count` | 追隨者 >1,000 且觀眾 ≥10 的台之中位數；追隨資料不完整或合格樣本為零時為 `null` |
| `filtered_audience.eligible_streamer_count` / `eligible_viewer_count` | 已確認符合條件的台數／其觀眾數總和；`partial` 時僅為已知部分 |
| `filtered_audience.excluded_low_viewer_count` | 當次觀眾 0～9 人，先行排除、不查追隨數的台數 |
| `filtered_audience.excluded_low_follower_count` | 當次觀眾 ≥10，但追隨者 ≤1,000 的台數 |
| `filtered_audience.unknown_follower_count` / `status` | 當次觀眾 ≥10 但追隨數未知的台數；有未知即為 `partial`，否則 `complete` |
| `verification.status` | `new`：有效的全新觀察；`pending`：尚未確認或觀察已過期 |
| `measurement_started_at` / `measurement_finished_at` | 該分類的分頁量測期間 |

每個候選都翻完直播分頁，沒有只取前 100 台。但直播人數與分類排序會在分頁期間變動；去重無法保證找回期間漏過的直播，因此不能稱為同一瞬間的完整普查。

### 追隨門檻與缺值處理

7,000 人入選門檻、全體總觀眾及全體開台數仍採完整分類的量測，新的中位數獨立存於 `filtered_audience`，規則版本為 `followers_gt_1000_viewers_gte_10_v1`。四種台數（合格、低觀眾、低追隨、未知）相加等於原始開台數。只要存在追隨數未知的待判定台，就不提供篩選中位數，避免優先查大台或預算耗盡產生偏差。查不到追隨數不代表 0，也不沿用過期值；全數排除時中位數顯示空值。

使用 Helix `GET channels/followers?broadcaster_id=...` 的公開 `total`，沿用既有 app access token，不查付費訂閱數、不取得追隨者名單、不使用 `user_id` 查詢條件。401／403 授權不符、429 限流時立即停止本輪補充；其他連續 3 次失敗也停止。失敗維持未知，完整的基礎量測仍可發布。

成功查得的追隨總數保存到 runner 的 `.cache/twitch_followers.json`，以實際查詢時間判斷 24 小時有效期。快取只含頻道 ID、`total` 及 `observed_at`，只保存有效成功結果；未知與失敗不持久化、超過 24 小時或未來時間的資料不使用。GitHub Actions 還原前次快取，完成後保存新快取。此快取不是前端公開資料，也不保存原始直播名單或追隨者身分。

**預設不再限制追隨數每輪 1,000 次／300 秒。** 收集器先完成官方 NEW，再處理日期實驗有新作線索的候選，最後查其他達標候選；在同一輪持續補查，正常情況查完整再發布。仍沿用請求間隔及 24 小時快取，不以無節制併發增加 API 負擔。

整輪收集共用 `--max-collection-seconds 1500`（25 分鐘）的軟期限，從開始收集即計時，包含分類／直播掃描、輔助日期和追隨數查詢；不是追隨查詢另加 25 分鐘。GitHub 工作仍有 30 分鐘硬逾時，差額留給安裝、保存快取及發布。若基礎分類尚未查完整就到期限，保留前端上一版、不發布不完整的總數；若基礎資料完整但追隨查詢遇到期限、限流或錯誤，發布時會明確保存 `partial`／`null`，不宣稱完整中位數。可選的 `--followers-max-calls`／`--followers-max-seconds` 僅供手動診斷，正式排程不設定。

`coverage.filtered_audience` 紀錄快取命中、API 查詢、失敗、完整／未完整分類與停止原因。`helix_calls_excluding_retries` 包含此次補充查詢；原分類掃描次數另列於 `census_helix_calls_excluding_retries`。未完成的查詢不會在工作結束後於背景繼續；下次執行會使用有效快取收集當時的直播資料，不倒填舊時段中位數。

分類探索會在目錄結束，或**整頁已量測分類都低於門檻，且至少量測一個本次新出現分類**時停止。跨頁重複分類重新量測；如果仍達標就繼續，若整頁都是已排除或重複分類也繼續。候選依 ID 去重，重複量測採較新的結果。這是利用 API 排名縮小探索範圍的策略，不保證掃描期間任何時刻曾超過門檻的遊戲都被捕捉；JSON 會記錄 `stop_reason`、`all_categories_enumerated` 與重複量測次數。

若達到分類頁數、直播頁數、API 呼叫上限，或發生 API 錯誤、重複游標與無效資料，會中止發布，保留前端上一版。IGDB 僅為輔助資訊，其失敗不會阻止人數資料發布。

Helix 的直播語言不等於主播所在地。目前提供 `language_streamers`，**不將中文直播當成台灣，也不推算亞洲地區**。

## 輸出與歷史

| 前端路徑 | 內容 |
|---|---|
| `data/twitch_live.json` | 最新 schema v2 候選、驗證狀態與收集範圍 |
| `data/twitch_tracking.json` | 持續追蹤狀態、首次收錄依據與最後觀測；退出後仍保留紀錄 |
| `data/twitch_history/YYYY-MM-DD.json` | 以台灣日期分檔、以 UTC 小時為索引的人數歷史 |
| `data/twitch_collection_status.json` | 與 latest/history 同 commit 發布的小型成功紀錄，供外部監控及防重複檢查 |

新版 workflow 輸出 `collection_schedule`：`target_slot` 為要求收集的 UTC 小時，另保存 `trigger_source`、GitHub `run_id` 與 `run_attempt`。歷史小時與 `observed_slot` 採**實際開始收集時間**；`generated_at`／receipt 的 `completed_at` 是完成時間，每個遊戲仍保留自己的分頁量測起訖。跨小時完成不代表下一小時的觀察，延遲也不會偽裝成過去的量測。既有未帶此 metadata 的舊歷史保持原樣，不自動改寫日期或倒填缺口。

成功紀錄的 `collection_complete` 代表基礎分類量測完整且已提交前端儲存庫，不代表每個分類的追隨者查詢都完整，也不代表 GitHub Pages 已完成部署。低優先分類 `filtered_audience` 仍有缺值時，監控不會因此無限重跑；Pages 部署有獨立的 Actions 紀錄。

`candidate_games` 包含當輪達標且未被排除的候選；`tracked_games` 包含已收錄且仍在追蹤期限內的遊戲，不受當輪 7,000 人限制。兩者以 ID 去重，歷史保存其實際量測。`top_games` 僅代表有效官方全新觀測，不用它決定已收錄遊戲的存續；`pending_verification` 保存待查 ID；`excluded_games` 記錄預先排除的分類。排除項目未量測，不能解讀為也達到 7,000 人。

歷史保存候選與持續追蹤遊戲的三項指標、量測時間及**當次**驗證狀態，不會事後把過去的 `pending` 改寫成已確認。同一小時重跑只接受較新的結果，跨小時保留；較晚完成的舊資料不會倒退最新檔。未出現在某小時的遊戲不會填 0；舊版可能低於門檻而漏列，新版保留已收錄遊戲並持續量測。

新歷史另外保存當次 `filtered_audience`（規則、樣本台數、完整性與中位數）。舊歷史沒有頻道層級資料，無法回頭套用新門檻；舊中位數不會補成新指標。查詢未補齊的時段保留 `partial` 與 `null`，不以較晚的追隨數重算或倒填。

**歷史持續累積，不設 24 小時或 30 天刪除期限。** 每個台灣日期各有一份歷史檔，更新當天時不刪除前一天、前一月或更早的檔案。每個小時保留最新一次成功快照，包括當時的候選、官方驗證狀態與兩種日期推算；新進榜及之後不再入列的遊戲，其已保存紀錄均保留。收集失敗的時段保持缺測，不複製上一筆也不補零。前端實驗頁目前查看最近 24 小時，並不代表只保存 24 小時。

Workflow artifact 的 `retention-days: 2` 僅是尚待發布結果的短期備份；已提交到前端 Git Repository 的每日歷史不受該期限影響。現有舊版取樣不會補寫為 schema v2 的完整量測，中位數與新作判斷從新版成功收集後開始累積。

發布前取得最新前端，只提交上述 Twitch 路徑；遇到其他後端同時發布時，重新合併，最多重試 5 次，不使用 force push。未成功發布的結果不冒充前端已更新。

## Secrets

位置：Settings → Secrets and variables → Actions → Repository secrets。

| Secret | 用途 |
|---|---|
| `TWITCH_CLIENT_ID` | Twitch 應用程式 Client ID；IGDB 查詢沿用同一應用程式 |
| `TWITCH_CLIENT_SECRET` | 取得 Twitch app access token |
| `FRONTEND_REPO_TOKEN` | 寫入 `danielet087/game-trend-radar`；fine-grained token 僅選前端 Repo，Contents: Read and write |

Python 收集工作沿用上述三個 Secrets。Cloudflare Worker 額外需要其自身的 `GITHUB_ACTIONS_TOKEN` Secret，只選 Twitch 後端 Repo，Actions: Read and write；不可把此值放入程式或公開變數。Twitch 憑證與前端寫入 Token 留在 GitHub。IGDB 無法使用時仍可收集 Twitch 指標；內建 `GITHUB_TOKEN` 不能取代跨 Repo 的 `FRONTEND_REPO_TOKEN`。

缺少收集或發布憑證時 workflow 明確失敗，不宣稱已更新。收集成功後先保存 2 天的 JSON artifact；若發布失敗，最新資料與成功紀錄不前進，可據實辨識尚未完成。

## 本機使用

需 Python 3.12。在環境中設定 `TWITCH_CLIENT_ID`、`TWITCH_CLIENT_SECRET`；`.env.example` 只列名稱，程式不會自動讀取 `.env`。

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m scripts.update_twitch --min-viewers 7000 --output output/twitch_live.json
```

| 參數 | 預設 | 用途 |
|---|---|---|
| `--min-viewers` | `7000` | 候選總觀眾數門檻，包含恰好達標 |
| `--max-category-pages` | `5` | 分類頁上限，每頁最多 100 類 |
| `--max-stream-pages` | `150` | 每類直播分頁上限，每頁最多 100 台 |
| `--max-api-calls` | `1200` | 基礎分類／直播掃描的 Helix 邏輯呼叫上限，不含 OAuth、重試、IGDB 與獨立追隨查詢 |
| `--max-collection-seconds` | `1500` | 整輪收集共用軟期限，預留工作保存／發布時間 |
| `--verification-registry` | 專案的驗證 JSON | 直接觀察紀錄檔 |
| `--tracking-state` | 未指定為獨立首次探索 | 持續追蹤名單 JSON；正式排程必須先取得已發布的狀態 |
| `--release-dates` | `data/twitch_release_dates.json` | 有來源與擷取時間的 Twitch 原始發售日期匯入檔 |
| `--no-release-hints` | 不啟用 | 加上此旗標即可略過 IGDB 及其 30 天收集篩選 |
| `--followers-cache` | `.cache/twitch_followers.json` | 追隨總數 24 小時快取，只保存在 runner／Actions cache |
| `--followers-max-calls` | 不限制 | 可選診斷限制；設 0 只使用有效快取，正式排程不設定 |
| `--followers-max-seconds` | 不另設限制 | 可選診斷限制；平常只受整輪收集期限約束 |
| `--no-filtered-audience` | 不啟用 | CLI 預設計算篩選指標；加上此旗標可略過，原指標不變 |

本機收集不需要發布 Token。只有執行 `bash scripts/publish_frontend.sh output/twitch_live.json` 才需要 `FRONTEND_REPO_TOKEN`。

## 來源

原始 Twitch 客戶端拆自 `danielet087/game-trend-radar-backend` 的 `86ff4fa9d15aa8c8b9756a102b5ea810241a1de6`；目前命令列改用新的候選流程，舊統計 helper 只保留相容性。未搬移舊 Repo 的 Secrets 或 Actions 紀錄。

- [Twitch Helix API：Get Top Games、Get Streams](https://dev.twitch.tv/docs/api/reference)
- [Twitch Helix API：Get Channel Followers（公開總數與授權差異）](https://dev.twitch.tv/docs/api/reference/#get-channel-followers)
- [IGDB API：認證與遊戲發售資料](https://api-docs.igdb.com/)
- [Glance：14 天條件的開源實作](https://github.com/glanceapp/glance/blob/372466c6d75318670dc66e4e452179350fc50c97/internal/glance/widget-twitch-top-games.go)
- [GitHub Actions Secrets](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets)

# Game Trend Radar — Twitch 獨立後端

找出總觀眾數達 **7,000 人**的遊戲候選，紀錄總觀眾數、開台實況主人數及**每台觀眾數的中位數**，並獨立管理 Twitch「全新」標記的驗證狀態。

## 執行狀態

**收集目前維持手動。** 到 Actions → Collect Twitch live data → Run workflow 執行。先確認新版候選收集的實際結果與用量，再啟用每小時排程。目前沒有 cron、push 或 workflow_run 收集觸發器；Test standalone collector 的 push CI 只跑離線測試，不查 API、不發布資料。

舊版收集器已成功連線及發布；這不代表新版候選流程已通過實際 API 測試。每次新版執行會在 Actions Summary 顯示候選數、待驗證數及 Helix 呼叫次數。

## 收集與驗證流程

1. 用 Helix `Get Top Games` 取得依觀眾數排列的分類。這個 API 沒有回傳總觀眾數，也沒有「全新」欄位。
2. 先略過非遊戲分類，以及仍在有效期內、曾直接確認沒有「全新」標記的分類，減少重複查詢。
3. 對剩餘分類逐一查詢 `Get Streams`，跟隨游標翻完直播分頁，以實況主 ID 去重，再計算總觀眾數與中位數。保留總觀眾數 **≥ 7,000** 的候選。
4. 有有效「全新」觀察紀錄的候選列為 `new`；其他候選列為 `pending`。待驗證候選同樣保留人數與歷史，避免等驗證完成才開始記錄。
5. 對待驗證候選批次查 IGDB 首次發售日期，用來排列驗證優先順序。近期發售、尚未發售與日期不明的候選優先；較早發售的候選排在後面。

**IGDB 發售日期不能證明 Twitch 的「全新」標記。** 例如本次目錄觀察中，Valheim 也有該標記。因此不以「上市超過 30 天」自動排除。IGDB 失敗、沒有 IGDB ID、或不在先前目錄樣本內，都維持待驗證。

### 直接觀察紀錄

`data/twitch_category_verification.json` 保存直接在 Twitch 目錄看見的標記結果。每筆包含 `status`、`observed_at`、`expires_at`、`source` 與 `source_url`，有效期最多 24 小時，過期即回到待驗證。

目前種子資料來自 **台灣時間 2026-09-29 00:11:31** 的目錄觀察：30 個分類，其中 6 個有「全新」標記、24 個沒有；僅代表當時實際載入的分類，並非完整目錄。種子資料在 **2026-09-30 00:11:31** 到期。

**目前沒有自動刷新這份標記紀錄的來源。** Twitch 網頁分頁實驗遇到 integrity check，未完成 7,000 人門檻的目錄掃描；本收集器不依賴該網頁爬取。後續可直接核對待驗證候選，在確認卡片標記後更新觀察紀錄；不能把未載入、查不到或缺少資料填成 `not_new`。`new` 與 `not_new` 都必須有直接觀察來源，不接受永久標記。

## 指標定義與範圍

| 欄位 | 意義 |
|---|---|
| `viewer_count` | 同一分類所有取得的直播台觀眾數總和 |
| `streamer_count` | 以 `user_id` 去重後的直播實況主人數，包含 0 觀眾台 |
| `median_viewer_count` | 所有這些台的觀眾數排序後的中位數；偶數台取中央兩個值的平均，並非全部台的平均觀眾數 |
| `verification.status` | `new`：有效的全新觀察；`pending`：尚未確認或觀察已過期 |
| `measurement_started_at` / `measurement_finished_at` | 該分類的分頁量測期間 |

每個候選都翻完直播分頁，沒有只取前 100 台。但直播人數與分類排序會在分頁期間變動；去重無法保證找回期間漏過的直播，因此不能稱為同一瞬間的完整普查。

分類探索會在目錄結束，或**整頁已量測分類都低於門檻**時停止。整頁都是已排除分類，或含跨頁重複分類時，不能作為門檻停止依據。這是利用 API 排名縮小探索範圍的策略，不保證掃描期間任何時刻曾超過門檻的遊戲都被捕捉；JSON 會記錄 `stop_reason` 與 `all_categories_enumerated`。

若達到分類頁數、直播頁數、API 呼叫上限，或發生 API 錯誤、重複游標與無效資料，會中止發布，保留前端上一版。IGDB 僅為輔助資訊，其失敗不會阻止人數資料發布。

Helix 的直播語言不等於主播所在地。目前提供 `language_streamers`，**不將中文直播當成台灣，也不推算亞洲地區**。

## 輸出與歷史

| 前端路徑 | 內容 |
|---|---|
| `data/twitch_live.json` | 最新 schema v2 候選、驗證狀態與收集範圍 |
| `data/twitch_history/YYYY-MM-DD.json` | 以台灣日期分檔、以 UTC 小時為索引的人數歷史 |

`candidate_games` 包含所有達標且未被排除的候選；`top_games` 只含已確認全新的候選；`pending_verification` 是依發售線索與觀眾數排序的待查 ID；`excluded_games` 記錄預先排除的分類。排除項目不再量測，所以不能解讀為也達到 7,000 人。

歷史保存候選的三項指標、量測時間及**當次**驗證狀態，不會事後把過去的 `pending` 改寫成已確認。同一小時重跑只接受較新的結果，跨小時保留；較晚完成的舊資料不會倒退最新檔。未出現在某小時的遊戲不會被填成 0 人；它可能低於門檻、被排除或未在當次探索範圍。

發布前取得最新前端，只提交上述 Twitch 路徑；遇到其他後端同時發布時，重新合併，最多重試 5 次，不使用 force push。未成功發布的結果不冒充前端已更新。

## Secrets

位置：Settings → Secrets and variables → Actions → Repository secrets。

| Secret | 用途 |
|---|---|
| `TWITCH_CLIENT_ID` | Twitch 應用程式 Client ID；IGDB 查詢沿用同一應用程式 |
| `TWITCH_CLIENT_SECRET` | 取得 Twitch app access token |
| `FRONTEND_REPO_TOKEN` | 寫入 `danielet087/game-trend-radar`；fine-grained token 僅選前端 Repo，Contents: Read and write |

本版沒有新增必填 Secrets，不需要 Steam 或 YouTube 憑證，也不讀取 Steam 清單。IGDB 無法使用時仍可收集 Twitch 指標。程式不附帶任何 Secret 值；內建 `GITHUB_TOKEN` 不能取代跨 Repo 的 `FRONTEND_REPO_TOKEN`。

缺少 Twitch 憑證時 workflow 明確跳過；缺少發布 Token 時只保存 2 天的 JSON artifact，前端不改動。

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
| `--max-api-calls` | `1200` | Helix 邏輯呼叫上限，不含 OAuth、重試與 IGDB |
| `--verification-registry` | 專案的驗證 JSON | 直接觀察紀錄檔 |
| `--no-release-hints` | 不啟用 | 加上此旗標即可略過 IGDB |

本機收集不需要發布 Token。只有執行 `bash scripts/publish_frontend.sh output/twitch_live.json` 才需要 `FRONTEND_REPO_TOKEN`。

## 來源

原始 Twitch 客戶端拆自 `danielet087/game-trend-radar-backend` 的 `86ff4fa9d15aa8c8b9756a102b5ea810241a1de6`；目前命令列改用新的候選流程，舊統計 helper 只保留相容性。未搬移舊 Repo 的 Secrets 或 Actions 紀錄。

- [Twitch Helix API：Get Top Games、Get Streams](https://dev.twitch.tv/docs/api/reference)
- [IGDB API：認證與遊戲發售資料](https://api-docs.igdb.com/)
- [GitHub Actions Secrets](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets)

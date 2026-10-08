# Twitch 後端分層與發布（第二、三批）

本次把收集入口、輸入狀態、HTTP、用例與執行摘要拆成可獨立呼叫的責任。既有 feature collectors 保留，不改 Twitch／Steam 收錄、IGDB／外部身分對照、日期窗口或人數計算。

| 責任 | 實作 | 邊界 |
|---|---|---|
| domain | `radar_backend/domain/collection.py`、`twitch.py`、`time.py` | 純請求資料、正式輸入要求、遊戲身分與時區／小時驗證；無檔案或網路 IO |
| application | `radar_backend/application/collect_twitch.py` | 注入 input store 與 collector；先取得完整輸入，才收集並附要求時段 |
| adapters | `radar_backend/adapters/twitch_http.py` | Twitch OAuth／Helix client、既有重試、請求間隔與期限 |
| state | `radar_backend/state/json_inputs.py`、`validation.py`、`json_snapshot.py` | JSON 讀取／驗證與本機快照寫入；損毀狀態不替換成空表 |
| jobs | `radar_backend/jobs/twitch.py`、`twitch_cli.py`、`twitch_report.py` | 參數、環境、憑證檢查、用例協調與文字摘要 |
| 既有 collector | `collectors/twitch_candidates.py` 及 tracking／audience／mapping 模組 | 繼續負責候選掃描、已收錄持續觀測、來源對照與統計規則 |
| publication | `radar_backend/publication/twitch.py`、`radar_core.publication` | 凍結收集輸出，對最新前端合併，限指定 JSON 的單一 commit，有限次重試並等待實際 push 確認 |
| publication job | `radar_backend/jobs/publish_twitch.py`、`scripts/publish_frontend.sh` | 憑證／CLI、暫存 clone、安全 Git adapter、成功後的發布回條；shell 僅保留相容入口 |

`scripts/update_twitch.py` 保留原 CLI，以及 `collect_twitch`、`write_json`、`build_parser` 等相容入口。它在呼叫 runner 時明確傳入當前的 collector／writer，因此既有 monkeypatch 不會落到未使用的匯入名稱。`collectors/twitch_live.py` 仍 re-export 同一個 `TwitchClient`、`TwitchGame`、期限錯誤與 JSON writer；沒有模組 alias，也沒有第二份 HTTP client 實作。既有 tracking loader／collection guard 的驗證函式繼續可從原路徑匯入。

application 可使用記憶體 input store 與假的 collector，不需環境變數、檔案或 API。正式 composition 則使用驗證 JSON 的 input store 與原 collector。所有 CLI 參數、預設 7,000 人初次收錄、API 次數、25 分鐘期限、followers 快取、來源聯集與 snapshot schema 維持。

本機寫出 snapshot 只代表收集輸出已保存。它不新增成功 receipt、不宣稱前端已發布；發布仍由 workflow 的後續步驟完成。同一來源 revision、正式持續觀測名單、Steam 清單、mapping 與 discovery 的缺失／損毀仍會停止正式收集。

## 第三批：版本與跨 repo 發布

application 先對已載入的 tracking、Steam catalog、mapping、discovery 四份輸入計算 canonical JSON 的 SHA-256 `input_revision`，再呼叫既有 collector。收集 output 保持 schema v2，僅追加這個 revision；沒有新增 API、資格規則或量測時間。

publication 在刷新目的地前只讀一次完整收集 JSON。每次 Git push 競爭失敗，都從最新 `origin/main` 重新呼叫既有 `store_snapshot`，沿用觀測排序、每日歷史、來源聯集與較新的 tracking／mapping／discovery 決策。latest、實際小時 history、tracking、mapping、discovery 與 collection status 在同一個 commit；不重查 API、不重算觀測時間、不覆蓋其他來源 JSON。延遲結果仍可保存其真實歷史及補回來源，但不能把最新 census 或時段回條倒退。同一觀測時間若已有不同 census，會停止發布，避免把新版本證明貼到未被接受的量測。

| 欄位 | 意義 | 保存位置 |
|---|---|---|
| `input_revision` | 收集前四份完整輸入的 canonical hash | 收集 snapshot、符合當前觀測的 collection status、發布 artifact |
| `payload_revision` | 本次凍結收集輸出的 canonical hash | 符合當前觀測的 collection status、發布 artifact |
| `target_snapshot_revision` | 共用發布器在已提交 HEAD 上讀取的實際 owned JSON presence／內容 hash，包含合併保留的較新資料 | 發布 artifact |
| `published_revision` | Git 已確認接受的實際 commit SHA | 發布 artifact，避免在 commit 內寫自身 SHA 的循環 |

舊 recovery snapshot 沒有 `input_revision` 時，以完整凍結 snapshot 的內容 hash 作回退，並明確標示 `input_kind=legacy_collected_snapshot`；不冒稱已知當年的四份輸入。新版標示 `collection_inputs`。現有 collection status 維持 schema v1 與全部既有時段／完成欄位，只追加 input／payload metadata；讀取端維持相容。

`output/twitch_publication.json` 只在實際 push 成功後產生，包含 typed `job_result`、提交 revision 與重試次數。沒有內容差異仍必須取得真實 push 確認；有限次拒絕、fetch／merge／commit 失敗均不得冒充發布成功。每次 recovery 先清除此路徑的舊回條，原收集 JSON 保留不動。workflow 追加短期回條 artifact，原觸發方式、concurrency、Secrets、收集期限及正常排程保留。`git_frontend_auth.sh` 繼續用 askpass，token 不放 URL、argv 或 Git config。

## 仍待拆分

- `collectors/twitch_candidates.py` 的分頁掃描、日期 metadata 查詢、來源 reconciliation 與 audience enrichment 仍在既有 feature 模組；本批先隔離入口與外部依賴，不搬動已驗證的資格邏輯。
- immutable frontend 載入的 HTTP 仍留在既有 input loader；同一次載入全部讀取相同固定 frontend commit，發布則使用共用 Git adapter。
- 本批未修改 IGDB console 的獨立發布入口、掃描邏輯或排程；共用 core 更新固定提交依賴，不增加外部 runtime 套件。

## 離線驗證

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m scripts.update_twitch --help
```

分層檢查覆蓋注入的用例、輸入損毀停止、正式 guard、HTTP 期限、舊 CLI 的 collector／writer patch、摘要保留及 domain 不依賴 IO。發布測試使用真正的本機 bare Git，驗證原子 JSON commit、被拒絕後重新合併較新輸入、no-op 的實際 push、過期結果不回寫最新時段、清除舊成功回條，以及損毀／重複 key／非有限值來源先於 Git 停止。CI 不呼叫正式 API。

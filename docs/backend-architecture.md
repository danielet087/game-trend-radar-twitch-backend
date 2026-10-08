# Twitch 後端分層（第二批）

本次把收集入口、輸入狀態、HTTP、用例與執行摘要拆成可獨立呼叫的責任。既有 feature collectors 保留，不改 Twitch／Steam 收錄、IGDB／外部身分對照、日期窗口或人數計算。

| 責任 | 實作 | 邊界 |
|---|---|---|
| domain | `radar_backend/domain/collection.py`、`twitch.py`、`time.py` | 純請求資料、正式輸入要求、遊戲身分與時區／小時驗證；無檔案或網路 IO |
| application | `radar_backend/application/collect_twitch.py` | 注入 input store 與 collector；先取得完整輸入，才收集並附要求時段 |
| adapters | `radar_backend/adapters/twitch_http.py` | Twitch OAuth／Helix client、既有重試、請求間隔與期限 |
| state | `radar_backend/state/json_inputs.py`、`validation.py`、`json_snapshot.py` | JSON 讀取／驗證與本機快照寫入；損毀狀態不替換成空表 |
| jobs | `radar_backend/jobs/twitch.py`、`twitch_cli.py`、`twitch_report.py` | 參數、環境、憑證檢查、用例協調與文字摘要 |
| 既有 collector | `collectors/twitch_candidates.py` 及 tracking／audience／mapping 模組 | 繼續負責候選掃描、已收錄持續觀測、來源對照與統計規則 |
| 既有 publication | `scripts/publish_frontend.sh` 與 history helpers | 保留跨 repo 發布、衝突重試與 receipt 行為；本批未搬遷 |

`scripts/update_twitch.py` 保留原 CLI，以及 `collect_twitch`、`write_json`、`build_parser` 等相容入口。它在呼叫 runner 時明確傳入當前的 collector／writer，因此既有 monkeypatch 不會落到未使用的匯入名稱。`collectors/twitch_live.py` 仍 re-export 同一個 `TwitchClient`、`TwitchGame`、期限錯誤與 JSON writer；沒有模組 alias，也沒有第二份 HTTP client 實作。既有 tracking loader／collection guard 的驗證函式繼續可從原路徑匯入。

application 可使用記憶體 input store 與假的 collector，不需環境變數、檔案或 API。正式 composition 則使用驗證 JSON 的 input store 與原 collector。所有 CLI 參數、預設 7,000 人初次收錄、API 次數、25 分鐘期限、followers 快取、來源聯集與 snapshot schema 維持。

本機寫出 snapshot 只代表收集輸出已保存。它不新增成功 receipt、不宣稱前端已發布；發布仍由原 workflow 的後續步驟完成。同一來源 revision、正式持續觀測名單、Steam 清單、mapping 與 discovery 的缺失／損毀仍會停止正式收集。

## 仍待拆分

- `collectors/twitch_candidates.py` 的分頁掃描、日期 metadata 查詢、來源 reconciliation 與 audience enrichment 仍在既有 feature 模組；本批先隔離入口與外部依賴，不搬動已驗證的資格邏輯。
- immutable frontend 載入與 publication 的 Git／HTTP 與 shell 協調仍留在既有 scripts，下一批再拆。快照不替代 durable publication 確認。
- `radar-core` 固定提交依賴與相容收錄 API 沿用第一批，不增加套件或修改版本。

## 離線驗證

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m scripts.update_twitch --help
```

新增檢查覆蓋注入的用例、輸入損毀停止、正式 guard、HTTP 期限、舊 CLI 的 collector／writer patch、摘要保留及 domain 不依賴 IO。CI 的 code path 增加 `radar_backend/**`；正常收集排程、production workflow、Secrets 與資料路徑均不調整。

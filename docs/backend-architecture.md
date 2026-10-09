# Twitch 後端分層與發布

第二、三批先把收集入口、輸入狀態、HTTP、用例與執行摘要拆成可獨立呼叫的責任，並加入共用 Git 發布。第十四批拆分收集、追蹤、audience 與固定前端輸入，第十五批完成 mapping、discovery、website identity 與 snapshot 的分層；Twitch／Steam 收錄、IGDB／外部身分對照、日期窗口及人數計算維持原契約。

| 責任 | 實作 | 邊界 |
|---|---|---|
| domain | `radar_backend/domain/collection.py`、`twitch.py`、`time.py` | 純請求資料、正式輸入要求、遊戲身分與時區／小時驗證；無檔案或網路 IO |
| application | `radar_backend/application/collect_twitch.py` | 注入 input store 與 collector；先取得完整輸入，才收集並附要求時段 |
| adapters | `radar_backend/adapters/twitch_http.py` | Twitch OAuth／Helix client、既有重試、請求間隔與期限 |
| state | `radar_backend/state/json_inputs.py`、`validation.py`、`json_snapshot.py` | JSON 讀取／驗證與本機快照寫入；損毀狀態不替換成空表 |
| jobs | `radar_backend/jobs/twitch.py`、`twitch_cli.py`、`twitch_report.py` | 參數、環境、憑證檢查、用例協調與文字摘要 |
| 相容入口 | `collectors/` 中的 feature 模組、`scripts/store_twitch_snapshot.py` | 薄 façade 注入原入口當下的 helpers，正式 composition 使用 `radar_backend/` owner |
| publication | `radar_backend/publication/twitch.py`、`radar_core.publication` | 凍結收集輸出，對最新前端合併，限指定 JSON 的單一 commit，有限次重試並等待實際 push 確認 |
| publication job | `radar_backend/jobs/publish_twitch.py`、`scripts/publish_frontend.sh` | 憑證／CLI、暫存 clone、安全 Git adapter、成功後的發布回條；shell 僅保留相容入口 |

`scripts/update_twitch.py` 保留原 CLI，以及 `collect_twitch`、`write_json`、`build_parser` 等相容入口。它在呼叫 runner 時明確傳入當前的 collector／writer，因此既有 monkeypatch 不會落到未使用的匯入名稱。`collectors/twitch_live.py` 仍 re-export 同一個 `TwitchClient`、`TwitchGame`、期限錯誤與 JSON writer；沒有模組 alias，也沒有第二份 HTTP client 實作。既有 tracking loader／collection guard 的驗證函式繼續可從原路徑匯入。

application 可使用記憶體 input store 與假的 collector，不需環境變數、檔案或 API。正式 composition 則使用驗證 JSON 的 input store 與原 collector。所有 CLI 參數、預設 7,000 人初次收錄、API 次數、25 分鐘期限、followers 快取、來源聯集與 snapshot schema 維持。

本機寫出 snapshot 只代表收集輸出已保存。它不新增成功 receipt、不宣稱前端已發布；發布仍由 workflow 的後續步驟完成。同一來源 revision、正式持續觀測名單、Steam 清單、mapping 與 discovery 的缺失／損毀仍會停止正式收集。

## 第三批：版本與跨 repo 發布

application 先對已載入的 tracking、Steam catalog、mapping、discovery 四份輸入計算 canonical JSON 的 SHA-256 `input_revision`，再呼叫既有 collector。收集 output 保持 schema v2，僅追加這個 revision；沒有新增 API、資格規則或量測時間。

publication 在刷新目的地前只讀一次完整收集 JSON。每次 Git push 競爭失敗，都從最新 `origin/main` 重新呼叫既有 `store_snapshot`，沿用觀測排序、每日歷史、來源聯集與較新的 tracking／mapping／discovery 決策。合併前會嚴格檢查所有現存發布 JSON（含完整歷史目錄），重複 key、非有限數值或 symlink 均先停止，避免重新寫檔把原資料損毀變成看似正常的輸出。latest、實際小時 history、tracking、mapping、discovery 與 collection status 在同一個 commit；不重查 API、不重算觀測時間、不覆蓋其他來源 JSON。延遲結果仍可保存其真實歷史及補回來源，但不能把最新 census 或時段回條倒退。同一觀測時間若已有不同 census，會停止發布，避免把新版本證明貼到未被接受的量測。

| 欄位 | 意義 | 保存位置 |
|---|---|---|
| `input_revision` | 收集前四份完整輸入的 canonical hash | 收集 snapshot、符合當前觀測的 collection status、發布 artifact |
| `payload_revision` | 本次凍結收集輸出的 canonical hash | 符合當前觀測的 collection status、發布 artifact |
| `target_snapshot_revision` | 共用發布器在已提交 HEAD 上讀取的實際 owned JSON presence／內容 hash，包含合併保留的較新資料 | 發布 artifact |
| `published_revision` | Git 已確認接受的實際 commit SHA | 發布 artifact，避免在 commit 內寫自身 SHA 的循環 |

舊 recovery snapshot 沒有 `input_revision` 時，以完整凍結 snapshot 的內容 hash 作回退，並明確標示 `input_kind=legacy_collected_snapshot`；不冒稱已知當年的四份輸入。新版標示 `collection_inputs`。現有 collection status 維持 schema v1 與全部既有時段／完成欄位，只追加 input／payload metadata；讀取端維持相容。

`output/twitch_publication.json` 只在實際 push 成功後產生，包含 typed `job_result`、提交 revision 與重試次數。沒有內容差異仍必須取得真實 push 確認；有限次拒絕、fetch／merge／commit 失敗均不得冒充發布成功。每次 recovery 先清除此路徑的舊回條，原收集 JSON 保留不動。workflow 追加短期回條 artifact，原觸發方式、concurrency、Secrets、收集期限及正常排程保留。`git_frontend_auth.sh` 繼續用 askpass，token 不放 URL、argv 或 Git config。

## 第十四批：收集、追蹤與固定前端輸入

| 責任 | 位置 |
| --- | --- |
| Badge 驗證、串流資料與 IGDB hints 列的純規則 | `domain/twitch_candidates.py` |
| Helix 預算與完整 census、候選／已追蹤觀測協調 | `application/twitch_candidates.py` |
| Registry JSON 讀取、IGDB hints HTTP 與 canonical 收集接線 | `state/twitch_candidates.py`、`adapters/igdb_release_hints.py`、`adapters/twitch_candidates.py` |
| Followers 快取資格與 audience 統計／排序規則 | `domain/twitch_audience.py` |
| 有時限 follower lookup、filtered metrics 與 finally 保存 | `application/twitch_audience.py` |
| Followers HTTP、runner cache 原子保存與接線 | `adapters/twitch_followers.py`、`state/twitch_followers.py`、`adapters/twitch_audience.py` |
| 來源成員聯集、收錄證據與追蹤期限 | `domain/twitch_tracking.py`、`adapters/twitch_tracking.py` |
| 分來源日期窗口、provenance、retrospective 比對 | `domain/twitch_newness.py` |
| Release-date JSON 讀取與時間／規則接線 | `state/twitch_newness.py`、`adapters/twitch_newness.py` |
| 固定 frontend commit 的四份輸入協調 | `application/frontend_inputs.py` |
| 原 Git HEAD／urllib 傳輸與輸入接線 | `adapters/frontend_input_http.py`、`adapters/frontend_inputs.py` |
| canonical CLI | `jobs/collect_twitch.py`、`jobs/load_frontend_inputs.py` |

上表路徑皆相對於 `radar_backend/`。Domain 不讀實際時鐘、HTTP 或檔案；application 使用明確的時鐘、來源、validator、census、resolver 與 cache ports；adapter 組裝原 TwitchClient、Core 與具體來源。原四個 feature collectors 與 tracking loader 保留薄相容入口，公開參數、預設綁定、class fields、當下 helper／HTTP／clock／logger 及錯誤範圍保持。新的兩個 CLI main 與原入口同 AST；正式 workflow 仍可使用原命令。

正式 CLI、input validation、尚未拆分的 mapping／discovery／website 與 snapshot／recovery 工具直接匯入新 owner。這些剩餘工具本批僅更換 import，不更改其來源判定、保存或 CLI body；既有具體 mapping／discovery 用明確 callbacks 接入收集用例。共用 publication 本體與 TwitchClient 沒有改動，仍在凍結輸入後向最新前端合併、有限重試並等待實際 push 回條。

Census 保留 1,500 秒共用期限、1,200 Helix 次數與所有既有分頁設定。PageReader 先查 deadline 再查 budget，請求後再次查 deadline；不完整分頁、重複 cursor、改變分類、非法人數或非 live stream 都會停止整輪輸出。類別以 ID 一輪只觀測一次；直播主以 ID 保存最新列並只計一次，measurement 起訖仍來自真實收集時鐘。Metadata 不明或失敗仍保持 unknown，不能藉此排除分類；排除頁與重複頁不會誤判為觀看門檻邊界。

初次 Twitch 收錄保留 7,000 人門檻與既有證據，已收錄成員則獨立持續觀測，排行榜缺席、觀看下降或 badge 消失不會移除有效追蹤。Twitch 與每個 Steam AppID 的來源成員按 Twitch ID 聯集；只要一個來源仍有效就保持 active。Steam 未上市不能提前成為近期上市成員，Twitch 的未上市證據可保持原追蹤；IGDB 日期仍優先於較弱的 Twitch 日期，30 天期限、永久排除、catalog 移除與 mapping 變更規則保持。真實完整零人數 census 可保存，失敗 census 不偽裝成零，也不替換先前觀測。

Followers 只查 public total，使用既有 app token 與同一 client。快取有效期保留 24 小時半開區間；未知 total 單輪不重試、超過 1,000 Followers 且至少 10 viewers 才進 filtered audience，有任何未知值則 median 保持空值。每 50 個成功查詢保存一次 cache，結尾及例外路徑仍 finally 保存；次數／時間／collection deadline、三次連續失敗、429／401／403 停止與已快取結果可在停止後使用的行為保持。Cache 的 JSON 格式、tempfile、atomic replace、清理與 IO 容錯範圍不變。

Newness 日期試驗保留 Twitch originalReleaseDate 14 天、IGDB 30 天與 metadata 24 小時窗口，涵蓋未上市日期，並保持其無法證實官方 NEW badge 的來源標記。已用於收集的 IGDB 決定會被重用，避免跨 30 天邊界造成接受觀測與報告矛盾；retrospective 比對仍以 badge 的觀測時間評估，不冒稱同時觀測。

Frontend loader 每輪只取得一次 HEAD，再按 tracking → Steam catalog → mapping → discovery 讀同一 immutable commit；catalog 先驗證才繼續。只有 mapping／discovery 的 HTTP 404 可建立原 bootstrap 空表，required 檔案缺失、其他 HTTP 錯誤、損毀 JSON 或無效狀態會停止。原 headers、20／30 秒 timeout、commit 驗證、來源標記與例外順序保持。

本批接在 Twitch 第三批 PR #3，並保留其他後端相依 PR 的既有順序。四個 workflows、scheduler、Secrets、requirements、Core 0.2.0 immutable SHA `bf1d4bc64b361ec35cd4041d78c5016396d5d785`、data 與 publication 本體均不變。本次離線驗證包含原／新公開 API、完整 fake API 收集、blocked legacy imports、固定快照輸入、來源不可變、實際 bare Git 出版及乾淨 checkout。

第十四批結束時估計剩餘 3～4 次；第十五批完成下列 Twitch 身分與保存拆分後，剩餘 Content 共用 metadata／文字規則及四個 consumer 總驗收，預留一次修正，估計再 2～3 次。完成界線沿用原四 consumer，前端 UI、IGDB 獨立後端及全面重寫退役工具另列範圍。

## 第十五批：來源身分與快照保存

| 責任 | 位置 |
| --- | --- |
| Steam catalog／mapping 驗證、台灣日期資格與反向 cache 的純規則 | `domain/steam_twitch_mapping.py` |
| Steam → IGDB → Helix 的 refresh、批次確認與 metadata-only 協調 | `application/steam_twitch_mapping.py` |
| IGDB request／分頁／deadline 與 mapping 接線 | `adapters/igdb_identity.py`、`adapters/steam_twitch_mapping.py` |
| Twitch discovery registry、資格、來源與身分證據驗證 | `domain/twitch_steam_discovery.py` |
| 反向 ID discovery、缺失 Helix IGDB ID 的官方 fallback 與 website 查詢協調 | `application/twitch_steam_discovery.py`、`adapters/twitch_steam_discovery.py` |
| Steam URL、確切商店日期、website proof 與 metadata 驗證 | `domain/twitch_steam_website_identity.py` |
| IGDB game／website ownership 與 Steam metadata 查詢協調 | `application/twitch_steam_website_identity.py` |
| 固定 Steam appdetails HTTP 與 website 接線 | `adapters/steam_identity_http.py`、`adapters/twitch_steam_website_identity.py` |
| 三 registry 合併、census／schedule／receipt 驗證與 history 投影 | `domain/twitch_snapshot.py` |
| 保持順序的本機快照讀寫與接線 | `state/twitch_snapshot.py`、`adapters/twitch_snapshot.py` |
| canonical 保存 CLI | `jobs/store_twitch_snapshot.py` |

上表路徑皆相對於 `radar_backend/`。純規則不讀 HTTP、檔案或實際時鐘；流程以明確 callbacks 接入 clock、API、normalizers、proof 與錯誤類型。原三個 identity collectors 與 snapshot script 保留同簽名的薄相容入口及當下 globals；正式 collection、frontend inputs、persisted validation、metadata reconciliation 與 publication 直接使用 canonical owner。Core admission 函式直接 re-export 同一物件，不新增規則副本。

映射仍只接受 Steam AppID → 官方 IGDB external source／game → Helix IGDB ID 的鏈結，不使用名稱猜測。確定映射重用、每日負向決定重試、Taipei 日界與 30 天窗口保持；metadata-only refresh 不開啟 HTTP，也不改變觀眾量測。反向 discovery 只處理真實 Twitch 收錄來源，保留多個官方 Steam AppID、24 小時 cache、缺失 Helix IGDB ID 的官方 fallback 與既有排除；website proof 不能冒稱 external-games 鏈結或公開 catalog 成員。

Website 只使用已驗證的 IGDB game／website ownership 及固定 Steam appdetails endpoint。URL／AppID／proof 必須一致；不跟隨任意 URL、redirect，不把失敗或不確定日期當成成功證據。HTTP params、headers、timeout、deadline 檢查點、錯誤範圍與先後順序保持。

Snapshot 在原讀檔及完整驗證順序後保存真實小時 history、latest、tracking、mapping、discovery 與時段 status；同一來源的較新決定及獨立來源聯集保持。延遲 census 可補歷史及來源證據，不倒退 latest／receipt，不改量測時間。History 永久累積，沒有新增清理期限；本機保存不代表 Git 已確認發布。共用 Git engine、凍結輸入、push 競爭重合併與成功回條本體均不變。

本批接在 Twitch 第十四批 PR #4。既有 workflows、排程、Secrets、Core immutable revision、requirements 與 data 保持；離線測試涵蓋原／新 API、正式 collection → identity → snapshot、無 API 的 metadata refresh、阻擋舊 owners 的完整 import graph、canonical CLI、獨立原 source 語意 oracle、精確檔案 checkout 與實際 bare Git 發布。

## 完成界線與保留入口

- Twitch 正式收集、來源身分、固定輸入與快照保存已分層；Content 共用 metadata／文字規則已於第十六批完成，第十七批已進行四個 consumer 總驗收。
- 固定 frontend 載入已使用新 application／HTTP／composition；collection guard 的獨立時段檢查保持原入口，發布繼續使用共用 Git adapter。
- 本批未修改 IGDB console 的獨立發布入口、掃描邏輯或排程；共用 core 更新固定提交依賴，不增加外部 runtime 套件。

## 第十七批：正式 workflow 入口驗收

`collect.yml` 實際呼叫的 `scripts.load_twitch_tracking.load_published_inputs` 原先仍動態匯入 legacy mapping collector；本批僅將該 import 改接 `radar_backend.adapters.steam_twitch_mapping`，其餘來源載入、CLI、公開參數、當下 globals、驗證順序與錯誤範圍保持。新增十七項 fresh-process 回歸，阻擋整個舊 collectors namespace 後，仍以同一 immutable frontend commit 讀取四份真實 local JSON、執行 canonical normalization／validators 並寫出原 CLI outputs。

修後完整 Python suite 為 1,247 項；既有 scheduler 77 項、workerd 2 項與 shell 語法驗證通過。Core 0.2.0 為固定非 editable 安裝，五份 Python source bytes 與 immutable revision `bf1d4bc64b361ec35cd4041d78c5016396d5d785` 相同。正式 workflows 使用完整 checkout；本批未更改 workflows、scheduler、來源資格、API 預算、Secrets 或公開資料。四 consumer 的驗收與先合併 Core、前端時鐘支援，再依各相依 PR 順序合併的程序記錄於 Steam repository 的 `docs/backend-acceptance.md`。

## 離線驗證

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m scripts.update_twitch --help
```

分層檢查覆蓋注入的用例、輸入損毀停止、正式 guard、HTTP 期限、舊 CLI 的 collector／writer patch、摘要保留及 domain 不依賴 IO。發布測試使用真正的本機 bare Git，驗證原子 JSON commit、被拒絕後重新合併較新輸入、no-op 的實際 push、過期結果不回寫最新時段、清除舊成功回條，以及損毀／重複 key／非有限值來源先於 Git 停止。CI 不呼叫正式 API。

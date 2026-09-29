# 14 天日期規則：第一輪查核

日期：2026-09-29。這是來源與規則可行性的實驗紀錄，**不是線上收集成功報告**。

## 已接入的實驗

- 採用 Glance `now - originalReleaseDate < 14 days` 的條件，滿 14 天不符合，未來日期另標示尚未發售。
- Twitch 原始日期與 IGDB 首次發售日期分開輸出。缺少 Twitch 原始日期時為未知，不使用 IGDB 偷補。
- 官方標記觀察仍保留獨立狀態；推算不改寫它，也不把推算不符合的遊戲刪除。
- 有效官方觀察與推算可逐款對照；資料擷取時間不同時標為回溯比較，不能當成正式準確率。
- 實驗結果隨收集輸出與每小時歷史保存。

## 目前可核對的真實樣本

官方標記樣本來自 `data/twitch_category_verification.json`：台灣時間 2026-09-29 00:11:31 的目錄直接觀察。下表四款當時都有「全新」標記。

以下日期來自獨立資料頁，**尚不是 Twitch 的 `originalReleaseDate`**。僅按台灣觀察日與頁面列示日期做日曆日差比較，並非精確到時區／秒的 API 實跑，也沒有把頁面日期寫進 Twitch 原始日期匯入檔。

| Twitch ID | 遊戲 | 頁面列示發售日期 | 日曆日差 | 14 天近似判定 | 與先前 NEW 觀察 |
|---|---|---|---|---|---|
| 123184772 | EA Sports FC 27 | 2026-09-25 | 4 | 符合 | 相符 |
| 1960720223 | SILENT HILL: townfall | 2026-09-24 | 5 | 符合 | 相符 |
| 1267582829 | Aniimo | 2026-09-16 | 13 | 符合 | 相符 |
| 508455 | Valheim | 2026-09-09 | 20 | 不符合 | 不相符 |

前三款來源：[BadgeNews Twitch Categories](https://www.badgenews.com/twitch/categories/)，頁面明示其遊戲資料使用 IGDB。Valheim 來源：[IGDB Press Kit](https://www.igdb.com/games/valheim/presskit)，查核時列示 Sep 09, 2026。

這個小樣本只包含先前的正例，且日期來源／擷取時間不同，**不能宣稱準確率 75%**。它顯示直接換成其他來源的日期會發生不一致，仍需 Twitch 原始日期才能測試 Glance 的實際條件。也不能從一款不一致便認定官方實際規則是 30 天。

## 尚缺與下一次執行

目前 `data/twitch_release_dates.json` 的 records 為空；未取得完整且可核對的 Twitch 日期資料。收集器沒有新增 Twitch 網頁爬蟲、代理或私有 GraphQL 請求。

下一次手動 Collect Twitch live data 執行時：

1. 用現有 Secrets 收集 ≥ 7,000 人的候選與三項指標。
2. 使用官方 IGDB API 做平行日期試算；失敗保留未知。
3. 有提供 Twitch 日期匯出時另外評估；沒有就明確報告 0 款可判定。
4. 把結果列在 Actions Summary、JSON 與歷史檔中。官方標記樣本過期後不再當成當下已確認。

本輪也修正前次實跑的停止條件：跨頁出現重複分類時重新量測，以整頁的量測結果判斷是否全都低於門檻；不再因一個重複 ID 導致後續頁面永遠不能停止。重複分類仍高於門檻、整頁只有重複／排除分類時則繼續掃描。API 預算與完整性保護維持原設定。

規則來源：[Glance 原始碼（固定版本）](https://github.com/glanceapp/glance/blob/372466c6d75318670dc66e4e452179350fc50c97/internal/glance/widget-twitch-top-games.go)。

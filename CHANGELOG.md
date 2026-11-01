# 變更紀錄

## 2026-11-01

### 修正

- 支援官網主入口以 HTML `meta refresh` 導向 `whotel001`、`whotel002` 等分流站。
- 瀏覽器會等待分流完成，登入驗證、Standby 官網校時及訂房操作會固定使用同一分流站。
- 唯讀 HTTP 預演會解析分流頁並沿用該站的 Cookie；非武陵官方 HTTPS 網域或異常路徑一律拒絕。

### 驗證

- 訂房與助手測試共 36 項通過。
- 實站唯讀連線經分流至 `whotel001` 後，成功取得 2026 年 11 月日曆。

## 2026-10-30

### 新增

- 正式訂房可設定 `booking_open_at`，提前排入工作並在開放前進入 Standby。
- 以官網 HTTPS `Date` 標頭及最低網路往返時間樣本校準網站時鐘。
- 開放前預先切換到目標月份；未到校準後的開放時間不會點選日期。
- Standby 刷新採 1500 ms 基準加 0–500 ms 隨機抖動；遇到 HTTP 403、429、503 或網站要求放慢時，自動倍增退避至最多 5 秒。
- 工作紀錄新增排程階段、網站時差、RTT、刷新次數、退避次數與日期出現時間。

### 設定

- `Secret\Secret.ini` 統一保存登入與排程設定：
  - `Phone`
  - `pwd`
  - `booking_open_at`
  - `booking_standby_seconds`
  - `booking_refresh_milliseconds`
- 命令列參數仍可覆寫設定檔；Live 訂房一般只需指定人數與日期。

### 修正

- 所有 PowerShell 腳本轉為 UTF-8 BOM，避免 Windows PowerShell 5.1 將中文誤判為 Big5 而產生 ParserError。
- 修正 Rehearsal 工作沒有 `open_at` 時，`-Wait` 在 StrictMode 下讀取不存在屬性而中止。
- 排程工作在尚未到達準備時間時不會阻塞已可執行的狀態工作。
- Standby 階段中斷可安全重新排隊；進入實際訂房階段後仍維持禁止自動重送的安全策略。

### 驗證

- Windows PowerShell 5.1 與 PowerShell 7 逐檔解析通過。
- 訂房、排程、網站時鐘、限流退避與本機 API 測試共 33 項通過。
- 以一次性本機假 API 驗證簡化命令可正確從 `Secret.ini` 帶入兩個日期及全部排程參數，未建立真實訂單。

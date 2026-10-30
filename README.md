# MH-WuLing

武陵國民賓館訂房輔助工具。現在的建議用法是先啟動一個常駐訂房助手，讓它持有同一個可見 Chromium 與登入狀態，再從另一個 PowerShell 視窗提交預演或正式訂房工作。

> [!WARNING]
> `-Mode Live` 可能送出真實訂單。網站回報成功仍須至官網核對；若程式顯示「送單結果不明」，先查詢官網訂單，絕對不要直接重送。

## 使用方式速覽

需要同時使用兩個 PowerShell 視窗：

- 視窗 A：啟動助手並保持開啟，瀏覽器也不要關閉。
- 視窗 B：確認登入、提交工作、查詢結果或停止助手。

助手只有一個瀏覽器工作者。多筆工作可快速加入，但一定按照先進先出順序逐筆執行，不會同時操作網站。

後續工作會保留目前登入頁面，直接點選「日期房型」與目標日期，不會每次重新載入首頁；只有找不到共用導覽列時才回首頁復原。官網操作之間會隨機等待 0.2–0.9 秒。預演只讀取房型與庫存，正式模式才會在重新核對後點選可訂房型。

## 第一次安裝

需求：Windows、PowerShell，以及 Python 3.11 以上版本。

在專案根目錄執行：

```powershell
.\PS\Setup.ps1
```

這會在專案內建立 `.venv`、安裝所需 Python 套件及 Playwright 自帶的 Chromium，不需要另外設定 Google Chrome 路徑。

接著建立本機設定：

```powershell
Copy-Item .\Secret\Secret.example.ini .\Secret\Secret.ini
```

在 `Secret\Secret.ini` 集中填入登入與搶訂時間設定：

```ini
Phone=你的電話
pwd=你的密碼
booking_open_at=2026-11-01 08:30:00 +08:00
booking_standby_seconds=15
booking_refresh_milliseconds=1500
```

`Phone` 與 `pwd` 可讓助手嘗試自動登入；未填或自動登入失敗時，也可以直接在助手開啟的瀏覽器中手動登入。請勿提交 `Secret.ini`、密碼、Session、瀏覽器資料或訂房紀錄。

## 標準操作流程

### 1. 啟動常駐助手

在視窗 A 執行：

```powershell
.\PS\Start-WulingAssistant.ps1
```

助手會開啟可見的 Chromium，並嘗試確認既有登入或使用本機設定登入。請保持這個 PowerShell 視窗與瀏覽器開啟。

若預設連接埠 `18765` 被其他無關程式占用，且確認沒有另一個訂房助手正在執行，才可指定其他連接埠：

```powershell
.\PS\Start-WulingAssistant.ps1 -Port 18766
```

同一時間只能啟動一個助手，因為所有工作共用同一份瀏覽器 Profile。看到「助手已在執行」或「Profile 正被使用」時，請回到原本的助手視窗，不要用其他 Port 再啟動一份。

### 2. 確認登入

若助手顯示自動登入未成功，請先在開啟的瀏覽器中手動登入，再到視窗 B 執行：

```powershell
.\PS\Invoke-WulingAssistant.ps1 -Action Status
```

看到「網站已確認登入」後，訂房佇列才會執行。這個命令會由持有瀏覽器的主執行緒重新向網站驗證，不只是讀取舊的本機狀態。

### 3. 先執行預演

預演會檢查指定日期的庫存並選出網站排列中第一個符合人數且可訂的房型，不會加入購物車或送出訂單：

```powershell
.\PS\Invoke-WulingAssistant.ps1 `
    -Mode Rehearsal -People 4 -Date 20270129 -Wait
```

一次檢查多個日期：

```powershell
.\PS\Invoke-WulingAssistant.ps1 `
    -Mode Rehearsal -People 4 `
    -Date 20270129,20270130,20270131 -Wait
```

日期格式固定為 `YYYYMMDD`；`People` 可接受 1–10。它用來比對房型名稱中的入住人數，不會任意改變網站房型容量。

省略 `-Wait` 時，命令會在工作成功加入佇列後立即回傳 `JobId`；加上 `-Wait` 則等待該工作完成並顯示結果。

`-Wait` 等待期間不會持續顯示進度。若畫面長時間空白，請在另一個 PowerShell 視窗執行 `-Action Jobs`；若助手因登入驗證暫停，重新確認瀏覽器已登入後執行 `-Action Status`。在等待視窗按 `Ctrl+C` 只會結束本機等待，不會自動取消已提交的工作。

### 4. 正式訂房

確認日期、人數及預演結果後，正式送出單一工作：

```powershell
.\PS\Invoke-WulingAssistant.ps1 `
    -Mode Live -People 4 -Date 20270129 -Wait
```

`Live` 會在每筆工作開始、加入房型前及最後送單前重新確認登入。它只會在能核對目標日期、選定房型且結帳頁中只有一個房間時嘗試送出；任何核對失敗都會停止該工作。

#### 開放時間 Standby

若官網在指定時間才釋出日期，可提前排程，不要等開放後才輸入命令。例如官網於 2026-11-01 上午 08:30（台灣時間）開放：

```powershell
.\PS\Invoke-WulingAssistant.ps1 `
    -Mode Live -People 2 -Date 20261230,20261231 -Wait
```

Live 模式預設讀取 `Secret.ini` 的 `booking_open_at`、`booking_standby_seconds` 與 `booking_refresh_milliseconds`。命令列的 `-OpenAt`（別名 `-BookingTime`）、`-StandbySeconds`、`-RefreshMilliseconds` 可覆寫個別設定；明確指定 `-OpenAt ""` 可略過設定檔時間並立即執行。`StandbySeconds` 限定為 10–20 秒；`RefreshMilliseconds` 限定為 1000–5000 毫秒。刷新會在基準值上隨機增加 0–500 毫秒，避免固定頻率；遇到 HTTP 403、429、503 或網站要求放慢時會自動倍增退避，最多 5 秒。助手會在開放前先確認登入、以官網 HTTPS 回應的 `Date` 標頭和網路往返中點校準網站時間，並把日曆預先切到目標月份。進入最後 Standby 窗口後只刷新日曆，不會提早選日期；校準後的網站時間到達 `OpenAt` 且日期按鈕出現，才會選擇日期、核對房型並沿用既有 Live 安全流程送單。

多個日期仍依佇列逐筆處理，不會同時送出。第一筆成功暫時保留後，付款頁籤會留在前景，助手再於背景頁籤處理下一筆。排程工作可提前加入；`-Wait` 的逾時會從 `OpenAt` 起算，因此可跨日等待。修改助手程式後必須安全停止並重新啟動，執行中的舊程序不會自動載入新版本。

結果判讀：

| 顯示結果 | 意義與處理方式 |
| --- | --- |
| `售罄` | 沒有符合人數的可訂房型，未送單。 |
| `預演可訂` | 找到可訂房型，未送單。 |
| `已有送出或結果未明的紀錄` | 重複訂單防護已阻擋操作，先查官網訂單。 |
| `結帳頁尚未可送出` | 未送單，查看瀏覽器確認網站要求。 |
| `無法核對結帳內容` | 未送單，日期、房型或數量未通過安全檢查。 |
| `目標日期目前不在可選日曆中` | 未送單；網站目前顯示的日曆沒有該日期。 |
| `訂房任務成功：房間已暫時保留` | 助手任務已成功完成；付款分頁留在前景供人工處理，助手在同一個 Chrome 的新分頁繼續下一筆工作。 |
| `網站回報成功` | 已嘗試送單；仍須到官網核對訂單內容與付款期限。 |
| `送單結果不明` | 可能已送出；正式佇列會暫停，先查官網，勿重送。 |

### 5. 查詢、取消與停止

列出最近工作：

```powershell
.\PS\Invoke-WulingAssistant.ps1 -Action Jobs
```

查詢指定工作：

```powershell
.\PS\Invoke-WulingAssistant.ps1 `
    -Action Status -JobId JOB-20261030-120000-1234ABCD
```

只有尚未開始的工作可以取消：

```powershell
.\PS\Invoke-WulingAssistant.ps1 `
    -Action Cancel -JobId JOB-20261030-120000-1234ABCD
```

安全停止助手：

```powershell
.\PS\Invoke-WulingAssistant.ps1 -Action Stop
```

`Stop` 不會中斷正在執行的工作；助手會等目前工作結束後才關閉瀏覽器。不要直接關閉瀏覽器或視窗 A，尤其不要在正式訂房執行中強制終止。

## 暫停與異常復原

### 登入失效

登入失效或無法確認時，訂房工作會留在佇列並暫停。請在助手的瀏覽器中重新登入，再執行：

```powershell
.\PS\Invoke-WulingAssistant.ps1 -Action Status
```

確認登入成功後，因登入問題而暫停的訂房佇列會繼續執行。

### 送單結果不明

若顯示「送單結果不明」，或助手在正式工作執行期間意外中斷：

1. 先到武陵國民賓館官網查詢訂單。
2. 確認是否已有相同日期及人數的訂單。
3. 只有在人工核對完成後，才解除暫停：

```powershell
.\PS\Invoke-WulingAssistant.ps1 `
    -Action Resume -AcknowledgeUnknownSubmission
```

`Status` 登入檢查不會解除「送單結果不明」造成的暫停。

### 暫時保留、等待付款

若網站顯示「暫時保留」，即代表訂房助手的任務成功。原付款分頁會留在前景，供使用者依期限人工完成信用卡訂金付款；助手在同一個 Chrome 另開共用登入 Session 的新分頁，若佇列仍有工作便立即處理下一筆，不需人工解除暫停。

### 助手重新啟動

尚未執行的工作保存在 `.wuling-assistant\jobs\`，下次啟動時會繼續排隊。意外中斷的預演工作可安全重新排隊；執行中的正式工作不會自動重試，而會標記為結果不明並暫停正式佇列。

## 安全與本機資料

- 本機命令服務只綁定 `127.0.0.1`，不對區域網路開放。
- CLI 依靠每次啟動隨機產生的權杖與助手通信。
- `.wuling-browser-profile\` 保存專用瀏覽器資料，可能包含有效登入資訊。
- `.wuling-assistant\` 保存執行狀態與工作紀錄。
- `LOG\booking-*.json` 與 `Secret\booking-guards\` 用來阻擋相同日期、人數的重複送單。
- 防護標記只表示曾開始或可能完成送單，不等於已確認訂房成功。
- 網站可隨時讓 Session 失效，HTML 改版也可能讓安全檢查停止操作。

上述敏感或執行期資料已列入 `.gitignore`，仍應限制本機檔案存取權限。

## 其他工具

以下是診斷或舊流程工具，不是目前正式訂房的主要入口。

驗證碼 OCR：

```powershell
.\PS\Get-WhotelCaptcha.ps1
.\PS\Get-WhotelCaptcha.ps1 `
    -InputPath .\IMAGE\whotel-captcha.png -Json
```

OCR 只有在多種前處理結果達成共識、字元數與信心均通過門檻時才接受；否則應人工輸入。需要觀察頁面時可加 `-Headed`。

舊式 Session 檢查與人工候選順序管理：

```powershell
.\PS\Check-Session.ps1
.\PS\Run-OrderSeq.ps1 -Command start
.\PS\Run-OrderSeq.ps1 -Command status
```

一次性 `Invoke-WulingBooking.ps1 -Mode Live` 已停用；正式訂房必須使用常駐助手。該腳本只保留一次性預演與既有訂單驗證用途。

## 測試

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

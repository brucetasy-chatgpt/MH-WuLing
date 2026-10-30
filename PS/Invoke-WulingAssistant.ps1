param(
    [ValidateSet("Status", "Jobs", "Cancel", "Stop", "Resume")]
    [string]$Action,

    [ValidateSet("Rehearsal", "Live")]
    [string]$Mode,

    [ValidateRange(1, 10)]
    [int]$People,

    [string[]]$Date,

    [Alias("BookingTime")]
    [string]$OpenAt,

    [ValidateRange(10, 20)]
    [int]$StandbySeconds = 15,

    [ValidateRange(1000, 5000)]
    [int]$RefreshMilliseconds = 1500,

    [string]$JobId,

    [switch]$Wait,

    [ValidateRange(1, 86400)]
    [int]$TimeoutSeconds = 600,

    [switch]$Json,

    [switch]$AcknowledgeUnknownSubmission
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimePath = Join-Path $projectRoot ".wuling-assistant\runtime.json"
$configPath = Join-Path $projectRoot "Secret\Secret.ini"

function Get-AssistantBookingConfig {
    param([Parameter(Mandatory = $true)][string]$Path)

    $values = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $values }
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith(";") -or
            $trimmed.StartsWith("#") -or -not $trimmed.Contains("=")) {
            continue
        }
        $parts = $trimmed.Split("=", 2)
        $values[$parts[0].Trim().ToLowerInvariant()] = $parts[1].Trim()
    }
    return $values
}

if (-not (Test-Path -LiteralPath $runtimePath)) {
    throw "訂房助手尚未啟動。請先執行 .\PS\Start-WulingAssistant.ps1。"
}

try {
    $runtime = Get-Content -LiteralPath $runtimePath -Raw -Encoding UTF8 | ConvertFrom-Json
} catch {
    throw "無法讀取訂房助手的執行狀態；請確認助手是否正常啟動。"
}

if (-not $runtime.token -or -not $runtime.port -or -not $runtime.pid) {
    throw "訂房助手的執行狀態不完整。"
}
if (-not (Get-Process -Id ([int]$runtime.pid) -ErrorAction SilentlyContinue)) {
    throw "訂房助手已停止，但留下舊的執行狀態；請重新啟動助手。"
}

$baseUri = "http://127.0.0.1:$([int]$runtime.port)"
$headers = @{ "X-Wuling-Token" = [string]$runtime.token }

function Invoke-AssistantApi {
    param(
        [Parameter(Mandatory = $true)][string]$Method,
        [Parameter(Mandatory = $true)][string]$Path,
        [object]$Body
    )
    $parameters = @{
        Uri        = "$baseUri$Path"
        Method     = $Method
        Headers    = $headers
        TimeoutSec = 10
    }
    if ($null -ne $Body) {
        $parameters.ContentType = "application/json; charset=utf-8"
        $parameters.Body = $Body | ConvertTo-Json -Compress -Depth 8
    }
    try {
        return Invoke-RestMethod @parameters
    } catch {
        $message = $_.ErrorDetails.Message
        if ($message) {
            try {
                $detail = $message | ConvertFrom-Json
                if ($detail.error) { throw [System.InvalidOperationException]::new([string]$detail.error) }
            } catch [System.InvalidOperationException] {
                throw
            } catch {
                # Fall through to the transport error below.
            }
        }
        throw "無法連線到訂房助手：$($_.Exception.Message)"
    }
}

function Get-AssistantJob {
    param([Parameter(Mandatory = $true)][string]$Id)
    return Invoke-AssistantApi -Method Get -Path "/v1/jobs/$($Id.ToUpperInvariant())"
}

function Wait-AssistantJob {
    param([Parameter(Mandatory = $true)][string]$Id)
    $deadline = [DateTimeOffset]::Now.AddSeconds($TimeoutSeconds)
    $initial = Get-AssistantJob -Id $Id
    if ($initial.payload.open_at) {
        $scheduledOpen = [DateTimeOffset]::Parse([string]$initial.payload.open_at)
        $scheduledDeadline = $scheduledOpen.AddSeconds($TimeoutSeconds)
        if ($scheduledDeadline -gt $deadline) { $deadline = $scheduledDeadline }
    }
    while ([DateTimeOffset]::Now -lt $deadline) {
        $job = Get-AssistantJob -Id $Id
        if ($job.state -in @("completed", "failed", "cancelled", "interrupted")) {
            return $job
        }
        Start-Sleep -Milliseconds 250
    }
    throw "等待工作 $Id 超過 $TimeoutSeconds 秒；工作仍會在助手中繼續執行。"
}

function Write-AssistantJobResult {
    param([Parameter(Mandatory = $true)][object]$Job)
    if ($Json) {
        $Job | ConvertTo-Json -Depth 12
        return
    }
    if ($Job.state -eq "queued") {
        if ($Job.payload.open_at) {
            Write-Output "$($Job.id) 已排程，將於 $($Job.payload.open_at) 開放前進入 Standby"
        } else {
            Write-Output "$($Job.id) 已加入佇列"
        }
        return
    }
    if ($Job.state -eq "running") {
        Write-Output "$($Job.id) 執行中（$($Job.phase)）"
        return
    }
    $status = [string]$Job.result.status
    switch ($status) {
        "authenticated" { Write-Output "網站已確認登入。" }
        "login_required" { Write-Output "登入失效或無法確認；訂房佇列已暫停。" }
        "sold_out" { Write-Output "售罄" }
        "rehearsal_ready" {
            $room = $Job.result.selected
            Write-Output "預演可訂：$($room.name)，NT`$$($room.price) 起，尚餘 $($room.remaining) 間。"
        }
        "previous_submission" { Write-Output "已有送出或結果未明的紀錄，未重複下訂。" }
        "checkout_blocked" { Write-Output "結帳頁尚未可送出；未送出訂單。" }
        "checkout_unverified" { Write-Output "無法核對結帳內容；未送出訂單。" }
        "held_pending_payment" { Write-Output "訂房任務成功：房間已暫時保留；請人工完成付款，助手將繼續下一筆工作。" }
        "submit_unknown" { Write-Output "送單結果不明；正式訂房佇列已暫停，請先核對官網訂單。" }
        "booked_unverified" {
            if ($Job.result.order_number) {
                Write-Output "網站回報成功，訂單編號：$($Job.result.order_number)；請至官網核對。"
            } else {
                Write-Output "網站回報成功；請至官網核對訂單。"
            }
        }
        "cancelled" { Write-Output "$($Job.id) 已取消" }
        default {
            if ($Job.error) {
                Write-Output "$($Job.id) 失敗：$($Job.error)"
            } else {
                Write-Output "$($Job.id) 狀態：$($Job.state) / $status"
            }
        }
    }
}

# Fail quickly when runtime.json is stale or belongs to a service that is not ready.
$null = Invoke-AssistantApi -Method Get -Path "/v1/health"

if ($Action) {
    if ($Mode -or $PSBoundParameters.ContainsKey("People") -or $Date -or
        $OpenAt -or $PSBoundParameters.ContainsKey("StandbySeconds") -or
        $PSBoundParameters.ContainsKey("RefreshMilliseconds")) {
        throw "-Action 不可與訂房參數同時使用。"
    }
    switch ($Action) {
        "Status" {
            if ($JobId) {
                Write-AssistantJobResult (Get-AssistantJob -Id $JobId)
            } else {
                $job = Invoke-AssistantApi -Method Post -Path "/v1/jobs" -Body @{ kind = "status" }
                Write-AssistantJobResult (Wait-AssistantJob -Id $job.id)
            }
        }
        "Jobs" {
            $response = Invoke-AssistantApi -Method Get -Path "/v1/jobs"
            if ($Json) {
                $response | ConvertTo-Json -Depth 12
            } else {
                $response.jobs |
                    Select-Object id, state, kind, created_at,
                        phase,
                        @{ Name = "mode"; Expression = { $_.payload.mode } },
                        @{ Name = "people"; Expression = { $_.payload.people } },
                        @{ Name = "date"; Expression = { $_.payload.date } },
                        @{ Name = "open_at"; Expression = { $_.payload.open_at } },
                        @{ Name = "result"; Expression = { $_.result.status } } |
                    Format-Table -AutoSize
            }
        }
        "Cancel" {
            if (-not $JobId) { throw "-Action Cancel 必須指定 -JobId。" }
            $job = Invoke-AssistantApi -Method Post -Path "/v1/jobs/$($JobId.ToUpperInvariant())/cancel"
            Write-AssistantJobResult $job
        }
        "Stop" {
            if ($JobId) { throw "-Action Stop 不接受 -JobId。" }
            $null = Invoke-AssistantApi -Method Post -Path "/v1/stop"
            Write-Output "助手將在目前工作完成後安全停止。"
        }
        "Resume" {
            if (-not $AcknowledgeUnknownSubmission) {
                throw "恢復前必須先人工核對官網訂單，再加上 -AcknowledgeUnknownSubmission。"
            }
            $null = Invoke-AssistantApi -Method Post -Path "/v1/resume" -Body @{
                acknowledge_unknown_submission = [bool]$AcknowledgeUnknownSubmission
            }
            Write-Output "已解除送單結果不明的暫停狀態。"
        }
    }
    return
}

if (-not $Mode) { throw "訂房工作必須指定 -Mode Rehearsal 或 -Mode Live。" }
if (-not $PSBoundParameters.ContainsKey("People")) { throw "訂房工作必須指定 -People。" }
if (-not $Date -or $Date.Count -eq 0) { throw "訂房工作必須指定 -Date YYYYMMDD。" }
if ($JobId) { throw "提交訂房工作時不可指定 -JobId。" }

$bookingConfig = Get-AssistantBookingConfig -Path $configPath
$effectiveOpenAt = $null
$effectiveStandbySeconds = $StandbySeconds
$effectiveRefreshMilliseconds = $RefreshMilliseconds
if ($Mode -ne "Live" -and $PSBoundParameters.ContainsKey("OpenAt")) {
    throw "-OpenAt 只可搭配 -Mode Live。"
}
if ($Mode -eq "Live") {
    $effectiveOpenAt = if ($PSBoundParameters.ContainsKey("OpenAt")) {
        $OpenAt
    } else {
        [string]$bookingConfig["booking_open_at"]
    }
    if (-not $PSBoundParameters.ContainsKey("StandbySeconds") -and
        $bookingConfig["booking_standby_seconds"]) {
        $configuredStandby = 0
        if (-not [int]::TryParse(
            [string]$bookingConfig["booking_standby_seconds"],
            [ref]$configuredStandby
        ) -or $configuredStandby -lt 10 -or $configuredStandby -gt 20) {
            throw "Secret.ini 的 booking_standby_seconds 必須介於 10 到 20。"
        }
        $effectiveStandbySeconds = $configuredStandby
    }
    if (-not $PSBoundParameters.ContainsKey("RefreshMilliseconds") -and
        $bookingConfig["booking_refresh_milliseconds"]) {
        $configuredRefresh = 0
        if (-not [int]::TryParse(
            [string]$bookingConfig["booking_refresh_milliseconds"],
            [ref]$configuredRefresh
        ) -or $configuredRefresh -lt 1000 -or $configuredRefresh -gt 5000) {
            throw "Secret.ini 的 booking_refresh_milliseconds 必須介於 1000 到 5000。"
        }
        $effectiveRefreshMilliseconds = $configuredRefresh
    }
}

$openAtIso = $null
if ($effectiveOpenAt) {
    $parsedOpenAt = [DateTimeOffset]::MinValue
    $parsed = [DateTimeOffset]::TryParse(
        $effectiveOpenAt,
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::AssumeLocal,
        [ref]$parsedOpenAt
    )
    if (-not $parsed) {
        throw "OpenAt 格式錯誤；例如：2026-11-01 08:30:00。"
    }
    $openAtIso = $parsedOpenAt.ToString("o")
} elseif ($PSBoundParameters.ContainsKey("StandbySeconds") -or
          $PSBoundParameters.ContainsKey("RefreshMilliseconds")) {
    throw "-StandbySeconds 與 -RefreshMilliseconds 必須搭配 -OpenAt。"
}

$submitted = @()
foreach ($day in $Date) {
    if ($day -notmatch '^20\d{6}$') {
        throw "Date 必須是 YYYYMMDD：$day"
    }
    $body = @{
        kind   = "booking"
        mode   = $Mode.ToLowerInvariant()
        people = $People
        date   = $day
    }
    if ($openAtIso) {
        $body.open_at = $openAtIso
        $body.standby_seconds = $effectiveStandbySeconds
        $body.refresh_interval_ms = $effectiveRefreshMilliseconds
    }
    $submitted += Invoke-AssistantApi -Method Post -Path "/v1/jobs" -Body $body
}

if ($Wait) {
    foreach ($job in $submitted) {
        Write-AssistantJobResult (Wait-AssistantJob -Id $job.id)
    }
} else {
    foreach ($job in $submitted) {
        Write-AssistantJobResult $job
    }
}

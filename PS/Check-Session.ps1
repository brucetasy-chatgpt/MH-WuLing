param(
    [string]$ConfigPath,
    [string]$LogDir,
    [switch]$OpenLoginOnFail
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot

function Resolve-ProjectPath {
    param(
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$DefaultRelativePath
    )

    $candidate = if ([string]::IsNullOrWhiteSpace($Path)) {
        Join-Path $projectRoot $DefaultRelativePath
    } elseif ([System.IO.Path]::IsPathRooted($Path)) {
        $Path
    } else {
        Join-Path $projectRoot $Path
    }

    return [System.IO.Path]::GetFullPath($candidate)
}

$ConfigPath = Resolve-ProjectPath -Path $ConfigPath -DefaultRelativePath "Secret\Secret.ini"
$LogDir = Resolve-ProjectPath -Path $LogDir -DefaultRelativePath "LOG"

function Get-Config {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Config not found: $Path"
    }

    $rawLines = Get-Content -LiteralPath $Path
    $kv = @{}

    foreach ($line in $rawLines) {
        $trim = $line.Trim()
        if ([string]::IsNullOrWhiteSpace($trim)) { continue }
        if ($trim.StartsWith(";")) { continue }

        if ($trim.Contains("=")) {
            $parts = $trim.Split("=", 2)
            $key = $parts[0].Trim()
            $val = $parts[1].Trim()
            $kv[$key] = $val
            continue
        }
    }

    [pscustomobject]@{
        WhotelSession          = $kv["whotel_session"]
        CampingSession         = $kv["camping_session"]
        WhotelFallbackLogin    = if ($kv.ContainsKey("whotel_fallback_login")) { $kv["whotel_fallback_login"] } else { "auto" }
        CampingFallbackLogin   = if ($kv.ContainsKey("camping_fallback_login")) { $kv["camping_fallback_login"] } else { "auto" }
    }
}

function Test-LoginState {
    param(
        [string]$Name,
        [string]$Url,
        [string]$Domain,
        [string]$SessionId
    )

    if ([string]::IsNullOrWhiteSpace($SessionId)) {
        return [pscustomobject]@{
            Site     = $Name
            Status   = "session_empty"
            LoggedIn = $false
            Reason   = "No session value in config"
            Url      = $Url
        }
    }

    $webSession = New-Object Microsoft.PowerShell.Commands.WebRequestSession
    $cookie = New-Object System.Net.Cookie("PHPSESSID", $SessionId, "/", $Domain)
    $webSession.Cookies.Add($cookie)

    try {
        $resp = Invoke-WebRequest -Uri $Url -WebSession $webSession -Method Get -MaximumRedirection 5
        $html = $resp.Content

        $loggedIn = $false
        if ($Name -eq "whotel") {
            $loggedIn = ($html -notmatch "未登入") -and ($html -notmatch "遊客登入")
        } elseif ($Name -eq "camping") {
            $loggedIn = ($html -match "登入使用者") -and ($html -notmatch "登入使用者\\s*:\\s*未登入")
        }

        return [pscustomobject]@{
            Site     = $Name
            Status   = if ($loggedIn) { "session_ok" } else { "session_invalid" }
            LoggedIn = $loggedIn
            Reason   = if ($loggedIn) { "Session accepted" } else { "Session rejected or expired" }
            Url      = $Url
        }
    }
    catch {
        return [pscustomobject]@{
            Site     = $Name
            Status   = "request_error"
            LoggedIn = $false
            Reason   = $_.Exception.Message
            Url      = $Url
        }
    }
}

$cfg = Get-Config -Path $ConfigPath

$results = @()
$results += Test-LoginState -Name "whotel" -Url "https://whotel.wuling-farm.com.tw/whotel/" -Domain "whotel.wuling-farm.com.tw" -SessionId $cfg.WhotelSession
$results += Test-LoginState -Name "camping" -Url "https://camping.wuling-farm.com.tw/camp/#booking" -Domain "camping.wuling-farm.com.tw" -SessionId $cfg.CampingSession

foreach ($r in $results) {
    $fallback = if ($r.Site -eq "whotel") { $cfg.WhotelFallbackLogin } else { $cfg.CampingFallbackLogin }
    if (-not $r.LoggedIn -and $fallback -eq "auto" -and $OpenLoginOnFail) {
        Start-Process $r.Url
    }
}

$now = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$summary = @()
$summary += "Session check time: $now"
$summary += "whotel:  $($results[0].Status) - $($results[0].Reason)"
$summary += "camping: $($results[1].Status) - $($results[1].Reason)"

if (-not (Test-Path -LiteralPath $LogDir)) {
    New-Item -Path $LogDir -ItemType Directory -Force | Out-Null
}
$logFile = Join-Path $LogDir ("session-check-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".txt")
$summary | Set-Content -LiteralPath $logFile -Encoding UTF8

$summary | ForEach-Object { Write-Output $_ }
Write-Output "Saved log: $logFile"

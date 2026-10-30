param(
    [ValidateRange(1, 65535)]
    [int]$Port = 18765
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$toolPath = Join-Path $projectRoot "tools\wuling_assistant.py"
$runtimePath = Join-Path $projectRoot ".wuling-assistant\runtime.json"
$profileLockPath = Join-Path $projectRoot ".wuling-browser-profile\lockfile"

if (Test-Path -LiteralPath $runtimePath) {
    $runtime = $null
    try {
        $runtime = Get-Content -LiteralPath $runtimePath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        # Ignore stale or incomplete runtime metadata. The profile lock check below
        # still prevents two assistants from sharing one Chromium profile.
    }
    if ($null -ne $runtime -and $runtime.pid -and
        (Get-Process -Id ([int]$runtime.pid) -ErrorAction SilentlyContinue)) {
        throw "訂房助手已在執行（PID $($runtime.pid)，Port $($runtime.port)）。請直接使用 .\PS\Invoke-WulingAssistant.ps1，不要重複啟動。"
    }
}

if (Test-Path -LiteralPath $profileLockPath) {
    try {
        $profileLock = [System.IO.File]::Open(
            $profileLockPath,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
        $profileLock.Dispose()
    } catch [System.IO.IOException] {
        throw "訂房瀏覽器仍在執行並占用專用 Profile。請回到原本的助手視窗使用，或先安全停止原助手；不要改用其他 Port 重複啟動。"
    }
}

if (Test-Path -LiteralPath $venvPython) {
    $python = $venvPython
} else {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $pythonCommand) { throw "找不到 Python，請先執行 .\PS\Setup.ps1。" }
    $python = $pythonCommand.Source
}

$previousOutputEncoding = [Console]::OutputEncoding
$previousPythonIoEncoding = $env:PYTHONIOENCODING
try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $env:PYTHONIOENCODING = "utf-8"
    & $python $toolPath --port $Port
    $assistantExitCode = $LASTEXITCODE
} finally {
    [Console]::OutputEncoding = $previousOutputEncoding
    $env:PYTHONIOENCODING = $previousPythonIoEncoding
}
if ($assistantExitCode -ne 0) { throw "訂房助手啟動失敗，結束碼：$assistantExitCode" }

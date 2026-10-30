param(
    [Parameter(Mandatory = $true)]
    [string]$Request,

    [ValidateSet("Rehearsal", "Live")]
    [string]$Mode = "Rehearsal",

    [switch]$Headed,

    [switch]$Json,

    [switch]$VerifyOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$toolPath = Join-Path $projectRoot "tools\wuling_booking.py"
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (Test-Path -LiteralPath $venvPython) {
    $python = $venvPython
} else {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $pythonCommand) {
        throw "Python was not found. Run .\PS\Setup.ps1 first."
    }
    $python = $pythonCommand.Source
}

$pythonArgs = @(
    $toolPath,
    "--request", $Request,
    "--mode", $Mode.ToLowerInvariant()
)
if ($Headed) { $pythonArgs += "--headed" }
if ($Json) { $pythonArgs += "--json" }
if ($VerifyOnly) { $pythonArgs += "--verify-only" }

$previousOutputEncoding = [Console]::OutputEncoding
$previousPythonIoEncoding = $env:PYTHONIOENCODING
try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $env:PYTHONIOENCODING = "utf-8"
    & $python @pythonArgs
    $bookingExitCode = $LASTEXITCODE
} finally {
    [Console]::OutputEncoding = $previousOutputEncoding
    $env:PYTHONIOENCODING = $previousPythonIoEncoding
}
if ($bookingExitCode -ne 0) {
    Write-Warning "Booking runner finished with exit code $bookingExitCode. See the status above."
}

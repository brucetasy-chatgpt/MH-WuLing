param(
    [string]$InputPath,
    [string]$OutputPath,
    [string]$Selector,
    [switch]$Headed,
    [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$toolPath = Join-Path $projectRoot "tools\whotel_captcha.py"
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (Test-Path -LiteralPath $venvPython) {
    $python = $venvPython
} else {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $pythonCommand) {
        throw "Python was not found. Install Python 3.11+ and run .\PS\Setup.ps1."
    }
    $python = $pythonCommand.Source
}

$pythonArgs = @($toolPath)
if (-not [string]::IsNullOrWhiteSpace($InputPath)) {
    $resolvedInput = if ([System.IO.Path]::IsPathRooted($InputPath)) { $InputPath } else { Join-Path $projectRoot $InputPath }
    $pythonArgs += @("--input", [System.IO.Path]::GetFullPath($resolvedInput))
}
if (-not [string]::IsNullOrWhiteSpace($OutputPath)) {
    $resolvedOutput = if ([System.IO.Path]::IsPathRooted($OutputPath)) { $OutputPath } else { Join-Path $projectRoot $OutputPath }
    $pythonArgs += @("--output", [System.IO.Path]::GetFullPath($resolvedOutput))
}
if (-not [string]::IsNullOrWhiteSpace($Selector)) { $pythonArgs += @("--selector", $Selector) }
if ($Headed) { $pythonArgs += "--headed" }
if ($Json) { $pythonArgs += "--json" }

& $python @pythonArgs
if ($LASTEXITCODE -eq 2) {
    throw "OCR result was not reliable enough. Enter the CAPTCHA manually."
}
if ($LASTEXITCODE -ne 0) {
    throw "CAPTCHA capture or OCR failed with exit code $LASTEXITCODE."
}

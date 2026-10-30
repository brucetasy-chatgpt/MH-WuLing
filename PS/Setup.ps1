param(
    [switch]$Upgrade
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPath = Join-Path $projectRoot ".venv"
$venvPython = Join-Path $venvPath "Scripts\python.exe"
$requirementsPath = Join-Path $projectRoot "requirements.txt"

$pythonCommand = Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $pythonCommand) {
    throw "Python was not found. Install Python 3.11 or newer first."
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    & $pythonCommand.Source -m venv $venvPath
    if ($LASTEXITCODE -ne 0) { throw "Failed to create the project virtual environment." }
}

if ($Upgrade) {
    & $venvPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "Failed to upgrade pip." }
}

& $venvPython -m pip install -r $requirementsPath
if ($LASTEXITCODE -ne 0) { throw "Failed to install Python dependencies." }

& $venvPython -m playwright install chromium
if ($LASTEXITCODE -ne 0) { throw "Failed to install Playwright Chromium." }

Write-Output "Setup complete. Bundled Chromium and OCR dependencies are ready."

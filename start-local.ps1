<#
.SYNOPSIS
  Launches the Azure BOM Region Support Dashboard locally (single process).

.DESCRIPTION
  - Ensures the Python venv exists and has dependencies
  - Starts the FastAPI host on http://localhost:4280 (serves the UI AND /api)
  - Opens the dashboard in your default browser

  Sign-in is handled in-app: the first time you run an analysis, a browser
  window opens for your Microsoft sign-in (no Azure CLI required). The session
  is cached, so later runs are silent.

  Press Ctrl+C in this window to stop the server.

.PARAMETER NoBrowser
  Don't open the browser after startup.

.PARAMETER Port
  Port to listen on (default 4280).

.EXAMPLE
  .\start-local.ps1
#>
[CmdletBinding()]
param(
    [switch]$NoBrowser,
    [int]$Port = 4280
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location $root

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-OK($msg)   { Write-Host "    $msg" -ForegroundColor Green }
function Write-Err($msg)  { Write-Host "    $msg" -ForegroundColor Red }

# 1. Verify tooling (Python only - sign-in is in-app, no Azure CLI needed)
Write-Step "Checking required tools"
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Err "Missing required tool: python"
    exit 1
}
Write-OK "Python present"

# 2. Ensure the venv exists and has dependencies installed
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Step "Creating Python venv"
    python -m venv .venv
    & $venvPython -m pip install --upgrade pip
    & $venvPython -m pip install -r api\requirements.txt
} else {
    Write-OK "Python venv present"
}

# 3. Runtime environment for the local app
$env:LOCAL_MODE     = "true"
$env:ALLOWED_ORIGIN = "http://localhost:$Port"
$env:HOST           = "127.0.0.1"
$env:PORT           = "$Port"

$dashUrl = "http://localhost:$Port/"

# 4. Open the browser shortly after launch
if (-not $NoBrowser) {
    Start-Job -ScriptBlock {
        param($url)
        Start-Sleep -Seconds 2
        Start-Process $url
    } -ArgumentList $dashUrl | Out-Null
}

Write-Host ""
Write-Host "===============================================================" -ForegroundColor Green
Write-Host "  Azure BOM Region Support Dashboard - local mode" -ForegroundColor Green
Write-Host "  Dashboard:   $dashUrl" -ForegroundColor Green
Write-Host "  API:         ${dashUrl}api/snapshots" -ForegroundColor Green
Write-Host "  Storage:     $root\local-storage\" -ForegroundColor Green
Write-Host ""
Write-Host "  First analysis opens a browser window for sign-in." -ForegroundColor Green
Write-Host "  Press Ctrl+C here to stop the server." -ForegroundColor Green
Write-Host "===============================================================" -ForegroundColor Green
Write-Host ""

# 5. Run the server in the foreground (Ctrl+C stops it)
Write-Step "Starting dashboard on $dashUrl"
& $venvPython -m server
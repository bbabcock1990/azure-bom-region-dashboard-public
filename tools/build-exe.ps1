<#
.SYNOPSIS
  Builds the single-file Windows executable for the dashboard.

.DESCRIPTION
  Installs PyInstaller into the local venv (if needed) and produces
  dist\AzureBomRegionDashboard.exe — a self-contained launcher a customer can
  double-click, with no Python/venv/pip setup required.

.EXAMPLE
  .\tools\build-exe.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$venvPython = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "Creating venv..." -ForegroundColor Cyan
    python -m venv .venv
    & $venvPython -m pip install --upgrade pip
    & $venvPython -m pip install -r api\requirements.txt
}

Write-Host "Ensuring PyInstaller is installed..." -ForegroundColor Cyan
& $venvPython -m pip install "pyinstaller>=6.0" | Out-Null

Write-Host "Building executable..." -ForegroundColor Cyan
& $venvPython -m PyInstaller bomdash.spec --noconfirm

$exe = Join-Path $root "dist\AzureBomRegionDashboard\AzureBomRegionDashboard.exe"
if (Test-Path $exe) {
    Write-Host ""
    Write-Host "Built: $exe" -ForegroundColor Green
    Write-Host "Run it (or the whole dist\AzureBomRegionDashboard folder can be zipped and shipped) to start the dashboard on http://localhost:4280/" -ForegroundColor Green
} else {
    Write-Error "Build did not produce $exe"
}

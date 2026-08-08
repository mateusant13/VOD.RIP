<#
.SYNOPSIS
    Start the VOD.RIP dev backend, killing whatever owns port 7897 first.

.DESCRIPTION
    One command to (re)launch the dev backend from source. The backend is
    fair game per project convention, so this script never fails with
    "port in use" - it finds the listener on the target port (default 7897,
    the dev/app port) and stops it before starting `python run.py`.

.PARAMETER Port
    Port to free and serve on. Defaults to 7897.

.PARAMETER RepoRoot
    Source repository root. Defaults to the folder above this script.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\dev-backend.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\dev-backend.ps1 -Port 7898
#>
[CmdletBinding()]
param(
    [int]$Port = 7897,
    [string]$RepoRoot
)

$ErrorActionPreference = 'Stop'

if (-not $RepoRoot) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}
$RepoRoot = (Resolve-Path $RepoRoot).Path
$BackendDir = Join-Path $RepoRoot 'backend'

if (-not (Test-Path (Join-Path $BackendDir 'run.py'))) {
    Write-Host "DEV-BACKEND FAILED: no backend\run.py in '$RepoRoot' - pass -RepoRoot" -ForegroundColor Red
    exit 1
}

Write-Host "== VOD.RIP dev backend (port $Port) ==" -ForegroundColor Cyan

# --- free the port ----------------------------------------------------------
$listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
foreach ($conn in $listeners) {
    $pid_ = $conn.OwningProcess
    try {
        $proc = Get-Process -Id $pid_ -ErrorAction Stop
        Write-Host "  killing $($proc.ProcessName) (pid $pid_) holding port $Port..." -ForegroundColor Yellow
        Stop-Process -Id $pid_ -Force
    } catch {
        Write-Host "  note: pid $pid_ already gone" -ForegroundColor DarkGray
    }
}
Start-Sleep -Milliseconds 500

# --- start ------------------------------------------------------------------
Write-Host "  starting: python run.py --port $Port (cwd $BackendDir)" -ForegroundColor Green
Push-Location $BackendDir
try {
    python run.py --port $Port
} finally {
    Pop-Location
}

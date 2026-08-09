<#
.SYNOPSIS
    Build the VOD.RIP frozen bundle and install it to the app launch folder.

.DESCRIPTION
    One command to go from source to a working installed app:
      1. npm run build-dist   (vite -> PyInstaller onedir -> deploy -> cookie ext -> sign)
      2. backup the previous install
      3. copy the fresh bundle over the launch folder
      4. smoke-test the installed exe on a free port (boots, serves /api/info, exits)

    Safe to re-run; idempotent. Keeps exactly one backup of the previous install.

.PARAMETER InstallDir
    Where the app lives and is launched from. Defaults to the H: launch folder
    on this machine (H:\VOD.RIP-build\dist\VOD-RIP).

.PARAMETER RepoRoot
    Source repository root. Defaults to the folder above this script.

.PARAMETER SkipBuild
    Skip the PyInstaller build; only re-install the existing dist/VOD-RIP.

.PARAMETER SkipSmoke
    Skip the boot smoke test after install.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\build-install.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\build-install.ps1 -SkipSmoke
#>
[CmdletBinding()]
param(
    [string]$InstallDir = 'H:\VOD.RIP-build\dist\VOD-RIP',
    [string]$RepoRoot  = '',
    [switch]$SkipBuild,
    [switch]$SkipSmoke
)

$ErrorActionPreference = 'Stop'

function Fail([string]$msg) {
    Write-Host "BUILD-INSTALL FAILED: $msg" -ForegroundColor Red
    exit 1
}

# --- locate repo root -------------------------------------------------------
if (-not $RepoRoot) {
    $RepoRoot = Split-Path -Parent $PSScriptRoot
}
$RepoRoot = (Resolve-Path $RepoRoot).Path
$DistDir  = Join-Path $RepoRoot 'dist\VOD-RIP'
$ExePath  = Join-Path $DistDir 'VOD-RIP.exe'

if (-not (Test-Path (Join-Path $RepoRoot 'package.json'))) {
    Fail "no package.json in '$RepoRoot' - pass -RepoRoot"
}

Write-Host "== VOD.RIP build+install ==" -ForegroundColor Cyan
Write-Host "  repo     : $RepoRoot"
Write-Host "  install  : $InstallDir"

# --- build ------------------------------------------------------------------
if ($SkipBuild) {
    Write-Host "skipping build (-SkipBuild)"
} else {
    Write-Host "`n[1/4] building dist (npm run build-dist)..." -ForegroundColor Yellow
    Push-Location $RepoRoot
    try {
        npm run build-dist
        if ($LASTEXITCODE -ne 0) { Fail "npm run build-dist exited $LASTEXITCODE" }
    } finally { Pop-Location }
    if (-not (Test-Path $ExePath)) { Fail "build produced no $ExePath" }
}

# --- backup previous install ------------------------------------------------
Write-Host "`n[2/4] backing up previous install..." -ForegroundColor Yellow
$backupDir = $null
if (Test-Path (Join-Path $InstallDir 'VOD-RIP.exe')) {
    $backupDir = "$InstallDir.backup"
    if (Test-Path $backupDir) { Remove-Item $backupDir -Recurse -Force }
    Rename-Item $InstallDir $backupDir
    Write-Host "  previous install -> $backupDir"
} else {
    Write-Host "  no previous install found"
}

# --- install ----------------------------------------------------------------
Write-Host "`n[3/4] copying bundle to $InstallDir ..." -ForegroundColor Yellow
$parent = Split-Path -Parent $InstallDir
if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
robocopy $DistDir $InstallDir /E /COPY:DAT /DCOPY:DAT /NFL /NDL /NP /MT:16 /R:2 /W:2 | Out-Null
if ($LASTEXITCODE -gt 7) { Fail "robocopy failed ($LASTEXITCODE)" }

# Mark-of-the-Web hygiene: SmartScreen keys off Zone.Identifier for downloads,
# and a freshly installed app should never carry it (e.g. when the repo or a
# previous install came from a downloaded zip). Cheap, idempotent, local.
Get-ChildItem -Path $InstallDir -Recurse -File -ErrorAction SilentlyContinue |
    ForEach-Object { Unblock-File -Path $_.FullName -ErrorAction SilentlyContinue }

# --- smoke test -------------------------------------------------------------
if ($SkipSmoke) {
    Write-Host "`n[4/4] smoke test skipped (-SkipSmoke)" -ForegroundColor Yellow
} else {
    Write-Host "`n[4/4] smoke-testing installed exe..." -ForegroundColor Yellow

    # pick a free port (never collide with the dev backend :7897)
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $port = ($listener.LocalEndpoint).Port
    $listener.Stop()

    $env:PORT = "$port"
    $proc = Start-Process -FilePath (Join-Path $InstallDir 'VOD-RIP.exe') `
        -WorkingDirectory $InstallDir `
        -PassThru -WindowStyle Hidden

    $ok = $false
    try {
        for ($i = 0; $i -lt 60; $i++) {
            Start-Sleep -Seconds 1
            if ($proc.HasExited) { break }
            try {
                $resp = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/info" -TimeoutSec 2
                if ($resp.desktop -eq $true) { $ok = $true; break }
            } catch { }
        }
        if (-not $ok) { Fail "installed exe did not serve /api/info on port $port" }
        Write-Host "  boot OK (desktop=true) on port $port"

        # exercise the settings round-trip (new-field sanity)
        $settings = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/settings" -TimeoutSec 5
        $hasAuto = $null -ne $settings.PSObject.Properties['start_with_windows']
        Write-Host "  settings API OK (start_with_windows field present: $hasAuto)"
    } finally {
        try { Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$port/api/exit" -TimeoutSec 5 | Out-Null } catch { }
        Start-Sleep -Seconds 3
        if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
    }
}

Write-Host "`nBUILD-INSTALL OK" -ForegroundColor Green
if ($backupDir) { Write-Host "  rollback: rename '$backupDir' back over '$InstallDir'" -ForegroundColor DarkGray }
exit 0

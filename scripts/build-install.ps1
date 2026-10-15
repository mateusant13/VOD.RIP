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

    BUILD DATA NEVER LANDS ON C:. Before building, this script redirects the
    two directories the build chain uses by convention (repo\dist and
    repo\build) to the H: build root through directory junctions, and pins TMP
    to the same root. Every downstream step (vite build/outDir, PyInstaller
    --distpath/--workpath defaults, build-copy, stage-cookie-extension,
    sign-release, deploy-dist) keeps the same repo-relative paths and needs no
    change of its own: those paths resolve through the junctions onto H:. That
    keeps ~2.6 GB of intermediates off the system NVMe (C: measured 20.1 GB
    free on 2026-10-15) and makes the final install an H:->H: robocopy.

.PARAMETER InstallDir
    Where the app lives and is launched from. Defaults to the H: launch folder
    on this machine (H:\VOD.RIP-build\dist\VOD-RIP).

.PARAMETER RepoRoot
    Source repository root. Defaults to the folder above this script.

.PARAMETER BuildRoot
    Root that receives every heavy build artifact on H: (default
    H:\VOD.RIP-build). Layout created under it:
      _build\dist   <- repo\dist  (vite index.html + PyInstaller onedir)
      _build\work   <- repo\build (PyInstaller workpath + icon staging)
      _deploy       <- deploy-dist.mjs root layout (VOD-RIP.EXE, _internal)
      tmp           <- TMP/TEMP for PyInstaller's isolated child + vite spawn

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
    [string]$BuildRoot = 'H:\VOD.RIP-build',
    [switch]$SkipBuild,
    [switch]$SkipSmoke
)

$ErrorActionPreference = 'Stop'

function Fail([string]$msg) {
    Write-Host "BUILD-INSTALL FAILED: $msg" -ForegroundColor Red
    exit 1
}

# --- build root on H: -------------------------------------------------------
$BuildRootAbs = $BuildRoot
if (-not [System.IO.Path]::IsPathRooted($BuildRootAbs)) {
    $BuildRootAbs = Join-Path (Get-Location).Path $BuildRootAbs
}
$BuildDistDir   = Join-Path $BuildRootAbs '_build\dist'
$BuildWorkDir   = Join-Path $BuildRootAbs '_build\work'
$BuildDeployDir = Join-Path $BuildRootAbs '_deploy'
$BuildTmpDir    = Join-Path $BuildRootAbs 'tmp'

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
Write-Host "  buildroot: $BuildRootAbs"

# --- TMP on H: ----------------------------------------------------------------
# PyInstaller's isolated child + vite spawn processes use %TMP%. The old pin
# was $env:LOCALAPPDATA\Temp (C:) because G:\Temp is slow/flaky on this box
# (MemoryError in marshal/bytearray, OSError 22 in _child.py). H: is the same
# NVMe class with 88 GB free (measured 2026-10-15), so it serves; if H: is ever
# unwritable we keep the local-profile pin so the build still runs.
try {
    New-Item -ItemType Directory -Path $BuildTmpDir -Force | Out-Null
    $probe = Join-Path $BuildTmpDir '.writable-probe'
    'ok' | Set-Content -LiteralPath $probe -ErrorAction Stop
    Remove-Item -LiteralPath $probe -Force -ErrorAction Stop
    $env:TMP  = $BuildTmpDir
    $env:TEMP = $BuildTmpDir
    Write-Host "  tmp      : $BuildTmpDir"
} catch {
    $env:TMP  = Join-Path $env:LOCALAPPDATA 'Temp'
    $env:TEMP = $env:TMP
    Write-Host "  tmp      : $env:TMP (H: tmp not writable - fell back)" -ForegroundColor Yellow
}

# The node deploy script (invoked by npm run build-dist) stages its own output
# under the same root; expose it to every child process.
$env:VODRIP_BUILD_ROOT = $BuildRootAbs

# --- redirect the two build dirs to H: via junctions -------------------------
function Set-BuildJunction([string]$Link, [string]$Target, [string]$Label) {
    <#
    Idempotently make $Link resolve to $Target through a directory junction
    (no admin needed, unlike symlinks). A junction is removed only with
    (Get-Item).Delete() / [System.IO.Directory]::Delete() - never with
    Remove-Item -Recurse, which follows into the target.
    #>
    if (-not (Test-Path -LiteralPath $Target)) {
        New-Item -ItemType Directory -Path $Target -Force | Out-Null
    }
    if (Test-Path -LiteralPath $Link) {
        $item = Get-Item -LiteralPath $Link -Force
        if ($item.LinkType -eq 'Junction') {
            $current = (($item.Target | ForEach-Object { "$_" }) -join '').TrimEnd('\')
            if ($current -ieq $Target.TrimEnd('\')) {
                Write-Host ("  " + $Label).PadRight(9) + ": already $Target"
                return
            }
            Write-Host ("  " + $Label).PadRight(9) + ": re-pointing $current -> $Target"
            $item.Delete()
        } else {
            # A real directory left by a pre-change build: MOVE its content to
            # the H: target first, verify the move, and only then swap the
            # junction in. robocopy is called exactly like the install step
            # below does (same exit-code contract: >= 8 is an error, 0/1 are
            # fine), and the target must end up holding at least as many files
            # as the source had, so a failed copy can never turn the next line
            # into a delete.
            Write-Host ("  " + $Label).PadRight(9) + ": migrating existing $Link -> $Target"
            $before = (Get-ChildItem -LiteralPath $Link -Recurse -File -ErrorAction SilentlyContinue | Measure-Object).Count
            robocopy $Link $Target /E /MOVE /NFL /NDL /NJH /NJS /NP /R:1 /W:1 | Out-Null
            if ($LASTEXITCODE -ge 8) { Fail "migrating $Link to $Target failed (robocopy $LASTEXITCODE)" }
            $after = (Get-ChildItem -LiteralPath $Target -Recurse -File -ErrorAction SilentlyContinue | Measure-Object).Count
            if ($after -lt $before) {
                Fail "migrating $Link to $Target copied $after/$before files - aborting before the swap"
            }
            $migMB = [math]::Round(((Get-ChildItem -LiteralPath $Target -Recurse -File -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum) / 1MB, 1)
            Write-Host ("  " + $Label).PadRight(9) + ": migrated $before files ($migMB MB)"
            if (Test-Path -LiteralPath $Link) { Remove-Item -LiteralPath $Link -Recurse -Force }
        }
    }
    if (-not (Test-Path -LiteralPath $Link)) {
        New-Item -ItemType Directory -Path $Link -Force | Out-Null
    }
    New-Item -ItemType Junction -Path $Link -Target $Target | Out-Null
    Write-Host ("  " + $Label).PadRight(9) + "-> $Target"
}

# --- build ------------------------------------------------------------------
if ($SkipBuild) {
    Write-Host "skipping build (-SkipBuild)"
} else {
    try {
        Set-BuildJunction -Link (Join-Path $RepoRoot 'dist')  -Target $BuildDistDir -Label 'dist'
        Set-BuildJunction -Link (Join-Path $RepoRoot 'build') -Target $BuildWorkDir -Label 'build'
    } catch {
        # Never trade a build that works for one that does not: fall back to the
        # historical in-repo layout so the pipeline still produces a bundle.
        Write-Host "  WARNING: could not redirect build dirs to $BuildRootAbs ($($_.Exception.Message))" -ForegroundColor Yellow
        Write-Host "  WARNING: build will write inside the repo (C:) as before" -ForegroundColor Yellow
    }

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
Write-Host "  source  : $DistDir (junction -> $BuildDistDir)"
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
Write-Host "  deploy root : $BuildDeployDir (deploy-dist.mjs root layout)"
if ($backupDir) { Write-Host "  rollback: rename '$backupDir' back over '$InstallDir'" -ForegroundColor DarkGray }
exit 0

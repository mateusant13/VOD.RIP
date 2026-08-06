<#
sign-release.ps1 â€” conditional Authenticode signing for VOD.RIP release artifacts.

Signs (when a certificate is available):
  - dist/VOD-RIP.exe            (onefile build â€” the primary download)
  - dist/VOD-RIP/VOD-RIP.exe    (onedir build, if present)
  - release/*.zip               (onedir zip artifact, if present)

Certificate resolution (first match wins):
  - $env:VODRIP_CERT_FILE
  - <repo>/signing/vodrip.pfx
Password:
  - $env:VODRIP_CERT_PWD
  - <repo>/signing/.pwd
Timestamp server: $env:VODRIP_TIMESTAMP, default http://timestamp.digicert.com

No certificate -> prints "no signing cert â€” skipping" and exits 0, so the
build pipeline never fails because signing is not configured. See
docs/SIGNING.md for how to obtain a certificate.

Usage: powershell -NoProfile -ExecutionPolicy Bypass -File scripts/sign-release.ps1
#>
$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

# --- resolve certificate -------------------------------------------------
$certFile = $null
if ($env:VODRIP_CERT_FILE -and (Test-Path $env:VODRIP_CERT_FILE)) {
    $certFile = $env:VODRIP_CERT_FILE
} else {
    $defaultCert = Join-Path $repoRoot 'signing\vodrip.pfx'
    if (Test-Path $defaultCert) { $certFile = $defaultCert }
}
if (-not $certFile) {
    Write-Host 'no signing cert â€” skipping'
    exit 0
}

$certPassword = $null
if ($env:VODRIP_CERT_PWD) {
    $certPassword = $env:VODRIP_CERT_PWD
} else {
    $pwdFile = Join-Path $repoRoot 'signing\.pwd'
    if (Test-Path $pwdFile) {
        $certPassword = (Get-Content $pwdFile -Raw).Trim()
    }
}
if (-not $certPassword) {
    Write-Host 'signing cert found but no password (env VODRIP_CERT_PWD or signing\.pwd) â€” skipping'
    exit 0
}

$timestamp = if ($env:VODRIP_TIMESTAMP) { $env:VODRIP_TIMESTAMP } else { 'http://timestamp.digicert.com' }

# --- locate signtool.exe (Windows SDK) -----------------------------------
$signtool = $null
$kitsRoot = "${env:ProgramFiles(x86)}\Windows Kits"
if (Test-Path $kitsRoot) {
    $signtool = (Get-ChildItem -Path $kitsRoot -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -First 1).FullName
}
if (-not $signtool) {
    $cmd = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($cmd) { $signtool = $cmd.Source }
}
if (-not $signtool) {
    Write-Host 'signtool.exe not found (Windows SDK not installed) â€” skipping signing'
    exit 0
}
Write-Host "Using signtool: $signtool"

# --- collect targets (missing ones are skipped, never fatal) -------------
$targets = @()
$onefileExe = Join-Path $repoRoot 'dist\VOD-RIP.exe'
if (Test-Path $onefileExe) { $targets += $onefileExe }
else { Write-Host 'onefile exe not found (dist\VOD-RIP.exe) â€” skipping' }
$onedirExe = Join-Path $repoRoot 'dist\VOD-RIP\VOD-RIP.exe'
if (Test-Path $onedirExe) { $targets += $onedirExe }
$releaseDir = Join-Path $repoRoot 'release'
if (Test-Path $releaseDir) {
    Get-ChildItem -Path $releaseDir -Filter '*.zip' -ErrorAction SilentlyContinue | ForEach-Object { $targets += $_.FullName }
}

if ($targets.Count -eq 0) {
    Write-Host 'no release artifacts found to sign â€” skipping'
    exit 0
}

foreach ($target in $targets) {
    Write-Host "Signing: $target"
    & $signtool sign /fd SHA256 /tr $timestamp /td sha256 /f $certFile /p $certPassword $target
    if ($LASTEXITCODE -ne 0) { throw "signtool sign failed for $target" }
    & $signtool verify /pa $target
    if ($LASTEXITCODE -ne 0) { throw "signtool verify failed for $target" }
    Write-Host "Signed and verified: $target"
}
Write-Host 'Signing complete'

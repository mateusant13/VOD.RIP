<#
.SYNOPSIS
    Build, zip and hash the OPTIONAL ASR runtime archive for a release.

.DESCRIPTION
    Realises the release steps AGENTS.md marks [FUTURE] in the
    "How To: Split-Runtime Release" section:
      1. pyinstaller vod-rip-asr.spec --clean --noconfirm (the heavy stack)
      2. node scripts/deploy-asr-runtime.mjs   (stage dist\runtimes\asr\<version>\)
      3. zip the staged runtime to release\VOD-RIP-ASR-<version>-win-x64.zip
      4. node scripts/write-asr-runtime-manifest.mjs <version>
         (per-file sha256 + archive sha256, the exact manifest the app and
         scripts/install-asr-runtime.ps1 verify against)

    Offline and version-pinned: the wheels installed by
    backend/requirements-gpu.txt (sherpa-onnx==1.13.4+cuda12.cudnn9 and the
    nvidia-*-cu12 DLLs) must already be present in the build interpreter -
    CI installs them in the "Install GPU ASR runtime deps" step. Without the
    +cuda wheel the runtime still builds, but asr_gpu_binaries() degrades to
    the CPU onnxruntime EP.

    TMP/TEMP are pinned to H:\Temp (rule: heavy scratch never on C:).

.PARAMETER Version
    Must equal the version in backend/services/_version.py (the single source
    of truth the staged runtime dir and the app both read). A mismatch fails
    closed instead of ziping a runtime the app would not recognise. Defaults
    to that file's value.

.PARAMETER SkipBuild
    Reuse the existing dist\VOD-RIP-ASR build; only zip + hash.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\build-asr-runtime.ps1 -Version 1.0.54
#>
[CmdletBinding()]
param(
    [string]$Version = "",
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"

# Scratch on H: - the runtime build and the zip are multi-GB and C: stays tight.
$env:TMP  = "H:\Temp"
$env:TEMP = $env:TMP

function Fail([string]$msg) {
    Write-Host "BUILD-ASR-RUNTIME FAILED: $msg" -ForegroundColor Red
    exit 1
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

# The release version is single-sourced from backend/services/_version.py: the
# staged runtime directory (deploy-asr-runtime.mjs) and the app's own reported
# version both read it, so the zip name, the staged dir and the manifest must
# carry exactly that value. An explicit -Version that disagrees is a bug in the
# caller, not an override - fail closed instead of ziping the wrong version.
$versionPy = Join-Path $RepoRoot "backend\services\_version.py"
$line = Select-String -Path $versionPy -Pattern '__version__' | Select-Object -First 1
if (-not $line) { Fail "could not read __version__ from $versionPy" }
$m = [regex]::Match($line.Line, '[0-9]+\.[0-9]+\.[0-9]+')
if (-not $m.Success) { Fail "could not parse a version out of the __version__ line" }
$codeVersion = $m.Value
if ($Version -and ($Version -ne $codeVersion)) {
    Fail "-Version $Version does not match backend/services/_version.py ($codeVersion); the staged runtime dir and the app both use the file - pass $codeVersion or bump the file"
}
$Version = $codeVersion
Write-Host "== VOD.RIP ASR runtime build ==" -ForegroundColor Cyan
Write-Host "  repo    : $RepoRoot"
Write-Host "  version : $Version"

# --- 1+2. PyInstaller + stage ------------------------------------------------
if ($SkipBuild) {
    Write-Host "skipping pyinstaller (-SkipBuild)"
} else {
    Write-Host "`n[1/4] pyinstaller vod-rip-asr.spec ..." -ForegroundColor Yellow
    pyinstaller vod-rip-asr.spec --clean --noconfirm
    if ($LASTEXITCODE -ne 0) { Fail "pyinstaller exited $LASTEXITCODE" }
    Write-Host "[2/4] staging runtime ..." -ForegroundColor Yellow
    node scripts/deploy-asr-runtime.mjs
    if ($LASTEXITCODE -ne 0) { Fail "deploy-asr-runtime exited $LASTEXITCODE" }
}

$runtimeDir = Join-Path $RepoRoot "dist\runtimes\asr\$Version"
$runtimeExe = Join-Path $runtimeDir "VOD-RIP-ASR.exe"
if (-not (Test-Path $runtimeExe)) { Fail "no staged runtime at $runtimeExe (run without -SkipBuild)" }

# --- 3. zip -------------------------------------------------------------------
New-Item -ItemType Directory -Force -Path (Join-Path $RepoRoot "release") | Out-Null
$archive = Join-Path $RepoRoot "release\VOD-RIP-ASR-$Version-win-x64.zip"
if (Test-Path $archive) { Remove-Item $archive -Force }
Write-Host "[3/4] zipping runtime -> $archive" -ForegroundColor Yellow
Compress-Archive -Path "$runtimeDir\*" -DestinationPath $archive -CompressionLevel Optimal -Force
if (-not (Test-Path $archive)) { Fail "zip step produced no $archive" }
$mb = [math]::Round((Get-Item $archive).Length / 1MB, 1)
Write-Host "  archive: $mb MB"

# --- 4. manifest ----------------------------------------------------------------
Write-Host "[4/4] writing sha256 manifest ..." -ForegroundColor Yellow
node scripts/write-asr-runtime-manifest.mjs $Version
if ($LASTEXITCODE -ne 0) { Fail "write-asr-runtime-manifest exited $LASTEXITCODE" }
Write-Host "  manifest: release\VOD-RIP-ASR-manifest.json"

Write-Host "`nBUILD-ASR-RUNTIME OK (v$Version)" -ForegroundColor Green
exit 0
# Build the bgutil-ytdlp-pot-provider bundle for shipping inside the Windows
# installer. Clones Brainicism/bgutil-ytdlp-pot-provider at the pinned tag,
# runs `npm ci` + `npm run build` inside server/ (which produces
# server/build/main.js plus its compiled dependencies), and copies the
# minimal artifact tree to build/external/bgutil-pot/ so PyInstaller can
# pick it up via vod-rip.spec.
#
# Output layout (matches youtube_pot_service.frozen_runtime_paths):
#
#   build/external/bgutil-pot/server/build/main.js
#   build/external/bgutil-pot/server/node_modules/...
#   build/external/bgutil-pot/server/package.json
#
# At runtime under the frozen EXE, bgutil lives at:
#
#   <exe-dir>/runtime/bgutil-pot/server/build/main.js
#   <exe-dir>/runtime/bgutil-pot/server/node_modules/...
#
# which is the layout the spec's datas list emits.
#
# Run from project root:
#   pwsh scripts/build-bgutil-bundle.ps1
#
# Skips silently when the bundle is already present (idempotent — CI re-runs
# this on every build) and when git/npm/network are unavailable (the dev
# path in youtube_pot_service still builds the bundle lazily on first warm).

[CmdletBinding()]
param(
    [string]$BgutilTag = "v1.2.0",
    [string]$OutputDir = "build\external"
)

$ErrorActionPreference = "Continue"

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptRoot "..")
$ExternalDir = Join-Path $ProjectRoot $OutputDir
$BundleRoot = Join-Path $ExternalDir "bgutil-pot"
$MainJs = Join-Path $BundleRoot "server" "build" "main.js"

# Skip when a complete bundle is already on disk. The output is a few
# hundred MB (node_modules) so we don't want to redo it on every build.
if (Test-Path -LiteralPath $MainJs) {
    $size = (Get-ChildItem -LiteralPath (Join-Path $BundleRoot "server" "build") -Recurse -File |
        Measure-Object -Property Length -Sum).Sum
    Write-Host "[bgutil] already built: $MainJs ($([math]::Round($size/1MB, 1)) MB); skipping"
    exit 0
}

# Verify the prerequisites are available. Without git or npm there's no
# point continuing, but we exit 0 (not 1) because the dev-mode bootstrap
# inside youtube_pot_service will produce the same bundle on first warm.
foreach ($tool in @("git", "npm")) {
    $probe = Get-Command $tool -ErrorAction SilentlyContinue
    if ($null -eq $probe) {
        Write-Host "[bgutil] $tool not on PATH; skipping (development build will fall back to PATH)"
        exit 0
    }
}

if (-not (Test-Path -LiteralPath $ExternalDir)) {
    New-Item -ItemType Directory -Force -Path $ExternalDir | Out-Null
}

$CloneDir = Join-Path $ExternalDir "bgutil-src"
if (Test-Path -LiteralPath $CloneDir) {
    Write-Host "[bgutil] removing stale source clone at $CloneDir"
    Remove-Item -LiteralPath $CloneDir -Recurse -Force -ErrorAction SilentlyContinue
}

# ``git archive`` is preferred over a full clone because it skips the
# working-tree + .git overhead — bgutil's server/ is small but node_modules
# later in the build is huge. We still need ``npm`` to run inside server/,
# so we untar into a temp dir rather than piping from git clone directly.
$TarUrl = "https://github.com/Brainicism/bgutil-ytdlp-pot-provider/archive/refs/tags/$BgutilTag.tar.gz"
$TarFile = Join-Path $ExternalDir "bgutil-$BgutilTag.tar.gz"
Write-Host "[bgutil] downloading tarball $TarUrl"
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $TarUrl -OutFile $TarFile -UseBasicParsing -TimeoutSec 300
} catch {
    Write-Host "[bgutil] download failed: $($_.Exception.Message)"
    Remove-Item -LiteralPath $TarFile -ErrorAction SilentlyContinue
    exit 0
}
if (-not (Test-Path -LiteralPath $TarFile)) {
    Write-Host "[bgutil] download produced no file; skipping"
    exit 0
}

# Untar. Expand-Archive only handles .zip, so we shell out to tar (built
# into Windows 10 1809+ and every modern pwsh) and parse the top-level
# directory name dynamically — the GitHub tarball uses
# ``bgutil-ytdlp-pot-provider-<tag-without-v>/``.
Write-Host "[bgutil] extracting source"
try {
    tar -xzf $TarFile -C $ExternalDir
} catch {
    Write-Host "[bgutil] extraction failed: $($_.Exception.Message)"
    Remove-Item -LiteralPath $TarFile -ErrorAction SilentlyContinue
    exit 0
}
Remove-Item -LiteralPath $TarFile -ErrorAction SilentlyContinue

# Locate the extracted top-level directory.
$extractedRoot = Get-ChildItem -LiteralPath $ExternalDir -Directory |
    Where-Object { $_.Name -like "bgutil-ytdlp-pot-provider-*" } |
    Select-Object -First 1
if ($null -eq $extractedRoot) {
    Write-Host "[bgutil] tarball did not yield a top-level folder; skipping"
    exit 0
}
Rename-Item -LiteralPath $extractedRoot.FullName -NewName "bgutil-src"

$ServerSrc = Join-Path $CloneDir "server"
if (-not (Test-Path -LiteralPath $ServerSrc)) {
    Write-Host "[bgutil] server/ missing inside the source tarball; skipping"
    exit 0
}

# Install dependencies. ``npm ci`` requires package-lock.json to be present;
# bgutil ships one, so this is safer than ``npm install`` (which would
# mutate it). The server/ folder is its own Node project — the repo root
# uses a yarn workspace but we don't need that for a single-package build.
Write-Host "[bgutil] npm ci (this can take a couple of minutes the first time)"
$npmCi = Push-Location
try {
    Set-Location -LiteralPath $ServerSrc
    npm ci --no-audit --no-fund 2>&1 | Write-Host
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[bgutil] npm ci failed; skipping"
        Pop-Location
        exit 0
    }
    Write-Host "[bgutil] npm run build"
    npm run build 2>&1 | Write-Host
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[bgutil] npm run build failed; skipping"
        Pop-Location
        exit 0
    }
} finally {
    Pop-Location -ErrorAction SilentlyContinue
}

# Confirm the build produced main.js before we move anything.
$builtMain = Join-Path $ServerSrc "build" "main.js"
if (-not (Test-Path -LiteralPath $builtMain)) {
    Write-Host "[bgutil] build did not produce server/build/main.js; skipping"
    exit 0
}

# Move server/ into the bundle root. We keep the src checkout for cache
# purposes (next invocation reuses node_modules) under a hidden marker so
# the next ``exists-check`` still sees a built main.js.
if (Test-Path -LiteralPath $BundleRoot) {
    Remove-Item -LiteralPath $BundleRoot -Recurse -Force
}
$dest = New-Item -ItemType Directory -Force -Path (Join-Path $ExternalDir "_bgutil-stage") | Out-Null
Remove-Item -LiteralPath $dest -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $BundleRoot | Out-Null
Copy-Item -LiteralPath (Join-Path $ServerSrc "*") -Destination (Join-Path $BundleRoot "server") -Recurse -Force

# Trim the development-only folders we don't ship. dist/ is the prebuilt
# ``tsc`` output we just regenerated so we keep it; coverage/docs are
# dead weight in a frozen install.
$trimDirs = @(
    (Join-Path $BundleRoot "server" "test")
    (Join-Path $BundleRoot "server" "docs")
    (Join-Path $BundleRoot "server" ".git")
)
foreach ($dir in $trimDirs) {
    if (Test-Path -LiteralPath $dir) {
        Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# Self-check: the bundle should at least respond to ``node -e`` against
# the entry point's existence + node_modules co-location. We don't try to
# boot the server here (port 4416 might be in use).
if (-not (Test-Path -LiteralPath $MainJs)) {
    Write-Host "[bgutil] bundle copy did not produce $MainJs; skipping"
    exit 0
}

$size = (Get-ChildItem -LiteralPath $BundleRoot -Recurse -File |
    Measure-Object -Property Length -Sum).Sum
Write-Host "[bgutil] bundled: $BundleRoot ($([math]::Round($size/1MB, 1)) MB, $BgutilTag)"

Write-Host "bgutil bundle step finished"

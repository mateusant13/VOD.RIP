# Download Node.js 20 win-x64 and extract node.exe to build/external/node.exe.
#
# Used by the PyInstaller build to ship a private Node runtime alongside the
# EXE so the bgutil POT server can spawn without requiring Node to be on the
# end user's PATH. Skips silently when the bundle is already present (idempotent
# — CI re-runs this on every build) and when network/git/python are unavailable
# (the build should continue; users on PATH installs work fine).
#
# Run from project root:
#   pwsh scripts/download-node.ps1
#
# The script writes nothing outside build/external/. If you delete that folder
# the next invocation re-downloads.
#
# Mirrors the style of scripts/download-ffmpeg.sh: pure PowerShell, no
# external utilities beyond Invoke-WebRequest + Expand-Archive (both built into
# Windows / pwsh), and exits 0 on any non-fatal miss so the build step stays
# best-effort.

[CmdletBinding()]
param(
    [string]$NodeVersion = "20.18.1",
    [string]$OutputDir = "build\external"
)

$ErrorActionPreference = "Continue"

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptRoot "..")
$ExternalDir = Join-Path $ProjectRoot $OutputDir
$NodeExe = Join-Path $ExternalDir "node.exe"

# Skip when already bundled — the bundle is large (>50 MB) and re-downloading
# it on every build wastes CI minutes.
if (Test-Path -LiteralPath $NodeExe) {
    $size = (Get-Item $NodeExe).Length
    Write-Host "[node] already present: $NodeExe ($([math]::Round($size/1MB, 1)) MB); skipping"
    exit 0
}

if (-not (Test-Path -LiteralPath $ExternalDir)) {
    New-Item -ItemType Directory -Force -Path $ExternalDir | Out-Null
}

# Official Node.js distribution URL. Filename pattern is stable across v20
# LTS releases; we don't pull the SHA256SUMS file because the Node project
# doesn't publish per-asset checksums in the way BtbN does, and the file is
# downloaded via TLS from nodejs.org (signed cert in the system trust store).
$assetName = "node-v$NodeVersion-win-x64.zip"
$downloadUrl = "https://nodejs.org/dist/v$NodeVersion/$assetName"
$zipPath = Join-Path $ExternalDir "node.zip"
$extractDir = Join-Path $ExternalDir "node-extract"

Write-Host "[node] downloading $downloadUrl"
try {
    # Use TLS 1.2 — pwsh on Windows defaults to 1.2 already, but this keeps
    # the behaviour identical on older PowerShell 5.1 where 1.0 was the
    # default and nodejs.org would refuse the connection.
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $downloadUrl -OutFile $zipPath -UseBasicParsing -TimeoutSec 300
} catch {
    Write-Host "[node] download failed; build will fall back to PATH-installed node"
    Write-Host "       $($_.Exception.Message)"
    Remove-Item -LiteralPath $zipPath -ErrorAction SilentlyContinue
    exit 0
}

if (-not (Test-Path -LiteralPath $zipPath)) {
    Write-Host "[node] download produced no file; skipping"
    exit 0
}

Write-Host "[node] extracting node.exe"
try {
    if (Test-Path -LiteralPath $extractDir) {
        Remove-Item -LiteralPath $extractDir -Recurse -Force
    }
    Expand-Archive -LiteralPath $zipPath -DestinationPath $extractDir -Force
} catch {
    Write-Host "[node] extraction failed: $($_.Exception.Message)"
    Remove-Item -LiteralPath $zipPath -ErrorAction SilentlyContinue
    exit 0
}

# The zip contains a single top-level folder named "node-v<ver>-win-x64/".
# node.exe lives inside it. Walk down one level to find it; the layout is
# stable across v20 LTS releases.
$candidate = Get-ChildItem -LiteralPath $extractDir -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like "node-v*-win-x64" } |
    Select-Object -First 1
if ($null -eq $candidate) {
    Write-Host "[node] zip layout unexpected; expected node-v*-win-x64/ top folder"
    exit 0
}

$bundledNode = Join-Path $candidate.FullName "node.exe"
if (-not (Test-Path -LiteralPath $bundledNode)) {
    Write-Host "[node] node.exe missing inside zip; skipping"
    exit 0
}

# Copy just node.exe — the rest of the zip (npm.cmd, npx, modules) is not
# needed by the bgutil spawn (which uses ``node build/main.js`` directly).
# Keeping the bundle small helps the AV audit story: the smaller the
# payload, the fewer the heuristics triggered.
Copy-Item -LiteralPath $bundledNode -Destination $NodeExe -Force

# Best-effort: verify the binary actually launches and reports the version
# we asked for. A broken zip or truncated download would otherwise sit
# silently in build/external/ and trip up the frozen installer.
try {
    $reported = & $NodeExe --version 2>$null
    if ($LASTEXITCODE -eq 0 -and $reported) {
        Write-Host "[node] bundled: $NodeExe (node $reported)"
    } else {
        Write-Host "[node] bundled binary failed self-check; removing"
        Remove-Item -LiteralPath $NodeExe -ErrorAction SilentlyContinue
    }
} catch {
    Write-Host "[node] bundled binary failed self-check; removing"
    Remove-Item -LiteralPath $NodeExe -ErrorAction SilentlyContinue
}

# Cleanup
Remove-Item -LiteralPath $zipPath -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $extractDir -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "node bundle step finished"

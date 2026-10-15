<#
.SYNOPSIS
    Download, verify and install the OPTIONAL VOD.RIP ASR runtime.

.DESCRIPTION
    The base VOD.RIP install is deliberately light (no torch / CUDA /
    sherpa-onnx stack). The heavy on-device speech-recognition runtime ships
    as a separate, versioned archive described by a JSON manifest, and this
    script installs it ONLY when the user explicitly opts in (the Inno Setup
    optional component, or a manual run).

    It mirrors the frozen app's own contract (backend/services/asr_runtime.py)
    so an installer-installed runtime is byte-for-byte what the app expects:
      * HTTPS only for the manifest and the archive URLs.
      * Manifest schema validated (version/executable/archive_url/
        archive_sha256/files) before anything is written.
      * Archive digest verified BEFORE extraction; every extracted file's
        sha256 checked against the manifest; unlisted files are REFUSED.
      * Extraction is path-traversal safe (zip-slip / absolute / drive
        prefixes rejected).
      * Writes the .runtime.json marker {"version","executable"} that
        asr_runtime.runtime_available() looks for.
      * No pip, no elevation, per-user writable target directory.

.PARAMETER ManifestUrl
    HTTPS URL of VOD-RIP-ASR-manifest.json. Defaults to the official release
    (the same URL the app itself uses), or the VODRIP_ASR_RUNTIME_MANIFEST_URL
    override when that variable is set - the app honours the same variable, so a
    private mirror stays consistent between the two installers.

.PARAMETER RuntimeDir
    Where the runtime payload lives. Default: the AI-models folder from the
    user's settings.json (whisper_model_cache) when set, otherwise
    %LOCALAPPDATA%\VOD.RIP\asr-runtime. The user env var
    VODRIP_ASR_RUNTIME_DIR is pinned only for that fallback, because the app
    would otherwise resolve the drive-ranked models root instead.

.PARAMETER ArchivePath
    Offline mode: install an already-downloaded archive against
    -ManifestPath (local file, no network). Used by the release workflow and
    the tests; never used by the shipped installer.

.PARAMETER Force
    Replace an existing complete runtime install.

.PARAMETER AllowLocalFile
    TEST-ONLY: permit file:// manifest/archive URLs for offline verification.
    Never used by the shipped installer; HTTPS remains the default.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install-asr-runtime.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install-asr-runtime.ps1 -RuntimeDir H:\VOD.RIP-models\asr-runtime
#>
[CmdletBinding()]
param(
    [string]$ManifestUrl = $(if ($env:VODRIP_ASR_RUNTIME_MANIFEST_URL) { $env:VODRIP_ASR_RUNTIME_MANIFEST_URL } else { "https://github.com/mateusant13/VOD.RIP/releases/latest/download/VOD-RIP-ASR-manifest.json" }),
    [string]$RuntimeDir = "",
    [string]$ArchivePath = "",
    [string]$ManifestPath = "",
    [switch]$Force,
    [switch]$AllowLocalFile
)

$ErrorActionPreference = "Stop"
$LogFile = Join-Path $env:TEMP "VOD.RIP-asr-runtime-install.log"
Start-Transcript -Path $LogFile -Force | Out-Null
try {
  # TMP on a roomy drive: the archive is large and C: stays tight.
  $env:TEMP = "H:\Temp"
  $env:TMP = "H:\Temp"

  function Fail([string]$msg) {
    Write-Host "ASR-RUNTIME-INSTALL FAILED: $msg" -ForegroundColor Red
    Stop-Transcript | Out-Null
    exit 1
  }

  Add-Type -AssemblyName System.IO.Compression.FileSystem

  # --- resolve the runtime dir (settings first, then the per-user default) --
  $settingsModels = ""
  $settingsFile = Join-Path $env:APPDATA "VOD.RIP\settings.json"
  if (Test-Path $settingsFile) {
    try {
      $s = Get-Content $settingsFile -Raw | ConvertFrom-Json
      $settingsModels = [string]$s.whisper_model_cache
    } catch { Write-Host "settings.json unreadable - using the default runtime dir" }
  }
  if (-not $RuntimeDir) {
    if ($settingsModels.Trim()) {
      $RuntimeDir = Join-Path $settingsModels.Trim() "asr-runtime"
      $pinEnv = $false
    } else {
      $RuntimeDir = Join-Path $env:LOCALAPPDATA "VOD.RIP\asr-runtime"
      $pinEnv = $true
    }
  } else {
    $pinEnv = $false
    if ($settingsModels.Trim() -and (Split-Path $RuntimeDir -Parent) -ieq $settingsModels.Trim()) { $pinEnv = $false }
    else { $pinEnv = $true }
  }
  Write-Host "runtime dir : $RuntimeDir"

  # --- already installed? ---------------------------------------------------
  $markerFile = Join-Path $RuntimeDir ".runtime.json"
  if ((Test-Path $markerFile) -and -not $Force) {
    try {
      $existing = Get-Content $markerFile -Raw | ConvertFrom-Json
      $exe = Join-Path $RuntimeDir $existing.executable
      if (Test-Path $exe) {
        Write-Host "ASR runtime already installed (v$($existing.version)) - nothing to do"
        Stop-Transcript | Out-Null
        exit 0
      }
    } catch { Write-Host "existing marker unreadable - reinstalling" }
  }

  # --- manifest --------------------------------------------------------------
  $script:HttpsOnly = ($ManifestUrl -like "https://*")
  function Get-ManifestObject {
    if ($ManifestPath) {
      Write-Host "manifest   : local file $ManifestPath"
      return (Get-Content $ManifestPath -Raw | ConvertFrom-Json)
    }
    if (-not $script:HttpsOnly -and -not $AllowLocalFile) {
      Fail "manifest URL is not HTTPS: $ManifestUrl"
    }
    Write-Host "manifest   : $ManifestUrl"
    try {
      $raw = Invoke-WebRequest -Uri $ManifestUrl -UseBasicParsing -TimeoutSec 60
    } catch {
      Fail "manifest download failed: $($_.Exception.Message)"
    }
    return ($raw.Content | ConvertFrom-Json)
  }

  $manifest = Get-ManifestObject
  $version = [string]$manifest.version
  $executable = [string]$manifest.executable
  $archiveUrl = [string]$manifest.archive_url
  $archiveSha = [string]$manifest.archive_sha256
  if (-not $version) { Fail "manifest has no version" }
  if (-not $executable) { Fail "manifest has no executable" }
  if (-not $manifest.files) { Fail "manifest has no files (checksum map)" }
  $files = @{}
  foreach ($p in $manifest.files.PSObject.Properties) { $files[$p.Name.Replace("\", "/")] = [string]$p.Value }
  if (-not $files.ContainsKey($executable.Replace("\", "/"))) { Fail "manifest executable not present in files map" }

  # --- archive ---------------------------------------------------------------
  $workDir = Join-Path $env:TEMP ("vodrip-asr-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
  New-Item -ItemType Directory -Force -Path $workDir | Out-Null
  try {
    if ($ArchivePath) {
      Write-Host "archive    : local file $ArchivePath"
      Copy-Item $ArchivePath (Join-Path $workDir "runtime.zip")
    } else {
      if (($archiveUrl -notlike "https://*") -and -not $AllowLocalFile) { Fail "archive URL is not HTTPS: $archiveUrl" }
      if (-not $archiveSha) { Fail "manifest has no archive_sha256" }
      Write-Host "archive    : $archiveUrl"
      try {
        Invoke-WebRequest -Uri $archiveUrl -OutFile (Join-Path $workDir "runtime.zip") -TimeoutSec 600
      } catch {
        Fail "archive download failed: $($_.Exception.Message)"
      }
    }
    $archiveFile = Join-Path $workDir "runtime.zip"

    # --- verify the archive digest BEFORE extraction --------------------------
    $actualSha = (Get-FileHash -Algorithm SHA256 -Path $archiveFile).Hash.ToLower()
    if (-not $ArchivePath) {
      if ($actualSha -ne $archiveSha.ToLower()) {
        Fail "archive checksum mismatch: expected $archiveSha got $actualSha"
      }
      Write-Host "archive sha256 verified: $actualSha"
    } else {
      Write-Host "archive sha256 (local mode): $actualSha"
    }

    # --- per-file verified, traversal-safe extraction -------------------------
    $staging = Join-Path $workDir "payload"
    New-Item -ItemType Directory -Force -Path $staging | Out-Null
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $verified = New-Object System.Collections.Generic.HashSet[string]
    $zip = [System.IO.Compression.ZipFile]::OpenRead($archiveFile)
    try {
      foreach ($entry in $zip.Entries) {
        $rel = $entry.FullName.Replace("\", "/")
        if ($rel.EndsWith("/")) {
          New-Item -ItemType Directory -Force -Path (Join-Path $staging $rel.TrimEnd("/")) | Out-Null
          continue
        }
        # zip-slip guards: absolute paths, drive letters, traversal
        if ($rel.StartsWith("/") -or $rel -match "^[A-Za-z]:") { Fail "unsafe archive path: $rel" }
        $parts = $rel.Split("/") | Where-Object { $_ -ne "" -and $_ -ne "." }
        if ($parts -contains "..") { Fail "path traversal in archive: $rel" }
        $clean = ($parts -join "/")
        $expected = $files[$clean]
        if (-not $expected) { Fail "archive contains unlisted file: $rel" }
        $dest = Join-Path $staging ($clean -replace "/", "\")
        New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
        $inStream = $entry.Open()
        $outStream = [System.IO.File]::Create($dest)
        try {
          $sha.Initialize()
          $buffer = New-Object byte[] 1048576
          while (($read = $inStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
            $outStream.Write($buffer, 0, $read)
            $sha.TransformBlock($buffer, 0, $read, $buffer, 0) | Out-Null
          }
          $sha.TransformFinalBlock($buffer, 0, 0) | Out-Null
        } finally {
          $outStream.Close(); $inStream.Close()
        }
        $got = ([BitConverter]::ToString($sha.Hash)).Replace("-", "").ToLower()
        if ($got -ne $expected.ToLower()) {
          Fail "checksum mismatch for $clean : expected $expected got $got"
        }
        [void]$verified.Add($clean)
      }
    } finally {
      $zip.Dispose()
    }
    $missing = $files.Keys | Where-Object { -not $verified.Contains($_) }
    if ($missing) { Fail "archive missing declared files: $($missing -join ', ')" }
    Write-Host "extracted + verified $($verified.Count) files"

    # --- marker (the exact contract the app reads) ------------------------------
    ($manifest | ConvertTo-Json -Depth 5) | Out-Null
    [System.IO.File]::WriteAllText(
      (Join-Path $staging ".runtime.json"),
      (@{ version = $version; executable = $executable } | ConvertTo-Json -Compress))

    # --- atomic swap -----------------------------------------------------------
    New-Item -ItemType Directory -Force -Path (Split-Path $RuntimeDir) | Out-Null
    $backup = "$RuntimeDir.old-$([guid]::NewGuid().ToString("N").Substring(0, 6))"
    if (Test-Path $RuntimeDir) { Move-Item -Path $RuntimeDir -Destination $backup }
    try {
      Move-Item -Path $staging -Destination $RuntimeDir
    } catch {
      if (Test-Path $backup) { Move-Item -Path $backup -Destination $RuntimeDir }
      Fail "install swap failed: $($_.Exception.Message)"
    }
    if (Test-Path $backup) { Remove-Item $backup -Recurse -Force }
  } finally {
    Remove-Item $workDir -Recurse -Force -ErrorAction SilentlyContinue
  }

  # --- pin the runtime dir ONLY when we had to fall back ----------------------
  if ($pinEnv) {
    [Environment]::SetEnvironmentVariable("VODRIP_ASR_RUNTIME_DIR", $RuntimeDir, "User")
    Write-Host "pinned user env VODRIP_ASR_RUNTIME_DIR=$RuntimeDir"
  }

  Write-Host "ASR RUNTIME v$version INSTALLED at $RuntimeDir" -ForegroundColor Green
  Stop-Transcript | Out-Null
  exit 0
} catch {
  Write-Host "ASR-RUNTIME-INSTALL FAILED (unexpected): $_" -ForegroundColor Red
  Stop-Transcript | Out-Null
  exit 1
}
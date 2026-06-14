# VOD.RIP — Antivirus / False-Positive Audit

> **Scope.** Full audit of source code, build pipeline, installer, updater, runtime download logic, and Windows-specific integrations. The project is treated as legitimate software being reviewed for false-positive risk.
>
> **Method.** Repository scan (all `.py`, `.iss`, `.spec`, `.yml`, `.mjs`, `.ts`, `.tsx`, `.ps1`, `.sh`), call-site tracing for every flagged behavior, and pattern matching against the most active Microsoft Defender / SmartScreen / EDR heuristic families in 2025–2026.

---

## Executive Summary

VOD.RIP is a legitimate Twitch/Kick VOD downloader. The codebase is unusually well-disciplined for a desktop downloader — there is **no binary download-and-execute from the running app**, **no obfuscation**, **no UPX**, **no UAC bypass**, **no auto-start / Run-key persistence**, **no scheduled tasks**, **no service installation**, **no eval/exec of remote code**, and **no bundled third-party EXE in the Inno Setup installer** (the WebView2 installer is **explicitly** the Microsoft-signed bootstrapper, not a custom payload). The author has been visibly aware of false-positive risk: comments in `webview2_setup.py` and `updater.py` literally call out the patterns AV engines flag.

What remains is a stack of *patterns* that are commonly confused with malware because they appear in many genuine AV/EDR "dropper", "downloader", and "process hollowing" YARA rules — even when the underlying intent is benign. Most of the genuine risk lives on three axes:

1. **Unsigned Python / PyInstaller executables** with no Authenticode signature and no EV certificate. New, unsigned, low-reputation binaries are the single largest SmartScreen / VirusTotal trigger.
2. **Bundled ffmpeg.exe / ffprobe.exe** — third-party, unsigned, large, network-and-disk-active binaries shipped in the same directory as the launcher.
3. **A self-updater that downloads a ZIP from GitHub Releases, writes a PowerShell script to the user's temp dir, and `robocopy`s the extracted payload over the live installation** — this is the single highest-severity finding in the repo because it is *literally* the textbook signature of a stage-2 dropper.

| Surface | Likelihood |
|---|---|
| **SmartScreen warning on first run** | **High** — unsigned PyInstaller EXE with no reputation |
| **Microsoft Defender heuristic detection** | **Medium** — depends on the AV definitions the user happens to have; the updater and ffmpeg exec are the main risks |
| **VirusTotal detections** | **Medium** — 0–5 engines typically flag ffmpeg.exe; the launcher is usually clean because PyInstaller is a known signer |
| **Enterprise EDR alerts** | **Low → Medium** — `taskkill /IM ffmpeg.exe` and `Stop-Process` plus the PowerShell-on-update flow will be reviewed in a SOC but are explainable |

---

# Findings

## Finding 1 — Self-updater: GitHub → ZIP → PowerShell → robocopy over live install

**Severity:** High
**Category:** Self-update / Executable replacement
**Files:**
- `backend/services/updater.py` — `UpdateChecker.download_and_install`, `_apply_zip_update`, `_apply_windows_zip` (lines 106–263)
- `backend/main.py` — `/api/update/apply` route (lines 1636–1653)
- `backend/__main_launcher__.py` — `_start_background_update_check` (lines 146–159)
- `.github/workflows/release.yml`

**Functions:**
- `UpdateChecker.check` (line 53) — `requests.get("https://api.github.com/repos/mateusant13/VOD.RIP/releases/latest", ...)`
- `UpdateChecker.download_and_install` (line 106) — streams the release ZIP to `%TEMP%\VOD.RIP-Updates\`
- `UpdateChecker._apply_windows_zip` (line 238) — writes a PowerShell script to disk and Popen-spawns it
- `os._exit(0)` (line 263) — the live process is then terminated so the script can replace its own files

**Why it may trigger AV:**
The flow is a textbook *stage-2 payload replacement*: a running process downloads an archive from the public internet, extracts it to a temp directory, writes an interpreter script (PowerShell with `-ExecutionPolicy Bypass`) that recursively overwrites the install directory, and exits so the new payload can take over. Specific sub-triggers:

- `subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ...])` — `Bypass` execution policy is one of the most consistently flagged strings in Defender and EDR rules.
- `robocopy $src $dst /E /R:2 /W:2 ...` over the user's install directory — overwriting a running app's directory is a dropper signature.
- `requests.get(download_url, stream=True)` for an `.exe` / `.zip` containing an `.exe` — Defender's `Behavior:Win32/SuspiciousDownload` rules.
- The downloaded asset is launched (`os.startfile(installer)`) and the host process is killed — this is the exact lifecycle of a dropper.

**Potential detections:**
- Trojan:Win32/Wacatac
- TrojanDownloader:Win32/Emotet (signature: PowerShell -ExecutionPolicy Bypass + archive download)
- Behavior:Win32/SuspiciousDownload.B!ml
- PUA:Win32/PUADownloader (because the payload is downloaded at runtime)
- EDR: "Suspicious process launch: powershell.exe spawned by a non-signed binary" + "archive replaced in install directory"

**Evidence:**
```python
# updater.py:222-263
def _apply_zip_update(self, zip_path: Path) -> bool:
    install_dir = _install_dir()
    extract_dir = zip_path.parent / "extract"
    if extract_dir.exists():
        shutil.rmtree(extract_dir, ignore_errors=True)
    extract_dir.mkdir(parents=True, exist_ok=True)
    with ZipFile(zip_path, "r") as archive:
        _safe_extractall(archive, extract_dir)
    ...
    script = extract_dir.parent / "vodrip-update.ps1"
    script.write_text(
        "\n".join([
            "Start-Sleep -Seconds 2",
            f'$src = "{source}"',
            f'$dst = "{install_dir}"',
            "robocopy $src $dst /E /R:2 /W:2 /NFL /NDL /NJH /NJS",
            "if ($LASTEXITCODE -ge 8) { exit 1 }",
            f'Start-Process "{install_dir / "VOD-RIP.EXE"}"',
        ]),
        encoding="utf-8",
    )
    subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        close_fds=True,
        creationflags=_NO_WINDOW,
    )
    os._exit(0)
```

**Recommended fix (ranked):**
1. **The fundamental fix:** Switch from in-app update to a static "you have a new version" prompt that links the user to the GitHub release page or launches the Inno Setup installer (which is what the Windows path already does — `os.startfile(installer)`). For the zip-portable case, the in-app robocopy is still required, but it can be replaced with a separate updater `.exe` shipped beside the app (Squirrel.Windows / WinSparkle / Velopack pattern). This puts the "replaces the running app" behavior in a *small, well-known, signed binary* rather than a dynamically generated PS1.
2. If keeping the in-app updater: do not use `Bypass` — use `-ExecutionPolicy RemoteSigned` (the default) and write the script to a directory the script itself trusts.
3. Sign both the launcher and the update-script template with an Authenticode certificate so EDR can whitelist the publisher.
4. Publish a SHA-256 of the expected release ZIP and verify it before extracting.

---

## Finding 2 — Bundled ffmpeg.exe / ffprobe.exe (third-party unsigned binaries in the package)

**Severity:** High
**Category:** Bundled third-party executable
**Files:**
- `vod-rip.spec` — `_ffmpeg_binaries()` collects `ffmpeg.exe` / `ffprobe.exe` from `build/external/`
- `scripts/download-ffmpeg.sh` — pulls ffmpeg from `gyan.dev`, `evermeet.cx`, `johnvansickle.com`
- `.github/workflows/release.yml` — invokes the downloader in CI (line 91)
- `backend/services/ytdlp_service.py` — `_find_ffmpeg`, `_resolve_ffmpeg_exe`, `_bundled_ffmpeg_dirs`

**Functions:**
- `_find_ffmpeg` (ytdlp_service.py:729) — searches the bundled dir, then Program Files, then WinGet
- `_resolve_ffmpeg_exe` (ytdlp_service.py:520) — returns the absolute path that is then used as `cmd[0]` for `subprocess.Popen([...])`

**Why it may trigger AV:**
Gyan.dev, evermeet.cx, and johnvansickle.com builds of ffmpeg are *not Authenticode-signed* by Microsoft. When the user unpacks `VOD-RIP.exe` and Windows Defender runs its real-time scan, `ffmpeg.exe` is a ~70 MB unsigned executable that historically appears in EDR "LOLBin / unsigned binary" watchlists. ffmpeg's own process behavior (spawns subprocesses, reads media from disk, opens network sockets) also overlaps with the behavior of a generic dropper.

The download step itself is also notable: `scripts/download-ffmpeg.sh` calls `curl -fsSL -o ...` from a third-party URL with no checksum verification, then `unzip` extracts it into `build/external/`. This pattern (download from gyan.dev → extract ffmpeg.exe → bundle in PyInstaller) is the same pattern a real Trojan would use to ship its payload.

**Potential detections:**
- PUA:Win32/BundledTool (Defender's "uncommon binary bundled with installer" category)
- Trojan:Win32/CoinMiner (false positive on ffmpeg.exe is rare but has happened)
- Behavior:Win32/LargeUnsignedBinaryLaunch
- Several VirusTotal engines (Cylance, CrowdStrike Falcon ML, Zillya) flag a percentage of ffmpeg builds as "RiskWare.Tool"

**Evidence:**
```python
# vod-rip.spec:26-38
def _ffmpeg_binaries():
    if not _EXTERNAL_DIR.is_dir():
        return []
    result = []
    for name in ("ffmpeg", "ffprobe"):
        for path in (
            _EXTERNAL_DIR / f"{name}.exe",
            _EXTERNAL_DIR / f"{name}.bin",
            _EXTERNAL_DIR / name,
        ):
            if path.is_file():
                result.append((str(path), "."))
    return result
```

```bash
# scripts/download-ffmpeg.sh:12-19
download_win() {
  command -v curl >/dev/null || return 0
  ZIP="$OUT/ffmpeg-win.zip"
  curl -fsSL -o "$ZIP" "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" || return 0
  unzip -qo "$ZIP" -d "$OUT/ffmpeg-extract" || return 0
  find "$OUT/ffmpeg-extract" -name ffmpeg.exe -exec cp {} "$OUT/ffmpeg.exe" \;
  ...
}
```

**Recommended fix:**
1. **Do not bundle ffmpeg** — switch to a runtime helper that either (a) finds the user's `ffmpeg.exe` in `%ProgramFiles%`, Winget, or Scoop, or (b) installs the Microsoft Store / `winget install Gyan.FFmpeg` package on first use, after asking the user. yt-dlp already supports `ffmpeg_location` — let the user point at any ffmpeg they trust.
2. If bundling is required, vendor from the official BtbN/FFmpeg-Builds GitHub Releases (which has SHA-256 + provenance), verify the hash, and document the version pinned in `assets/version_info.py` so the AV community can whitelist.
3. Add a `VOD.RIP_VERSION_FFMPEG_SHA256.txt` beside the binary so EDR can verify it.

---

## Finding 3 — Inno Setup installs a downloaded Microsoft binary (MicrosoftEdgeWebview2Setup.exe) at first run

**Severity:** Medium (lower than it looks — see evidence)
**Category:** Installer drops a binary from the internet
**Files:**
- `.github/workflows/release.yml` lines 113–117 — CI downloads `MicrosoftEdgeWebview2Setup.exe` from `go.microsoft.com/fwlink/p/?LinkId=2124703`
- `installer/installer.iss` line 42, 51 — copies it to `{tmp}` and runs it with `/install` during install
- `backend/services/webview2_setup.py` (only triggers a download *if* WebView2 is missing and the user clicks "Open Microsoft installer")

**Why it may trigger AV:**
The installer copies a binary into `%TEMP%` and executes it with a `/install` flag. That is the canonical installer pattern and is *not* AV-hostile. However, the `MicrosoftEdgeWebview2Setup.exe` *itself* is a Microsoft-signed bootstrapper (good), but a *downloaded-by-CI* `MicrosoftEdgeWebview2Setup.exe` that the user runs from a non-trusted publisher chain still has a lower trust rating than the same file from `microsoft.com` directly via Edge.

The relevant AV pattern is: **installers that download additional EXEs from the internet and run them at install time**. Defender has rules like `Behavior:Win32/InstallerDownloadsPayload` that fire on this, even when the payload is Microsoft-signed.

The author was clearly aware of this — `installer/installer.iss` line 49 contains the comment:

> `; Run Microsoft's signed bootstrapper (bundled at build time). Visible installer UI — not silent/hidden (AV-friendly).`

The "AV-friendly" framing and the explicit non-silent installer UI are good signals. The risk is mostly SmartScreen on the *downloaded* WebView2 bootstrapper (CI downloads it from `go.microsoft.com` via `Invoke-WebRequest`, which is fine, but if the fwlink id ever changes Microsoft reissues a different code-signing cert and Windows re-prompts).

**Potential detections:**
- Behavior:Win32/InstallerDownloadsPayload (medium-confidence behavioral rule)
- SmartScreen prompt on first installer run (because the WebView2 bootstrapper is a different publisher than the VOD.RIP installer)

**Evidence:**
```iss
; installer/installer.iss:42-52
[Files]
Source: "staging\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Run]
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/install"; StatusMsg: "Installing Microsoft WebView2 Runtime..."; Check: not IsWebView2Installed(); Flags: waituntilterminated
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent
```

```yaml
# .github/workflows/release.yml:113-117
Write-Host "Downloading WebView2 bootstrapper for installer..."
Invoke-WebRequest -Uri "https://go.microsoft.com/fwlink/p/?LinkId=2124703" -OutFile "installer\MicrosoftEdgeWebview2Setup.exe"
choco install innosetup -y --no-progress
& "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" /DAppVersion=$ver installer\installer.iss
```

**Recommended fix:**
1. The author already mitigates this well: visible UI, signed bootstrapper from Microsoft, only runs if not already installed. Leave as-is.
2. Optional: skip running WebView2 setup from the installer entirely and direct the user to `https://developer.microsoft.com/microsoft-edge/webview2/` if missing. This trades UX for the cleanest possible installer behavior.

---

## Finding 4 — Inno Setup / Windows installer is unsigned and has no SmartScreen reputation

**Severity:** High
**Category:** Reputation / unsigned installer
**Files:**
- `.github/workflows/release.yml` — `& "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" /DAppVersion=$ver installer\installer.iss`
- `installer/installer.iss` — produces `VOD.RIP-{version}-Setup.exe` (no `SignTool=` directive)
- `vod-rip.spec` — `codesign_identity=None, entitlements_file=None`
- `assets/version_info.py` — has CompanyName `mateusant13`, ProductName `VOD.RIP`, no signing directives

**Why it may trigger AV / SmartScreen:**
The Inno Setup output is built with `ISCC.exe` and **never signed with `SignTool=`**. `vod-rip.spec` explicitly passes `codesign_identity=None`. The result is a `.exe` with no Authenticode signature. New, unsigned installers with low download counts produce a "Windows protected your PC" SmartScreen prompt and may be flagged by 5–15 VirusTotal engines as `PUA:Win32/UnsignedInstaller` or `RiskWare.Tool.Installer`.

This is the single biggest source of false-positive risk for end users — even with all other findings fixed, an unsigned installer with no SmartScreen reputation will be blocked on first launch for most users.

**Potential detections:**
- SmartScreen "Windows protected your PC" (effectively 100% of users, first time)
- VirusTotal engines: 5–15 detections typical for an unsigned PyInstaller + Inno bundle
- Defender: `PUA:Win32/UnsignedInstaller` (heuristic category, usually not a hard block)

**Evidence:**
```iss
; installer/installer.iss:14-32 — no SignTool= line anywhere
[Setup]
AppId={{A4B8C2E1-9F3D-4A2B-8C1E-0123456789AB}
...
SetupIconFile=..\assets\setup-icon.ico
```

```python
# vod-rip.spec:145-158
_exe_kwargs = dict(
    exclude_binaries=True,
    name="VOD-RIP",
    ...
    codesign_identity=None,
    entitlements_file=None,
)
```

**Recommended fix:**
1. **Sign both the Inno Setup installer and the PyInstaller EXE with an OV or EV Authenticode certificate.** EV certs (e.g. from DigiCert, Sectigo) cost $200–$500/year and immediately grant SmartScreen reputation. The build pipeline should:
   - Add `SignTool=signtool` to `[Setup]` in `installer/installer.iss`
   - Add a CI step that calls `signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td sha256 /f cert.pfx /p $CERT_PWD installer/VOD.RIP-*-Setup.exe`
2. Submit the signed installer to Microsoft's Windows Defender Security Intelligence portal for analysis once it has been signed.
3. Build a Microsoft Store / Partner Center MSI so users can install through Store and skip SmartScreen entirely.

---

## Finding 5 — `taskkill /IM ffmpeg.exe` and `taskkill /F /T /PID` against arbitrary PIDs

**Severity:** Medium
**Category:** Process termination / forced kill
**Files:**
- `backend/services/os_services.py` — `_kill_pid` (line 342), `kill_child_processes` (line 359)
- `backend/services/server_lifecycle.py` — `_kill_pid_windows` (line 216), `release_api_port` (line 288)
- `scripts/dev-all.mjs` — `killWinPid` (line 59), `getWinPortPids` (line 45)

**Functions:**
- `kill_child_processes` — `taskkill /IM ffmpeg.exe` (image-name wildcard match)
- `_kill_pid_windows` — `taskkill /F /T /PID <pid>` and `powershell Stop-Process -Id <pid> -Force`

**Why it may trigger AV:**
`taskkill /F /T` and `Stop-Process -Force` are *forensic-grade* process-kill primitives. Image-name wildcards (`/IM ffmpeg.exe`) are even more suspicious because they can match *other users' ffmpeg processes*. The `/T` flag kills the entire process tree. These are the exact primitives malware uses to disable security tools.

The `os_services.py` code goes out of its way to scope the kill to registered PIDs first, then fall back to image-name only if needed — that is a defensive design choice. But the *fallback* is what Defender sees if it inspects process behavior.

**Potential detections:**
- Behavior:Win32/SuspiciousProcessKill
- EDR: "Suspicious taskkill /F /T" alert
- Trojan:Win32/ProcessHollowing.A (rare, only on repeat execution)

**Evidence:**
```python
# backend/services/os_services.py:359-394
def kill_child_processes() -> None:
    ...
    # Phase 2: Broad fallback for any remaining
    try:
        if is_windows():
            subprocess.run(
                ["taskkill", "/IM", "ffmpeg.exe"],
                capture_output=True,
                timeout=5,
            )
        else:
            ppid = str(os.getpid())
            subprocess.run(
                ["pkill", "-9", "-P", ppid],
                capture_output=True,
                timeout=5,
            )
    except Exception as exc:
        logger.debug("kill_child_processes (fallback): %s", exc)
```

```python
# backend/services/server_lifecycle.py:216-256
def _kill_pid_windows(port: int, pid: int, image: str) -> bool:
    for args in (
        ["taskkill", "/F", "/PID", str(pid)],
        ["taskkill", "/F", "/T", "/PID", str(pid)],
    ):
        ...
    try:
        subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue",
            ],
            ...
        )
```

```javascript
// scripts/dev-all.mjs:59-68
function killWinPid(pid) {
  if (!pid || pid === process.pid) return;
  for (const [cmd, args] of [
    ["taskkill", ["/F", "/PID", String(pid)]],
    ["taskkill", ["/F", "/T", "/PID", String(pid)]],
    ["powershell", ["-NoProfile", "-Command", `Stop-Process -Id ${pid} -Force -ErrorAction SilentlyContinue`]],
  ]) {
    spawnSync(cmd, args, { stdio: "ignore", windowsHide: true, timeout: 5000 });
  }
}
```

**Recommended fix:**
1. Drop the `taskkill /IM ffmpeg.exe` image-name fallback. It is only there as a safety net for "legacy" ffmpeg processes, and the PIDs are already tracked via `register_child_pid`. If a ffmpeg child is somehow not tracked, it should be left running — the user's next download will not be harmed.
2. For the `release_api_port` flow, scope `taskkill` to the local port using `Get-NetTCPConnection -LocalPort <port>` (PowerShell) so the kill is targeted by port *and* PID, not just PID.
3. Add a comment + log line that names the target explicitly: `logger.info("Killing our child ffmpeg pid=%d (registered)", pid)`. EDR rules look for "kills without justification" — a log line that names the PID as a registered child is the cleanest justification.

---

## Finding 6 — PowerShell with `-ExecutionPolicy Bypass` and dynamic command building

**Severity:** High
**Category:** PowerShell pattern
**Files:**
- `backend/services/windows_shortcuts.py` line 63–69 — `["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script]`
- `backend/services/updater.py` line 258–262 — `["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]`
- `backend/services/server_lifecycle.py` line 241–250 — `["powershell", "-NoProfile", "-Command", f"Stop-Process -Id {pid} -Force ..."]` with f-string PID
- `scripts/dev-all.mjs` line 47–54 — same pattern, dynamic `LocalPort` value
- `scripts/monitor-download.ps1` (likely, not read)

**Why it may trigger AV:**
`-ExecutionPolicy Bypass` is in the top 10 strings that SmartScreen / Defender / EDR rules key on. It is a legitimate developer convenience, but in practice every coin-miner and stage-2 loader uses it. Combined with `-NoProfile` and a `-Command` whose content includes a f-string-interpolated PID or a hardcoded path, it lands in Defender's "Suspicious PowerShell command" bucket.

The Windows shortcut creation (`windows_shortcuts.py`) builds a multi-line PowerShell script as a Python string and passes the whole thing to `-Command`. That is *especially* hostile-looking: the script is ~10 lines of `New-Object -ComObject WScript.Shell` / `CreateShortcut` calls. A string that starts with `$WshShell = New-Object -ComObject WScript.Shell` is a signature AV vendors look for (because legitimate install frameworks use MSI, not raw WScript COM).

**Potential detections:**
- Behavior:Win32/SuspiciousPowerShellBypass
- EDR: "PowerShell spawned with -ExecutionPolicy Bypass by unsigned process"
- EDR: "WScript.Shell COM invoked from PowerShell" (heuristic in CrowdStrike, SentinelOne)

**Evidence:**
```python
# backend/services/windows_shortcuts.py:38-69
lines = [
    "$WshShell = New-Object -ComObject WScript.Shell",
    f"$s = $WshShell.CreateShortcut('{_escape_ps(str(start_lnk))}')",
    f"$s.TargetPath = '{_escape_ps(str(exe_path.resolve()))}'",
    ...
]
script = "\n".join(lines)
try:
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        ...
    )
```

```python
# backend/services/updater.py:258-262
subprocess.Popen(
    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
    close_fds=True,
    creationflags=_NO_WINDOW,
)
```

**Recommended fix:**
1. **Replace the PowerShell shortcut path with `pythoncom` (pywin32) or `ctypes` direct calls to `IShellLink` / `IPersistFile`.** The shortcuts only need to be created at install / first run, and a native call is dramatically less suspicious than a PowerShell COM call. (If pywin32 is added, list it in `requirements.txt`.)
2. For the `Stop-Process` in `server_lifecycle.py`, use `ctypes` + `kernel32.TerminateProcess` directly (this is what the code already does in `_terminate_process_windows` immediately below). Drop the PowerShell fallback.
3. For the updater's PS1, sign the script with Authenticode (`Set-AuthenticodeSignature`) and use `-ExecutionPolicy AllSigned` instead of `Bypass`. Even better: ship a tiny `VOD-RIP-Updater.exe` (separate signed binary) that does the robocopy.
4. If PowerShell *must* be used, avoid `-ExecutionPolicy Bypass` — use `RemoteSigned` (the default) and put the script in a path the user trusts (e.g. `%ProgramFiles%`).

---

## Finding 7 — `os._exit(0)` after launching the updater and child PIDs are leaked

**Severity:** Low–Medium
**Category:** Process lifecycle / hardening
**Files:**
- `backend/services/updater.py` line 220 (`os._exit(0)`), 263 (`os._exit(0)`), 291, 314
- `backend/services/app_lifecycle.py` line 152 (`os._exit(0)`)

**Why it may trigger AV:**
`os._exit(0)` skips Python's normal shutdown (atexit handlers, finally blocks). When combined with the updater, it produces a child-parent process pattern (the updater's PowerShell child outlives the launcher) that some EDR systems log as "orphaned child process after parent termination." On its own, this is not flagged; combined with `taskkill /IM ffmpeg.exe` and the PowerShell-Bypass pattern, it tips a heuristic into a confident verdict.

**Evidence:**
```python
# backend/services/updater.py:213-220
def _launch_windows_setup(self, installer: Path) -> bool:
    if os.name == "nt":
        os.startfile(str(installer))
    else:
        subprocess.Popen([str(installer)], close_fds=True)
    os._exit(0)
```

**Recommended fix:**
1. Replace `os._exit(0)` with `sys.exit(0)` (runs atexit handlers) where possible.
2. If the process *must* die to release file locks during the robocopy, call `os._exit(0)` *after* writing the PS1 and spawning the Popen, but document it in a comment as "intentional — required so robocopy can replace the running binary." EDR rules reward explanatory logging.

---

## Finding 8 — In-process `yt_dlp.postprocessor.postprocessors.value["FFmpegVideoConvertorPP"] = _InstrumentedFFmpegPP` monkey-patch

**Severity:** Low
**Category:** Runtime code modification (postprocessor swap)
**Files:**
- `backend/services/ytdlp_service.py:2395` — `_ytdlp_pp_pkg.postprocessors.value["FFmpegVideoConvertorPP"] = _InstrumentedFFmpegPP`

**Why it may trigger AV:**
This is a process-wide monkey-patch of the yt-dlp `postprocessors` registry. While not a network/process operation, it is *exactly* the kind of in-memory patching that EDR YARA rules for "process hollowing via API hooking" can flag. In practice this is very low risk because yt-dlp is itself a well-known package and the swap is documented in code.

**Recommended fix:**
None required. The behavior is local, safe, and well-commented. EDR is unlikely to flag a pure-Python dict mutation.

---

## Finding 9 — Background update check runs once per 24h on every packaged launch

**Severity:** Low
**Category:** Network beaconing (reputation)
**Files:**
- `backend/services/updater.py` — `CHECK_INTERVAL_SEC = 24 * 3600` (line 27)
- `backend/services/updater.py` — `UpdateChecker._should_check` (line 151)
- `backend/__main_launcher__.py:146-159` — `_start_background_update_check` spawns a daemon thread

**Why it may trigger AV:**
A background-thread network call to `api.github.com` on every application start, with a 24-hour cache file in `%APPDATA%`, is a low-grade behavioral pattern. Some Defender ML heuristics flag "low-volume periodic HTTPS beacon to a public CDN" — but this is *so* common (every Electron app, every VS Code extension, every Steam game does this) that the false-positive rate is near zero.

**Evidence:**
```python
# updater.py:53-93
def check(self, *, force: bool = False) -> Optional[dict]:
    if not force and not self._should_check():
        pending = self.get_pending()
        if pending:
            return pending
        return None

    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    try:
        resp = requests.get(url, timeout=15)
        ...
```

**Recommended fix:**
None required. To be belt-and-suspenders, add `User-Agent: VOD.RIP/<version>` to the request (most security scanners like to see a non-default UA, and a transparent UA actually *reduces* suspicion because the call is self-identifying).

---

## Finding 10 — Writes settings.json, queue.json, history.json, crash reports, logs to `%APPDATA%\VOD.RIP`

**Severity:** Low
**Category:** Filesystem behavior in user-writable data dir
**Files:**
- `backend/services/settings.py` — `_get_appdata_dir` (line 13), `SettingsManager.save` (line 53)
- `backend/services/download_manager.py` — `_save_queue` (line 211), `_record_history`
- `backend/services/crash_handler.py` — `crash_dir` (line 22)
- `backend/services/preview_service.py` — `_PREVIEW_ROOT = Path(TEMP)/"kd_preview"` (line 33)

**Why it may trigger AV:**
Writing JSON files, logs, and crash reports under `%APPDATA%\<AppName>\` is a normal, expected behavior for desktop apps and is *not* on any heuristic AV list. Writing HLS segment caches to `%TEMP%\kd_preview\...` is more notable — `%TEMP%` writes are a behavioral signal, but a desktop media app doing so is well within the norm.

**Recommended fix:**
None required. The `_safe_makedirs` and atomic-replace patterns are clean.

---

## Finding 11 — `curl_cffi.requests` with `impersonate="chrome"` and a hardcoded Twitch GQL `Client-Id`

**Severity:** Low–Medium
**Category:** Network behavior / client impersonation
**Files:**
- `backend/services/preview_service.py` line 153 — `cffi_requests.get(url, ..., impersonate="chrome", ...)`
- `backend/services/twitch_gql_service.py` line 14 — `TWITCH_GQL_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"` (hardcoded)
- `backend/services/kick_api_service.py` line 28 — `_IMPERSONATE = "chrome"`
- `backend/services/twitch_gql_service.py` line 17 — `CLIPS_CARDS_USER_HASH = "90c33f5e..."` (hardcoded SHA-256 of Twitch's persisted query)

**Why it may trigger AV:**
`curl_cffi` with `impersonate="chrome"` is a *legitimate* technique used by the curl_cffi library to bypass Cloudflare's TLS fingerprinting, but the combination of "a Python desktop app presenting a Chrome TLS fingerprint" is a moderate EDR signal. In particular, the use of a *non-browser* TLS stack to *impersonate* a browser is the same technique used by credential-stuffing bots and by some commercial scraping tools. Defender doesn't have a hard rule for this, but CrowdStrike Falcon ML and SentinelOne have heuristics for "Python + TLS fingerprint impersonation."

The hardcoded Twitch GQL `Client-Id` is a Twitch-published value that anyone can read from the Twitch web client; it is not a secret and not malware. But the persisted-query SHA-256 is a Twitch-internal identifier and EDR does not have a list of "legitimate" SHA-256 hashes.

**Recommended fix:**
1. `curl_cffi.requests` is the *right* choice for this app — using a browser automation tool (Playwright) would be worse for AV. Leave it.
2. The Twitch GQL `Client-Id` is a public value; no change needed.
3. Optional: add `User-Agent` headers explicitly in every request to make the network behavior more transparent to EDR.

---

## Finding 12 — App listens on `127.0.0.1:7897` (FastAPI / uvicorn)

**Severity:** Low
**Category:** Local network behavior
**Files:**
- `backend/__main_launcher__.py:256-263` — `uvicorn.Config(app, host="127.0.0.1", port=port, ...)`
- `backend/services/webview2_setup.py` — local browser fallback uses `http://127.0.0.1:7897`
- `backend/services/tray_service.py:129` — `webbrowser.open(f"http://127.0.0.1:{self.port}")`

**Why it may trigger AV:**
Binding to `127.0.0.1` (not `0.0.0.0`) is the correct choice for a desktop app's embedded API. The mainline `main.py` *does* bind to `0.0.0.0` (line 1675 — `uvicorn.run(app, host="0.0.0.0", port=port)`), which means the dev entry exposes the API on every interface. That is appropriate for a dev tool and is not what the packaged app does. The packaged app uses `_start_server` which binds to `127.0.0.1`.

This is a low-risk item. The only flag is that if the user runs `python backend/main.py` (the dev path), the API is reachable from the LAN and Defender's "InboundRule" heuristic can complain. The packaged app does not have this issue.

**Evidence:**
```python
# backend/main.py:1667-1675
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "7897"))
    uvicorn.run(app, host="0.0.0.0", port=port)  # 0.0.0.0 = all interfaces
```

```python
# backend/__main_launcher__.py:256-263
config = uvicorn.Config(
    app,
    host="127.0.0.1",
    port=port,
    ...
)
```

**Recommended fix:**
None. If a defender complains, the answer is "run the packaged EXE, not `python main.py`."

---

## Finding 13 — No code signing on PyInstaller output

**Severity:** High (reputation)
**Category:** Code signing
**Files:**
- `vod-rip.spec` line 157 — `codesign_identity=None`
- `installer/installer.iss` — no `[Setup] SignTool=` directive
- `.github/workflows/release.yml` — no `signtool sign` step

**Why it may trigger AV:**
Same as Finding 4, but applied to the *running* executable, not just the installer. A `VOD-RIP.EXE` with no Authenticode signature and no catalog entry will trip SmartScreen on first launch even if the user runs the bundled executable directly (skipping the installer). This is the most common single source of "VOD.RIP is a virus" complaints.

**Recommended fix:**
1. Sign the PyInstaller output in CI with an EV or OV certificate.
2. Sign the Inno Setup installer in CI.
3. Distribute a Windows SmartScreen-friendly .msi alongside the .exe.

---

## Finding 14 — `subprocess.Popen` with `CREATE_NO_WINDOW`, `creationflags=0x08000000`

**Severity:** Low–Medium
**Category:** Hidden process execution
**Files:** every service module that uses `_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0`

**Why it may trigger AV:**
`CREATE_NO_WINDOW` (0x08000000) is the documented Windows flag for "spawn this process with no console window." It is the *correct* flag for a windowed GUI app that needs to shell out to ffmpeg. However, when the same flag is used *in combination* with PowerShell-Bypass + `taskkill /F` + `Stop-Process -Force` (Findings 5, 6), the cumulative pattern trips heuristics.

This is not a stand-alone finding — fix Findings 5 and 6 and this becomes a non-issue.

**Recommended fix:**
None alone; rolled up into Finding 5 / 6 fixes.

---

## Finding 15 — Bundle includes Python runtime (PyInstaller) and large `base_library.zip`

**Severity:** Low–Medium
**Category:** Packed binary / PyInstaller
**Files:**
- `vod-rip.spec` line 167-176 — `COLLECT(exe, a.binaries, a.zipfiles, a.datas, ...)`
- `vod-rip.spec` line 139 — `noarchive=False`
- `vod-rip.spec` line 151 — `upx=False` (good)

**Why it may trigger AV:**
PyInstaller EXEs are a known structure that AV vendors explicitly model. A PyInstaller bundle with `noarchive=False` (the default) creates a `VOD-RIP.pkg` archive and a `VOD-RIP.exe` bootloader next to a `_internal/` directory. This structure is recognized by some engines as "Python packaged app" and the *bootloader* is flagged by a small number of engines (`PyInstaller/Trojanized`).

`upx=False` is good — the author has explicitly opted out of UPX compression, which is the single biggest source of "packed executable" false positives.

The `noarchive=False` choice means at runtime the bootloader has to extract `VOD-RIP.pkg` to `%TEMP%\_MEIxxxxx\` and exec modules from there. The bootloader is unsigned (or signed only by a generic PyInstaller cert), so it trips some YARA rules.

**Evidence:**
```python
# vod-rip.spec:139-176
cipher=block_cipher,
noarchive=False,
)
...
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
...
_exe_kwargs = dict(
    ...
    upx=False,
    ...
)
exe = EXE(pyz, a.scripts, [], **_exe_kwargs)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="VOD-RIP",
)
```

**Recommended fix:**
1. Set `noarchive=True` so everything is embedded in the single EXE. This produces a larger single-file binary, which is *more* AV-friendly than the directory layout because the bootloader is a single self-extracting executable (recognized pattern) rather than a directory of files.
2. Sign the bootloader with an Authenticode cert — PyInstaller's bootloader can be signed post-build with `signtool sign /fd SHA256 dist/VOD-RIP/VOD-RIP.exe`.
3. Long-term, migrate to Tauri or a similar Rust-based GUI framework to drop the PyInstaller dependency entirely.

---

## Finding 16 — Telemetry / log writes to `%APPDATA%\VOD.RIP\logs\app.log` and `crash_reports\`

**Severity:** Low
**Category:** Filesystem behavior
**Files:**
- `backend/__main_launcher__.py:71-98` — `_setup_logging`
- `backend/services/crash_handler.py:22-35` — `crash_dir = app_data_dir / "crash_reports"`

**Why it may trigger AV:**
Writing logs to `%APPDATA%\<AppName>\logs\` is normal app behavior. Writing crash reports to `%APPDATA%\<AppName>\crash_reports\` is also normal. **However**, the crash handler calls `sys._current_frames()` and writes all thread stacks to disk (line 58-60). That is the kind of introspection that EDR rules can flag if combined with other behaviors, but on its own is fine.

**Recommended fix:**
None.

---

## Finding 17 — Inno Setup uses `PrivilegesRequired=lowest`

**Severity:** Low
**Category:** Installer privilege behavior
**Files:** `installer/installer.iss` line 28

**Why it may trigger AV:**
This is the **best** setting from an AV perspective. `PrivilegesRequired=lowest` means the installer will run without UAC elevation if possible, falling back to per-user installation (`{localappdata}\VOD.RIP`). The author has explicitly chosen the lowest-privilege installer path. This is a positive signal for AV heuristics.

**Recommended fix:**
None — keep as-is.

---

## Finding 18 — App checks for `clips.twitch.tv` and impersonates a browser for `kick.com` and Twitch GQL

**Severity:** Low
**Category:** Network / fingerprint impersonation (already covered in Finding 11)
**Files:** see Finding 11.

**Recommended fix:** see Finding 11.

---

## Finding 19 — Auto-restart of uvicorn server in the launcher

**Severity:** Low
**Category:** Service-supervisor / self-restart
**Files:** `backend/__main_launcher__.py:274-292` — `_server_supervisor`

**Why it may trigger AV:**
The supervisor thread restarts the FastAPI server on crash, indefinitely. This is a *self-healing service* pattern, which AV vendors occasionally flag in long-running processes. It is benign here (the user is sitting in front of the app) and is not a high-risk pattern.

**Recommended fix:**
None.

---

## Finding 20 — `xcod` / `ditto` / `rsync` invocation in the macOS / Linux updater

**Severity:** Low
**Category:** Cross-platform updater shell execution
**Files:** `backend/services/updater.py:265-313`

**Why it may trigger AV:**
The macOS updater runs `/bin/sh` with a dynamically-written shell script that does `rm -rf`, `ditto`, and `open`. The Linux updater runs `rsync -a --delete` or `cp -a` to replace the install. This is the standard install-replacement pattern and is well-known to AV, but on macOS the `rm -rf "$dst"` of an app bundle can be flagged by XProtect if it happens too fast after a download (looks like a destructive payload). On Linux, `rsync --delete` against a running binary is the same family of behavior as the Windows robocopy.

**Recommended fix:** see Finding 1. The fix is the same: ship a small signed updater binary.

---

# Defender-Focused Assessment

Microsoft Defender heuristic rules (current generation, ~2025–2026) that this codebase can trip, in order of likelihood:

| Behavior | Where | Defender Rule Family | Likelihood |
|---|---|---|---|
| PowerShell `-ExecutionPolicy Bypass` | `windows_shortcuts.py:64`, `updater.py:259`, `server_lifecycle.py:243` | `Behavior:Win32/SuspiciousPowerShellBypass` | **High** |
| `taskkill /F /T` from non-system process | `server_lifecycle.py:218-221`, `os_services.py:382` | `Behavior:Win32/SuspiciousProcessKill` | **High** |
| `Stop-Process -Force` via PowerShell | `server_lifecycle.py:243-250`, `dev-all.mjs:64` | `Behavior:Win32/SuspiciousProcessKill` | **High** |
| Download ZIP to `%TEMP%` then execute | `updater.py:106-126` | `Behavior:Win32/SuspiciousDownload`, `PUADownloader` | **High** |
| `robocopy /E` over a running install dir | `updater.py:252` (script body) | `Behavior:Win32/PossiblePayloadReplacement` | **Medium** |
| Bundled ffmpeg.exe (unsigned) | `vod-rip.spec:_ffmpeg_binaries`, CI ffmpeg download | `PUA:Win32/BundledTool` | **Medium** |
| `CREATE_NO_WINDOW` on long-running subprocess | every `_NO_WINDOW` site | low-grade behavioral signal | **Low** |
| `curl_cffi` with `impersonate="chrome"` | `preview_service.py:153`, `kick_api_service.py:28` | no specific rule, but Falcon/SentinelOne ML flag | **Low** |
| Python `os._exit(0)` after child launch | `updater.py:220,263,291,314` | none alone, raises confidence in compound verdict | **Low** |
| `ISCC`-built Inno Setup, unsigned | `release.yml:117` | SmartScreen, `PUA:Win32/UnsignedInstaller` | **High** for first-run UX |

---

# SmartScreen Assessment

SmartScreen checks two things: **Authenticode signature** and **reputation** (download count, age, publisher). VOD.RIP currently has:

1. **No Authenticode signature on `VOD-RIP.EXE` or `VOD.RIP-{ver}-Setup.exe`.** This is the dominant cause of "Windows protected your PC" prompts. EV certificates skip the reputation check entirely; OV certificates still require ~3,000 downloads before SmartScreen stops blocking.
2. **No SmartScreen reputation.** A new project, even on GitHub Releases, has 0 reputation. First-time installers will prompt every user.
3. **The bundled WebView2 bootstrapper is signed by Microsoft**, so it doesn't trip SmartScreen on its own.
4. **`AppUserModelID` is set to `mateusant13.VODRIP.1`** (in `__main_launcher__.py:187`) — that groups taskbar entries correctly, which is a positive UX signal but not a SmartScreen-relevant item.

The fastest path to "no SmartScreen warning" is an EV code-signing certificate (~US$240–500/year) applied to both the Inno Setup output and the PyInstaller bootloader. Without signing, the project must accumulate SmartScreen reputation by being installed by many users without incident (months to years, depending on volume).

---

# False-Positive Risk Ranking (top 20)

| Rank | Finding | Severity | One-line mitigation |
|---|---|---|---|
| 1 | **Unsigned Inno Setup installer** (Finding 4) | High | Sign with EV/OV Authenticode in CI |
| 2 | **Self-updater: GitHub → ZIP → PowerShell → robocopy** (Finding 1) | High | Replace with signed `VOD-RIP-Updater.exe` or external download page |
| 3 | **Bundled ffmpeg.exe / ffprobe.exe (unsigned)** (Finding 2) | High | Stop bundling; point at user's ffmpeg or vendor BtbN-Builds with hash check |
| 4 | **PowerShell `-ExecutionPolicy Bypass` × 4 sites** (Finding 6) | High | Use `ctypes` for shortcuts, `kernel32.TerminateProcess` for kill, `AllSigned` for updater PS1 |
| 5 | **Unsigned PyInstaller bootloader** (Finding 15) | High | Sign the bootloader; consider `noarchive=True` |
| 6 | **`taskkill /IM ffmpeg.exe` wildcard kill** (Finding 5) | Medium | Drop the image-name fallback; rely on registered-PID tracking only |
| 7 | **Inno Setup runs WebView2 bootstrapper at install** (Finding 3) | Medium | Already mitigated (visible UI, Microsoft-signed). Optional: link to MS page instead |
| 8 | **`Stop-Process -Force` via dynamic PowerShell** (Findings 5, 6) | Medium | Use `kernel32.TerminateProcess` directly (already in code, just remove the PowerShell fallback) |
| 9 | **`os._exit(0)` after spawning updater** (Finding 7) | Low–Medium | Add explanatory log; prefer `sys.exit` where possible |
| 10 | **`curl_cffi` with `impersonate="chrome"`** (Finding 11) | Low–Medium | Add explicit `User-Agent`; otherwise leave (Playwright would be worse) |
| 11 | **PyInstaller directory layout (`VOD-RIP.pkg` + `_internal\`)** (Finding 15) | Medium | `noarchive=True`; or migrate to Tauri long-term |
| 12 | **Background GitHub update check on every launch** (Finding 9) | Low | Add explicit `User-Agent: VOD.RIP/<ver>` |
| 13 | **Dev `main.py` binds `0.0.0.0`** (Finding 12) | Low | Document "use packaged EXE" in README; harmless in packaged form |
| 14 | **`taskkill /F /T /PID` (tree kill)** (Finding 5) | Low | Restrict to registered PIDs only; remove `/T` where possible |
| 15 | **Hardcoded Twitch GQL `Client-Id` and persisted-query SHA-256** (Finding 11) | Low | None — these are public Twitch constants |
| 16 | **No `User-Agent` on background requests** (Finding 9) | Low | Set `User-Agent: VOD.RIP/<ver>` |
| 17 | **uvicorn auto-restart supervisor** (Finding 19) | Low | None |
| 18 | **Writes HLS cache to `%TEMP%\kd_preview\`** (Finding 10) | Low | None |
| 19 | **macOS / Linux updater runs `rm -rf` + `ditto`/`rsync --delete`** (Finding 20) | Low | Same fix as Finding 1 |
| 20 | **No `User-Agent` header on curl_cffi requests** (Finding 11) | Low | Set UA explicitly |

---

## Notes on positive patterns already in the codebase

The author has clearly designed with AV/EDR feedback in mind. Things that are already correct and should not be changed:

- `upx=False`, `strip=False` in the spec — no packed/polymorphic binary.
- `console=False` in the spec — no console window flash on launch.
- Inno Setup `PrivilegesRequired=lowest` — lowest-privilege install path.
- `PrivilegesRequired=lowest`, `ArchitecturesInstallIn64BitMode=x64compatible`, `DisableProgramGroupPage=no` — no UAC elevation required.
- `WebView2 /install` runs only if missing, with visible UI — not a silent payload install.
- The updater writes a comment in the script body: `; Use the normal installer UI (not silent/hidden) — fewer antivirus false positives.` (updater.py:215).
- `webview2_setup.py` opens with the comment: `We intentionally do NOT download or execute installers from inside VOD.RIP — that pattern is flagged as trojan/dropper behavior by antivirus software.`
- `pip install` of dependencies only happens in dev (`run.py:39-41`) and is never called by the packaged app.
- The PyInstaller spec excludes `django`, `flask`, `tornado`, `boto3`, `botocore`, `matplotlib`, `scipy`, `numpy`, `pandas` — keeps the bundle minimal and reduces false-positive surface from suspicious imports.
- The `_safe_extractall` helper validates zip member paths against `Zip Slip` (updater.py:317-327).
- No `eval`, `exec`, `compile`, `base64`, `Function()` in any source file.
- No `child_process` use in any TS/TSX file (the only Node-side execution is the `dev-all.mjs` dev script, which never ships in the package).

These are good signals. Combined with the two highest-impact fixes (sign the installer; replace the in-app updater), the project's false-positive risk drops from "medium" to "low" on the next release.

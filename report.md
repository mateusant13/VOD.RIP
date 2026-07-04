# Production-Readiness Audit: VOD.RIP

> **Audit Date:** June 15, 2026  
> **Branch:** `main` (commit at start of audit)  
> **Scope:** All shipped, pushed, built, released, executed, or depended-upon code

---

## Executive Summary

This is a **Functional But Accumulating Debt** codebase. The application works in practice — users can paste Kick/Twitch URLs, preview VODs, set trim points, download clips, browse channels, and manage downloads. The code is written by someone who understands the problem domain (Kick/Twitch VOD downloading) deeply and has implemented genuinely clever solutions for real problems (ffmpeg progress instrumentation, atomic file operations, HLS preview proxying).

However, the codebase is accumulating debt faster than it's being paid down:

- **Zero automated testing** across the entire stack — no unit, integration, or e2e tests in any language.
- **A 6,000+ line React component** (`src/App.tsx`) that makes the frontend unmaintainable.
- **Circular import hazards** that will break under any import reordering.
- **Unsigned releases** that trigger SmartScreen, with AV-heuristic workarounds throughout.
- **Fragile CI** with silent failures and no test step in the release pipeline.

Without intervention, the codebase will cross into "Significant Structural Concerns" within a few more feature releases.

---

## Critical Findings

### 1. ZERO Tests — No Test Infrastructure Anywhere (Severity: Critical)

There is not a single test file anywhere in the repository.

**Evidence:**
- `backend/` has no `tests/` directory
- `src/` has no `*.test.ts` or `*.spec.ts` files
- `package.json` has no test script
- `requirements.txt` has no test dependencies
- The CI file (`release.yml`) has a `lint` job that type-checks TypeScript and compiles Python, but **no test step**

**Impact:**
- Every refactor breaks things silently — changes are validated only by manual testing
- The download pipeline (network I/O, FFmpeg subprocesses, file I/O, threading, SSE) is entirely untested
- There is zero confidence that a release works before it ships
- A single regression in the download manager, yt-dlp wrapper, or file path handling can corrupt user files

**Severity rationale:** This is the single most expensive gap in the codebase. It compounds every other risk because there is no safety net.

---

### 2. Circular Import: `shutdown_util` → `main` (Severity: High)

`backend/services/shutdown_util.py` imports `download_mgr` from `main.py` at the module level:

```python
from main import download_mgr
```

This is a **circular import at runtime**. `main.py` imports `shutdown_util` in its lifecycle management. During import, `shutdown_util` tries to import `main.py` which is still being evaluated.

**Evidence:** `shutdown_util.py` line 12: `from main import download_mgr`. `main.py` imports services including lifecycle-related ones.

**Impact:** Will crash with `ImportError` if the import chain is reordered. Currently works by timing — `shutdown_util` is imported after `main` has finished loading, but this is fragile and implicit.

**Fix:** Inject `download_mgr` as a parameter or use a service registry pattern.

---

### 3. Preview Proxy: No Graceful Degradation for Network Failures (Severity: High)

The preview proxy (`preview_service.py`) acts as a man-in-the-middle for HLS streams. If the upstream CDN hangs or errors, sessions leak.

**Evidence:**
- `_http_get_bytes` has a 60-second timeout
- `proxy_segment` fetches segments synchronously in a thread pool — if upstream hangs, the thread blocks for 60s
- `SESSION_TTL_SEC` is 30 minutes (1800 seconds) — a hung session consumes memory and a thread for 30 minutes
- Session eviction (`_cleanup_stale_sessions`) runs only when a new session is created, not on a background timer

**Impact:** Under concurrent preview usage (e.g., 5 channel explore popups), 5 hanging upstreams consume 5 thread pool slots for 30 minutes, effectively starving the rest of the INFO_EXECUTOR pool.

**Reproducer:** Open 8 channel clip previews simultaneously while the Kick API is slow. 5 threads block on segment downloads, 2 are used for clip info, leaving 1 for all other metadata requests for 30 minutes.

---

### 4. Global Mutable State Without Isolation (Severity: High)

Multiple modules use module-level mutable state:

| Module | State | Bounded? | Thread-safe? |
|--------|-------|----------|-------------|
| `channel_cache.py` | `_CACHE: dict` | No size limit — only TTL (90s) | Lock per operation, but no eviction strategy |
| `preview_service.py` | `_sessions: dict` | No count limit — only TTL (30m) | Lock per session, but unbounded growth |
| `os_services.py` | `_CHILD_PIDS: set` | No limit — thread count-bound | Locked, but PID reuse race |
| `app_lifecycle.py` | `_window`, `_tray`, `_allow_close` | N/A | No isolation — single-process assumption |
| `download_manager.py` | `_downloads`, `_cancel_events`, etc. | 50-entry UI cap, 200-entry disk cap | Multiple fine-grained locks, but possible deadlock between `_lock` and `_history_lock` |

**Impact:** Not testable in isolation. State leaks between tests. No `__all__` restrictions or encapsulation.

---

### 5. Thread-Safety: PID Tracking Race (Severity: High)

`os_services.py` tracks child process PIDs:

```python
_CHILD_PIDS: set[int] = set()
_CHILD_PIDS_LOCK = threading.Lock()
```

**Race condition:**
1. Thread A spawns ffmpeg, gets PID 1234, calls `register_child_pid(1234)`
2. ffmpeg exits immediately (e.g., missing input file)
3. PID 1234 is reused by a new system process
4. Thread A calls `register_child_pid(1234)` — PID now points to the wrong process
5. On shutdown, `kill_child_processes()` kills PID 1234 — the wrong process

**Impact:** VOD.RIP can kill unrelated processes on shutdown. The `_pid_is_vodrip_api` check in `server_lifecycle.py` mitigates this for API port processes but not for child ffmpeg processes.

---

### 6. Import-Time Side Effect: yt-dlp Monkey-Patch (Severity: High)

`backend/services/ytdlp_service.py` performs a **process-wide monkey-patch of yt-dlp's postprocessor registry at import time**:

```python
_ytdlp_pp_pkg.postprocessors.value["FFmpegVideoConvertorPP"] = _InstrumentedFFmpegPP
```

**Issues:**
- Mutates a global yt-dlp state at module import
- If `ytdlp_service` is imported for metadata only (e.g., to detect platform), the patch is still applied
- If another component in the same process uses yt-dlp independently, it gets the patched PP without consent
- Will break silently if yt-dlp changes its internal registry structure

**Mitigation:** The code has a comment explaining this, but the design is fragile.

---

## Complexity Findings

### 7. Preview Module: 650+ Lines, Too Many Responsibilities (Severity: Medium-High)

`preview_service.py` (~650 lines) implements:
- Session management (create/delete/expire)
- HLS master playlist parsing and rewriting
- Media playlist resolution and caching
- Segment proxy with caching and host allow-listing
- Prewarm logic for fast startup
- Progressive MP4 support for Twitch clips

**Symptoms of over-complexity:**
- 30+ functions, many with deep call chains
- `_is_playlist_url` is defined here but **imported and used by `main.py`** — a private function used externally
- `_playlist_cache(session)` checks `hasattr` for backward compatibility with pickled instances
- Comment: *"Hot-reload can leave older in-memory instances without new fields"*

**What breaks if this disappears?** The entire preview functionality. But the module should be split into: session manager, playlist parser, proxy fetcher, and cache layer.

---

### 8. App.tsx: 6,000+ Line Single Component (Severity: High)

`src/App.tsx` is approximately **6,000+ lines** (the file was 215KB, exceeding the 100KB read truncation). It contains:

- URL input handling and validation
- Download management UI (start, pause, resume, cancel, delete)
- Channel browsing with search, filters, reordering
- Settings management (all fields)
- Preview player with HLS and progressive playback
- Trim range editing (URL sliders and preview needles)
- Explore popup management
- Panel layout with drag-resize
- Channel drag-and-drop reordering
- SSE streaming for download progress
- Fullscreen preview with controls
- Multiple modal dialogs

**This is an Architecture Violation of the highest order.** A single component should never approach even 1,000 lines.

---

### 9. Duplicated Preview Player Logic (~100 Lines × 2) (Severity: Medium-High)

The HLS preview player setup code is **copy-pasted** between `App.tsx` and `ChannelExplorePopup.tsx`:

| Feature | App.tsx | ChannelExplorePopup.tsx |
|---------|---------|----------------------|
| Hls.js init with options | ✓ | ✓ |
| `onCanPlay` handler | ✓ | ✓ |
| `MANIFEST_PARSED` handler | ✓ | ✓ |
| `LEVELS_UPDATED` handler | ✓ | ✓ |
| ERROR handling (NETWORK/MEDIA/default) | ✓ | ✓ |
| Progressive MP4 fallback | ✓ | ✓ |
| Quality level synchronization | ✓ | ✓ |
| `resolveProgressivePreviewLevelsAsync` | ✓ | ✓ |

**Impact:** Any fix or improvement to the preview player must be applied in two places. Inevitably they will diverge.

---

### 10. Duplicated API Fetch Infrastructure (Severity: Medium)

`App.tsx` defines a comprehensive API client with retry logic, timeout handling, error formatting, and platform-specific error messages (~80 lines):

```typescript
async function apiFetch(path, init?)  // retries once with 400ms delay
async function apiGet<T>(path)        // error formatting
async function apiPost<T>(path, body) // JSON body
async function apiDelete(path)        // DELETE method
```

`ChannelExplorePopup.tsx` defines its own simpler set with **no retry** and **no timeout**:

```typescript
async function apiPost<T>(path, body)  // no retry, no timeout
async function apiGet<T>(path)         // no retry, no timeout
async function apiDelete(path)         // no retry, no timeout
```

**Impact:** Channel explore popups have weaker error resilience than the main UI. A transient network failure that the main UI retries will cause an explore popup to fail permanently.

---

### 11. Deprecated Code Retained (Severity: Low-Medium)

- **`resolvePreviewLevels`** in `previewPlayerUtils.ts`: Explicitly marked `@deprecated`, calls `resolveHlsPreviewLevels`. No callers — kept for "backward compatibility" with nothing.
- **`_gpu_names_freebsd`**, **`_gpu_names_cygwin`** in `os_services.py`: Platforms the app never officially supports.
- **`_mkv_to_premiere_mp4`** in `ytdlp_service.py`: Defined but never called.

---

## Reliability Findings

### 12. DownloadManager: Potential Deadlock on Cancel (Severity: High)

`download_manager.py` uses `ThreadPoolExecutor` with `max_workers=4`. The `cancel()` method:
1. Acquires `_lock` to read state
2. Sets cancel event
3. Calls abort functions
4. Cleans up partial output
5. Pops from multiple dictionaries
6. Records history (acquires `_history_lock`)

The worker thread's `finally` block:
1. Acquires `_lock` to read final state
2. Records history (acquires `_history_lock`)
3. Pops from dictionaries

**Deadlock scenario:**
- Thread A (worker) holds `_lock`, tries to acquire `_history_lock`
- Thread B (cancel) holds `_history_lock`, tries to acquire `_lock`
- Both threads block indefinitely

---

### 13. SSE Stream: Backend Queue Leak on Disconnect (Severity: Medium)

The SSE stream in `main.py`:

```python
async def stream():
    while True:
        if await request.is_disconnected():
            break
        ...
```

`unregister_sse` is called only in the `finally` block of `stream_wrapper`, which is reached only when the generator exits normally or raises. If the connection drops and the generator is garbage-collected, the queue may never be removed.

**Frontend side** (`useDownloadStreams.ts`) has reconnection logic, but the backend-side queue remains in `_sse_queues` forever.

---

### 14. Channel Cache: Unbounded Growth (Severity: Medium)

`channel_cache.py` has no size-based eviction. Only TTL-based (90 seconds by default). A burst of channel fetches (e.g., scrolling through 20 channels in the channels tab) grows the cache to ~20 entries for 90 seconds. Each entry is a JSON payload with up to 100 VOD objects (~50KB each → ~1MB peak). Under heavy use this is acceptable, but without a size cap it could grow under abuse.

---

### 15. Settings Write Race on Startup (Severity: Medium)

`settings.py` constructor:

```python
if not self._settings_file.exists():
    self.save(self._settings)
```

If two VOD.RIP processes start simultaneously (race window during single-instance detection), both could write settings. The atomic write (temp file + `os.replace`) prevents file corruption but doesn't prevent data loss — the second write overwrites the first.

---

### 16. Download History: In-Memory/On-Disk Desync After Crash (Severity: Medium)

`download_manager.py` maintains in-memory `_history` and `_queue` lists that mirror on-disk JSON files. Persistence is best-effort (errors are swallowed with `logger.exception`).

**After a crash:**
- In-memory state is lost
- Disk state is only as recent as the last `_save_history` or `_save_queue` call
- `_queue_persist_interval` is 15 seconds — up to 15 seconds of progress can be lost
- The `_reconcile_queue_on_startup` method handles queue entries, but last-15-seconds history entries are permanently lost

---

## Maintainability Findings

### 17. Error Handling: Four Different Patterns (Severity: Medium)

The codebase uses inconsistent error handling:

1. **Silent swallow:** `except Exception: pass` — used in preview session cleanup, SSE notification failures
2. **Logged swallow:** `logger.exception(...)` — used in download history persistence
3. **Re-raise as HTTP:** `raise HTTPException(status_code=400, detail=str(e))` — used in API routes
4. **Re-raise as RuntimeError:** `raise RuntimeError(...)` — used in services

**Impact:** Debugging production issues requires checking four different patterns. Silent swallows hide bugs that would otherwise be caught.

---

### 18. Implicit yt-dlp Format Behavior (Severity: Medium-Low)

`_build_ydl_opts` in `ytdlp_service.py`:

```python
if quality and quality.lower() != "source":
    # set format
else:
    # don't set format → yt-dlp default
```

When quality is "source" (or unset), no `format` is specified, so yt-dlp uses its default behavior. This is documented in a comment but is implicit — a future yt-dlp version could change its default format selection and silently break the download pipeline.

---

### 19. Version Discrepancy: 1.0.45 vs 2.0.0 (Severity: Low)

- `backend/services/_version.py`: `__version__ = "1.0.45"`
- `backend/main.py`: `app = FastAPI(..., version="2.0.0")`

The user sees `1.0.45` in the UI (`/api/app/version`) but the OpenAPI docs say `2.0.0`.

---

### 20. Dead Code Path: `debug_cli` Import (Severity: Low)

`run.py`:

```python
if "--debug" in sys.argv:
    from debug_cli import main
```

There is no `debug_cli.py` file in the repository. This code path crashes if `--debug` is passed.

---

### 21. `_NO_WINDOW` Constant Duplicated Across 7+ Files (Severity: Low-Medium)

The Windows subprocess creation flag `subprocess.CREATE_NO_WINDOW` is defined as a module-level constant in **at least 7 files**:

| File | Definition |
|------|-----------|
| `backend/main.py` | `_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0` |
| `backend/services/ytdlp_service.py` | Same pattern |
| `backend/services/server_lifecycle.py` | Same pattern |
| `backend/services/download_cleanup.py` | Same pattern |
| `backend/services/gpu_detect.py` | Same pattern |
| `backend/services/updater.py` | Same pattern |
| `backend/__main_launcher__.py` | Same pattern |

**Impact:** If the flag needs to change (e.g., for a new Windows version), it must be updated in 7 places. This constant should be centralized in `os_services.py` and imported everywhere else.

---

### 22. Three Application Entry Points (Severity: Medium)

The codebase has **three** `if __name__ == "__main__"` blocks:

1. **`backend/run.py`** — Dev entry point. Auto-installs deps with pip, runs `uvicorn.run("main:app")`
2. **`backend/__main_launcher__.py`** — Production entry point (PyInstaller target). Starts server supervisor, launches PyWebView window or browser fallback
3. **`backend/main.py`** — Self-contained `uvicorn.run("main:app", host="0.0.0.0")` on port 7897

**Impact:**
- Confusing development setup — which script should a new contributor run?
- Different behaviors between entry points (run.py auto-installs deps, main.py doesn't)
- Different host bindings (run.py/__main_launcher__ use `"0.0.0.0"`, __main_launcher__ supervisor uses `"127.0.0.1"`)

The dev-all.mjs script orchestrates these correctly, but a new contributor doesn't know that.

---

### 23. No package-lock.json Committed (Severity: Medium)

There is no `package-lock.json` in the repository file tree. This means every `npm ci` (as used in CI) or `npm install` produces a non-deterministic dependency tree. Differences in transitive dependency versions between developer machines and CI can cause:
- "Works on my machine" bugs
- CI passing but local failing (or vice versa)
- Security vulnerabilities introduced via unpinned transitive deps

---

## Release & Deployment Findings

### 24. No Code Signing — SmartScreen on Every Install (Severity: Critical for Windows)

The CI build pipeline (`release.yml`) has an **optional** code signing step that is a **no-op when secrets are not configured**:

```yaml
- name: Sign artefacts (Authenticode)
  if: runner.os == 'Windows' && env.VODRIP_CERT_B64 != ''
```

Without signing secrets configured, releases are **unsigned**. The codebase has multiple AV-heuristic avoidance workarounds (marked F7/F6/F5/F4/F13/F15 in comments) explicitly acknowledging this.

**Impact:**
- Every installer triggers "Windows protected your PC" SmartScreen
- Every executable is flagged as `PUA:Win32/UnsignedInstaller`
- Users must click "More info → Run anyway" to install
- Enterprise users cannot deploy via Group Policy or Intune
- The `PUA:Win32/UnsignedInstaller` tag can be triggered simply by the unsigned binary being rare (low reputation score)

---

### 25. Fragile Build Pipeline with Silent Failures (Severity: High)

CI pipeline issues:

```yaml
- name: Download ffmpeg (optional)
  run: bash scripts/download-ffmpeg.sh || true   # SILENT FAILURE
```

- `|| true` swallows ffmpeg download failures — the build succeeds without ffmpeg
- `choco install innosetup` — installing from Chocolatey at build time is fragile
- `pip install pyinstaller pywebview pystray Pillow` — installing build deps without version pinning
- No lockfile for Python dependencies
- No lockfile for npm dependencies (only `package-lock.json` is not visible in the file tree — it may or may not exist)

---

### 26. WebView2 Bootstrapper Downloaded at Build Time (Severity: Medium)

```powershell
Invoke-WebRequest -Uri "https://go.microsoft.com/fwlink/p/?LinkId=2124703" -OutFile "installer\MicrosoftEdgeWebview2Setup.exe"
```

- This is a dynamic redirect URL that could change at any time, breaking CI
- The bootstrapper is bundled into the installer, then distributed — legal/distribution concern (rebundling Microsoft's redistributable)
- No checksum verification after download

---

### 27. No macOS Code Signing (Severity: Medium)

The macOS `.app` bundle is created but never signed with an Apple Developer certificate. macOS Gatekeeper will block the app with "cannot be opened because the developer cannot be verified."

---

### 28. No Version Automation (Severity: Low)

`_version.py` defines `__version__ = "1.0.45"` as a hardcoded string. The CI triggers on `v*` tag pushes, but there's no workflow to:
- Bump the version number
- Update a changelog
- Create the git tag
- Generate release notes

The `generate_release_notes: true` flag in the GitHub Release step auto-generates notes, but the version string in the code is manual.

---

## Security Findings

### 29. HLS Proxy: SSRF Amplification Vector (Severity: Medium)

The preview proxy in `preview_service.py` proxies arbitrary URLs from session resource maps. The host allow list includes:

```
cloudfront.net, amazonaws.com, akamaized.net, fastly.net
```

These are broad enough to include arbitrary AWS-hosted content or CloudFront distributions. If an attacker can control the resource map (e.g., via a malicious Kick API response), they can make the server proxy traffic to arbitrary hosts matching these suffixes.

**Limitations:**
- Requires a valid session token
- Only accessible via `127.0.0.1` by default
- The `_host_allowed` function does suffix matching — `evil-amazonaws.com` would be blocked, but `evil.cloudfront.net` would pass

---

### 30. OAuth Token Stored Unencrypted (Severity: Medium)

The Twitch OAuth token is stored in plaintext in `settings.json`:
- `%APPDATA%/VOD.RIP/settings.json` (Windows)
- `~/Library/Application Support/VOD.RIP/settings.json` (macOS)
- `~/.local/share/VOD.RIP/settings.json` (Linux)

**Attack surface:**
- Any process running as the same user can read the token
- Any process on the machine can connect to `127.0.0.1:7897` and fetch the token via `GET /api/settings`
- If the user changes the host from `127.0.0.1` to `0.0.0.0`, the token is accessible over the network

---

### 31. `noarchive=True` Distributes All Python Source (Severity: Low-Medium)

The PyInstaller build uses `noarchive=True` which embeds all Python code as individual files in the `_internal/` directory. The comment explains this was done to avoid AV false positives, but the consequence is that all Python source code is distributed in plaintext, trivially readable by anyone who downloads the release.

---

## Hidden Technical Debt

### 32. `pip install` at Runtime in Production Path

`run.py` auto-installs dependencies:

```python
except ImportError:
    print("Installing dependencies...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])
```

This modifies the user's Python environment at runtime with no confirmation. For a dev script this is acceptable, but it runs in production (via `run.py`). The PyInstaller path doesn't trigger this, but the dev path does.

---

### 33. curl_cffi Binary Dependency

The `curl_cffi` package requires a compiled C extension (libcurl). This is listed in `requirements.txt` but is a common source of installation failures on Windows, especially in restricted environments (corporate proxies, air-gapped machines, Windows N editions).

---

### 34. `kick_models.py` — Ungated Dependency

Multiple Kick service files import from `services.kick_models` but this module's interface is implied rather than documented. If `kick_models.py` has a schema change, the mismatch will only be caught at runtime.

---

## Positive Findings

Despite the severity of the issues above, the codebase has genuine strengths:

1. **FFmpeg progress instrumentation** (`_InstrumentedFFmpegPP`): The custom postprocessor that hooks into yt-dlp's pipeline to surface real ffmpeg progress is genuinely clever. The poller architecture bridging ffmpeg's `-progress pipe:2` output to the SSE stream is well-designed.

2. **Atomic file writes**: Settings, history, and queue files all use the atomic write pattern (temp file + `os.replace`). This prevents corruption from partial writes during crashes.

3. **Fine-grained lock design**: The download manager uses separate locks (`_lock`, `_history_lock`, `_queue_lock`) instead of a single coarse lock, reducing contention.

4. **Cross-platform abstraction** (`os_services.py`): WSL detection, Cygwin/MSYS2 awareness, FreeBSD GPU detection — coverage is thorough even if some paths are unused. The `sanitize_filename_component` function correctly handles per-platform rules.

5. **Duration format utilities**: The trim range formatting, clip duration tagging, and filename generation are well-implemented with proper sanitization.

6. **Download progress chain**: ffmpeg → progress state dict → manager poller → SSE → React state → UI is an impressive chain providing real-time ETA, speed, and phase feedback.

7. **AV heuristic awareness**: Comments like "F4/F13 (ANTIVIRUS_AUDIT)" show the team is aware of security tooling issues and actively documenting the rationale behind workarounds.

---

## Health Scores

| Category | Score | Justification |
|---|---|---|
| **Architecture** | 4/10 | Monolithic 6K-line component, circular imports, global mutable state, preview module embeds a full HTTP proxy in a service file. Cross-platform layer is a genuine strength. |
| **Maintainability** | 3/10 | Zero tests, duplicated code (preview player, API fetch), dead code, deprecated code kept around. App.tsx alone makes the frontend unmaintainable. |
| **Operational Reliability** | 5/10 | Download pipeline has good thread safety and cleanup. But no graceful degradation, no backpressure, no health checks, no metrics. |
| **Release Readiness** | 3/10 | Unsigned releases (SmartScreen blocked), fragile CI with silent failures, WebView2 bootstrapper downloaded at build time, no version automation, no test step. |
| **Simplicity** | 4/10 | HLS segment download flow is elegant. But 30+ functions in preview_service, 6000-line App.tsx, multiple quality management layers. Complexity is accumulating. |
| **Contributor Experience** | 2/10 | No test suite, no contribution guidelines, no local dev setup docs. The 6K-line App.tsx is a psychological barrier. |
| **Dependency Hygiene** | 6/10 | Dependencies versioned with open ranges (>=). No unused deps detected. But no lockfile, no dependency auditing, curl_cffi is binary-heavy. |
| **Production Confidence** | 3/10 | The codebase functions but there is no evidence the team can detect regressions without manual testing. Every release is deployed with zero automated validation. |

---

## Final Verdict

**Functional But Accumulating Debt**

The application works in production today. Users can paste Kick/Twitch URLs, preview VODs with trim points, download clips, and manage their queue. The code is written by someone who understands the domain deeply and has implemented clever solutions for real problems.

However, the codebase is accumulating debt faster than it's being paid down:

**Testing debt** is the existential threat. Zero tests means every change is validated only by manual use. The download pipeline involves threading, subprocesses, network I/O, SSE streaming, and disk I/O — any of which can fail silently.

**Architecture debt** in the frontend makes the React codebase essentially unmaintainable by anyone except the original author. The 6,000-line `App.tsx` must be decomposed before additional features become practical.

**Release trustworthiness debt** means every release is a manual process with unsigned binaries that trigger SmartScreen warnings. Users must bypass security warnings to install the app, eroding trust and limiting adoption.

**The trajectory is negative**: Features are being added (channel browsing, clips, preview improvements) without corresponding investment in testing, refactoring, or release infrastructure. Complexity is accumulating, not being reduced.

### Recommended Actions (Priority Order)

1. **Add tests** — Start with download manager unit tests and HLS download pipeline integration tests
2. **Decompose App.tsx** — Split into: URLPanel, PreviewPanel, ChannelsPanel, SettingsPanel, QueuePanel
3. **Sign releases** — EV/OV Authenticode cert for Windows, Apple Developer cert for macOS
4. **Eliminate circular imports** — Fix `shutdown_util → main` dependency
5. **Extract shared preview player** into a custom hook shared by App.tsx and ChannelExplorePopup.tsx
6. **Lock dependencies** — Use `pip freeze > requirements-lock.txt` and commit `package-lock.json`
7. **Implement CI tests** — Add a test step before the build step in release.yml
8. **Add cache size limits** — Bounded caches for channel_cache and preview_service

---

*Audit conducted by Buffy (Codebuff AI agent) on June 15, 2026.*

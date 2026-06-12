# VOD.RIP — Full Codebase Audit Report

**Date:** June 12, 2026
**Scope:** All frontend (TypeScript/React) and backend (Python/FastAPI) source code, excluding assets, installer scripts, screenshots, and readme templates.

---

## Table of Contents

1. [Security](#1-security)
2. [Backend Issues](#2-backend-issues)
3. [Frontend Issues](#3-frontend-issues)
4. [Code Quality & Maintainability](#4-code-quality--maintainability)
5. [Data Integrity & Persistence](#5-data-integrity--persistence)
6. [Performance](#6-performance)
7. [Summary of Findings](#7-summary-of-findings)

---

## 1. Security

### 1.1 SSRF via Preview Resource Proxy (Medium)

**File:** `backend/services/preview_service.py`
**Function:** `resolve_upstream()` (line ~820)

The `u` query parameter on `/api/preview/hls/{session_id}/resource?u=<url>` allows a caller to specify an arbitrary upstream URL. The only guard is a host allowlist check (`_host_allowed`). While the allowlist covers CDN hosts used by Twitch/Kick, **any allowed host can be fetched**, including user-controlled resources served from a matching hostname (e.g., `evil.cloudfront.net`).

**Risk:** An attacker who can craft a preview session request could proxy arbitrary URLs through the VOD.RIP server, potentially bypassing network ACLs.

**Recommendation:** Restrict the `u` parameter to only return URLs already in `session.resource_map` (i.e., enforce that `id` is always required and `u` is never accepted as an override).

### 1.2 Zip Slip Protection (Low — Already Fixed)

**File:** `backend/services/updater.py`
**Function:** `_safe_extractall()` (line ~320)

The Zip Slip vulnerability from the previous audit has been fixed. Each member path is resolved and validated to stay within the target directory before extraction.

### 1.3 Path Traversal in Output File Names (Low)

**File:** `backend/main.py`
**Functions:** `_build_output_path()`, `_build_clip_output_path()`, `_resolve_output_file_override()`

User-supplied `output_file` from `DownloadRequest` flows into `_sanitize_path_component()` which strips Windows-forbidden characters. However, absolute paths (starting with `/` or a drive letter like `C:`) are passed through without validation. This is **intentional** (users should be able to set an absolute download location), but the path is not checked against the configured download directory, so a user could write files outside the expected folder via the API.

**Risk:** Low — the API is local-only and typically not exposed to the network.

### 1.4 Credential Exposure via OAuth (Low)

**File:** `backend/main.py`, `backend/services/settings.py`

The Twitch OAuth token is stored in plaintext in `settings.json` (`%APPDATA%/VOD.RIP/settings.json`). Any process with read access to the user's app data directory can read it.

**Recommendation:** Consider encrypting the OAuth token at rest using the OS keychain (Windows Credential Manager, macOS Keychain, Linux Secret Service).

### 1.5 CSRF / Unauthenticated API (Info)

**File:** `backend/main.py`

The entire FastAPI is unauthenticated. This is by design (local-only, bound to `127.0.0.1` in production). However, the server binds to `0.0.0.0` in dev mode, which could expose the API on the local network.

**Risk:** Low in production (bound to 127.0.0.1). Medium in dev mode where `host="0.0.0.0"` is used.

---

## 2. Backend Issues

### 2.1 Thread Safety: Download Manager Race Conditions (Medium)

**File:** `backend/services/download_manager.py`

**Issues found:**

1. **`_spawn_worker` reads state outside lock** (line ~104-111) — Multiple dictionaries are read in a `with self._lock` block, but the `abort_fns` list is created outside the lock. The worker thread starts asynchronously, and there's a window where cancel/resume operations could race.

2. **`pause()` and `resume()` race with final cleanup** — When a download is paused, `_download_worker`'s `finally` block returns early (preserving events). But when resumed via `resume()`, a new worker is spawned. The old SSE queue entries from `_notify_sse` may still be pending, causing duplicate notifications.

3. **`cancel_all()` reads IDs outside lock** (line ~125) — The list of active IDs is captured under the lock, but individual `cancel()` calls run outside it, so the state may change between `cancel_all()` and the individual cancellation.

### 2.2 FFmpeg Subprocess: Pipe Buffer Deadlock Risk (Low)

**File:** `backend/services/ytdlp_service.py`
**Function:** `_run_ffmpeg()` (line ~60)

The stderr pipe is drained on a background thread to prevent deadlock. This is handled correctly. However, if `proc.poll()` never returns None (ffmpeg hangs), the inner loop spins with `time.sleep(0.05)` indefinitely. The cancel/pause events provide an escape hatch.

### 2.3 Preview Session Cleanup Race (Low)

**File:** `backend/services/preview_service.py`

**Issue:** `_cleanup_stale_sessions()` iterates sessions, collects stale IDs, and calls `delete_session()` for each. But `delete_session()` acquires `_lock` independently, and between the stale check and deletion, the session could be accessed by another thread (via `proxy_segment` or similar). The `get_session()` function touches `last_access`, but doesn't acquire `_lock` for the touch — it's read under lock, then modified outside.

### 2.4 Settings Save: Temp File Left Behind (Low)

**File:** `backend/services/settings.py`

The `save()` method writes to a temp file then `os.replace()` to the target. If `os.replace` succeeds but the cleanup `os.unlink(tmp)` fails, a temp file is left behind. The `tmp` variable tracks the path, but on Windows, if the temp file is on a different drive than the settings dir, `os.replace` may fail (WinError 17), and `shutil.copy2` is used as fallback — but the temp file is then *never cleaned up* for that case.

### 2.5 Server Port Release Inconsistency (Low)

**File:** `backend/services/server_lifecycle.py`

The `_request_graceful_shutdown()` checks `/api/info` to verify the server is VOD.RIP before sending `/api/exit`. If the server has already started shutting down or is in a half-initialized state, this could cause a false positive or hang.

### 2.6 Segment Download Missing Per-Request Timeout (Medium)

**File:** `backend/services/ytdlp_service.py`
**Function:** `_download_one_segment()` (line ~735)

The `requests.get()` call for each HLS segment uses `timeout=60` but this is a *connection timeout*, not a read timeout. A slow segment could take 60s before timing out. The `stream=True` mode means `iter_content()` blocks indefinitely between chunks. If a CDN server stops sending data (connection drop without TCP RST), the download thread hangs forever.

**Recommendation:** Implement a read-timeout mechanism (e.g., `iter_content` with a timeout wrapper) or set `timeout=(connect, read)` on `requests.get()`.

### 2.7 `os._exit(0)` in Update Paths (Info)

**File:** `backend/services/updater.py` (lines 220, 262, 291, 314)

The update process calls `os._exit(0)` after launching the installer or update script. This bypasses Python's cleanup handlers (file descriptors, atexit, etc.), but is intentional — the process should terminate immediately after spawning the update.

---

## 3. Frontend Issues

### 3.1 App.tsx is Excessively Large (High)

**File:** `src/App.tsx` — **208,467 characters**

This single file contains:
- The entire app state and logic (~1800 lines of hooks/callbacks)
- 8+ standalone components (`NeedleGlancePopup`, `DownloadConfirmDialog`, `EditableHmsTime`, `ClipDurationAdjustButtons`, `ChannelListIndexBadge`, `PlatformVodIcon`, `ChannelClipThumb`, `FieldCaption`)
- All API client code
- All panel resize/drag logic
- The main JSX render tree

**Impact:** Extremely difficult to maintain, review, or debug. Any change risks breaking unrelated functionality. Dead code cannot be spotted easily.

**Recommendation:** Extract standalone components into their own files. Split the main App component into smaller, focused hooks files.

### 3.2 Duplicate API Client Code (Medium)

**Files:** `src/App.tsx` (lines 227-290), `src/ChannelExplorePopup.tsx` (lines 74-120)

Both files define their own `apiGet`, `apiPost`, `apiDelete` functions. The `App.tsx` version has retry logic and timeout handling; the `ChannelExplorePopup.tsx` version is simpler.

**Recommendation:** Extract a shared `api.ts` module.

### 3.3 Duplicate PanelResizeHandles Component (Medium)

**Files:** `src/App.tsx` (line ~1180), `src/explorePopupUtils.tsx` (line ~133)

The `PanelResizeHandles` component is defined in both files with slightly different cursor mappings:

- **App.tsx:** Uses `arrow` cursors with Tailwind `group-hover:cursor-*`
- **explorePopupUtils.tsx:** Same approach but different initial cursor class (`cursor-default`)

**Recommendation:** Define once in `explorePopupUtils.tsx` since both files already import from it.

### 3.4 Stale Closure in applyPreviewQuality (Low)

**File:** `src/App.tsx` (line ~2065)

```typescript
const applyPreviewQuality = useCallback(async (levelIndex: number) => {
  const level = previewLevels[levelIndex];
  if (!level) return;
  previewRequestedHeightRef.current = level.height;
  setPreviewQualityLevel(levelIndex);
  setPreviewQualityMenuOpen(false);
  previewAppliedHeightRef.current = 0;
  await syncPreviewPlaybackToViewport();
}, [previewLevels, syncPreviewPlaybackToViewport]);
```

The `previewFullscreen` value inside `syncPreviewPlaybackToViewport` is read from the React state (via closure), not from a ref. If the user triggers a fullscreen change and immediately changes quality, the closure may capture a stale `previewFullscreen` value.

### 3.5 Unused Imports (Low)

**File:** `src/App.tsx` (line 5-11)

Several lucide-react icons are imported but never used directly in the render tree. E.g., `Download`, `RefreshCw`, `Trash2`, `Plus`, `ExternalLink`, `Eye`, `CheckCircle2`, `AlertCircle`, `GripVertical`. These may be used in child components or dynamic rendering — but a scan of the entire file suggests some are dead.

**File:** `src/ChannelExplorePopup.tsx` (line 7)

`Maximize2` is imported but unused (the explore popup uses `Minimize2` for fullscreen exit and creates its own Maximize2 button inline-only... actually looking at the code, `Maximize2` is only used in `App.tsx`, not in ChannelExplorePopup).

**Recommendation:** Run `tsc --noEmit` to catch unused imports (the `noUnusedLocals: true` flag is set in tsconfig, but imports of type-only symbols might not trigger warnings).

### 3.6 Concurrent Explore Popup Limit Not Enforced (Low)

**File:** `src/App.tsx` (line ~970)

`MAX_EXPLORE_POPUPS = 5` is defined but never referenced in any limiting logic. The `explorePopups` state can grow unboundedly based on user clicks.

### 3.7 Video Element Memory Leak on Component Unmount (Low)

**File:** `src/ChannelExplorePopup.tsx` (cleanup at line ~308)
**File:** `src/App.tsx` (cleanup at line ~2258)

Both components clean up HLS instances and detach progressive previews on unmount. However, the cleanup is in the effect's return function, which runs *before* the next effect runs (or on unmount). The `detachProgressivePreview` function calls `video.load()` which schedules asynchronous cleanup. If a new session starts before the old video element fully resets, there's a potential race.

### 3.8 localStorage Read Failures Not Silent (Low)

**File:** `src/App.tsx` (various)

`localStorage.getItem()` and `JSON.parse` are wrapped in try/catch blocks. However, `parseFloat()` (used in `readPreviewFsUiScale`) and other parsing operations are **not** wrapped. In private browsing modes where localStorage might throw `SecurityError`, these could crash the app.

### 3.9 Retry Queue File is Orphaned (Info)

**File:** `retry_queue.json`

This file exists at the project root but is never created or read by any source file. It appears to be a leftover artifact.

---

## 4. Code Quality & Maintainability

### 4.1 Inconsistent Error Handling Patterns (Medium)

**Backend:** Several patterns:
- `_normalize_err()` truncates errors to 200 chars
- `_format_platform_error()` handles Playwright NotImplementedError specially
- Many `except: pass` blocks (download_manager.py, preview_service.py, etc.)
- `_explain_oserror()` adds filename context to OSError messages

**Issue:** The error handling is inconsistent. Some errors are logged, some are silently swallowed, some are truncated. For a desktop app, users need clear, actionable errors.

### 4.2 Duplicate HLS Parsing Logic (Low)

**Files:** `backend/services/preview_service.py` (`_pick_preview_variant`, `_parse_playlist_assets`, `_segment_index_for_time`), `backend/services/ytdlp_service.py` (`_parse_m3u8`, `_resolve_media_playlist`, `_select_segments`)

Both files independently parse HLS playlists. The `ytdlp_service.py` version is used for downloads; the `preview_service.py` version is used for preview proxying. They have slightly different logic for resolving variants.

**Recommendation:** Share a common HLS parsing utility.

### 4.3 Deprecated Export Left in Place (Info)

**File:** `src/previewPlayerUtils.ts` (line ~279)

```typescript
/** @deprecated Use resolveHlsPreviewLevels */
export function resolvePreviewLevels(...)
```

Marked as deprecated but still exported and potentially used. A grep shows it's not used anywhere else in the codebase.

### 4.4 Magic Numbers (Low)

Several magic numbers scattered through the codebase:
- `PROGRESS_CAP = 90` in download_manager.py — why cap at 90?
- `MAX_SEGMENT_BYTES = 100 * 1024 * 1024` in preview_service.py (100MB!)
- `CHANNEL_CLIP_MAX_DURATION_SEC = 60` — clips longer than 60s are excluded, but Kick clips can be up to 90s in some cases
- `CLIP_FETCH_TIMEOUT_SEC = 35` — why 35?
- `_DEFAULT_UA = "Chrome/131.0.0.0"` — hardcoded and will go stale

### 4.5 Mixed Import Strategies (Low)

**Backend:** Mixes:
- `from module import function` (twitch_gql_service, kick_api_service)
- `import module` then `module.function()` (yt_dlp, requests)
- Late imports inside functions (gpu_detect, size_estimate)

This is partly intentional (avoiding circular imports) but makes tracing dependencies harder.

---

## 5. Data Integrity & Persistence

### 5.1 Settings JSON Atomic Writes (Low — Already Handled)

**File:** `backend/services/settings.py`

The `save()` method uses temp file + `os.replace` for atomic writes. This prevents corruption from partial writes. Good.

However, the `_load()` method does **not** validate the schema beyond Pydantic's model validation. If a future schema migration renames or removes a field, old settings files will silently lose data.

### 5.2 localStorage Quota Exceeded (Low)

**File:** `src/App.tsx`

Channel data (saved_channels) can grow to several MB for channels with many VODs. `localStorage` quotas are typically 5-10MB. No quota monitoring or graceful fallback is implemented. The `persistChannels` function silently catches errors.

### 5.3 Panel Layout Conflicts with Backend Persistence (Low)

**File:** `src/App.tsx` (lines 1885-1921)

Panel layout is saved to both localStorage AND sent to the backend via `/api/settings`. The `flushPanelLayoutToBackend` function fires on `pagehide`, but the debounced `apiPost` on panel change (line 1914) could race with the `pagehide` flush, potentially saving stale state.

---

## 6. Performance

### 6.1 Large React Re-render Scope (Medium)

**File:** `src/App.tsx`

All state is in a single `App` component. Any state change (e.g., progress update, preview time update) triggers a re-render of the entire app tree, including panels and controls that didn't change.

**Impact:** The `onTimeUpdate` handler fires ~4 times/second, re-rendering the entire 208k-char component tree each time.

**Recommendation:** Split state into smaller contexts or use `React.memo` on major sub-sections.

### 6.2 HLS Segment Cache Growth (Low)

**File:** `backend/services/preview_service.py`

The `_evict_cache_if_needed` function deletes cached segments from the session cache directory when `SESSION_CACHE_MAX_BYTES` (100MB) is exceeded. However, sessions are cleaned up by TTL (30 min), and the cache dir is cleaned on `delete_session`. In the worst case, a stuck session could accumulate 100MB of segments before eviction kicks in.

### 6.3 Thread Pool Starvation (Low)

**File:** `backend/main.py` (line ~70)

`INFO_EXECUTOR` has 8 workers. A typical preview session resolves kick + twitch metadata in parallel. But if 8+ slow requests are in-flight (e.g., Kick API timeouts), all workers are blocked and new requests queue up. The 60s API timeout provides eventual relief, but during that window the app feels unresponsive.

---

## 7. Summary of Findings

| Severity | Count | Key Issues |
|----------|-------|------------|
| **High** | 1 | `App.tsx` is 208k chars — impossible to maintain |
| **Medium** | 5 | SSRF proxy, download manager races, segment download timeout, duplicate components, broad re-renders |
| **Low** | 12 | Various thread safety, error handling, dead code, magic numbers |
| **Info** | 4 | Orphaned retry queue, deprecated exports, hardcoded UA, unauthenticated dev API |

### Top 5 Action Items

1. **Split App.tsx** into focused modules (hooks files, component files)
2. **Restrict preview proxy** to only allow URLs from `session.resource_map`
3. **Add read timeout** to HLS segment downloads
4. **Extract shared API client** into `src/api.ts`
5. **Deduplicate** `PanelResizeHandles` and HLS parsing logic

---

*Report generated by automated codebase analysis of VOD.RIP v1.0.15*

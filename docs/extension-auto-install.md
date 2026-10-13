# Unpacked-extension auto-install (VOD.RIP mechanism)

Status: active. This is the in-repo source of truth for how VOD.RIP installs
its companion extensions into the owner's real Chromium profile **without the
user dragging a folder by hand**.

Until 2026-10-13 this mechanism had **no documentation inside the repo**. The
only write-up lived in the manager repo
(`I:/!manager/playbooks/extension-auto-install.md`), and it points at an
external path (`I:/!aicompanion/VOD.RIP/scripts/...`) that does not exist in
this tree. Anyone grepping `docs/` for "auto-install" found nothing. This file
closes that gap.

## 0. TL;DR — where every piece lives

| piece | path |
|---|---|
| **Silent UIA installer** (first-time install) | `backend/scripts/cookie_extension_auto_install.ps1` |
| **Visible fallback** (opens `chrome://extensions` in a new tab) | `backend/scripts/manual_extension_manager_tab.ps1` |
| Routes + orchestration | `backend/routers/cookie_bridge.py` |
| Browser detection | `backend/services/browser_detect.py` |
| Frontend trigger | `src/lib/cookieAutoInstall.ts` |
| Extension sources | `vendor/cookie-extension/src/`, `vendor/kick-overlay/` |
| Staged unpacked folder (what the installer loads) | `%APPDATA%\VOD.RIP\cookie-extension\VOD.RIP-cookies\` |

## 1. The honest frame: this is NOT a Chrome-policy bypass

Modern Chrome and Edge deliberately refuse to auto-install extensions that are
not published on the Chrome Web Store. **No flag, policy, or script bypasses
that on an unmanaged machine** (see `README.md` → "Why this is a manual step").

What the code calls "auto-install" is **UIA automation of exactly the manual
flow**: open `chrome://extensions`, turn on Developer mode, click
**Load unpacked**, drive the folder picker, verify. The user never types or
drags; Chrome still sees a normal developer-mode unpacked load. Keep this
distinction when describing the feature — the README and this doc must agree.

## 2. End-to-end flow

```
POST /api/session/cookies/auto-install          (cookie_bridge.py:985)
  └─ _ext_src_override() or _materialize_ext_src()   → staged unpacked folder
  └─ _find_browser("chrome"|"msedge"|"brave")        → first Chromium found
  └─ _start_auto_install(browser, src, include_kick_overlay)
       └─ thread "cookie-auto-install" → _auto_install_worker
            └─ _run_auto_install_script(browser, dir)   (cookie_bridge.py:781)
                 └─ powershell -File cookie_extension_auto_install.ps1
                        -ExtensionDir <dir> -Browser <browser> -DebugPort 7897
                 └─ parse the trailing JSON line (stdout = ONE JSON object)
            └─ (optional) same for vendor/kick-overlay/
  └─ on success: _wake_extension_browser()  → opens a platform page in the
     background so the extension's service worker wakes and pushes cookies
```

Frontend: `runSilentCookieExtensionInstall()` in `src/lib/cookieAutoInstall.ts`
POSTs the route and polls `GET /api/session/cookies/status` every 2 s until
`auto_install.state` is no longer `running` (120 s timeout).

`_materialize_ext_src()` extracts the zip payload of the shipped `.crx` into the
staging folder, drops `_metadata/` (Chrome's own bookkeeping), guards against
zip-slip, and writes a `drag this folder above.txt` note next to it.

## 3. The silent installer (`cookie_extension_auto_install.ps1`) — how it works

Pure BCL/PowerShell + `Add-Type` P/Invoke + UIAutomation. **No CDP** (see §4.9).

1. **Spawn** `chrome.exe --new-window <extensions URL>` with a WinEvent hook
   installed *before* the spawn (event-driven, no polling).
2. **Alpha-0, on-screen.** The window is placed on-screen (off-screen counts as
   occluded → the WebUI renderer throttles and exposes no UIA controls) but set
   `WS_EX_LAYERED` + `LWA_ALPHA=0` — invisible to the user, fully live to UIA.
   It is alpha-0'd before the first DWM frame paints.
3. **Focus bounce.** The user's foreground HWND is captured *before* the spawn;
   any steal by our window or the picker is returned via
   `AttachThreadInput` + `SetForegroundWindow`. Zero `keybd_event`.
4. **Navigate silently.** Chrome 151+ drops `chrome://` URLs from the command
   line. The script sets the omnibox via UIA `ValuePattern.SetValue` and posts
   `WM_KEYDOWN/WM_KEYUP` Enter (posted keys go to *our* window, never the
   foreground).
5. **Developer mode.** Read the `devMode` toggle by automation id; toggle only
   when `OFF`.
6. **Load unpacked.** Click the `loadUnpacked` button by automation id (id
   first — the page-reload button is also named "Reload"). Coordinate-click
   fallback exists for builds where the WebUI is absent from the UIA tree.
7. **Drive the folder picker.** `DrivePicker` sets the "Pasta:" edit
   (automation id `1152`, fallback by name) with one
   `ValuePattern.SetValue`, then `Invoke`s "Selecionar pasta". **No keyboard
   input into the picker** — posted `WM_CHAR` never reached the field.
8. **Verify by profile, not by page.** Chrome 151 hides `chrome://` WebUI from
   UIA, so the install is confirmed by reading the profile's
   `Secure Preferences` → `extensions.settings` for an entry whose `path` equals
   the staged folder. The extension id is read from the same JSON.
9. **Cleanup.** Close the spawned window (wait until the handle is gone), sweep
   stray folder pickers, and only on the error path sweep spawned-but-undiscovered
   windows (never close a window the user opened during the run).

## 4. Installer rules (every one is a trap)

1. **Normalize the path before it is typed.** The picker receives
   `-ExtensionDir` verbatim. A lowercase drive or forward slashes make Chrome
   answer *"Nome de pasta inválido" / "Invalid folder name"*. The script
   normalizes once (uppercase drive, `\`, collapse doubled separators, drop the
   trailing separator) and aborts before opening any window when the result is
   not a real drive (`code:"bad_path"`, rc 2).
2. **Exit code is meaningful; the JSON is the source of truth.**
   `0` installed/reloaded/idempotent · `1` generic UIA failure · `2` bad path
   (nothing opened) · `3` the folder dialog refused the path or lost its state.
   Parse the JSON line, never the rc alone.
3. **Idempotent shortcut.** An already-installed path returns
   `installed:true` + `version` in ~2–3 s with zero windows.
4. **Detecting the install later.** Look up `Secure Preferences` →
   `extensions.settings` by `path` (an unpacked extension's id is a hash of that
   path). Normalize separators on both sides.
5. **No alternative profile, ever.** No `--user-data-dir`, no temp/copied/new
   profile, no harness headless. The install runs in the owner's real profile.
6. **Focus + deadline.** The spawned window is alpha-0 and the picker also
   activates, so the focus guard runs for the whole drive; a hard `-TimeoutSec`
   (default 120 s) prevents a wedged picker from stranding a hidden window.
7. **A refused dialog must not be clicked through.** Scan the dialog's own text
   for `inválido|invalid folder`; on a hit return `REJECTED:<text>` without
   pressing the commit button, and exit 3 with a two-cause diagnosis.
8. **"Load unpacked" does NOT update an already-loaded extension.** Re-driving
   it hits a duplicate refusal; with a `\`-separated path the profile check can
   match the OLD record and report a false `installed:true`. Updates use §5.
9. **CDP limits learned the hard way.**
   - A `chrome-extension://` page is **not attachable** by the relay;
     `bg-window.py` returns `{ok:false}` — indistinguishable from total failure.
   - A tab that merely *contains* a `chrome-extension://` **iframe** kills the
     CDP session ("Detached while handling command").
   - Chrome 137+ refuses `--remote-debugging-port` on the **real** profile
     (account revocation). CDP is only reachable through the relay at
     `ws://127.0.0.1:9224/cdp`.
10. **Stale service worker.** Chrome serves the old extension `background.js`
    from the service-worker cache even after a disk change and even after a
    browser restart. Deleting `Service Worker\CacheStorage\<id>\`,
    `ScriptCache\` and `Database\` with the browser closed is the only reliable
    reset.

## 5. The update path (reload in place)

Because "Load unpacked" cannot update, a live unpacked extension reloads
**itself** — zero windows, zero focus steal:

1. Stage the new folder (same path, so the id is unchanged).
2. `POST /api/extension/reload {to:<version>}` persists a directive; the
   extension's service worker polls `GET /api/extension/status` on its 30 s
   alarm, calls `chrome.runtime.reload()` in place, and the fresh SW POSTs
   `/api/extension/reload-done` — clearing the directive. **The directive
   clearing is the version proof** (the SW only confirms after its manifest
   version matches the shipped one).
3. `cookie_extension_auto_install.ps1 -ReloadOnly` wraps that: it POSTs and
   polls until the directive clears (max 90 s) and **never spawns Chrome**.

For extensions that need a CDP/UIA nudge (manager tooling), the proven 4-step
recipe is in `I:/!manager/playbooks/extension-auto-install.md`: `bg-window.py
open` on a page the content script runs on → open
`chrome-extension://<id>/reload-bootstrap.html` via `ext-reload-cdp.mjs` →
click the bootstrap button by UIA (`ext-reload-uia-click.ps1`) →
verify `version` + `scriptable_host` in `Secure Preferences`.

## 6. The two shipped extensions

- **Cookie bridge** — `vendor/cookie-extension/src/` (MV3, ESM service worker,
  forked from *Get cookies.txt LOCALLY*). Reads login cookies for
  `youtube.com`, `twitch.tv`, `kick.com` only and POSTs them to
  `http://127.0.0.1:7897`. Staged as `VOD.RIP-cookies`.
- **Kick Overlay** — `vendor/kick-overlay/` (MV3 content script + popup). Ad-free
  Twitch playback. Installed by the same installer when
  `include_kick_overlay: true`.

Both install through the same `_run_auto_install_script` path; the installer is
generic (it derives the extension card name from the target folder's
`manifest.json`).

## 7. Measured (real profile, 2026-09-20)

| flow | wall | windows | foreground moved off the owner |
|---|---|---|---|
| idempotent (`installed:true`) | 2.3 s | 0 | none |
| full UIA install (`-Force`) | 8.9–12.9 s | 1 (alpha-0) | spawn 0.04 s + click 1.5 s + picker ~8–9 s |

## 8. Canonical sources

1. This file — the in-repo mechanism + traps.
2. `README.md` → "Optional: the Cookie Bridge extension" — the user-facing
   manual flow and the policy explanation.
3. `AGENTS.md` → "Browser Extensions" — where the extension sources live.
4. `backend/tests/test_cookie_extension_install.py` — the source-level guards
   that pin the driver contract (no restore of visible windows, no clipboard,
   exit-code contract).
5. `I:/!manager/playbooks/extension-auto-install.md` — manager-side write-up
   (verified 2026-09-20; references an external `I:/aicompanion/VOD.RIP` path).

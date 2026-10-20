# VOD.RIP-remake — Agent Instructions

## consultgpt — Terminal ChatGPT (No API Key)

consultgpt is installed globally and available as `gpt` and `codeintel` commands.

It uses a **real browser session** to interact with ChatGPT. No API key needed — just log in once.

### Quick Reference

| Command | What it does |
|---------|--------------|
| `gpt "question"` | One-shot question to ChatGPT |
| `gpt -f file.py "review"` | Inject file contents + ask |
| `gpt @src/main.py "explain"` | Same via @file syntax |
| `gpt -s name "question"` | Start named session (persistent) |
| `gpt -s name "follow-up"` | Continue previous session |
| `gpt kill name` | Kill a session |
| `gpt audit` | Full codebase audit |
| `codeintel search "query"` | Search code index |
| `codeintel ask "how does X work?"` | Synthesize architecture answer |

### Code Review Loop (MANDATORY)

Every significant code change must go through this loop:

```
1. codeintel search "concept"     → find relevant files
2. make changes                   → implement
3. gpt -f changed.py "review"     → get ChatGPT review
4. fix issues found               → iterate
5. done                           → only when gpt says pass
```

### File Injection

```bash
# Inject single file
gpt -f backend/services/preview_service.py "Review for bugs"

# Inject multiple files
gpt -f file1.py -f file2.py "Review these two files"

# Line ranges
gpt -f backend/app.py:50-100 "Explain this section"

# @file syntax (inline in question)
gpt "Review @backend/services/preview_service.py for security issues"
```

### Multi-Turn Sessions

```bash
# Start a named session (injects code on first turn)
gpt -s review "Review @src/main.py for bugs"

# Follow-up (context preserved automatically)
gpt -s review "Now fix the issues you found"

# Verify fixes
gpt -s review "Verify my changes are correct"

# Kill when done
gpt kill review
```

### Code Index (codeintel)

```bash
# Index the project
codeintel index .

# Search for symbols
codeintel search "PreviewSession"

# Search for callers
codeintel search "callers of create_session"

# Ask architectural questions
codeintel ask "how does the preview pipeline work?"

# Code health check
codeintel health
```

### Audit Mode

```bash
# Full codebase audit
gpt audit

# Audit specific files
gpt audit --files backend/app.py

# Audit specific folders
gpt audit --folders backend/services/
```

### Flags

| Flag | Purpose |
|------|---------|
| `-f, --files` | Inject files as code context |
| `-s, --session` | Named session for persistence |
| `--no-code` | Skip code injection (required when no files) |
| `--headed` | Show browser window |
| `--auto` | Auto-route based on prompt size |
| `--kill-after N` | Timeout in minutes |
| `--codeintel` | Use index for search (NOT for local file review) |

### Common Patterns for This Project

```bash
# Review a router change
gpt -f backend/routers/preview.py "Review for API correctness"

# Review a service change
gpt -f backend/services/preview_service.py "Check for race conditions"

# Review frontend changes
gpt -f src/App.tsx "Review for React best practices"

# Full backend audit
gpt audit --folders backend/

# Find where a function is used
codeintel search "callers of schedule_youtube_window_hls_mux"
```

### Rules

1. **Always review after changes** — `gpt -f changed_file.py "review"` before commit
2. **Use codeintel first** — search before writing code
3. **Don't mix --codeintel and -f** — they're different paths
4. **--kill-after on long runs** — prevent runaway processes
5. **CLI only** — use terminal commands, not workarounds

### Windows/PowerShell Notes

- `--files a b c` works with space-separated paths
- Use `--` to separate flags from question: `gpt -f a.py -- "review this"`
- Progress prints to stderr, response to stdout

## Testing rule: the FULL user path — never test the backend in isolation

**Testing preview timing or any user-facing behaviour through the backend alone is
misleading.** The backend is one link of a chain; the user's real experience is
`browser → Vite proxy → backend → external (YouTube/Kick/Twitch)`, and every link
adds variables an isolated test silently drops:

- the **Vite dev proxy** — cold transforms, the fail-fast 8s lanes, connection reuse, the IPv4/IPv6 bind
- the **browser** — connection pool, Happy Eyeballs IPv4/IPv6 fallback, cached modules, the pre-React loader
- the **app's own background state** — the startup warm wave, the archive scheduler, rate budgets, the archive cache
- **real concurrency** — the owner plays a game; Steady Watcher throttles; other tabs are open

**So: when you measure a user-facing path (preview start, archive freshness,
download start, search), drive it the way the user does** — the browser, through
the proxy, against the *running* app — and read the real timings from the page's
own network timeline (`chrome-devtools` → `list_network_requests`, or the page's
Performance API). Backend-only checks are for *unit* questions ("does this
function return the right thing?"), never for *latency or freshness* claims.
If you must isolate a link, say so in the same breath as the number and label it
backend-only.

## Browser Extensions

- Kick Overlay (Twitch ad-kill): `vendor/kick-overlay/`
- Cookies (VOD.RIP cookie/po_token): `vendor/cookie-extension/src/` — installed from the `VOD.RIP-cookies` subfolder (staging: `%APPDATA%\VOD.RIP\cookie-extension\VOD.RIP-cookies\`)
- **Install/update mechanism** (silent UIA installer + zero-window reload, every trap): `docs/extension-auto-install.md`. Read it before touching `backend/scripts/cookie_extension_auto_install.ps1` or the `/api/session/cookies/auto-install` route.

### Auto-install, verified 2026-10-20

**It is UIA automation of the manual flow, not a Chrome-policy bypass**, and that distinction is the whole design: Chrome/Edge refuse sideloading except through Developer mode → `Load unpacked`. The script opens `chrome://extensions`, turns Developer mode on, clicks *Load unpacked*, drives the folder picker and verifies. The user never drags a folder; Chrome still sees a normal unpacked developer load.

Pieces (all in-tree; this used to live only in the manager repo):

| piece | path |
|---|---|
| route + orchestration | `backend/routers/cookie_bridge.py` (`POST /api/session/cookies/auto-install`) |
| silent installer | `backend/scripts/cookie_extension_auto_install.ps1` |
| visible fallback | `backend/scripts/manual_extension_manager_tab.ps1` |
| browser detection | `backend/services/browser_detect.py` |
| frontend trigger | `src/lib/cookieAutoInstall.ts` |

Flow: `_materialize_ext_src()` stages the unpacked folder → `_find_browser()` (chrome/msedge/brave) → thread → `powershell -File cookie_extension_auto_install.ps1 -ExtensionDir <dir> -Browser <browser>` → the script prints ONE JSON line → the route records it in `auto_install`. Nothing runs browser-side afterwards: a fresh install fires `chrome.runtime.onInstalled`, and the service worker pushes cookies by itself (`cookies.onChanged`, `tabs.onUpdated`, a 10-min alarm, a 30-s poll of `/api/extension/status`). There is deliberately no "wake tab" — the old `_wake_extension_browser()` looked the browser up by PLATFORM name and never ran.

**`installed: true` does NOT mean cookies exist.** These are separate facts and conflating them cost real debugging time:

```jsonc
// GET /api/session/cookies/status — measured 2026-10-20 01:05
{"paired": true, "enabled": true, "bridge_ready": true,   // bridge + SW alive
 "auto_install": {"state":"done","installed":true},        // extension IS loaded
 "platforms": {},                                          // NO cookies stored
 "youtube_authenticated": false}
```

With `platforms: {}` the authenticated InnerTube race still answers, but with **`formats=1 heights=[360]`** — no 720/1080 tier — and yt-dlp logs `Sign in to confirm your age`. The extension is only useful once a logged-in profile exports: the popup's export (or the SW alarm) is what fills `platforms`. **Installing it is not the fix; exporting a signed-in session is.** Verify with `platforms` being non-empty and `youtube_authenticated: true`, never with `installed`.


## Runtime logs

- Application/server error log: `%APPDATA%\VOD.RIP\logs\errors.jsonl` (override the data root with `VODRIP_APP_DATA`).
- The error log retains the latest 500 sanitized error records; the running API exposes them at `GET /api/errors/latest?limit=500`.
- Dev supervisor stdout/stderr logs: `tmp\vodrip-devall-api.log` and `tmp\vodrip-devall-web.log`.

## The live archive.db lives on H: (learned 2026-10-04)

**As of 2026-10-19 there is exactly ONE `archive.db` on this machine: `H:\VOD.RIP-data\archive.db`.** The two orphaned predecessors this section used to document were merged and deleted (owner-approved — see "The orphans — GONE" below for the exact row counts that were carried over). Before that they were read by mistake repeatedly, and every count from either was wrong.

| path | size | last write | status |
|---|---|---|---|
| `H:\VOD.RIP-data\archive.db` | **9.63 GB** (measured 2026-10-19 22:38) | written continuously | **LIVE — the only one left** |
| `C:\VOD.RIP-data\archive.db` | — | — | **deleted 2026-10-19** (293 videos merged first) |
| `G:\VOD.RIP-data\archive.db` | — | — | **deleted 2026-10-19** (244 videos merged first) |
| `%APPDATA%\VOD.RIP\archive.db` | — | — | **deleted 2026-10-19** (nothing unique) |

This has already caused three wrong reports. An orchestrator read the `%APPDATA%` orphan, found no `rate_limit_events` table, and reported twice — with a formatted table — that rate history was inert in production. It is not: the live archive has that table with 33 rows. A backlog plan was then built on the orphan (1,161 videos / 3,723.6 hours). The live archive holds 12,715 videos / 27,631 hours / 1,216 videos with transcripts. A third agent re-read the orphan and repeated the first error. (Measured read-only on 2026-10-04. The live DB keeps growing — re-measure, do not reuse these totals.)

### ideos.platform is stored LOWERCASE — never hand-type it in a query

youtube, 	witch, kick. NOT YouTube. A filter written as
`platform = 'YouTube'` returns **0 rows**, and zero rows reads exactly like
"that platform's backlog is done". It is the same class of error as the
orphan-DB mistake above: a wrong query is worse than no query, because the
result looks like a finding. This one was made on 2026-10-05 and briefly
reported the YouTube backlog as 0; the real figure was 7,119.

**Enumerate the values, never type them.** `SELECT platform, COUNT(*) FROM
videos GROUP BY platform` — the column also carries no UNNORMALISED
variants, so a filter that returns nothing is a filter that is wrong.

Re-measured 2026-10-05 07:17, read-only from `H:\VOD.RIP-data\archive.db`
(7,543.8 MiB, 33 tables, 13,054 videos, 3,104,739 transcripts): backlog =
videos with NO `transcripts` row, joined `videos.video_id =
transcripts.video_id`:

| platform | videos | hours |
|---|---|---|
| youtube | 7,119 | 9,062.1 |
| twitch | 4,126 | 17,610.6 |
| kick | 594 | 1,766.9 |
| **total** | **11,839** | **28,439.6** |
### How the path is resolved

```text
_db_path()                            archive_db.py:340
├─ VODRIP_ARCHIVE_DB (env)            archive_db.py:341   — unset
└─ data_dir() / "archive.db"          archive_db.py:356
   ├─ VODRIP_DATA_DIR (env)           disk_hygiene.py:169 — unset
   ├─ settings.data_dir               disk_hygiene.py:174 — "" in the live settings.json
   └─ auto tier (speed-first)         disk_hygiene.py:180-183
      fastest_disk()                  disk_detect.py:131  — answers "H:"
      → <drive>\VOD.RIP-data
```

Every override is unset and `settings.data_dir` is empty on this box, so the answer falls all the way through to the auto tier: `H:\VOD.RIP-data\archive.db`.

`_migrate_db_to_data_dir()` (`archive_db.py:374`) is what walked the database along that chain — `%APPDATA%` → `G:` on 2026-09-17, then `G:` → `H:` later — **copying** at each step and leaving every predecessor behind.

### Env overrides the code actually reads (learned 2026-10-05)

Only three of these were documented before this table. The rest are live and undiscoverable, which is the same defect as a lying knob in reverse: a feature the owner cannot find is a feature the owner never uses. Every row is a real read, verified in the AST; the default lives at the cited line and is deliberately NOT restated here, so this table cannot drift away from the code.

| variable | read at | what it moves |
|---|---|---|
| `VODRIP_ARCHIVE_DB` | `archive_db.py:341` | the live archive DB file (whole path) |
| `VODRIP_DATA_DIR` | `archive_db.py:334` | the data root the auto tier starts from |
| `VODRIP_APP_DATA` | `settings.py:21` | `%APPDATA%\VOD.RIP` — settings.json, logs |
| `VODRIP_CACHE_DIR` | `settings.py:45` | yt-dlp cache, transcript-fix cache, temp |
| `VODRIP_ARCHIVE_DIR` | `routers/disk.py:47` | the archive media folder, when not in settings |
| `VODRIP_COOKIE_DB` | `cookie_store.py:70` | the cookie store DB (separate from the archive) |
| `VODRIP_EMBED_MODEL` | `archive_embed.py:43` | semantic-embedding model directory |
| `VODRIP_WHISPER_CACHE` | `archive_embed.py:61` | per-model weights cache (beats `VODRIP_CACHE_DIR`) |
| `VODRIP_EMBED_CACHE` | `archive_embed.py:97` | embedding output cache |
| `VODRIP_EMBED_BACKFILL` | `app.py:791` | on by default; set `0` to skip the backfill |
| `VODRIP_KICK_GATE_FREEZE_SEC` | `kick_gate.py:38` | how long a Kick 403/429 streak freezes Kick |
| `VODRIP_YT_GATE_FREEZE_SEC` | `yt_gate.py:35` | how long a YouTube bot-gate freezes YouTube jobs |
| `VODRIP_TRANSCRIBE_WORKERS` | `archive_transcribe.py:113` | CPU ASR slots; `0` = GPU-only on a CUDA host |
| `VODRIP_NO_CUDA_LIBS` | `archive_transcribe.py:1390` | force the CPU path even with a GPU |
| `VODRIP_WHISPER_DEVICE` | `archive_transcribe.py:368` | pin the ASR device (supersedes auto-detect) |
| `VODRIP_MAX_DOWNLOAD_BYTES` | `routers/downloads.py:71` | per-download size ceiling |
| `VODRIP_NO_DAEMONS` | `app.py:128` | `1` = no background daemons (debug). **NEVER use it to measure latency.** |
| `VODRIP_TAKE_PORT` | `server_lifecycle.py:456` | take a busy API port instead of failing |
| `VODRIP_SKIP_PORT_RELEASE` | `run.py:85` | skip the pre-bind port release |
| `VODRIP_ALLOW_PIP_INSTALL` | `run.py:103` | allow the dev path to shell out to pip |

### `VODRIP_NO_DAEMONS=1` is a MEASUREMENT TRAP (learned 2026-10-15, expensively)

It is documented as "no background daemons (debug)". What it actually does is
different in kind, and four rounds of diagnosis were built on the difference.

Measured, same frozen build, same box, same harness, ONE variable:

| warm wave | `VODRIP_NO_DAEMONS` | result |
|---|---|---|
| on  | **set** | server stops answering; process alive at 600+ MB with NO listener; connections refused |
| off | **set** | server stable 220 s |
| on (source) | unset | server stable 190 s; panel 0.02 s |
| on (frozen) | **unset** | server stable 190 s; panel 0.04-0.17 s |

Six live previews, A/B, 12 reps: **11 of 11 TIMEOUTS with the flag set, 12 of 12
PLAYED without it.** With it unset and after the cold-load reps, 8 of 8 consecutive
reps land at <= 2.49 s.

So the flag does not make the app lighter, it makes it a DIFFERENT app: the HTTP
listener dies under load while the process stays alive. That symptom is
indistinguishable from a real app defect, and it produced these wrong conclusions
in commits that are still in the history:

- "the stale install does not boot" (rounds 100-101)
- "six 20 000-row panel fetches stall every preview" (457a66c, walked back in d600e3b)
- "the startup warm wave kills the server" (d600e3b, refuted in f43739e)

**The rule: never measure latency, boot time, or server stability with this flag
set.** Use it only when you deliberately want the daemons out of the way for a
unit-level check.
**One knob in the tree is RETIRED and does nothing:** `VODRIP_TRANSCRIBE_GPU_COPIES` is annotated `# DEPRECATED, IGNORED` at `archive_transcribe.py:115` and is read nowhere except the module self-check. Multi-copy ASR was removed; the budget is one shared-model CUDA slot. `todo.md` used to list it as a live dial — `backend/tests/test_env_knob_truthfulness.py` now fails if any doc advertises it as working, or if a live read path is reintroduced.

Naming a knob in a doc is a claim the code must honour. Before documenting one, confirm the read exists; before trusting one, confirm it is not on this retired list.

### The rule

**The log directory and the database directory are resolved by different code and can disagree.** Logs go to `%APPDATA%\VOD.RIP\logs\errors.jsonl` (`backend/services/error_log.py:62` — anchored to appdata, not to the data disk); the database goes through `disk_hygiene.data_dir()`. `errors.jsonl` is written there daily, which is exactly what keeps the stale `%APPDATA%` archive looking like the production one.

- **Logs in `%APPDATA%` do NOT mean the database is in `%APPDATA%`.**
- **Before quoting any count, name the exact file you read** — full path, in the same breath as the number.
- When in doubt, re-run the check below. Do not reason from log freshness.

### Verify which file is live (read-only)

```powershell
Get-ChildItem "$env:APPDATA\VOD.RIP\archive.db","G:\VOD.RIP-data\archive.db","H:\VOD.RIP-data\archive.db" -EA SilentlyContinue | Sort-Object LastWriteTime -Desc | ForEach-Object { "{0,-46} {1,6:0} MB  {2:yyyy-MM-dd HH:mm}" -f $_.FullName, ($_.Length/1MB), $_.LastWriteTime }
```

The newest write is the live archive. On 2026-10-04 that was `H:\VOD.RIP-data\archive.db`, 7,537 MB, 2026-10-04 22:25 — the two orphans were 17 days and 18 days stale respectively. This command only stats files; it opens nothing and writes nothing.

### The orphans — GONE, merged and deleted 2026-10-19 (owner-approved)

**Status as of 2026-10-19: only `H:\VOD.RIP-data\archive.db` exists.** The two
predecessor files were merged and then deleted, on the owner's explicit
instruction, after a set-difference proved what each held:

| file | unique videos | carried into H: | now |
|---|---|---|---|
| `C:\VOD.RIP-data\archive.db` | 293 | 293 videos, 103,499 messages, 20 archive_jobs | **deleted** |
| `G:\VOD.RIP-data\archive.db` | 244 | 244 videos, 98,687 messages, 1 alias, 2 archive_jobs | **deleted** |
| `%APPDATA%\VOD.RIP\archive.db` | 0 | nothing | **deleted** |

Live archive afterwards: **18,696 videos · 4,051,807 transcript segments ·
16,969,566 chat messages.** DB files freed 1.67 GB; the now-orphaned data-root
caches (`C:\VOD.RIP-data` 0.49 GB, `G:\VOD.RIP-data` 1.65 GB — preview caches,
pytest scratch and three redundant Parakeet Redux copies verified byte-size
identical to `H:\VOD.RIP-models\parakeet-models\sherpa-onnx-nemo-parakeet-redux`)
were removed too. Nothing outside `H:` remains.

**The cause is now pinned shut, and that is the part that matters.**
`settings.data_dir` was `""`, so every boot went through the auto tier and
`fastest_disk()`; the 2026-10-19 dev boot answered `C:` instead of `H:`, so the
day's ingest landed in a fresh 1 GB archive while the 9.6 GB live one went
untouched — a different, smaller archive in the same UI. That is the same class
as the "wrong file" trap below, not a historical curiosity. `settings.data_dir`
is now the literal `H:\VOD.RIP-data`, which the resolver reads **before** the
probe (`disk_hygiene.data_dir()`: env → settings → auto tier), so a transient
free-space probe miss can no longer choose the system drive.

The text below is kept as the generic warning, not as a description of files
that exist. **Deleting or merging any archive is still the owner's decision, not
a worker's** — and `grep`-ing the tree is still not how you find a reader of a
stale file.

Both predecessors were still on disk until 2026-10-19 and misled readers for
weeks. **Deleting them is the owner's decision, not a worker's** — do not clear
them as part of "cleanup" or disk-pressure work. Report their existence; let the
owner choose.

**The two are NOT equally safe to delete — check before recommending either.** Comparing `(platform, video_id)` sets against the live archive on 2026-10-05:

| orphan | videos | rows absent from the live archive | verdict |
|---|---|---|---|
| `%APPDATA%\VOD.RIP\archive.db` | 1,161 | **0** | a strict subset — nothing unique is lost |
| `G:\VOD.RIP-data\archive.db` | 2,309 | **244** | holds 244 videos the live archive does not — deleting it destroys rows unless they are merged first |

The live archive keeps growing, so the 244 is a floor, not a fixed number. Re-measure before acting on either row, and re-derive it the same way: open the live archive and each orphan with `file:<path>?mode=ro` (read-only URI — never a plain `connect()`, and never a `mode=rw` write), read `(platform, video_id)` from `videos` in each, and set-difference. Do not run a merge on the owner's initiative either — that writes to the live database.

**A fresh `-shm` mtime is a genuine "someone just read the wrong file" signal — but only in that direction.** When the `-shm` moves while `archive.db` and `-wal` stay frozen, something opened that database read-only: a WAL connection touches the shared-memory index on open, which a plain file copy or a live app write would not do. Measured 2026-10-05 07:11, `G:\VOD.RIP-data\archive.db-shm` had been written 06:39 that morning while its `archive.db` and `-wal` were frozen at 2026-09-17 — a process had the **wrong archive** open half an hour earlier. The trap is not historical.

**The converse does not hold, and the live database is the control that proves it.** `H:\VOD.RIP-data\archive.db-shm` sat frozen at 02:02 on 2026-10-05 while its `archive.db` was rewritten at 13:32 and its `-wal` at 13:33 — an actively written database whose `-shm` had not moved in 11 hours. So a *stale* `-shm` is not evidence that nothing is reading a file; the index mtime only records some accesses. Check a fresh one before recommending a delete, and never read a still one as clearance.

**It recurs, and it is not this repo.** The same `-shm` moved again to 07:17 with `archive.db` and `-wal` still frozen at 2026-09-17. No committed code path opens that file: the only `G:\VOD.RIP-data` literal in the tree is `backend/tests/test_data_dir_pin_cache.py:39`, a stub path the test compares and never opens. No running process holds the path in its command line either, so the reader is short-lived and lives outside the repo. Attribute it with handle-level tooling before deleting — do not re-grep this tree, it has already been ruled out.

**The recurrence is irregular, so re-measure rather than infer.** The gaps between touches were ~14 h (04/10 16:22 → 05/10 06:39), ~38 min (06:39 → 07:17), then **94 min of silence** (still 07:17:59 when re-read at 08:52, with no agent turn in between). That is a bursty, on-demand pattern, not a fixed interval and not a steady daemon. Two consequences: do not treat a single fresh mtime as proof of a running daemon, and do not treat a single stale one as proof of safety — the orphan was quiet at 08:52 and was being read hours earlier. Re-read the `-shm` mtime immediately before acting, and take a reading that is already stale as "no reader at this moment", not as clearance.

### Decoys: `vodrip.db` is not the database

The live data dir also holds a **`vodrip.db` that is 0 bytes** and referenced nowhere in the codebase — `grep vodrip.db` across `backend/` returns no matches. It sits *beside* the real archive rather than in a stale directory, which makes it the more dangerous of the four: a glob for `*.db` finds it, and it is the only one obviously empty. It is not a database and holds no rows. It is not an orphan predecessor either — it was never written to.

The live database in the same folder is 7,910,268,928 bytes with a 27 MB `-wal` that moves minute to minute. Never pick a file by name; pick it by size and write time.

```powershell
Get-ChildItem "H:\VOD.RIP-data\*.db","H:\VOD.RIP-data\*.db-wal","H:\VOD.RIP-data\*.db-shm" -EA SilentlyContinue | Sort-Object Length -Desc | ForEach-Object { "{0,-16} {1,14:0} bytes  {2:yyyy-MM-dd HH:mm}" -f $_.Name, $_.Length, $_.LastWriteTime }
```

Read-only, and it puts the decoy and the live archive in one view. Substitute the drive the resolver actually returned rather than assuming `H:`.

### Second disagreement: which drive wins for models

`best_model_cache_drive()` (`backend/services/disk_hygiene.py:286-305`) answers **`H:`** in practice. It is speed-first — fastest bus tier with >= 8 GB free, ties broken by free space — and H: is NVMe with more room than G:. The "Heavy project data lives OFF C:" section below documents **`G:`** for models.

**The code and that section currently disagree, and that section is not authoritative for which drive wins** — read the function, not the prose. Not resolved here: which drive should own model weights is a separate decision for the owner, not a documentation fix.

## Heavy project data lives OFF C: (learned 2026-08-15)

**C: is the system NVMe — never put heavy project artifacts there.** It had **13.2 GB free** when measured on 2026-10-04 (this number drifts; re-measure rather than quoting it) and the repo bloated to 18.7GB on C: (9GB `dist` + 9GB `_internal` + `build` + a stray CUDA-13 stack). Disk map:

- **G:** (NVMe, ~10 GB free on 2026-10-04) — `G:\VOD.RIP-models`, `G:\VOD.RIP-data`, `G:\vodrip-bench`, downloads
- **H:** (NVMe, ~109 GB free on 2026-10-04) — **the live `archive.db` is here**, plus frozen bundle installs `H:\VOD.RIP-build\dist\VOD-RIP` (build-install.ps1 default)
- **I:** (HDD, 4TB) — bulk/long-term storage

**Which drive a given artifact lands on is decided by code, not by this map.** `disk_hygiene.best_model_cache_drive()` and the auto tier inside `data_dir()` pick the fastest bus tier with enough free space, ties broken by free space — so with G: at ~10 GB and H: at ~109 GB, both answers land on **`H:`**. Models and the live database currently sit on `H:`; G: retains older copies (including the orphaned predecessors listed above). Do not hand-place a model or database on G: expecting the app to find it there, and do not read this map as authoritative for which drive wins — read the resolver.

Rules:

- Build outputs (`dist\`, `_internal\`, `build\`) are gitignored and regenerable — **delete them from the repo after `scripts/build-install.ps1` installs to H:**; do not leave ~18GB of build trees on C:.
- Model caches, ASR scratch, benchmark audio → the models root chosen by `disk_hygiene.best_model_cache_drive()` (currently `H:\VOD.RIP-models`); NOT `G:\Temp`, where pytest's session-end wipe deletes `vodrip-*` (`tempfile.gettempdir()` = `G:\Temp` on this box).
- **VOD archives and DBs → the live `archive.db` is on `H:`** (`H:\VOD.RIP-data`), resolved by the auto tier in `disk_hygiene.data_dir()`. The two files on `%APPDATA%` and `G:` are **orphaned predecessors** — see "The live archive.db lives on H:" above. Do not treat them as the archive.
- `vod-rip.spec` skips `cu13`/`*-cu13` nvidia packages (stack pinned to cu12) — keep it; a cu13 tree adds ~850MB to every bundle.
- Pagefile: `H:\pagefile.sys` 16GB fixed (same NVMe as C: so it mounts at boot).


## Worktrees: the root is `I:`, and every move is ledgered

**`scripts/worktree-roots.tsv` is the source of truth for this project. Read it
before touching anything worktree-shaped.** It is repo-scoped on purpose; the
shared portfolio manifest at `I:\!manager\worktree-roots.tsv` is another repo's
file and is left untouched.

**THE ROOT IS `I:/vod-rip-wt`, NOT `G:/vod-rip-wt`.** I got this wrong by
reading the shared manifest and not measuring, and it is worth recording why
the shared row is wrong for us:

- **`git worktree move` is SAME-DRIVE-ONLY.** Every registered VOD.RIP worktree
  is on `I:`. A root on `G:` would not make the migration slow, it would make
  it **IMPOSSIBLE** — git refuses a cross-drive move outright.
- **Free space, measured 2026-10-05:** C: 13.1 GB · G: 7.4 GB · H: 102.1 GB ·
  I: 49.0 GB. `G:` does not have room for the tree set plus growth.
- `archive/` is a SIBLING of `wip`/`unsure`/`done` under the same root, so it
  is on the same drive for free and a later move into `archive/` works.

**Reorganising worktrees reclaims ZERO bytes.** It is a reorganisation, not a
storage move. Measured: ~1.29 GiB across the worktrees, and it stays exactly
where it was. Do not present a migration as freeing space.

**Every move is ledgered, and every move is reversible.**
`docs/wt-migration-ledger.tsv` has one row per move: `lane, original_path,
state, new_path, sha, moved_utc`. Undo one with `scripts/wt-restore.sh --lane
<lane>`, undo all with `scripts/wt-restore.sh --all`. **Nothing in that ledger
is a deletion record.**

Measured 2026-10-05 from `git worktree list --porcelain`: **60 registered
worktrees = 1 primary + 47 under the root + 12 strays still in `I:/TEMP`.**
Before that pass it was 1 + 37 + 22; 10 junction-free lanes were migrated and
the total stayed at 60, which is the number that proves none was lost.
Per state: `wip` 6, `unsure` 9, `done` 30, `archive` 2.

- Each lane's state was **measured, not assumed**: `wip` = the branch has
  commits `main` lacks, `unsure` = merged-or-not but the tree is dirty,
  `done` = merged AND clean, `archive` = abandoned with a recorded reason.
- The **12 junctioned lanes are refused on purpose.** `wt-migrate.sh` exits 3
  and NO-OPs on them. `git worktree remove` destroyed a dependency tree in this
  project through exactly such a junction, so a junctioned tree is not moved
  either. They stay in `I:/TEMP`. Retiring them is the owner's call, not a
  script's.

### Never merge one of these branches wholesale

Six parked lanes hold commits `main` lacks, and the three-dot diff makes them
look valuable: `agent/ejs-remote-solver` (17 commits),
`agent/shorts-crashfix-review` (11), `agent/obsv` (9),
`agent/audio-only-both-sites` (3), `agent/clip-editor` (1),
`agent/flake-hunt` (1).

**They are STALE, and merging one would gut `main`.** The two-dot diff
(`git diff main <branch>`) is 77-157 files and the direction is overwhelmingly
DELETION of what `main` already has — `live_upcoming_backoff.py -205`,
`archive_db.py 881`, `archive_transcribe.py 675`,
`scripts/lib/worktree-roots.sh -372`, `docs/wt-migration-ledger.tsv -49`,
`AGENTS.md -181`. Their merge bases are old tips (8d4fe23c, 53643d57,
5848b5df, 27e9da74, bd346e6d, de3774e0).

Take the useful fragment **file by file** — cherry-pick the commit or port the
hunk. Never the branch. And measure "is this already in main" as
`git diff main <branch>`: an empty diff means the content IS in main and only
the history differs.

**Create lanes only through the chokepoint**, never `git worktree add <path>`
by hand:

```bash
bash scripts/wt-new.sh --project vod-rip --lane <name> --state wip|unsure|done|archive
```

Every refusal in that script is load-bearing. Each one exists because the
wrong thing was MEASURED to succeed, and `git` has no concept of a correct
worktree location, so the wrapper is the only choke point. A deleted guard is
a permanent hole, not a cleanup. `--selftest` is read-only outside its own
fixture and is the thing to run after touching it.

### An empty commit is not "committing early"

Measured: `git rev-list origin/main..HEAD` carried **four empty commits**, each
repeating the previous commit's message verbatim. That is a lane's
commit-early reflex firing on an already-clean tree — `git commit -F` with
nothing staged still creates a commit.

It is harmless, and it was deliberately **not** cleaned by rewriting shared
history: squashing `main` under other lanes that pull from it is destructive
for a cosmetic gain. The real cost is that a duplicate message reads as two
independent pieces of work when it is one.

<!-- STEADY-WATCHER -->
## Steady Watcher (local governor — OMP must read this)

This machine runs **Steady Watcher** (`python -m watcher` in `I:\!watcher`) so VOD.RIP, consultgpt, superharness, BrandOps, and any other heavy work can run **together**, including while the user plays League of Legends or Marvel Rivals.

Live status (always JSON 200): `http://127.0.0.1:47891/status`
Same payload on disk: `I:\!watcher\status\now.json`

- Heavy jobs are **slow and steady**, not stopped. Treat slowness as the governor, not a hang.
- If you must wait, `GET http://127.0.0.1:47891/wait?max_ms=250000` (pulses every 4:10, under 4:40) or `python -m watcher wait`.
- Do not kill/retry GPU or Chromium workers to "unstick" them — that cold-starts CUDA/Playwright.
- Skill: `.omp/skills/steady-watcher/SKILL.md`
<!-- /STEADY-WATCHER -->

## yt-dlp executes a REMOTE challenge solver (owner-authorised, 2026-10-05)

**This is the one place VOD.RIP downloads and executes code from the internet.** It is
enabled by a named env var, it is off by default in the code, and turning it back off is
one variable. Read this before touching `ytdlp_guard.sanitize_ytdlp_opts`.

### What it does and why it exists

YouTube hides its adaptive (audio-only) streams behind an **n-challenge** that can only
be answered by *executing JavaScript*. The installed yt-dlp (`2026.08.19`) vendors only
the `core` half of its solver — the `lib` half is **absent from the package** — so with
no remote component the challenge cannot be solved, **every audio-only itag (140/251)
drops out of the format list**, and `archive_ytdlp._AUDIO_ONLY_FORMAT_SPEC`
(`bestaudio/best[vcodec=none][acodec!=none]`) has nothing to match. The lane then
requeues, and before the `ce22f9e`/`f272355` guards it fell through to muxed itag 18 and
handed h264 video to a speech recogniser.

The option name, value, and default are read from the **installed package**
(`yt_dlp/options.py` `--remote-components`, `globals.py:supported_remote_components`),
not from memory: dest `remote_components`, values `ejs:github` / `ejs:npm`, **default
empty — no remote component is allowed by default.**

### The security consequence, stated plainly

Enabling this makes yt-dlp **download JavaScript from a remote source and execute it**,
in a local JS runtime (deno/node), while solving the challenge. That is remote code
execution by design, and enabling it is a **permanent trust grant in the config** — it
does not expire and it is not sandboxed beyond the runtime's own permissions.

The source is **`github.com/yt-dlp/ejs` release assets**. Two bounds make it narrower
than "run whatever GitHub serves": the URL is pinned to a version tag that yt-dlp itself
carries (`jsc/_builtin/vendor/_info.py:VERSION`), and the downloaded script is verified
against a **sha3-512 hash vendored inside the installed yt-dlp**, which yt-dlp refuses on
mismatch. So a tampered asset is rejected — but the trust is in the installed package's
manifest, and the package is part of the trust boundary.

### PROVEN WORKING against a real YouTube URL (2026-10-05)

Not "the knob is set" — an actual extract of `Vja1Z1eoQrM` (a real id from
the LIVE archive) through the app's own `guarded_youtube_dl` seam, with the
grant ON:

- 33 formats returned, **6 audio-only**, itags `599 600 249 250 140 251`
- the app's own selector `bestaudio/best[vcodec=none][acodec!=none]` chose
  **itag 251, `vcodec=none`, `acodec=opus`, 139.2 kbps** — no video
- pinned solver v0.8.0, `lib.min.js` sha3-512 `8420c259ad16e99c...`

Before: **0 of 10** real extracts offered any audio-only itag. The gap is
closed. A googlevideo URL carries the po_token, so a probe must print only
format_id / vcodec / acodec / tbr — never a URL.

### Why setting the knob to 0 is a REAL revert, not just "stop fetching"

`remote_components=[]` alone stops the FETCH and not the USE. From the
installed yt-dlp 2026.08.19, `_builtin/ejs.py`:

- `_iter_script_sources` (:259) yields PYPACKAGE -> CACHE -> BUILTIN -> WEB,
  so a cached entry outranks the vendored one;
- `_cached_source` (:277) reads the cache and NEVER consults
  `remote_components` — only `_web_release_source` (:291) checks it.

So a box that solved once would keep EXECUTING the cached script with the
knob at 0, and "I set it to 0" would be a false claim. That is why
`sanitize_ytdlp_opts` also PURGES the cache when disabled.

Measured end-to-end, not assumed: a real solve wrote
`<cachedir>/challenge-solver/lib.json` (157,701 bytes) and the disabled
path removed it. The path is right because `yt_dlp/cache.py::_get_root_dir`
returns the `cachedir` param UNCHANGED when the app sets it
(`ytdlp_hls.py:496`, `ytdlp_download.py:972`) and only appends
`yt-dlp` on the unset fallback. The purge covers BOTH `core.json` and
`lib.json`; in practice only `lib.json` is written, because the CORE half
is vendored locally so `_builtin_source` satisfies it first. The pair is
defensive against a yt-dlp that changes which half it fetches, and
`test_remote_challenge_solver.py` pins the list against the installed
yt-dlp's own `ScriptType` values.
### The knob

```
VODRIP_YT_EXECUTE_REMOTE_CHALLENGE_SOLVER=1
```

- The name is deliberately verbose: it says *execute*, so nobody enables it by accident.
- `1/true/yes/on` allows it. **`0/false/no/off` — or leaving it unset — forbids it.**
- Only `ejs:github` is granted. `ejs:npm` would pull npm packages at solve time (a
  second remote source) and is **not** enabled.

### ONE documented way to turn it off

```powershell
# set this to 0 (or delete it) and restart the app
$env:VODRIP_YT_EXECUTE_REMOTE_CHALLENGE_SOLVER='0'
```

This restores the **previous behaviour exactly**: no remote fetch, the n-challenge stays
unsolved, audio-only formats stay absent, and the transcription lane requeues instead of
downloading a video stream. It is a real revert, not a soft disable — `sanitize_ytdlp_opts`
*overwrites* `remote_components` with `[]` when off, so a caller cannot smuggle the grant
back in.

### Where it lives, and the audit trail

The grant is decided in **`services/ytdlp_guard.sanitize_ytdlp_opts`** — the function that
already decides `fetch_pot` — so it rides the single guarded egress seam
(`guarded_youtube_dl`) and cannot be bypassed by a caller. Do **not** move it into a
module that constructs `YoutubeDL` directly.

On startup, once per process, the module logs the resolved state: the component, the
pinned version, and the first 16 hex of the expected hash — that log line is the answer
to "did this box execute remote JS, and which bytes?".

`backend/tests/test_remote_challenge_solver.py` fails if the grant is ever dropped from
the effective opts at the seam. It asserts the **opts handed to a real `YoutubeDL`**,
not a constant, because the failure mode is a refactor silently un-solving the challenge.

## How To: Split-Runtime Release (FUTURE releases)

> **This is the release process for FUTURE releases — it is a documented target, NOT something to implement in this task.** No code, installer, or build changes are made to realize it here. Use the steps below when publishing the next release.

### Model

Ship the frozen app as a **small CPU-only base** plus a **separate, versioned GPU-ASR runtime archive** that the app installs/downloads on demand (optional, per the user's hardware).

- **Base installer** — small CPU build (no bundled NVIDIA stack): the web UI, orchestration, and CPU ASR paths (parakeet/sherpa-onnx CPU wheel) with NO GPU runtime/DLLs.
- **GPU-ASR runtime archive** — versioned `.7z`/`.zip` containing only the GPU runtime: the `sherpa-onnx==1.13.4+cuda12.cudnn9` CUDA wheel (bundled CUDA-enabled onnxruntime), the `nvidia-*cu12` DLLs from `backend/requirements-gpu.txt`, and the parakeet model — NOT pip-installed at runtime. Pinned to the app build.
- **Inno Setup** — optional component: a downloader/installer entry that fetches and extracts the GPU-ASR runtime in-app when the user opts in; absent from the base, never bundled.
- **CPU ASR stays** — the base is "CPU-only" w.r.t. NVIDIA/GPU only; it still ships CPU parakeet (sherpa-onnx CPU wheel). Do NOT claim "no ASR on CPU."

#### Current primitives in-tree (reuse these)
- ASR engine: **Parakeet Redux** (sherpa-onnx `nemo_transducer`; `Codyfederer/sherpa-onnx-nemo-parakeet-redux`, `archive_transcribe.py:1645`) — the ONLY engine; faster-whisper was removed (`backend/services/archive_transcribe.py`, `backend/requirements.txt`). **Swapped in 2026-10-04:** Redux is the 1.58-bit ternary re-quantisation of NVIDIA's `parakeet-tdt-0.6b-v3` — same architecture, same tokenizer, same 25 European languages, **397 MB on disk (encoder.onnx 327.9 + decoder 45.1 + joiner 24.1), and it is 4-bit, not ternary** — re-measured 2026-10-15 against the file: 193 `MatMulNBits` nodes, every one `bits=4/block_size=128`, 324.9 MB of initializers in a 327.9 MB encoder. The upstream tarball is 161 MB compressed and unpacks to exactly 397 MB, so this copy IS the published artifact. The "ternary / ~170 MB" figure that used to sit here belongs to the OPT-IN Photon accelerator, whose weights are `moondream/parakeet-redux` (`model.safetensors` 169.5 MB + tokenizer + `ternary.json`) — same base model, different engine. It beats the int8 original on the 25-language FLEURS aggregate (WER 10.56 vs 11.62) and on long-form TEDLIUM (2.51 vs 2.71), is slightly worse on English (6.55 vs 6.26), and is notably worse in background noise (9.04 vs 6.72) — the real cost of the swap on noisy Twitch/Kick VODs, accepted for the size and RSS win. Redis weights live inside `encoder.onnx` as `MatMulNBits` 4-bit blocks (no separate `.int8.onnx` file — that name means the model never resolves and every ASR job fails as "no model"). **The int8 sibling was REMOVED on 2026-10-15.** It was `H:\VOD.RIP-models\parakeet-models\sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8` (639 MB), and it was the same base model (`parakeet-tdt-0.6b-v3`) in a second quantisation — not a runtime choice, only a revert path held by a constant, so it cost 639 MB to make one line change cheaper. `moondream/parakeet-redux` (170 MB, ternary) was re-fetched into the durable path `H:\VOD.RIP-models\photon-hf` at the same time, so the Photon accelerator still has its weights; `photon_asr.hf_home()` now answers that path directly. The status fields that reported the int8 (`int8_fallback_*`) are gone with it — a status surface describing a file that no longer exists is a lie, and this repo already has a test for that class (`test_env_knob_truthfulness`). The Photon VENV (5.3 GB, its own CUDA torch) is still NOT installed on this box, so Photon is unavailable until someone runs its installer.
- CPU: `sherpa-onnx>=1.13.0` (base). GPU: `backend/requirements-gpu.txt` → `sherpa-onnx==1.13.4+cuda12.cudnn9` + `nvidia-{cublas,cuda-runtime,cufft,curand,cudnn}-cu12`; `archive_transcribe._ensure_cuda_libs` exposes the DLL dirs.
- Existing release scripts: `scripts/sign-release.ps1` (Authenticode) and `scripts/build-install.ps1` (build+install to H:). A runtime-archive builder and a sha256-hashing step do NOT exist yet — entries below marked `[FUTURE]` are proposed, not yet written.

### Security / integrity invariants (non-negotiable)

1. **HTTPS only** — every download (base, runtime, Inno Setup component) is fetched over HTTPS, never plain HTTP.
2. **SHA-256 verification** — every artifact ships a signed `.sha256` manifest; the app verifies the download digest against the manifest before extraction/execution. Refuse on mismatch.
3. **Per-user writable runtime location** — the GPU-ASR runtime installs to a per-user writable path (e.g. `%LOCALAPPDATA%\VOD.RIP\runtime\<asr-version>\`), never `C:\Program Files`, so in-app install works without elevation.
4. **No `pip install` from the frozen app** — the frozen app MUST NOT shell out to `pip`/`uv` to install the runtime; it downloads the versioned runtime archive and extracts it, matching its own pinned ABI. (The runtime archive is built once, offline, during release.)
5. **Version pinning** — app build and runtime archive share one release version; the app requests exactly that version and validates it in the manifest.

### Release steps (concrete)

```bash
# 1. Build CPU base — no bundled NVIDIA stack, but CPU parakeet (sherpa-onnx CPU wheel) stays
pyinstaller vod-rip.spec            # CPU sherpa-onnx; NO +cuda wheel, NO nvidia-*cu12
# 2. [FUTURE] Build the GPU-ASR runtime archive from the SAME commit's pinned deps
scripts/build-gpu-runtime.ps1       # proposed: offline, install backend/requirements-gpu.txt wheels to a
                                    #   temp venv, bundle parakeet model + CUDA DLLs -> .7z
# 3. [FUTURE] Generate a SHA-256 manifest per artifact; sign with the existing Authenticode key
scripts/hash-artifacts.ps1          # proposed: writes *.sha256; reuse scripts/sign-release.ps1 cert/timestamp
# 4. Upload base + runtime archive + manifest to the HTTPS release endpoint
# 5. Inno Setup [FUTURE]: add the runtime as an optional component
#    (download + verify SHA-256 + extract to %LOCALAPPDATA%\VOD.RIP\runtime\<version>\)
```

### Release verification (runs BEFORE shipping)

- Fetch every artifact **over HTTPS** and assert `sha256 -c <artifact>.sha256` passes.
- From a **clean frozen CPU install** (no runtime present): confirm the base runs end-to-end with CPU parakeet (sherpa-onnx CPU wheel).
- Install the GPU-ASR runtime via the Inno Setup optional component; confirm it verifies the digest, extracts only to the per-user writable path, and enables GPU parakeet (sherpa-onnx +cuda) with no `pip` involved.
- Negative tests: tampered archive → manifest mismatch → install refused; missing runtime → base still works on CPU parakeet, no crash.








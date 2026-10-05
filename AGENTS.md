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

## Browser Extensions

- Kick Overlay (Twitch ad-kill): `vendor/kick-overlay/`
- Cookies (VOD.RIP cookie/po_token): `vendor/cookie-extension/src/` — installed from the `VOD.RIP-cookies` subfolder (staging: `%APPDATA%\VOD.RIP\cookie-extension\VOD.RIP-cookies\`)


## Runtime logs

- Application/server error log: `%APPDATA%\VOD.RIP\logs\errors.jsonl` (override the data root with `VODRIP_APP_DATA`).
- The error log retains the latest 500 sanitized error records; the running API exposes them at `GET /api/errors/latest?limit=500`.
- Dev supervisor stdout/stderr logs: `tmp\vodrip-devall-api.log` and `tmp\vodrip-devall-web.log`.

## The live archive.db lives on H: (learned 2026-10-04)

**There are three `archive.db` files on this machine and the app opens exactly one: `H:\VOD.RIP-data\archive.db`.** The other two are orphaned predecessors. Every count read from either of them is wrong.

| path | size | last write | status |
|---|---|---|---|
| `H:\VOD.RIP-data\archive.db` | 7.5 GB | written continuously | **LIVE — the one the app opens** |
| `G:\VOD.RIP-data\archive.db` | 486 MB | 2026-09-17 | orphan (migrated away) |
| `%APPDATA%\VOD.RIP\archive.db` | 139 MB | 2026-09-16 | orphan (migrated away) |

This has already caused three wrong reports. An orchestrator read the `%APPDATA%` orphan, found no `rate_limit_events` table, and reported twice — with a formatted table — that rate history was inert in production. It is not: the live archive has that table with 33 rows. A backlog plan was then built on the orphan (1,161 videos / 3,723.6 hours). The live archive holds 12,715 videos / 27,631 hours / 1,216 videos with transcripts. A third agent re-read the orphan and repeated the first error. (Measured read-only on 2026-10-04. The live DB keeps growing — re-measure, do not reuse these totals.)

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
| `VODRIP_NO_DAEMONS` | `app.py:128` | `1` = no background daemons (debug) |
| `VODRIP_TAKE_PORT` | `server_lifecycle.py:456` | take a busy API port instead of failing |
| `VODRIP_SKIP_PORT_RELEASE` | `run.py:85` | skip the pre-bind port release |
| `VODRIP_ALLOW_PIP_INSTALL` | `run.py:103` | allow the dev path to shell out to pip |

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

### The orphans

Both predecessors are still on disk and still mislead readers. **Deleting them is the owner's decision, not a worker's** — do not clear them as part of "cleanup" or disk-pressure work. Report their existence; let the owner choose.

**The two are NOT equally safe to delete — check before recommending either.** Comparing `(platform, video_id)` sets against the live archive on 2026-10-05:

| orphan | videos | rows absent from the live archive | verdict |
|---|---|---|---|
| `%APPDATA%\VOD.RIP\archive.db` | 1,161 | **0** | a strict subset — nothing unique is lost |
| `G:\VOD.RIP-data\archive.db` | 2,309 | **244** | holds 244 videos the live archive does not — deleting it destroys rows unless they are merged first |

The live archive keeps growing, so the 244 is a floor, not a fixed number. Re-measure before acting on either row, and re-derive it the same way: open the live archive and each orphan with `file:<path>?mode=ro` (read-only URI — never a plain `connect()`, and never a `mode=rw` write), read `(platform, video_id)` from `videos` in each, and set-difference. Do not run a merge on the owner's initiative either — that writes to the live database.

**A fresh `-shm` mtime is the "someone just read the wrong file" signal.** A WAL database's `-shm` is touched when a connection opens it, so it moves even for a read-only open, while `archive.db` and `-wal` stay put. Measured 2026-10-05 07:11: `G:\VOD.RIP-data\archive.db-shm` had been written 06:39 that morning while its `archive.db` and `-wal` were frozen at 2026-09-17 — a process had the **wrong archive** open half an hour earlier. The trap is not historical. Check that mtime before recommending a delete, and read a fresh one as an active reader rather than a stale file.

**It recurs, and it is not this repo.** The same `-shm` moved again to 07:17 with `archive.db` and `-wal` still frozen at 2026-09-17, so the reader is periodic rather than a one-off. No committed code path opens that file: the only `G:\VOD.RIP-data` literal in the tree is `backend/tests/test_data_dir_pin_cache.py:39`, a stub path the test compares and never opens. No running process holds the path in its command line either, so the reader is short-lived and lives outside the repo. Attribute it with handle-level tooling before deleting — do not re-grep this tree, it has already been ruled out.

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
- ASR engine: **Parakeet Redux** (sherpa-onnx `nemo_transducer`; `Codyfederer/sherpa-onnx-nemo-parakeet-redux`, `archive_transcribe.py:1645`) — the ONLY engine; faster-whisper was removed (`backend/services/archive_transcribe.py`, `backend/requirements.txt`). **Swapped in 2026-10-04:** Redux is the 1.58-bit ternary re-quantisation of NVIDIA's `parakeet-tdt-0.6b-v3` — same architecture, same tokenizer, same 25 European languages, ~178 MB of weights instead of ~1.2 GB. It beats the int8 original on the 25-language FLEURS aggregate (WER 10.56 vs 11.62) and on long-form TEDLIUM (2.51 vs 2.71), is slightly worse on English (6.55 vs 6.26), and is notably worse in background noise (9.04 vs 6.72) — the real cost of the swap on noisy Twitch/Kick VODs, accepted for the size and RSS win. Redis weights live inside `encoder.onnx` as `MatMulNBits` 4-bit blocks (no separate `.int8.onnx` file — that name means the model never resolves and every ASR job fails as "no model"). **The int8 model is deliberately KEPT on disk** (`H:\VOD.RIP-models\parakeet-models\sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8`, ~639 MB) so a revert is a constant change rather than a re-download — **do not delete it until the Photon path is proven in the field.**
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








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

## Heavy project data lives OFF C: (learned 2026-08-15)

**C: is the system NVMe — never put heavy project artifacts there.** It has ~15GB free and the repo bloated to 18.7GB on C: (9GB `dist` + 9GB `_internal` + `build` + a stray CUDA-13 stack). Disk map:

- **G:** (NVMe) — models `G:\VOD.RIP-models`, data `G:\VOD.RIP-data`, benchmarks `G:\vodrip-bench`, downloads
- **H:** (NVMe) — frozen bundle installs `H:\VOD.RIP-build\dist\VOD-RIP` (build-install.ps1 default)
- **I:** (HDD, 4TB) — bulk/long-term storage

Rules:

- Build outputs (`dist\`, `_internal\`, `build\`) are gitignored and regenerable — **delete them from the repo after `scripts/build-install.ps1` installs to H:**; do not leave ~18GB of build trees on C:.
- Model caches, ASR scratch, benchmark audio, VOD archives, DBs → **G:** stable roots (NOT `G:\Temp` — pytest's session-end wipe deletes `vodrip-*` there; `tempfile.gettempdir()` = `G:\Temp` on this box).
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

## How To: Split-Runtime Release (FUTURE releases)

> **This is the release process for FUTURE releases — it is a documented target, NOT something to implement in this task.** No code, installer, or build changes are made to realize it here. Use the steps below when publishing the next release.

### Model

Ship the frozen app as a **small CPU-only base** plus a **separate, versioned GPU-ASR runtime archive** that the app installs/downloads on demand (optional, per the user's hardware).

- **Base installer** — small CPU build (no bundled NVIDIA stack): the web UI, orchestration, and CPU ASR paths (parakeet/sherpa-onnx CPU wheel) with NO GPU runtime/DLLs.
- **GPU-ASR runtime archive** — versioned `.7z`/`.zip` containing only the GPU runtime: the `sherpa-onnx==1.13.4+cuda12.cudnn9` CUDA wheel (bundled CUDA-enabled onnxruntime), the `nvidia-*cu12` DLLs from `backend/requirements-gpu.txt`, and the parakeet model — NOT pip-installed at runtime. Pinned to the app build.
- **Inno Setup** — optional component: a downloader/installer entry that fetches and extracts the GPU-ASR runtime in-app when the user opts in; absent from the base, never bundled.
- **CPU ASR stays** — the base is "CPU-only" w.r.t. NVIDIA/GPU only; it still ships CPU parakeet (sherpa-onnx CPU wheel). Do NOT claim "no ASR on CPU."

#### Current primitives in-tree (reuse these)
- ASR engine: **Parakeet** (sherpa-onnx `nemo_transducer`, TDT v3, `int8`) — the ONLY engine; faster-whisper was removed (`backend/services/archive_transcribe.py`, `backend/requirements.txt`).
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








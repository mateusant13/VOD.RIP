# P4 · VOD.RIP · project status

**Slug:** `vodrip-status` · **Written:** 2026-10-08 (local, UTC-3) · **Seat:** recon (read-only)
**Project:** `C:\Users\Administrador\Desktop\Nova pasta (3)\TESTE\VOD.RIP`

This file is a status report, not a change. Nothing was committed, staged, checked out,
or reset. The only file written is this one. Every figure below was measured this turn
unless it is explicitly attributed to an earlier lane and marked as such.

---

## 1. Current state of the project

### 1.1 The branch is stalled, not dead

| fact | value | how measured |
|---|---|---|
| HEAD | `6379ea2` "feat(preview): offer the Subtitles tab for Twitch/Kick previews" | `git rev-parse --short HEAD` |
| HEAD date | **2026-10-06 09:19:42 -0300** | `git log -3 --format='%h %ci %s'` |
| today | 2026-10-08 | this turn |
| last commit → now | **~2 days, 11 h with no commit** | arithmetic |

So `main` has not moved in ~2.5 days. That is the stall. It is not idleness: a large,
finished-looking body of work is sitting **uncommitted in the working tree** (§1.2), and
the lane reports that describe it are dated 2026-10-06 with P4 action specs dated
2026-10-08.

### 1.2 The working tree carries the real work (uncommitted)

`git status --short`, measured this turn:

**13 modified tracked files**

| file | Δ |
|---|---|
| `.steady-watcher.json` | machine-local governor config (212 lines churned) |
| `backend/conftest.py` | +86 |
| `backend/routers/subtitles.py` | +79/-… (ASR sidecar wired here — §2.2) |
| `backend/services/archive_db.py` | +223/-… |
| `backend/services/archive_scheduler.py` | +60 |
| `backend/services/archive_ytdlp.py` | +37 |
| `backend/services/instant_preview.py` | +26 |
| `backend/services/rate_budget.py` | +142 |
| `backend/tests/test_instant_preview.py` | +12 |
| `backend/tests/test_scratch_db_teardown.py` | +93 |
| `scripts/observability/tests/test_verify_gate.py` | +45 |
| `scripts/observability/verify_gate.py` | +22 |
| `src/previewRetry.ts` | +198 |

(`git diff --stat` reported by the land-preview lane: **13 files, +1023/−212**.)

**Untracked product/source/test files**

- `backend/services/archive_journal.py`
- `backend/services/preview_asr_sidecar.py`
- `backend/services/preview_asr_worker.py`
- `backend/services/probe_gate.py`
- `backend/tests/test_background_remediation.py`
- `backend/tests/test_preview_asr_sidecar_e2e.py`
- `backend/tests/test_preview_asr_worker.py`
- `src/previewRetryCeilings.test.ts`
- `runs/` (the lane reports + the three P4 specs + this file)
- `control/` (optchat continuity view — not product code)

**Untracked scratch / machine-local (must NOT be committed):** `.steady-watcher.json`,
`tmp_*.txt`, `git_*.txt`, `backend/co_list.txt`, `backend/lc.txt`, `backend/win.txt`,
`backend/repro_out.txt`, `backend/diff_probe.py`, `backend/tmp_probe_ping.py`,
`backend/tests/_chat_history_scratch.db-shm/-wal`, `control/`.

### 1.3 Most recent activity (08/10), and the API is dark

- The newest lane measurements are from **2026-10-08 ~19:06–19:13** (`tmp_lane_*.txt`):
  - `tmp_lane_pytest_asr.txt` → **29 passed**, 1 warning (preview ASR worker tests)
  - `tmp_lane_pytest_remediation.txt` → **11 passed**, 1 warning (background remediation tests)
  - `tmp_lane_vitest_iso.txt` → **74 passed (3 files)** under vitest v4.1.11
  - `tmp_lane_tsc.txt` → **0 bytes**; the exit code was not preserved, so this report
    does **not** claim "tsc clean" from it (the standing rule: an empty redirected file is
    not evidence of exit 0).
- **The live API is down.** Measured this turn: `Get-NetTCPConnection -State Listen`
  filtered to `127.0.0.1:7897` (and the whole 7800–7900 range) → **no listener**. This is
  the same state Lane A found on 2026-10-06 and the concurrent-preview probe spec re-measured
  on 2026-10-08.
- `node_modules` population this turn: **10,848 files** (well above the 500 floor), so
  `tsc`/`vitest` are trustworthy here — the fake-"tsc clean" trap (empty `node_modules`)
  is not the failure mode now.
- The repo gate exists at `scripts/verify.ps1` (6,083 bytes, 2026-10-05).

---

## 2. Pending work / TODOs found

Three independent backlogs coexist in the tree. They are not duplicates; they are
different layers of the same project.

### 2.1 `todo.md` — the feature backlog (dated 2026-08/10)

A ~33 KB backlog: the "Archive search v2" pipeline spec plus **9 user workstreams**
(WS-1 preview-queue priority … WS-9 240Hz UI optimization), each already decomposed into
worktree-ready tasks with acceptance criteria, plus a recorded WS-9 websearch findings
section. These are *product features*, none of which appear started in the current diff.
This is the long tail, not the stall.

### 2.2 Four lane slices (2026-10-06) — built, two of them **not wired**

The `runs/SA-vodrip-lane{A,B,C,D}-*.md` reports describe completed slices with their own
test evidence. Status re-verified this turn against the current tree:

| lane | slice | state now |
|---|---|---|
| **A** latency scout | read-only | Report only. All latency figures **UNMEASURED** (API down). Identified 3 shared serialisation points. |
| **B** preview ASR | Twitch/Kick real-time captions | Code + tests present. **The router integration now exists**: `backend/routers/subtitles.py:593` imports `services.preview_asr_sidecar` and `:606` calls `transcribe_head(...)`. So Lane B §7's "router integration unwritten" is *no longer* accurate — the wiring is in the (uncommitted) tree. **Never exercised live.** |
| **C** preview timeout law | silence vs overrun verdict | `src/previewRetry.ts` **exports** `classifyPreviewDeadline`, `previewDeadlineVerdict`, `PREVIEW_FIRST_OUTPUT_CEILING_MS`, `PREVIEW_STAGE_TOTAL_CEILING_MS` (`previewRetry.ts:84,99,129,231`). **Nothing in the app calls them** — re-verified this turn: the only caller is `src/previewRetryCeilings.test.ts`. `App.tsx` still hard-codes `t('Preview took too long — try again')` at `:1777`, `:2002`, `:2027` and still derives the stage from `err.name === 'AbortError'` at `:2026`. **The wiring is still unapplied.** |
| **D** crash scout | 3rd `Maximum update depth exceeded` cycle | Read-only. Verdict: **no unbounded third cycle found**; two RISK sites on the frame-snap seam, one refactor away from a crash. |

### 2.3 Three P4 action specs (2026-10-08) — written, none executed

`runs/P4-vodrip-*.md` are read-only specs, each a one-paragraph action with acceptance:

1. **`land-preview-wip`** — run the gate as three redirected commands with real
   `$LASTEXITCODE`, classify the dirty/untracked paths by owning lane, and have the **parent**
   commit the preview slice in coherent groups (sidecar+worker+tests / `previewRetry.ts`+ceilings
   test / backend `archive_db`/`scheduler`/`ytdlp`/`rate_budget`+tests). Explicitly exclude the
   scratch/machine-local files listed in §1.2.
2. **`wire-deadline-verdict`** — apply Lane C §2.3 (a)–(d): split `PREVIEW_START_TIMEOUT_MS`
   (`src/previewStartTimeout.ts:19`) into the two stage ceilings; widen the `onTimeout` callback
   to report which ceiling fired; replace the four hard-coded error sites with
   `previewDeadlineVerdict(...)`; delete the `AbortError` string match. Acceptance: `tsc` exit 0,
   `vitest` exit 0, `src/previewRetry.test.ts` untouched as the independent control, and two
   distinct messages for the owner's two symptoms.
3. **`concurrent-preview-probe`** — add a `--concurrent-platforms` mode to
   `scripts/observability/probe.py` that fires one preview per platform from a single barrier and
   reports the **worst-of-three** time-to-play against the owner's 5,000 ms budget, plus
   caption-present per platform and separate `attempted`/`arrived` counts. Requires the app to be
   running (API is down today).

### 2.4 `STATE.md` — the measured-truth file (2026-10-05)

An observability baseline with a "FACTS THAT WERE WRONG, do not reintroduce" section (six
retracted numbers, including "`/api/health` returned 200 ⇒ healthy"). §5 (backend suite) is a
stub that says "see §5" but never fills it in — a small dangling TODO in that file.

---

## 3. Why the project looks stalled

The stall is **not** a missing decision or a broken build. It is structural:

1. **No commits for ~2.5 days** while ~1,000 lines of finished, tested work sit uncommitted.
   Every additional lane that edits these shared files (`App.tsx` is ~380 KB, `archive_db.py`,
   `rate_budget.py`) compounds merge risk.
2. **Two slices are half-landed:** Lane C's mechanism is built and tested but nothing calls it,
   so the owner's two distinct preview symptoms still collapse into one message; Lane B's ASR
   sidecar is now wired into the router but has never run against a live API.
3. **The instrument that would prove the owner's actual test is dark:** the API has no listener
   on `:7897`, so "3 channels at once, ≤5 s, captions on all three" remains **UNMEASURED** — a
   belief, not a number. Three shared serialisation points (global `_YTDLP_LOCK` in
   `backend/services/ytdlp_guard.py`, the per-platform `rate_budget` ceiling, and the ASR sidecar
   process) could turn "3 at once" into "one, three times slower", and nothing is watching.
4. **The owner is absent**, so the only decisions left (which slice to commit first, when to
   start the app for a live probe) have no human in the loop.

---

## 4. Recommended next steps (ordered, smallest-safe-first)

1. **Land the uncommitted preview slice** (`P4-vodrip-land-preview-wip.md`). This is the
   cheapest way to unstall: run `npx tsc --noEmit`, `npx vitest run`, and `python -m pytest`
   from `backend/` as **three separate commands redirected to files, reading `$LASTEXITCODE`
   afterwards** (never piped). If green, the parent commits in coherent groups; scratch/machine
   files stay untracked. Owner of commits: parent (not this lane).
2. **Wire the deadline verdict** (`P4-vodrip-wire-deadline-verdict.md`). Smallest high-value
   frontend diff: the hard part is done and tested; what remains is the four named call sites.
   Acceptance is `tsc`/`vitest` exit 0 with `src/previewRetry.test.ts` untouched.
3. **Add and run the concurrent preview probe** (`P4-vodrip-concurrent-preview-probe.md`) —
   but only once the app is started. Until then, record the owner's headline as `unmeasured`
   with the reason (API down), never as zero.
4. **Fill `STATE.md` §5** with the real pytest summary line from the next gate run.
5. **Treat `todo.md` WS-1…WS-9 as the post-stall backlog**, not as blockers; they are unstarted
   product features and should wait until the preview slice is committed and measured.

---

## 5. Boundary / what this report did NOT do

- No commit, no `git add`, no `git checkout/reset`, no push.
- No file written except this one.
- Did not start/restart/kill the app or MiniMax Code; did not kill GPU/Chromium workers.
- Did **not** run `tsc`/`vitest`/`pytest` this turn; the gate results in §1.3 are attributed to
  the 2026-10-08 lane runs, and `tsc` is explicitly **not** claimed clean from a 0-byte file.
- Did not modify the archive DB.

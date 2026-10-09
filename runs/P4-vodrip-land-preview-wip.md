# P4 · VOD.RIP · land the uncommitted preview slice behind the gate

**Slug:** `land-preview-wip` · **Written:** 2026-10-08 · **Owner:** parent orchestrator (commits)
**Seat:** recon (read-only) — this file is a spec, not a change.

## Why this is the top action

`main` is at `6379ea2` ("feat(preview): offer the Subtitles tab for Twitch/Kick previews"),
committed **2026-10-06 09:19**. Today is **2026-10-08**: the branch has not moved in ~2 days
while the working tree carries a large, finished-but-uncommitted body of work. `git status
--porcelain` (measured 2026-10-08) shows **13 modified files** — `backend/conftest.py`,
`backend/routers/subtitles.py`, `backend/services/archive_db.py`,
`backend/services/archive_scheduler.py`, `backend/services/archive_ytdlp.py`,
`backend/services/instant_preview.py`, `backend/services/rate_budget.py`,
`backend/tests/test_instant_preview.py`, `backend/tests/test_scratch_db_teardown.py`,
`scripts/observability/verify_gate.py`, `scripts/observability/tests/test_verify_gate.py`,
`src/previewRetry.ts`, `.steady-watcher.json` — plus ~15 untracked source/test files
(`backend/services/archive_journal.py`, `backend/services/probe_gate.py`,
`backend/services/preview_asr_sidecar.py`, `backend/services/preview_asr_worker.py`,
`backend/tests/test_background_remediation.py`, `backend/tests/test_preview_asr_sidecar_e2e.py`,
`backend/tests/test_preview_asr_worker.py`, `src/previewRetryCeilings.test.ts`, `runs/`,
`control/`). The four lane reports in `runs/SA-vodrip-lane{A,B,C,D}-*.md` describe these as
**completed slices with their own test evidence**, and two of them say the slice is *not yet
wired* — Lane C §2.3 ("Nothing calls the new exports… the app still runs one wall-clock total")
and Lane B §7 ("Router integration is untested and unwritten"). Every additional lane that
edits these files compounds the merge risk; the cheapest way to unstall the project is to
verify the tree as a whole and land it in coherent slices before more work piles on.

## The action (one paragraph spec)

Run the repo gate as three separate commands, each redirected to a file with the real
`$LASTEXITCODE` read afterwards (never piped — see the standing rule: `cmd > file 2>&1`, then
read `$LASTEXITCODE`): `npx tsc --noEmit`, `npx vitest run`, and `python -m pytest` from
`backend/` (the suite `scripts/verify.ps1` orchestrates). Pre-flight the environment the way
Lane C did — count `node_modules` files (floor 500; measured **10,848** on 2026-10-08, so
`tsc`/`vitest` are trustworthy here) — because an empty `node_modules` is how this project
once published a fake "tsc clean". Then classify each dirty/untracked path by owning lane from
the `runs/` reports and the diff itself, and have the **parent** commit the preview slice in
coherent groups (e.g. one commit for the ASR sidecar/worker + its tests, one for
`previewRetry.ts` + `previewRetryCeilings.test.ts`, one for the backend
`archive_db`/`scheduler`/`ytdlp`/`rate_budget` changes with their tests). Do **not** commit
scratch or machine-local files: `.steady-watcher.json` (the machine-wide Steady Watcher
governor config), `tmp_*.txt`, `git_*.txt`, `tmp_git_sb.txt`, `tmp_diffstat.txt`,
`backend/tests/_chat_history_scratch.db-shm/-wal`, and `control/` are not product code.
Acceptance: the gate is green with real exit codes recorded (the exact pytest summary line
named, not a status code); each preview slice is committed with its tests; `git status` no
longer lists the preview source/test files; every scratch/untracked artifact is either left
untracked or explicitly ignored, never deleted.

## Boundary

The parent owns all commits — this lane does not `git add`, commit, push, or delete untracked
files. If the gate goes red, the spec is to report the exact failing command, population, and
exit code and stop, not to "fix" it into green.

## Evidence

- `git rev-parse --short HEAD` → `6379ea2`; `git log --oneline -8` → top commit dated Tue Oct 6 09:19:42 2026.
- `git status --porcelain` and `git diff --stat` (13 files, +992/−215) — measured 2026-10-08, this workspace.
- `runs/SA-vodrip-laneB-asr.md` §7 (router integration unwritten), `runs/SA-vodrip-laneC-timeout.md` §2.3 (wiring unapplied).
- `node_modules` file count = 10,848; `node_modules/vitest/package.json` present.

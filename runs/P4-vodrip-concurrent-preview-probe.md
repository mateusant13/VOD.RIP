# P4 · VOD.RIP · measure the owner's actual test: 3 channels at once, ≤5 s to play, captions on all

**Slug:** `concurrent-preview-probe` · **Written:** 2026-10-08 · **Owner:** observability lane
(`scripts/observability/probe.py`) + whoever runs the live app
**Seat:** recon (read-only) — this file is a spec, not a change.

## Why this is a top action

The owner's stated goal — "3 canais AO MESMO TEMPO, todos play ≤ 5 s, todos com legenda,
YouTube sem ASR local" (prompt id=192509) — has **never been measured as a population**. Lane A
(`runs/SA-vodrip-laneA-latency.md` §P4) returned `UNMEASURED` for every latency figure the
owner asked for: `probe.py` has no concurrent mode, so "3 at once" is a belief, not a
measurement. Worse, the substrate that would measure it is currently dark: measured
**2026-10-08**, there is **no TCP listener on `127.0.0.1:7897`** (the documented VOD.RIP API
port) — the same state Lane A found on 2026-10-06 — so nothing can be sampled until the app is
running. Lane A also found three shared serialisation points that decide whether "3 at once"
degrades into "one channel, three times slower": the global `_YTDLP_LOCK` + `_PriorityGate` in
`backend/services/ytdlp_guard.py:122-123` (one YouTube resolve at a time; a recorded
`server_ms=22875` single-preview resolve is quoted in that file's own header against the
owner's 5 000 ms budget), the per-platform `rate_budget` ceiling
(`backend/services/rate_budget.py:130-134`, youtube 20 rpm), and the preview ASR sidecar lock
(`preview_asr_sidecar.py`) — which Lane B removed in the uncommitted rewrite but which has
**never been exercised live**. Until a concurrent probe exists and runs, every claim about the
5 s budget and about "captions on all three" is unverifiable, and the fix lanes have no
instrument to prove their work.

## The action (one paragraph spec)

Add a `--concurrent-platforms` mode to `scripts/observability/probe.py` (Lane A §P-1's diff)
that fires one `POST /api/preview/session` per platform (YouTube / Twitch / Kick) from a single
`threading.Barrier`, records each platform's time-to-first-playable and caption-presence
separately, and reports **the worst-of-three** as the headline (the owner's number, not the
best) **plus** a distinct `arrived / attempted` count per platform so a request that never
reached the endpoint is recorded as `not_measured` instead of silently vanishing from the
percentiles — the exact defect `STATE.md` §1 made invisible ("20 of 40 attempts never reached
the endpoint"). Pin three **long-lived** fixture VOD URLs (a dead fixture reads as a slow
platform) — one per platform — and run the probe against a live API (start the app, or take a
busy port via `VODRIP_TAKE_PORT`, per `backend/services/server_lifecycle.py:456`). Acceptance:
a verdict file that states, per platform, p50/p95/max time-to-play with `attempted` and
`arrived` as separate rows, the worst-of-three against the 5 000 ms budget, and a
caption-present boolean per platform — and the raw sample log left in `tmp/` so the numbers can
be re-derived. If any figure cannot be produced (API down, fixture dead), record it as
`unmeasured` with the reason, never as zero.

## Boundary

Read-only against the live app and the live archive (`mode=ro` URIs only — never a plain
`connect()`, never a write); do not modify the archive DB. Do not start/restart/kill MiniMax
Code, and do not kill the app's GPU/Chromium workers to "unstick" a slow probe (Steady Watcher
governs those). Parent owns the commit of the `probe.py` change.

## Evidence

- `runs/SA-vodrip-laneA-latency.md` §P4 (all latency figures `UNMEASURED`, n=0), §P-1 (the `--concurrent-platforms` diff), GATE 1–3 (the serialisation points).
- `backend/services/ytdlp_guard.py:122-123` (global lock), `backend/services/rate_budget.py:130-134` (per-platform ceiling), `backend/services/preview_asr_sidecar.py` (sidecar lock — removed by Lane B's uncommitted rewrite).
- Live check 2026-10-08: `Get-NetTCPConnection -State Listen` filtered to port 7897 → **no listener** (API down).
- `STATE.md` §1: `POST /api/preview/session`, 3000 ms budget, n=40, **20 answered** — reachability, not speed.

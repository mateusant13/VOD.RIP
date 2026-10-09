# P4 · VOD.RIP · wire the silence-vs-overrun verdict into the preview UI

**Slug:** `wire-deadline-verdict` · **Written:** 2026-10-08 · **Owner:** frontend lane (App.tsx
+ previewStartTimeout.ts are the hot files)
**Seat:** recon (read-only) — this file is a spec, not a change.

## Why this is a top action

The owner reported two different preview failures — "a pré-visualização demorou demais, tente
novamente" and a silent no-response — and today the app shows **one** message for both. Lane C
(`runs/SA-vodrip-laneC-timeout.md`) found and *measured* the defect: four call sites hard-code
the same i18n key `'Preview took too long — try again'` (`src/i18n.tsx:443` pt / `:1303` es)
for four semantically different conditions — `src/App.tsx:1777` (guard total timeout fired),
`src/ChannelExplorePopup.tsx:634` (same guard, explore popup), `src/App.tsx:2002`
(`!muxReady` after mux poll), `src/App.tsx:2027` (fetch aborted). The worst is `App.tsx:2027`,
where both the message *and* the stage branch on `aborted`, which is derived from a **string
match on an exception name** (`err instanceof DOMException && err.name === 'AbortError'`) —
exactly the external-string dependency the project's own deadline law forbids. Lane C built and
proved the fix mechanism (`src/previewRetry.ts`: `classifyPreviewDeadline`,
`previewDeadlineVerdict`, `PREVIEW_FIRST_OUTPUT_CEILING_MS` session 45 000 / playback 20 000,
`PREVIEW_STAGE_TOTAL_CEILING_MS` session 75 000 / playback 60 000 — 18 tests in
`src/previewRetryCeilings.test.ts`, 4/4 mutations red), but **nothing calls the new exports**,
so the owner's two symptoms still collapse into one. This is the highest-value, smallest-diff
frontend action: the hard part is done and tested; what remains is the wiring.

## The action (one paragraph spec)

Apply Lane C §2.3 (a)–(d) exactly as proposed: (a) in `src/previewStartTimeout.ts`, split the
single 60 s `PREVIEW_START_TIMEOUT_MS` into the two stage ceilings from `previewRetry.ts` —
arm both for the `session` stage, retire the silence timer and re-arm the `playback` pair in
`markCreateResolved()` — and widen the `onTimeout` callback (interface at
`previewStartTimeout.ts:30`, call sites `App.tsx:1773` and `ChannelExplorePopup.tsx:632`) to
pass **which** ceiling fired (`fired: 'first-output' | 'total'`); (b) at `App.tsx:1777` and
`ChannelExplorePopup.tsx:634`, replace the hard-coded `setError(t('Preview took too long — try
again'))` with `previewDeadlineVerdict({ stage, firstOutputArrived: guard.createResolved })`
and use `verdict.messageKey` / `verdict.stage`; (c) at `App.tsx:2027-2028`, delete the
`aborted ? … : …` ternary driven by `err.name === 'AbortError'` and route through the same
verdict call; (d) at `App.tsx:2002`, treat the `!muxReady` case as its own failure via
`previewDeadlineVerdict({ stage: 'session', firstOutputArrived: false })`. Acceptance: `npx tsc
--noEmit` exit 0; `npx vitest run` exit 0 with `src/previewRetry.test.ts` left **untouched** as
the independent control that still passes (proving no export contract changed); and a new
assertion that a no-first-byte failure resolves to the `stalled` key (`'No response from
server'`) while bytes-then-quiet resolves to the `timeout` key — two distinct messages for the
owner's two symptoms.

## Boundary

`src/App.tsx` is ~380 KB and shared; keep the edit to the four named call sites and do not
refactor the surrounding preview code. Do not invent new i18n keys — `t()` falls back to the
English source for a missing key, so a new key would show English to a Portuguese user; both
keys used here are already translated in pt and es (`i18n.tsx:443/:1303` and `:543/:1401`).
Parent owns the commit.

## Evidence

- `runs/SA-vodrip-laneC-timeout.md` §1.2 (the four call sites), §2.3 (the (a)–(d) diff proposals), §3 (before/after ceilings).
- `src/previewRetry.ts` exports + `src/previewRetryCeilings.test.ts` (18 tests, 4/4 mutations red) — present in the working tree, uncommitted.
- `src/previewStartTimeout.ts:19` `PREVIEW_START_TIMEOUT_MS = 60_000` (the single wall-clock timer the split replaces).

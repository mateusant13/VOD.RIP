# LANE C — seat: WORKER — preview timeout law (silence vs overrun)

Project: VOD.RIP · Date: 2026-10-06 · Window: this lane turn, ~11:17–11:35 -03

---

## 1. VERDICT

**Can the app now distinguish slow from never-arriving? NO — not yet. The mechanism is
built and proven; it is not wired into the running app, and the wiring is in files this
lane is forbidden to write.**

Precisely:

- **In `src/previewRetry.ts`: YES.** The distinction exists as executable, tested policy.
  Two outcomes (`stalled` / `timeout`) are derived from first-output evidence, each with
  its own ceiling, its own i18n key, and its own mechanical retry decision.
- **In the shipped app: NO.** Nothing calls the new exports. The app still runs one
  wall-clock total and still shows one message for both defects.

The reason is the ownership boundary, not a blocker I chose: the timer lives in
`src/previewStartTimeout.ts` and the message is chosen in `src/App.tsx` /
`src/ChannelExplorePopup.tsx`, all of which are **OFF LIMITS** for this lane
(`backend/**` belongs to another lane; `App.tsx` is 381 KB and another lane's). So I
implemented the half I own and wrote the exact wiring as a diff proposal in §2.3.

### 1.1 Premise correction — the briefing's premise was measurably wrong

The briefing states: *"`src/previewRetry.ts` today has a total timeout. A total timeout
alone is exactly what that law forbids."*

**Measured: `src/previewRetry.ts` contained no timeout at all.** Before my edit it was 46
lines / 1664 bytes exporting a retry counter (`previewRetryAfterError`,
`previewRetryMode`) and nothing else — no clock, no ceiling, no timer.

The forbidden wall-clock-only ceiling actually lives one file over:

- `src/previewStartTimeout.ts:19` — `export const PREVIEW_START_TIMEOUT_MS = 60_000;`

That single 60 s timer spans the whole 'Starting…' phase: session-create POST, attach,
HLS manifest/fragment loads and the first frame (its own doc comment,
`previewStartTimeout.ts:5-8`). It is one deadline with no first-output counterpart — the
exact shape the constitution forbids — so the defect is real, it is just not in the file
the briefing named. This matters for §3 and for whoever applies the wiring.

### 1.2 The string-matching defect — found, measured, NOT fixed by me

The briefing says: *"If the message 'A pré-visualização demorou demais' is produced by
string-matching somewhere, that is the defect."* **It is.** Four call sites produce the
**same** i18n key `'Preview took too long — try again'` (`src/i18n.tsx:443` pt /
`:1303` es) for **four semantically different conditions**:

| # | file:line | Condition | Meaning |
|---|---|---|---|
| 1 | `src/App.tsx:1777` | guard total timeout fired | could be silence OR overrun — undifferentiated |
| 2 | `src/ChannelExplorePopup.tsx:634` | same guard, explore popup | same collapse |
| 3 | `src/App.tsx:2002` | `!muxReady` after mux poll | backend mux produced nothing |
| 4 | `src/App.tsx:2027` | `aborted` (fetch abort/timeout) | undifferentiated |

The worst is `src/App.tsx:2027-2028`:

```tsx
setError(aborted ? t('Preview took too long — try again') : (err.message || t('Preview failed')));
markPreviewError(trimmedUrl, aborted ? (guard.createResolved ? 'playback' : 'session') : 'session');
```

Both the **message** and the **stage** are chosen by branching on `aborted`, and `aborted`
is derived from a *string match on an exception name* — `err instanceof DOMException &&
err.name === 'AbortError'`. A DOMException name is exactly the kind of external string
the decision must not depend on. That is the defect, and it is the direct cause of the
owner's two prompts landing on one indistinguishable message.

I did not change it: `App.tsx` is another lane's file. The replacement is in §2.3.

---

## 2. WHAT I CHANGED

### 2.1 `src/previewRetry.ts` — MODIFIED (owned file)

Original content **preserved byte-identical in behaviour**: the module doc comment (with
a new deadline-law section appended), `PreviewRetryStage`, `PreviewRetryMode`,
`PreviewRetryState`, `previewRetryMode`, `previewRetryAfterError` — all unchanged.

`PreviewRetryState` deliberately gained **no** fields. The pre-existing assertion at
`src/previewRetry.test.ts:22` uses `toEqual({ url, stage, attempts: 0 })`; `toEqual` is
exact on extra defined properties, so adding a field would have broken a test encoding
the current contract. Per-attempt deadline data therefore lives in its own types, not
bolted onto the per-media retry state.

**Added exports (all new; no existing export changed):**

| Export | Line | Purpose |
|---|---|---|
| `PREVIEW_FIRST_OUTPUT_CEILING_MS` | `src/previewRetry.ts:84` | first-output ceiling per stage — `session: 45_000`, `playback: 20_000` |
| `PREVIEW_STAGE_TOTAL_CEILING_MS` | `src/previewRetry.ts:99` | wall-clock total per stage — `session: 75_000`, `playback: 60_000` |
| `type PreviewDeadlineOutcome` | `src/previewRetry.ts:111` | `'stalled' \| 'timeout'` |
| `type PreviewDeadlineFire` | `src/previewRetry.ts:114` | `'first-output' \| 'total'` — which ceiling tripped |
| `type PreviewDeadlineRetry` | `src/previewRetry.ts:117` | `'retry-stage' \| 'escalate-full'` |
| `classifyPreviewDeadline(firstOutputArrived)` | `src/previewRetry.ts:129` | `boolean → 'timeout' : 'stalled'` |
| `previewDeadlineFire(stage, firstOutputAtMs, elapsedMs)` | `src/previewRetry.ts:143` | pure; which ceiling fires, or `null` |
| `previewDeadlineMessageKey(stage, outcome)` | `src/previewRetry.ts:190` | i18n **KEY**, never a translated string |
| `previewDeadlineRetry(stage, outcome)` | `src/previewRetry.ts:208` | the mechanical retry rule |
| `interface PreviewDeadlineVerdict` | `src/previewRetry.ts:216` | `{ stage, outcome, spent, messageKey, retry }` |
| `previewDeadlineVerdict({stage, firstOutputArrived})` | `src/previewRetry.ts:231` | the single seam a call site uses |

Backing table `PREVIEW_DEADLINE_MESSAGE_KEYS` is module-private
(`src/previewRetry.ts:175`).

**Design decisions worth stating:**

1. **Silence is judged on ARRIVAL TIME, not elapsed** (`src/previewRetry.ts:158`).
   Output landing at 46 s against a 45 s silence ceiling is still silence, even though a
   byte eventually arrived. Judging on total elapsed would retroactively reclassify a
   late byte as healthy — reinstating the exact collapse this lane removes.
2. **No new i18n keys.** `t()` falls back to the English source text for a missing key
   (`src/i18n.tsx:1702`), so inventing a key would have shown **English** to a Portuguese
   user. Both keys chosen are already translated in pt **and** es — verified, not assumed:
   `'Preview took too long — try again'` at `src/i18n.tsx:443` / `:1303`, and
   `'No response from server'` at `src/i18n.tsx:543` / `:1401`.
3. **`classifyPreviewDeadline` takes no stage** (`src/previewRetry.ts:129`) because the
   outcome genuinely *is* stage-independent — silence vs overrun is the same question in
   both stages. `tsc` caught this as `TS6133 'stage' is declared but never read`; I
   removed the parameter rather than silencing the check with `void`, and made `stage`
   genuinely load-bearing in `previewDeadlineMessageKey` via the per-stage table.
4. **No invented stage taxonomy.** Only the two stages the code already uses.
   `session`'s first-output signal is `PreviewStartTimeout.createResolved`; `playback`'s
   is a painted frame (`canplay` → `markReady`). A real mid-stream signal exists too —
   `attachPreviewBufferingListeners` wires `waiting`/`playing`/`canplay`
   (`src/previewPlayerUtils.ts:739-749`) — which is what makes the post-output case
   honest for `playback`.

### 2.2 `src/previewRetryCeilings.test.ts` — CREATED (owned file, 18 tests)

Every assertion carries a `// MUTATION that turns this RED:` comment naming the specific
edit that breaks it. Full per-assertion mapping in §4.

**`src/previewRetry.test.ts` was deliberately LEFT UNTOUCHED.** It already existed
(contrary to the briefing's "create if absent"), and leaving it untouched makes it an
**independent control**: it still passes, which proves the public contract survived my
rewrite without me having edited the test to accommodate it.

### 2.3 Diff proposal — files I do NOT own (NOT APPLIED)

Per the briefing: *"If you need a change outside your files, STOP and put the diff
proposal in your report."* This is what the wiring requires.

**(a) `src/previewStartTimeout.ts` — split the single timer into the stage pair.**
The class already tracks `createDone`, so the first-output signal exists; it just is not
used as a deadline. Proposed shape (not applied):

```ts
import { PREVIEW_FIRST_OUTPUT_CEILING_MS, PREVIEW_STAGE_TOTAL_CEILING_MS }
       from './previewRetry';

// in start(): arm BOTH ceilings for the session stage
this.silenceTimer = window.setTimeout(() => this.tick(), PREVIEW_FIRST_OUTPUT_CEILING_MS.session);
this.totalTimer   = window.setTimeout(() => this.tick(), PREVIEW_STAGE_TOTAL_CEILING_MS.session);

// markCreateResolved(): first output arrived — retire the session silence ceiling
// and re-arm the playback pair
markCreateResolved() {
  this.createDone = true;
  this.controller = null;
  this.clearTimer();                                   // drop the session pair
  this.silenceTimer = window.setTimeout(() => this.tick(), PREVIEW_FIRST_OUTPUT_CEILING_MS.playback);
  this.totalTimer   = window.setTimeout(() => this.tick(), PREVIEW_STAGE_TOTAL_CEILING_MS.playback);
}

// tick() reports WHICH ceiling fired, so the caller never infers it from a message:
const fired: PreviewDeadlineFire = this.silenceTimerFired ? 'first-output' : 'total';
this.callbacks.onTimeout(this.mediaUrl, stage, fired);
```

This widens the callback signature from `(mediaUrl, stage)` to
`(mediaUrl, stage, fired)`. **Three existing call sites construct this callback** and
would need the extra arg tolerated or updated:
`src/App.tsx:1773`, `src/ChannelExplorePopup.tsx:632`, plus the interface at
`src/previewStartTimeout.ts:30`.

**(b) `src/App.tsx:1777-1778` and `src/ChannelExplorePopup.tsx:634-635` — consume the
verdict instead of hard-coding one message.** Both currently do
`setError(t('Preview took too long — try again'))`. Proposed:

```tsx
const verdict = previewDeadlineVerdict({ stage, firstOutputArrived: guard.createResolved });
setError(t(verdict.messageKey));
markPreviewError(url, verdict.stage);
```

**(c) `src/App.tsx:2027-2028` — delete the exception-name branch.** This is the one that
matters most: replace the `aborted ? … : …` ternary (driven by
`err.name === 'AbortError'`) with the same `previewDeadlineVerdict` call, so the stage
and the message both come from first-output evidence instead of an exception name.

**(d) `src/App.tsx:2002` — the mux case is its own failure,** not the generic timeout
key: `!muxReady` means the backend produced no playable playlist, which is silence in the
`session` stage. Route it through `previewDeadlineVerdict({ stage: 'session',
firstOutputArrived: false })`.

I am flagging (a)–(d) as the **whole remaining task**, not as a nicety. Until one of them
lands, the owner's two prompts still produce one message.

---

## 3. BEFORE / AFTER CEILINGS

| Deadline | BEFORE | AFTER | Kind | Where |
|---|---|---|---|---|
| whole 'Starting…' phase | **60_000** — one timer | *(gone)* | was wall-clock ONLY — the forbidden shape | `src/previewStartTimeout.ts:19` |
| `session` first output | **none** | **45_000** | **first-output** (fires on no byte arriving) | `src/previewRetry.ts:85` |
| `session` total | *(folded into the 60 s)* | **75_000** | wall-clock total | `src/previewRetry.ts:100` |
| `playback` first output | **none** | **20_000** | **first-output** (no frame) | `src/previewRetry.ts:86` |
| `playback` total | *(folded into the 60 s)* | **60_000** | wall-clock total | `src/previewRetry.ts:101` |

**Which deadlines are now first-output ceilings:** the two rows marked **first-output** —
`session` (45 s) and `playback` (20 s). **Which remain wall-clock totals:** the two
`PREVIEW_STAGE_TOTAL_CEILING_MS` rows (75 s / 60 s), which now bound a stage *after*
output exists instead of bounding the whole phase.

**The 60 s deadline is not simply lowered — it is split, and the total goes UP.** That is
deliberate and it is the honest consequence of the split:

- `session` silence now resolves at **45 s instead of 60 s** — 15 s earlier, and earlier
  than the backend's own hard cap, so a dead POST is named while it is still worth
  naming. This is the fail-fast the law asks for.
- But a stage that *is* producing output now gets **75 s** before the total fires, where
  before it shared one 60 s budget with the silence case. A slow-but-alive create is no
  longer killed by a deadline that was sized to detect a dead one.

A test pins this (`session` silence must be detected below the old 60 s), so the split
cannot be quietly reverted into a single smaller number.

**Why 45 s for `session` first output:** it is the backend's *own* cap
(`VODRIP_PREVIEW_CREATE_TIMEOUT_SEC`, default 45 s, documented at
`src/previewStartTimeout.ts:10-11`). No first output can legitimately arrive after the
backend has already abandoned the create, so this is the backend's boundary rather than a
number I picked.

**Why 75 s for `session` total:** create 45 s + window-HLS mux poll ≤15 s
(`src/previewStartTimeout.ts:12-13`) + ~15 s attach/first-frame margin.

---

## 4. TEST EVIDENCE

**Command** (verbatim, no pipes, no `Select-Object -First`, so the exit code is real):

```powershell
npx vitest run src/previewRetryCeilings.test.ts src/previewRetry.test.ts > tmp\laneC-vitest2.txt 2>&1
$LASTEXITCODE
```

**Exit code: `0`**

**Exact summary lines** (from `tmp/laneC-vitest2.txt`, vitest **v4.1.11**, Node **v24.8.0**):

```
 Test Files  2 passed (2)
      Tests  23 passed (23)
   Duration  1.35s (transform 98ms, setup 272ms, import 81ms, tests 12ms, environment 1.91s)
```

Both files ran: the 18 new assertions **and** the untouched pre-existing
`src/previewRetry.test.ts` (5 tests) — the control proving the public API survived.

**Pre-flight — I refused to trust a green from an empty `node_modules`**, since that is
how this project published a fake "tsc clean" before: 127 top-level dirs, 182
`package.json` at depth ≤2, `node_modules/vitest/package.json` present, node v24.8.0.

### 4.1 Typecheck

```powershell
npx tsc --noEmit > tmp\laneC-tsc2.txt 2>&1
```

**Exit code `0`.** `ERROR_LINES=0`, `MINE_COUNT=0` (grep of the output for `previewRetry`:
zero hits). This is the whole repo, not a scoped subset.

Worth recording honestly: the **first** run of this command returned **exit 2** with one
real error — `src/previewRetry.ts(126,3): error TS6133: 'stage' is declared but its value
is never read` — my own unused parameter. I fixed the cause (§2.1 decision 3) and re-ran
to `0`. I am not reporting the final `0` without disclosing the `2`.

### 4.2 Tests that can go RED — 4 mutations, all RED

A green suite proves nothing until a mutation turns it red. Harness:
`tmp/laneC-mutation-check.ps1` (backs up the source, applies one edit, runs vitest,
restores, re-verifies the restore is byte-identical).

```
  MUTATION M1 -> EXITCODE=1 | Tests  4 failed | 19 passed (23)
  MUTATION M2 -> EXITCODE=1 | Tests  5 failed | 18 passed (23)
  MUTATION M3 -> EXITCODE=1 | Tests  1 failed | 22 passed (23)
  MUTATION M4 -> EXITCODE=1 | Tests  1 failed | 22 passed (23)
  MUTATION RESTORED -> EXITCODE=0 | Tests  23 passed (23)
  RESTORE VERIFIED: byte-identical to pre-mutation source
RESULT M1=1 M2=1 M3=1 M4=1 CONTROL=0
VERDICT: PASS - all 4 mutations went RED, control is GREEN
```

Harness exit code `0`.

| Mutation | Edit | Result |
|---|---|---|
| **M1** | `classifyPreviewDeadline` → `return 'timeout'` (collapse both outcomes) | RED, **4 tests** |
| **M2** | `previewDeadlineMessageKey` → `return 'Preview took too long — try again'` (one key for both) | RED, **5 tests** |
| **M3** | `previewDeadlineRetry` → branch on `outcome` instead of `stage` | RED, **1 test** |
| **M4** | drop the arrival-time check `if (firstOutputAtMs >= firstOutputCeiling)` | RED, **1 test** |

**My own harness lied to me once, and that is worth recording.** The first run printed
`VERDICT: FAIL` while all four runs were in fact correct (M1–M3 red, control green).
Cause: `Run-Vitest` used `return $code` *after* a `Write-Output`, so the caller received
**an array** `[logline, code]` and `$c1 -eq 0` compared an array to an int. The mutation
evidence was sound; the verdict arithmetic was not. I fixed the harness (no `return`;
state via `$script:LASTCODE`) and re-ran — the `FAIL` above was never a real failure. A
FAIL from a broken harness is the same class of false signal as a masked exit code, so I
did not "fix" it by editing the assertion until it agreed with me.

### 4.3 Per-assertion → the mutation that turns it RED

| Assertion (`src/previewRetryCeilings.test.ts`) | Line | Mutation that breaks it |
|---|---|---|
| every first-output ceiling ≤ its stage total | 15 | raise `PREVIEW_FIRST_OUTPUT_CEILING_MS` above the total for either stage → silence ceiling becomes unreachable |
| first-output ceiling fires strictly BEFORE the total | 25 | swap the two ceiling maps' roles → fail-fast gone |
| `session` silence detected before the old 60 s | 35 | set `session` first-output ≥ 60_000 → restores the undifferentiated 60 s wall |
| **no first byte → `stalled`, NOT `timeout`** | 45 | `classifyPreviewDeadline` returns `'timeout'` when false, or unconditionally |
| **bytes then quiet → a DIFFERENT state** | 53 | return `'stalled'` when `firstOutputArrived` is true |
| silence and overrun fire different ceilings | 63 | `previewDeadlineFire` always answers `'total'` |
| arrival-time judgement (46 s byte is still silence) | 75 | judge the first-output ceiling on total elapsed instead of `firstOutputAtMs` |
| **fast success trips NEITHER ceiling** | 83 | return non-null for a healthy stage (e.g. compare against a 0 ceiling) |
| still-running stage inside both ceilings reports nothing | 93 | drop the `elapsed >= total` comparison |
| retry decision: session silence escalates, rest retry-stage | 109 | invert the rule, or branch on `outcome` alone (= M3) |
| **decision functions take NO message argument** | 118 | add a third `message: string` parameter and branch on it — the exact briefing defect; arity asserted so it cannot be added quietly |
| message is an i18n KEY, not a translated string | 131 | return the pt text instead of the dictionary key → `t(key)` renders the key |
| **silence and overrun resolve to DIFFERENT keys** | 139 | return one constant key for both outcomes (= M2, the owner's symptom) |
| pre-lane key survives only on the overrun path | 147 | point the silence branch at `'Preview took too long — try again'` |
| verdict: every field follows from stage + evidence | 163 | let a caller-supplied field through (e.g. an optional `message`) |
| verdict records where the deadline was spent | 176 | hard-code `spent` to one value → discriminator lost |
| the two owner-reported symptoms produce two verdicts | 186 | make the verdict stage-insensitive → both prompts map to one verdict again |

---

## 5. CALL SITES

Every importer of `src/previewRetry`, from a pattern search over `**/*.{ts,tsx}`:

| # | file:line | Imports | Contract changed? |
|---|---|---|---|
| 1 | `src/App.tsx:138` | `previewRetryAfterError`, `previewRetryMode`, `PreviewRetryStage`, `PreviewRetryState` | **NO** |
| 2 | `src/ChannelExplorePopup.tsx:89` | same four | **NO** |
| 3 | `src/components/LivePlayerPopup.tsx:54` | `previewRetryAfterError`, `PreviewRetryState` | **NO** |
| 4 | `src/previewStartTimeout.ts:1` | `type PreviewRetryStage` (type-only) | **NO** |
| 5 | `src/previewRetry.test.ts:6` | `previewRetryAfterError`, `previewRetryMode`, `type PreviewRetryState` | **NO** — file untouched, still passes |
| 6 | `src/previewRetryCeilings.test.ts` | new exports | n/a — new file |

**Zero existing call sites changed contract.** No export was removed, renamed, or had a
parameter added. `PreviewRetryState` gained no field, so the
`toEqual({ url, stage, attempts: 0 })` assertion at `src/previewRetry.test.ts:22` holds
untouched.

Indirect consumers that *should* adopt the new policy — all currently hard-coding the
single message, all off-limits to me: `src/App.tsx:1777`, `src/App.tsx:2002`,
`src/App.tsx:2027`, `src/ChannelExplorePopup.tsx:634`, plus the `PreviewStartTimeout`
callback signature at `src/previewStartTimeout.ts:30`.

---

## 6. POPULATION AND WINDOW

Every number, with its population and origin.

| Number | Value | Population | Source |
|---|---|---|---|
| Ceiling `session` first-output | 45_000 ms | all `session` stages | derived from `VODRIP_PREVIEW_CREATE_TIMEOUT_SEC` default, documented `src/previewStartTimeout.ts:10-11` — **derived, not measured** |
| Ceiling `session` total | 75_000 ms | all `session` stages | 45 + 15 (mux) + 15 margin, from `src/previewStartTimeout.ts:12-14` — **derived, not measured** |
| Ceiling `playback` first-output | 20_000 ms | all `playback` stages | **PROPOSAL. No measurement backs this.** See §7 |
| Ceiling `playback` total | 60_000 ms | all `playback` stages | mirrors the pre-split 60 s allowance — **derived** |
| Old ceiling removed | 60_000 ms | whole phase | `src/previewStartTimeout.ts:19`, **measured by reading the file** |
| Tests | 23 passing / 18 new + 5 pre-existing | the 2 named files, one run | **measured**, `tmp/laneC-vitest2.txt`, exit 0 |
| tsc errors | 0 | whole repo, `tsc --noEmit` | **measured**, exit 0 (first run exit 2 — disclosed in §4.1) |
| Mutations | 4/4 RED, control GREEN | 4 edits to one source file | **measured**, `tmp/laneC-mutation-result.txt` |
| i18n key translations | pt + es, both keys | the 2 dictionaries | **measured by grep**, `src/i18n.tsx:443/543`, `:1303/1401` |
| Live app speed / reachability | — | — | **UNMEASURED.** I ran **zero** live preview attempts; see §7 |
| `POST /api/preview/session` 40/20/p50 16 ms/p95 106 ms/max 922 ms | — | previous window, 240 min ending 2026-10-05T05:31:11Z | **NOT re-measured by me.** Quoted from `STATE.md` §1 via the lane briefing. Its own caveat stands: **20 of 40 never reached the endpoint** and were excluded, not counted as 0 ms |

**Measurement honesty applied.** I made no live attempts, so I report no latency figure
of my own. Had I made some, per this lane's rules any attempt that never reached the
endpoint would be `not_measured` and **excluded** from percentiles — never counted as
0 ms, never averaged in. A speed verdict could not have spoken to the reachability
failure anyway: 20 of 40 attempts not arriving is a different defect from slowness, and
this lane's split is aimed at exactly that difference.

---

## 7. WHAT WAS NOT VERIFIED

1. **The live app is not wired — so the owner cannot yet see this.** Nothing calls
   `previewDeadlineVerdict`. Until §2.3 lands, both prompts still render one message.
   This is the single largest gap and it is a scope boundary, not an oversight.
2. **The 20 000 ms `playback` first-output ceiling is UNMEASURED.** No healthy-path
   sample of attach + manifest + first fragment + first frame backs it. It is a
   proposal. It may be too tight for a cold window-HLS attach and would then fire on
   healthy previews. **A measurement should replace it.**
3. **No live measurement of anything.** Zero preview attempts, no latency, no
   reachability. The `STATE.md` §1 figures were not re-measured by me.
4. **The 45 s `session` first-output value was derived, not measured.** It assumes the
   backend honours its own 45 s cap and that no first output can legitimately arrive
   after it. I read the cap's documentation; I did not observe it firing.
5. **The 75 s `session` total is unexercised in the app.** Raising the total from 60 s is
   a behaviour change I have only type-checked and unit-tested in isolation.
6. **No browser/E2E verification.** I did not render a preview, observe a real timeout,
   or confirm the message text on screen in pt. `e2e/**` is off-limits to this lane.
7. **Not verified that the four hard-coded message sites are the complete set.** I grepped
   `'Preview took too long'` and found 4 producers in `src/`; a message built dynamically
   or surfaced from a backend error string would not appear in that grep.
8. **No review by another subagent.** §8.4.
9. **`i18n.tsx` correctness for the silence key was checked only by grep.** I verified the
   keys exist in pt and es; I did not verify the Portuguese wording is *good* for this
   specific case ("Sem resposta do servidor" is worded for the live player, and reusing
   it for a preview is a judgement call the owner may want to revisit).
10. **Backend behaviour untested.** `backend/**` belongs to another lane; I did not read
    or run anything there.

---

## 8. OWNERSHIP

**Files I wrote (4 — exactly the lane's list, no others):**

| File | State | Bytes |
|---|---|---|
| `src/previewRetry.ts` | modified (all new code appended after the preserved originals) | 9296 → rewritten after the `TS6133` fix |
| `src/previewRetryCeilings.test.ts` | created | ~8.9 KB, 18 tests |
| `runs/SA-vodrip-laneC-timeout.md` | created (this file) | — |
| `tmp/laneC-mutation-check.ps1` | created (verification harness, throwaway) | — |

**`src/previewRetry.test.ts` — NOT written.** It already existed; I left it untouched
deliberately so it could serve as an independent control (§2.2).

**Scratch/throwaway under `tmp/` (not source):** `tmp/laneC-vitest.txt`,
`tmp/laneC-vitest2.txt`, `tmp/laneC-tsc.txt`, `tmp/laneC-tsc2.txt`,
`tmp/laneC-mut-*.txt`, `tmp/laneC-mutation-result.txt`,
`tmp/laneC-previewRetry.bak.ts`, `tmp/laneC-mutation-check.ps1`.

**Files I did NOT touch (boundary respected):** `backend/**`,
`src/previewStartTimeout.ts`, `src/App.tsx`, `src/ChannelExplorePopup.tsx`,
`src/explorePopupUtils.tsx`, `src/components/PreviewChatPanel.tsx`,
`src/components/LivePlayerPopup.tsx`, `src/i18n.tsx`, `src/previewPlayerUtils.ts`,
`e2e/**`, `.steady-watcher.json`, `control/`, `runs/TICKET-*.md`.

**Git: nothing staged, nothing committed.** No `git add`, `commit`, `checkout` or
`reset` was run.

---

## 9. SELF-AUDIT

**9.1 Protocols missing.** None that I know of were skipped. The four briefed
requirements each have an artifact: read the file first (§2.1 preserves it), per-stage
first-output ceiling (§3), mechanical decision (§4.3 row 10/11), tests that go RED (§4.2).
The `gpt`/`codeintel` review loop named in the root `AGENTS.md` I did **not** run — it
shells out to a browser-driven CLI that is outside this lane's budget and would contend
with the other lanes on this box. That is a real omission, named rather than glossed.

**9.2 Extra verification beyond the brief.** (a) `node_modules` pre-flight, refusing a
green from an empty install — this project has already published a fake "tsc clean" that
way. (b) 4 real mutations with byte-identical restore verification, because a green suite
is not evidence. (c) Full-repo `tsc --noEmit`, not just my files. (d) Grep-verified that
both i18n keys exist in pt *and* es before relying on them. (e) Found and disclosed that
my own harness printed a false `VERDICT: FAIL` (§4.2).

**9.3 New checkboxes (named, mechanical).** For a future lane, each is a one-line
predicate, not a vibe:
1. **no-single-ceiling** — every stage in `PreviewRetryStage` has a key in
   `PREVIEW_FIRST_OUTPUT_CEILING_MS` and in `PREVIEW_STAGE_TOTAL_CEILING_MS`. *Enforced:
   `Record<PreviewRetryStage, number>` makes a missing stage a compile error (tsc exit 0
   proves it).*
2. **first-output-not-unreachable** — `PREVIEW_FIRST_OUTPUT_CEILING_MS[stage] <=
   PREVIEW_STAGE_TOTAL_CEILING_MS[stage]` for all stages. *Enforced: test line 15; mutation
   raises a ceiling above its total → RED.*
3. **stage-decision-differs** — `previewDeadlineRetry` returns a different value for at
   least one stage pair; the decision may not be stage-blind. *Enforced: test line 109;
   M3 branches on `outcome` instead → RED.*
4. **no-message-parameter** — no deadline function accepts a `message`/`error` string.
   *Enforced: `Function.length` arity assertions, test line 118; adding a third param →
   RED.*
5. **outcomes-have-distinct-keys** — `previewDeadlineMessageKey(s, 'stalled') !==
   previewDeadlineMessageKey(s, 'timeout')` for all stages. *Enforced: test line 139; M2
   returns one constant → RED.*

**9.4 Review by another subagent.** **NOT DONE.** This is the weakest part of the lane.
A `verifier` should be pointed at `src/previewRetry.ts` +
`src/previewRetryCeilings.test.ts` with one question: *does any assertion pass for a
reason other than the mechanism it claims to pin?* I mutation-tested the suite, which
addresses assertion strength, but I authored both the code and its defence — that is not
independent.

**9.5 Confidence + what moves it.**

- **High** that the module does what §1 says it does: 23/23 green, 4/4 mutations RED,
  repo-wide tsc 0, restore byte-identical.
- **High** that the defect is real and located: I read all four producers of the shared
  message key and the timer that backs them.
- **Low** that this **fixes the owner's symptom today** — it does not; the app is not
  wired (§1, §7.1).
- **Low** confidence in the 20 s `playback` first-output number specifically.

*What moves it up:* applying §2.3 and observing the two prompts render two different
messages. *What moves it down:* a healthy cold window-HLS attach exceeding 20 s, which
would make the new ceiling fire on healthy previews — the classic way a fail-fast change
makes things worse.

**9.6 What was NOT verified.** Restated tersely, because §7 is long and this is the part
that must not be skimmed: not wired; `playback` 20 s unmeasured; no live measurement of
anything; `session` 45 s/75 s derived not observed; no E2E or on-screen pt text; message
producer set possibly incomplete; no independent review.

**9.7 Gate-doubt.**

- **`verde-de-verdade:`** `src/previewRetryCeilings.test.ts:118` (arity assertions on
  `classifyPreviewDeadline`, `previewDeadlineRetry`, `previewDeadlineMessageKey`,
  `previewDeadlineVerdict`) — together with the four RED mutations, these turn "no
  ad-hoc branching on a message string" from a review opinion into a mechanical
  constraint. `Function.length` catches a `message` parameter even if the body still
  looks clean, which a grep for `message` in the file would not reliably catch.

- **`falta-no-gate:`** **A future change that routes the retry decision from a *backend
  error string*.** Everything in this lane is a pure function of
  `(stage, firstOutputArrived)`, so a backend that starts returning
  `{"error": "no response"}` and a caller that does
  `previewDeadlineRetry(err.error.includes('no response') ? 'session' : 'playback', ...)`
  passes **every** gate in this report — tsc clean, 23/23 green, arity intact — while
  destroying the law, because the message re-enters as an INPUT. No gate here crosses the
  process boundary, and the backend is explicitly another lane's code, so no test in this
  lane can see it. The scenario a future change crosses is the HTTP seam, not this file.

- **`gate-melhor:`** **Make the deadline decision refuse an error string at the type
  level**, and assert it from the *caller* side rather than the callee. Concretely: type
  `previewDeadlineVerdict`'s input as `{ stage: PreviewRetryStage; firstOutputArrived:
  boolean }` with no index signature and no `unknown`/`string` member (it already is, at
  `src/previewRetry.ts:231`), then add one test per consuming call site asserting the
  call is driven by `guard.createResolved` and never by `err`/`catch (e)`. A lint rule
  banning `.includes(`/`.startsWith(` on any value inside a `catch` block in `src/` would
  be mechanical and would catch the `err.name === 'AbortError'` shape at
  `src/App.tsx:2027` that this lane could only document.
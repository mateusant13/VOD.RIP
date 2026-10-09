# LANE A — latency scout (READ-ONLY seat). Report.

- **Lane:** SA-vodrip-laneA-latency
- **Seat:** scout — READ-ONLY. No source file, test, or config was modified. No commit,
  no `git add`, no `git checkout`, no `git reset`.
- **Workspace:** `C:\Users\Administrador\Desktop\Nova pasta (3)\TESTE\VOD.RIP`
- **Written:** 2026-10-06 (local, UTC-3)
- **Owner prompt under test:** id=192509, 2026-10-06T09:45:43Z
  ("3 canais AO MESMO TEMPO, todos play ≤ 5s, todos com legenda, YouTube sem ASR local")

---

## VERDICT

| # | claim | verdict | reason (refutable) |
|---|---|---|---|
| 1 | **START-UP LATENCY** — 3 channels simultaneously, each ≤ 5000 ms to play | **UNMEASURED** | The VOD.RIP API is not running. No listener exists on the documented port `127.0.0.1:7897`, and a full `Get-NetTCPConnection -State Listen` enumeration contains no VOD.RIP process at all. I ran no probe because there is no server to probe. **The refusal: any latency number published from this seat would be fabricated.** |
| 2 | **REAL-TIME CAPTIONS** on all three | **UNMEASURED** (mechanism verified, outcome not) | The code path exists for all three platforms, but "has captions" is a runtime property of an ASR model + network + media decode. The API is down, so no caption was observed. What I *can* refute on the owner: claim 2 and claim 1 are **not independent** — see the ASR sidecar gate in SHARED GATES, which couples the caption path of all three platforms. |
| 3 | **YOUTUBE EXCEPTION** — no local ASR for YouTube; prefer YouTube's own captions | **PASS (in code)** | The backend refuses YouTube on the ASR endpoint and routes it to YouTube's native caption track. Verified on BOTH sides of the wire — backend `subtitles.py:98` + `:553`, frontend `PreviewChatPanel.tsx:330`. Caveat that keeps this from being an unqualified PASS: the *live-stream* caption path (`live_captions.py`) still runs local parakeet ASR for YouTube. See §6. |

**The headline for the owner, stated as a falsifiable claim:** the 5-second budget is not
at risk from a slow server. It is at risk from **three shared serialisation points that
make "3 channels at once" degrade into "one channel, three times slower"**, one of which
(the ASR sidecar) is a single process behind a single lock. Details and `file:line` in
§3. These are code facts, independent of the backend being down.

---

## POPULATION AND WINDOW

Every number this report publishes, with its population, window, and exact command.

### P1 — API liveness probe (population: 6 ports, one moment)

- **What:** TCP reachability of the documented API port and adjacent candidates.
- **n = 6 ports** (`7897, 8000, 5173, 5174, 3000, 7860`), **1 window = ~1 s**, 2026-10-06.
- **Command:**

```powershell
foreach ($p in 7897,8000,5173,5174,3000,7860) {
  $r = Test-NetConnection -ComputerName 127.0.0.1 -Port $p -WarningAction SilentlyContinue
  "port {0,-6} tcpTestSucceeded={1}" -f $p, $r.TcpTestSucceeded
}
```

- **Result:** `7897=False`, `8000=True`, `5173=False` (IPv4), `5174=False`, `3000=False`,
  `7860=False`.

### P2 — Full listener enumeration (population: every listening socket on the host)

- **What:** the authoritative liveness check — not a port guess. A port probe can only
  say "nothing answered on 7897"; this says whether ANY process is serving the app.
- **n = 40+ sockets**, **1 window = ~1 s**, 2026-10-06.
- **Command:**

```powershell
Get-NetTCPConnection -State Listen | Where-Object { $_.OwningProcess -gt 1000 } |
  Sort-Object LocalPort | ForEach-Object { $pr=(Get-Process -Id $_.OwningProcess).ProcessName; "{0}:{1,-6} {2}" -f $_.LocalAddress,$_.LocalPort,$pr }
```

- **Result — no VOD.RIP API listener anywhere.** Relevant entries only:
  - `0.0.0.0:8000  svchost` → identified as `iphlpsvc` ("Auxiliar de IP"), a Windows
    service. **This is NOT our API and must never be reported as one.**
  - `::1:5173  node` → Vite dev server (`node_modules/.bin/vite`), frontend only.
  - `127.0.0.1:8010  pythonw` → **BrandOps** (`I:\!produtos202608\BrandOps\...`), another project.
  - No `uvicorn`/`python` process anywhere serves this repo's API.
- **Corroborating process list:** `backend/worker_server.py` (pid 28660) and
  `services.archive_transcribe --once` (pid 2296) ARE running from this repo — so the
  app is *partially* alive (background worker + transcription daemon) while the **API
  server is down**. That distinction matters: "the app is running" is not evidence the
  preview path answers.

### P3 — Prior measurement, QUOTED NOT RE-MEASURED (population: 40 attempts, 240 min)

- **Source:** `STATE.md` §1, verbatim. **I did not reproduce these numbers and do not
  restate them as current.**
- Window: the 240 minutes ending `2026-10-05T05:31:11Z`. Command:

```powershell
python scripts/observability/probe.py --report-only --window-minutes 240 `
  --jsonl "<repo>/tmp/liveness.jsonl" --verdict tmp/draft-window.json
```

- Prior result: `POST /api/preview/session`, 3000 ms budget, **n=40 attempts, 20 answered**,
  p50 16 ms, p95 106 ms, max 922 ms, 0 over budget → PASS.
- **The reason I refuse to use this as a speed result, and it is the point of §4 of my
  brief:** **20 of 40 attempts never reached the endpoint at all.** They are `not_measured`,
  excluded from the percentiles. So "p95 = 106 ms" describes *half the population* — the
  half that got through. A PASS computed on the arrived subset while half the requests
  never arrived is a reachability defect wearing a latency costume. Per my own standing
  rule (never quote a count without naming its file and population), the correct reading
  of that row is: **the endpoint lost half its traffic, and the latency of the lost half is
  unknown, not zero.**

### P4 — The numbers I did NOT measure

**`UNMEASURED` for every latency figure the owner asked for**: per-channel time-to-play,
the worst-of-three under contention, p50/p95/max for the concurrent 3-platform probe, and
the Twitch/Kick/YouTube breakdown. Population: **n = 0**. Reason: **P2 — no listener.**
`UNMEASURED` is not a failure; it is the only honest answer from this seat.

---

## SHARED GATES

Every serialisation point found on the preview start-up path. This is the heart of the
brief: *"3 channels at the same time" degrading into "one channel three times slower."*
I found **three real gates. Two of them apply to all three platforms at once.**

### Searched for, and NOT found (absence is a claim, so here is its population)

I searched the whole `backend/` tree (`*.py`) for: `Semaphore`, `asyncio.Lock`,
`threading.Lock`, `BoundedSemaphore`, `MAX_CONCURRENT`, `CONCURRENTLY`, `ThreadPool`,
`_pool`, `global `. **~60 matches**, then narrowed to the preview path specifically
(`backend/routers/preview.py`, `backend/routers/previews.py`, `backend/services/instant_preview.py`,
`backend/services/preview/**`).

Explicitly **not found** on the preview start-up path:

| searched for | result |
|---|---|
| A per-platform semaphore in `create_session` | **NOT FOUND** — `preview.py` holds exactly one `asyncio.Semaphore(min(6, len(urls)))` at `preview.py:357`, and it is in **`POST /api/preview/warm/batch`** (background batch warm), NOT in the user's session-create path. Not a gate on the click. |
| A rate limiter in `preview.py` / `previews.py` | **NOT FOUND** — no `rate_limit`/`throttle` token in either router. |
| A shared session-store lock in the create path | **NOT FOUND** in `preview.py`. The archive-DB global lock that once blocked the response for 43 s is **already fixed** (see Gate 3 note). |
| A semaphore limiting concurrent `create_session` | **NOT FOUND** — the pool is bounded at 12 workers, which is > 3, so the pool is *not* the constraint at n=3. |

The locks I did find in `backend/services/archive_db.py`, `archive_embed.py`,
`cookie_bridge.py`, `channels.py`, `archive.py`, etc. are **off the preview start-up path**
(archive search/index, cookie store, channel backfill). I am explicitly not claiming them
as gates.

### GATE 1 — `_YTDLP_LOCK`: a global mutex, one YouTube request at a time. **CONFIRMED.**

`backend/services/ytdlp_guard.py:122-123`

```python
_YTDLP_LOCK = threading.Lock()
_YTDLP_CHANNEL_LOCK = threading.Lock()
```

And every construction of `YoutubeDL` goes through it — `ytdlp_guard.py:541-542`:

```python
with priority_lock.acquire(kind=kind):
with _YTDLP_LOCK:
```

`guarded_youtube_dl` is documented as *"Only supported way to construct YoutubeDL — one
instance at a time"* (`ytdlp_guard.py:518`). So **all** YouTube metadata/resolve/download
work in the process serialises through one mutex. Three YouTube previews at once = one at a
time. **This is a real answer to the brief's central suspicion.**

Mitigation already present, and it is a good one: `_PriorityGate` (`ytdlp_guard.py:50-116`)
grants the gate to the *earliest-waiting interactive* caller, and the preview resolve is
marked `kind="interactive"` (`ytdlp_guard.py:529-530`). A user's click outranks a background
channel-list walk. So a preview is not starved by *background* work — but two previews are
still serialised against **each other**, and the gate explicitly never preempts or times
out a holder (`ytdlp_guard.py:33-44`).

### GATE 2 — `_PriorityGate` + the learned YouTube rate ceiling. **CONFIRMED, and quantified.**

`backend/services/ytdlp_guard.py:62-64`

```python
self._cond = threading.Condition(threading.Lock())
self._busy = False
self._waiters: list = []
```

The gate is **one-at-a-time by construction** (`_busy` is a single bool). It admits the
best waiter, then blocks everyone else.

Underneath it sits a per-platform token budget, `backend/services/rate_budget.py:130-134`:

```python
_PLATFORM_CEILING_RPM: Dict[str, float] = {
"youtube": 20.0,
"twitch": 30.0,
"kick": 20.0,
}
```

with a floor of 4.0 rpm (`rate_budget.py:136`), a learned ceiling that can be *lowered* by
observed rate-limit events (`:598-600` — an event may only lower, never raise), and a
background caller told to wait at most `MAX_AUTO_WAIT_S = 30.0` seconds (`:160`).

**The number that matters is in this file's own comment** (`ytdlp_guard.py:26-28`), quoting
this project's log:

> `rate_budget: pacing yt-dlp yt_channel_list for 21.2s` … and in the same window a real
> preview session measured **`server_ms=22875`**.

**22.875 seconds for one preview resolve** — recorded in this repo, in this file's header,
as the reason the priority gate exists. Against the owner's **5000 ms** budget, that is
**4.6× over, on a single channel.** This is the single strongest piece of evidence I
found, and it is *historical and unverified by me* — see §6. It is the reason I believe the
5 s target will not be met on YouTube cold, even before any three-way contention.

### GATE 3 — The preview ASR sidecar: ONE process, ONE lock, all three platforms. **CONFIRMED. This is the claim-1/claim-2 coupling.**

`backend/services/preview_asr_sidecar.py:43` and `:115`

```python
self._lock = threading.Lock()          # :43
...
with self._lock:                        # :115, inside request()
```

The module holds exactly one sidecar instance — `preview_asr_sidecar.py:140`:

```python
_sidecar = _Sidecar()
```

and its own docstring states the design decision plainly (`:10-14`):

> **Concurrency: requests are serialised with a lock.** Two previews decoding at once would
> contend for the same CUDA context, and the measured crash shows this stack is not
> forgiving about concurrent native use.

**Consequence for the owner's ask:** the Twitch and Kick preview captions go through this
**one lock**. Three channels open at once ⇒ captions are produced **one after another**.
The claim "all three must have real-time captions" and the claim "all three must play
within 5 s" are therefore **not independent claims** — they contend for one resource. If
the owner believes these are three separate tests, that belief is wrong, and it is the
most actionable thing in this report.

The worker side already anticipated three-at-once and made it *safe* (`preview_asr_worker.py:49-55`:
correlation-by-`id` exists *"because it is a precondition for running three channels at
once (the owner's ask)"*). **Safe, yes — but still serialised.** The worker was built for
concurrency; the supervisor deliberately prevents it.

### GATE 4 (NOT a gate — recorded so it is not mistaken for one)

`backend/services/instant_preview.py:34` `_worker_lock = threading.Lock()`, and
`deps.py:99` `WARM_WORK_SEMAPHORE = threading.BoundedSemaphore(8)`.

Both are real locks, but neither sits on the user's click:
- `instant_preview.py` is the **background** channel-preview refresher ("the archive
  scheduler kicks a background pass" — `instant_preview.py:2`), one daemon thread
  (`:33`, `:15-16`).
- `WARM_WORK_SEMAPHORE` caps *warm* work at 8, and `deps.py:97-98` states plainly:
  *"User-click paths (create_session / PREVIEW_EXECUTOR / live paths) **never acquire it** —
  clicks stay responsive while the warm queue drains."*

I checked both and am reporting them as **NOT** contributors, with the quotes.

### What is NOT a gate (searched, population shown)

`PREVIEW_EXECUTOR = ThreadPoolExecutor(max_workers=12, ...)` (`deps.py:37`). At n=3
concurrent previews this pool has **12 workers for 3 jobs** — it is not the constraint. It
would only bind at >12 simultaneous creates. This is the one thing the brief's worry does
*not* support, and I am recording it so the owner does not spend effort there.

---

## TIMEOUT CEILINGS

Current values, with `file:line`. **Every one of these is far above the owner's 5000 ms
budget — by 4× to 15×.** That asymmetry is the finding: the guards are built to eventually
surface a failure, not to protect a 5-second promise.

| ceiling | value | `file:line` |
|---|---|---|
| `PREVIEW_START_TIMEOUT_MS` (hard start-phase guard) | **60 000 ms (60 s)** | `src/previewStartTimeout.ts:19` |
| `PREVIEW_FIRST_OUTPUT_CEILING_MS.session` | **45 000 ms (45 s)** | `src/previewRetry.ts:85` |
| `PREVIEW_FIRST_OUTPUT_CEILING_MS.playback` | **20 000 ms (20 s)** | `src/previewRetry.ts:86` |
| `PREVIEW_STAGE_TOTAL_CEILING_MS.session` | **75 000 ms (75 s)** | `src/previewRetry.ts:100` |
| `PREVIEW_STAGE_TOTAL_CEILING_MS.playback` | **60 000 ms (60 s)** | `src/previewRetry.ts:101` |
| Backend create hard timeout `_PREVIEW_CREATE_HARD_TIMEOUT_SEC` | **45 s** (env `VODRIP_PREVIEW_CREATE_TIMEOUT_SEC`, default 45) | `backend/routers/preview.py:71`, enforced `:534` |
| Panel hard timeout `_PANEL_HARD_TIMEOUT_SEC` | (panel path; separate budget) | `backend/routers/preview.py:278, :293` |
| Sidecar startup timeout `_STARTUP_TIMEOUT_S` | **180 s** | `backend/services/preview_asr_sidecar.py:34` |
| Sidecar per-request timeout `_REQUEST_TIMEOUT_S` | **240 s** | `backend/services/preview_asr_sidecar.py:35` |
| ASR first-output ceiling `FIRST_OUTPUT_DEFAULT_S` | **4.0 s** | `backend/services/preview_asr_worker.py:107` |
| ASR total ceiling | **20 s** | `backend/services/preview_asr_worker.py:109-111` |
| ASR head default / max | **120 s / 180 s** | `backend/services/preview_asr_worker.py:94-95` |
| rate_budget max background wait | **30 s** | `backend/services/rate_budget.py:160` |
| probe.py preview budget (measurement instrument, not a UI guard) | **3000 ms** | `scripts/observability/probe.py:37-38` |

**The two that decide what the user sees for the owner's prompt id=192486 message**
("A pré-visualização demorou demais tente novamente"):

- `src/previewRetry.ts:170-177` `previewDeadlineMessageKey()` returns an **i18n key**, one
  per outcome:
  - `stalled` → `'No response from server'`
  - `timeout` → `'Preview took too long — try again'`

  So the "took too long" message is **the `timeout` branch** — output arrived, stage never
  finished. The *silence* case (`stalled`) is deliberately a **different** message. This is
  a deliberate, documented design (`previewRetry.ts:10-24`: *"A message is an output, never
  an input"*) and it is correct. I could not find a message string being matched on, which
  is the defect this module exists to prevent.

- The retry decision: `previewDeadlineRetry` (`previewRetry.ts:190-195`) — `session` +
  `stalled` → `escalate-full`; everything else → `retry-stage`. Pure function of
  `(stage, outcome)`, no string input. Sound.

**The important gap:** the frontend's `playback` first-output ceiling is **20 s** and its
own comment admits it is unbacked (`previewRetry.ts:79-82`):

> `playback` … **UNMEASURED — no healthy-path sample backs it. It is a proposal, and it is
> the one number here that a measurement should replace.**

**A 20 s ceiling cannot defend a 5 s promise.** If the owner wants play ≤ 5 s enforced by
the app rather than merely desired, `PREVIEW_START_TIMEOUT_MS` (60 s) and the `playback`
first-output ceiling (20 s) are the two constants that would have to move, and moving them
is a product decision (it converts slow loads into visible retry prompts sooner), not a
silent refactor.

---

## PROPOSED FIX

**NOT APPLIED.** This seat is read-only; every block below is a proposal for the owner or
the owning lane. Named file + exact function for each.

### P-1 (highest value, smallest diff) — Make the two claims separable in the measurement

**Problem:** `probe.py` currently has no 3-platform concurrent mode, so "3 at once" has
never been sampled as a population. `scripts/observability/probe.py` measures
`take_health` and a single `POST /api/preview/session` with one real YouTube id.

**Proposed:** add a `--concurrent-platforms` mode to `probe.py` that fires one create per
platform (YouTube / Twitch / Kick) from one barrier, records each latency, and reports
**the worst-of-three** as the headline — plus a separate **arrived/attempted** count, so a
reachability failure can never again hide inside a percentile.

```diff
--- a/scripts/observability/probe.py
+++ b/scripts/observability/probe.py
@@ def take_health(api, budget_ms, timeout_s, ident)
+def take_concurrent_previews(api, budget_ms, timeout_s, ident) -> dict:
+    """Fire one create per platform from a single barrier.
+
+    Reports the WORST of the three (the owner's number), not the best, and
+    separates `attempted` from `arrived` so a request that never reached the
+    endpoint is `not_measured` rather than silently excluded from the
+    percentiles — the defect STATE.md §1 §'20 of 40 never reached the endpoint'
+    made invisible.
+    """
+    urls = {p: _preview_url_for(p) for p in ("youtube", "twitch", "kick")}
+    barrier = threading.Barrier(len(urls))
+    results = {}
+    def one(platform, url):
+        barrier.wait()          # simultaneous open, per the owner's ask
+        results[platform] = timed_post(f"{api}/api/preview/session", {"url": url}, timeout_s)
+    threads = [threading.Thread(target=one, args=(p, u)) for p, u in urls.items()]
+    for t in threads: t.start()
+    for t in threads: t.join()
+    # arrived vs attempted is reported as its OWN row; never folded into p95.
+    return sample("preview_3platform_concurrent", results, budget_ms, ident)
```

*Owner decision required:* which three concrete VOD URLs to pin as fixtures. They must be
long-lived; a dead fixture reads as a slow platform.

### P-2 — Give the ASR sidecar a bounded queue with an explicit "busy" answer

**Problem:** Gate 3. Three Twitch/Kick previews queue behind one lock with no admission
signal, so the 2nd and 3rd pay the full 1st's decode with nothing to show for it.

**File/function:** `backend/services/preview_asr_sidecar.py` → `_Sidecar.request`
(currently `with self._lock:` at `:115`).

```diff
--- a/backend/services/preview_asr_sidecar.py
+++ b/backend/services/preview_asr_sidecar.py
@@ class _Sidecar:
+    #: Max seconds a caller waits for the single decode slot before being told
+    #: the answer is "busy" instead of hanging. Sized against the owner's 5 s
+    #: play budget: a caller that would be served after the user has already
+    #: seen the video is better served by the cache/panel path on the next tick.
+    _QUEUE_WAIT_S = float(os.environ.get("VODRIP_PREVIEW_ASR_QUEUE_WAIT_S", "4.0"))
+
     def request(self, payload: dict, *, timeout: float = _REQUEST_TIMEOUT_S) -> dict:
-        with self._lock:
+        if not self._lock.acquire(timeout=self._QUEUE_WAIT_S):
+            # Honest answer, not silence: the caller renders "captions still
+            # decoding" and re-polls, instead of blocking a request that no UI
+            # timeout shorter than the 240 s request bound is watching.
+            raise SidecarBusy("preview ASR slot busy for %.1fs" % self._QUEUE_WAIT_S)
+        try:
             last: Exception | None = None
             ...
+        finally:
+            self._lock.release()
```

and a new `class SidecarBusy(RuntimeError)` next to `SidecarUnavailable`
(`preview_asr_sidecar.py:152`), handled in `subtitles.py::_fetch_preview_asr`
(`subtitles.py:581`) as a **retryable** outcome — *not* cached as
`SidecarPermanent`, because a busy slot is not a settled fact about the VOD.

**This does not make captions concurrent** — the GPU cannot. It converts an invisible
multi-second stall into an honest, pollable state. If the owner wants genuine concurrency
instead, that is a **different fix** (N sidecars, `N>1`), and it trades directly against
the CUDA-crash history recorded at `preview_asr_sidecar.py:10-14` and `subtitles.py:528-534`.
**That trade is the owner's call, not mine.**

### P-3 — Say out loud that YouTube is serialised, in the code that enforces it

**Problem:** `_YTDLP_LOCK` is a correct, deliberate design. The risk is not the lock; it is
that no reader of `preview.py` learns from it that two YouTube previews serialise.

**File/function:** `backend/routers/preview.py` → `preview_create_session` (`:498`), or
`ytdlp_guard.py:518`.

```diff
--- a/backend/services/ytdlp_guard.py
+++ b/backend/services/ytdlp_guard.py
@@ def guarded_youtube_dl(opts, kind="background")
-    """Only supported way to construct YoutubeDL — one instance at a time.
+    """Only supported way to construct YoutubeDL — one instance at a time.
+
+    CONSEQUENCE FOR THE OWNER'S "3 CHANNELS AT ONCE" ASK: this mutex is
+    process-global and UNCONDITIONAL, so N simultaneous YouTube previews
+    serialise into one, while Twitch and Kick (no yt-dlp) do not. The
+    _PriorityGate above orders *who* waits, never *how many* wait at once —
+    it cannot grant this lock to two callers. Any 5 s concurrent-launch target
+    for YouTube must account for N x (single resolve), not 1 x resolve.
+    Measured in this repo's own history: server_ms=22875 for ONE preview
+    resolve (see header comment), i.e. ~4.6x the 5000 ms budget before any
+    contention at all.
     """
```

### P-4 — Re-measure, then replace the one number its own comment calls a guess

**File:** `src/previewRetry.ts:79-87`. `PREVIEW_FIRST_OUTPUT_CEILING_MS.playback = 20_000`
is self-documented as *"UNMEASURED — no healthy-path sample backs it … the one number here
that a measurement should replace."*

**Do not** lower it to 5000 to match the owner's ask. Lowering a ceiling does not make the
app faster; it makes the app *report failure sooner* on a path nobody has sampled. The
honest order is: run P-1's probe with a live API → get a real healthy-path distribution →
*then* set the ceiling. Until then the constant stays 20 s and is labelled unmeasured,
which is exactly what its comment already says.

---

## WHAT WAS NOT VERIFIED

Everything below is a real limit on this report. None of it is a hedge for its own sake.

1. **All latency numbers — the core ask — `UNMEASURED`.** n = 0. The API has no listener
   (P2). I did not start the app: a read-only seat does not own process lifecycle, and
   other lanes have uncommitted work in this tree.
2. **Time-to-play for any of the three platforms.** Unmeasured, same reason. Note the
   distinction: the *server-side* hop (`POST /api/preview/session`) and the *browser*
   time-to-play (HLS manifest → first fragment → first frame → `canplay`) are different
   numbers. The owner's "dar play em 5 segundos" is the **second**. A server probe can only
   ever measure the first, which is why `e2e/tests/preview-stress.spec.ts` (Playwright,
   reads `ready`/`playing`/`error` off real players) exists and why server numbers alone
   can never close this ask.
3. **Whether any of the gates are hit in practice at n=3.** I proved the gates EXIST in
   code. I did **not** measure three concurrent previews contending on them. "Gate exists"
   and "gate costs the owner X ms" are different claims; only the first is mine.
4. **`server_ms=22875`** is quoted from this repo's own comment
   (`ytdlp_guard.py:28`) and `STATE.md`. I did not reproduce it. It is historical and
   unverified by me.
5. **The YouTube-exception PASS is a CODE claim.** I verified the routing decision on both
   sides of the wire. I did **not** run a YouTube preview and confirm that native captions
   actually appear — the API is down.
6. **Live-stream YouTube captions were not audited to the same depth.** `live_captions.py`
   plainly runs local ASR for all three platforms including YouTube
   (`live_captions.py:48`, `:315-331`); I did not trace every live path.
7. **No test suite was run** (not my seat; other lanes are mid-edit in this tree).
8. **No secret was read or printed.** Had I found one, the report would carry
   `file:line` + variable name only.

---

## SELF-AUDIT

**1. Protocols missing.** Two, and I name them rather than hide them:
- I did **not** read `backend/services/preview/session.py` end to end. It is large and I
  was working against a hard clock. I read its imports, its YouTube warm/lock surface
  (`_YOUTUBE_WARM_LOCK`, `_YOUTUBE_WARM_INFLIGHT`, `_build_and_cache_youtube_snapshot`)
  and the constants. **A per-key warm-lock on the YouTube resolve is a plausible additional
  gate I did not fully chase** — if the owner wants it closed, that file is the next read.
- I did not read `previewPlayerUtils.ts` (69 KB) or `usePreviewPlayer.ts`. The frontend
  hop list in the brief's §1 is therefore **incomplete on the client side**: I mapped the
  server path and the timeout/retry layer, not the player attach logic.

**2. Extra verification I did beyond the brief.**
- Full listener enumeration (P2) instead of a single port probe — because "7897 didn't
  answer" alone cannot distinguish *stopped* from *moved*, and a wrong answer there would
  poison every other number in the report.
- Proved the two decoys are NOT our API before reporting the app as down:
  `:8000` = `iphlpsvc` (a Windows service), `:8010` = **BrandOps**, a *different project*
  on this same box. Reporting either as VOD.RIP would have been the orphan-DB class of
  error this repo has already been bitten by three times.
- Traced the YouTube-exception on **both** sides (backend AND frontend) rather than
  trusting the backend's own constant — the two could disagree, and they don't.
- Searched for gates I expected to find and **did not** (`previews.py` rate limiter,
  per-platform semaphore, session-store lock), and recorded the negatives with the
  searched-for list, because "I found none" is a claim that needs a population too.

**3. New checkboxes (named, mechanical).** Each is a `grep`/`assert` a CI job could run:
- **`CHECK-ASR-SIDECAR-SERIALISED`** — assert `preview_asr_sidecar.py` still holds exactly
  one `_Sidecar` instance. If it becomes a pool, the caption path changed shape and any
  latency claim about it is void.
- **`CHECK-YTDLIP-LOCK-GLOBAL`** — assert `_YTDLP_LOCK` is a bare `threading.Lock()` and
  `guarded_youtube_dl` is the only `YoutubeDL` constructor. Guards the Gate 1 invariant.
- **`CHECK-YOUTUBE-EXCLUDED-FROM-ASR`** — assert `_PREVIEW_ASR_PLATFORMS ==
  ("twitch", "kick")` **and** that the frontend `ASR_SUBTITLE_PLATFORMS` set matches. Two
  sides, one invariant; drift between them is the real failure mode.
- **`CHECK-PROBE-REPORTS-ARRIVED`** — assert probe output carries an `arrived` field
  distinct from `attempts`. Directly prevents a repeat of the "20 of 40 never reached the
  endpoint" invisibility.

**4. Review by another subagent.** **Not done.** I am a single-seat read-only lane; I did
not dispatch a reviewer, and none of my findings has a second pair of eyes. **Every gate in
§3 should be treated as "one scout read the code once."** Given that this repo's own
history is three wrong reports from unverified readings (§2 of this self-audit is precisely
that failure mode), the reviewer's absence is the largest soft spot in this report.

**5. Confidence + what moves it.**
- **HIGH** (code text, quoted with `file:line`): the three gates exist; the ceiling values;
  the YouTube ASR exclusion. These are quotes, not inferences — refutable by opening the file.
- **MEDIUM**: that Gate 1/3 actually *dominate* the owner's 5 s budget at n=3. Rests on
  `server_ms=22875` which I did not reproduce.
- **NIL — no confidence to offer**: any latency figure. I published none.
- **What would move it, in order of cost:** (a) start the API, run P-1 → converts the
  MEDIUM into a number; (b) a second scout re-reads §3; (c) a Playwright run of
  `preview-stress.spec.ts` for the true time-to-play.

**6. What was NOT verified** — see the section above; it is the same list and I am not
re-narrowing it here. The three that matter most: all latencies (`n=0`), gate contention
cost at n=3, and the `server_ms=22875` history.

**7. Gate-doubt.**
- **`verde-de-verdade:`** *"The 5-second budget looks fine — p95 was 106 ms."* This is the
  green that is not. That 106 ms came from **20 of 40 attempts that arrived**; the other 20
  never reached the endpoint. A percentile over the arrived half is a speed claim wearing a
  reachability failure's clothes. Until `attempted` and `arrived` are separate rows, this
  project cannot honestly pass a latency gate.
- **`falta-no-gate:`** *"Three channels opened at the same time."* No existing check opens
  three. `preview-stress.spec.ts` drives **6 previews, all YouTube** (`MAIN_URL` is a single
  YouTube id, `UI_URL` localhost:5173). Nothing in the tree opens one YouTube + one Twitch +
  one Kick together — which is exactly the population the owner's ask names, and exactly
  the population where Gate 1 and Gate 3 bite. A future change can pass every existing
  preview test and still miss the owner's scenario entirely.
- **`gate-melhor:`** a mechanical gate for the above, runnable in CI:
  `grep -q 'twitch' && grep -q 'kick' && grep -q 'youtube' e2e/tests/preview-stress.spec.ts`
  over the *concurrent* spec — i.e. **fail CI if a concurrent preview spec exists that does
  not exercise all three platforms**, and fail if probe output omits `arrived`.

---

*End of LANE A report. Read-only seat: no source file, test, or config was modified; the
only file written by this lane is this report.*
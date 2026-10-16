# Preview latency: what is measured, and where two measurements disagree

Two measurements of the SAME committed code disagree by 5x. Recorded so the
next round does not lose the contradiction, and so neither number is quoted
without the other.

## Measurement A (subagent, 2026-10-14, tmp/ab-bg-queue.mjs, tmp/ab-run3.txt)

  no cap   median 17662 ms   n=3   background in flight at click 36-40
  cap 3    median  3355 ms   n=3   background in flight at click  3-10
  cap 2    median  2184 ms   n=3   background in flight at click  2-3

Paired, one page, one session, Latin-square rotated. Same page for both arms,
so backend drift cannot explain the gap.

## Measurement B (lead, 2026-10-15, tmp/det_lane2.mjs, tmp/det_lane2.txt)

  cap 2 only: 17532, null(45s timeout), 28284, 11081, 10241, 13225, 5460, 6754
  median 11081 ms   n=7 (+1 timeout)   0 of 7 at or under 3000 ms
  livePend at click: 2, 5, 2, 6, 9, 12, 2, 13

## What is the same in both

  * the committed code: LIVE_POLL_MAX_IN_FLIGHT = 2 (App.tsx:442), the lane is
    created at :2298 and the poll wave is routed through it at :2363
  * the browser seeds localStorage vodrip.plugins to all-true and aborts
    /api/plugins, because the preview surfaces are disabled on this box
  * 39 saved channels (measured through GET /api/settings)

## What differs, and it is the likely cause

Measurement B opens CONFIGURACOES then CANAIS before every rep. Opening the
Channels tab is also what fires the incremental channel SYNC wave (39 channels
x videos + clips), and that wave has its own ceiling of 3. A/B measured that
lowering the sync ceiling 3 -> 2 shows no gain, so it was left alone.

Measurement A drives the same page without forcing a fresh tab open per rep,
so the sync wave is mostly settled when the click happens.

CONCLUSION, stated as a hypothesis and not a fact: the 5x gap is probably the
sync wave, not the poll lane. NOT PROVEN - proving it means re-running
measurement B against a build with the sync ceiling at 2, paired, same page.
Until then B is the conservative number for the goal and A is the optimistic
one, and the honest statement is "median between 2.2s and 11.1s".

## What the goal needs from here

A PAIRED run of the sync ceiling, on the same page, with the same tab-open
sequence as measurement B. That is the experiment that separates the two.

## 2026-10-15, round 2 — the proxy had no timeout, and it is fixed

POST /api/preview/session fell into the no-timeout "/api" proxy lane in
vite.config.ts (the same class as the 2026-09-13 CLOSE_WAIT incident: an
upstream that accepts but never answers leaves the proxied fetch pending
FOREVER). The narrow fail-fast lane covered settings/info/health but NOT the
requests that OPEN a preview.

Measured direct vs proxied, same URL, after adding a lane:
  direct  POST /api/preview/session  0.12-0.44 s  (6/6)
  proxied POST /api/preview/session  0.58-1.33 s  (5/5)

So the proxy costs ~0.7 s and is now bounded at 8 s instead of hanging.

## And where the remaining click time actually goes (8 real reps)

  playable  5388  7352  7921  15854  16156  20057  22961  (+1 timeout45s)
  session   2797  1276  1537   2520   4031    5539  6660

The session POST swings 1.3-6.7 s while the same call direct is 0.12-0.44 s.
The difference is the app own background wave: the harness still fires the
39-channel live poll, which holds the browser ~6 per-origin connections.
The poll lane (ceiling 2, commit 5423441) caps the LIVE wave; what is not
capped is the 39-channel INCREMENTAL SYNC, whose ceiling A/B measured 3 to
be the right value (2c04df2) and which this harness re-fires every rep by
opening CONFIGURACOES -> CANAIS.

HONEST STATE: click -> playable is 5.4-23.0 s in this harness, NOT under 3 s.

## 2026-10-15, round 3 — the answer: the main thread, not the network

Client instrumentation added between the click and the first fetch
(tmp/ab_cli.mjs): a pointerdown/click stamp, a longtask observer, and the gap
from click to the session fetch actually being issued.

  rep  playable  session  click->fetch issued  longtask total
  r0     9447     2321        1601 ms             7082 ms
  r1     5077      437        1397 ms            11641 ms
  r2    12439     3210        1717 ms            15842 ms
  r3     8809      955        1430 ms            21495 ms
  r4     7220     1155        1430 ms            27074 ms
  r5    11241     1701        1368 ms            31500 ms

TWO HARD FACTS.

1. The app takes 1.37-1.72 s (median 1430 ms) after the click before it even
ISSUES the session fetch. No network is involved in that gap - it is pure
client work between the gesture and the first request. That single segment is
roughly half of a 3 s budget.

2. Long tasks accumulate monotonically (7082 -> 31500 ms across six reps) and
the page never settles. The background waves network concurrency was capped
by the lane (5423441), but the work BETWEEN the responses runs on the main
thread: 39 channels x (vods + clips + streams) merge and re-render.

This invalidates the standing hypothesis of the whole session. The queue was
never the problem: every route is fast direct (session 0.13-0.26 s, panel
0.04-0.07 s), the 39-channel /live wave does not slow it (0.13-0.21 s), the
warm/batch does not slow it (0.14-0.25 s), and the server own log agrees
(session_created server_ms 110-219 ms).

THE LEVER for <=3 s is therefore the 1.43 s click-to-fetch, and the cause to
attack is main-thread work in the click path - not connection pooling, not the
backend, not the sync ceiling (already measured as not a lever: 2c04df2).

### The long tasks, sized

Long tasks attributed (tmp/ab_lt.mjs, same harness):
  645 ms @1106   |  606 ms @18838   |  584 ms @2283   |  494 ms @5136

Half-second main-thread blocks. Two or three of those landing inside the
click path is exactly the 1.4-1.8 s click-to-fetch. INSTRUMENT LIMIT, stated
so the next round does not chase it: containerName/containerType come back
"unknown" in this Chromium build, so the tasks are sized but not named. To
name them, the next step is a CDP Profiler trace over the click window, not
another PerformanceObserver pass.

Recurring number across every run this round:
  click -> session fetch issued   1368-1828 ms   (median ~1450 ms)
  longtask total                  5176-5540 ms within 2 reps

## 2026-10-15, round 4 — my measurement was measuring the DEV BUILD

Every browser number this session came from the Vite DEV server. The
production bundle has no `jsxDEV` (verified absent from dist/index.html), and
the difference is the whole story:

                          dev build      production build
  click -> fetch issued   1430 ms        436-460 ms  (~455 ms)
  longtask total          7082-31500 ms  679-2465 ms
  JS in the click window  692 ms         18-40 ms
  top self-hit function   jsxDEV 1007    (idle)

About 1 SECOND of the click-to-fetch and ~20 s of accumulated long tasks were
the instrument, not the product: the React DEV JSX runtime building elements
with validation. b095771 and 25095f2 were real but inflated by a third; the
half-second long tasks were 34x worse in dev than in production.

THE GARGALLO MOVED WITH THE CLIENT. With the client delay gone, the session
POST through the proxy is the dominant leg:

  rep   playable   session(proxied)   inflight at click
  r2     12558 ms      3665 ms              6
  r3     15302 ms      3275 ms              7
  r4     15178 ms      4776 ms             10
  r5      7025 ms      1857 ms              6

The same POST direct is 0.13-0.26 s (6/6) and 0.58-1.33 s through an idle
proxy (5/5). With the app own traffic holding the browser ~6 per-origin
connections - and 6 IS the browser ceiling - the preview request queues.

So the connection-queue hypothesis is back and is now the ONLY remaining
suspect: client cheap (455 ms), server fast (session_created 110-219 ms), and
1.8-4.8 s appears exactly when inflight sits at the 6 ceiling.

MEASUREMENT RULE, expensive kind: a harness against dev measures dev, not the
product. Production is the target from here.

## 2026-10-15, round 5 — the variance is AFTER the session POST, not in it

Two arms on the frozen app, poll and sync identical, only the priority
window differing (tmp/ab_prio.txt):

  OFF (no priority)  median 3917 ms  [846, 862, 3917, 7095, 8197]
  ON                 median 3499 ms  [736, 846, 862, 1027, 2959, 3499,
                                          3917, 7095, 8197, 11149]

The distributions OVERLAP: both arms contain the same fast reps (736-1027 ms)
and the same slow ones (7095-11149 ms). So the priority window does not
separate, and this round does not claim it does.

WHAT THE SAME DATA DOES LOCALISE, and it moves the target again:

  rep  session(proxied)  playable   after-session cost
  r6        160 ms         736 ms        576 ms
  r9        450 ms        1027 ms        577 ms
  r1        266 ms       11149 ms      10883 ms
  r2        795 ms        2959 ms       2164 ms
  r3       1773 ms        3917 ms       2144 ms

The session POST is NOT the variable leg. When it is fast, the preview still
takes 10.9 s - and when it is slow the extra time is small. The 1.0-10.9 s
lives AFTER the session: hls master fetch -> hls.js parse -> first segment
-> canplay. Earlier runs sized those legs individually (master 375-4420 ms,
segment 392-2710 ms), which fits: the whole post-session chain varies by 10x
while the session POST varies by 7x and never dominates.

## 2026-10-15, round 6 — the manifest chain is NOT the problem, and it is measured

Per-leg instrumentation on the frozen app (tmp/ab_hls.mjs, tmp/ab_hls.txt,
15 reps). Queue wait matched BY URL, not by index - index pairing was the bug
that produced the nonsense negative queue numbers in earlier rounds.

  leg       median    max    count   queue wait
  playable   1686    3278      -          -
  session     707    2376      1         null
  master       99     978      1         null
  segment     104     977      1         null
  panel       447    2376      -          -

So the manifest is ONE fetch and the segment is ONE fetch, both fast, and
NEITHER waits for a socket (queue = null for every one of the 15 reps). The
"hls master -> parse -> segment -> canplay chain" that round 5 blamed is not a
chain: there is no cascade, no re-fetch, and no queue. What varies is the
session POST and the panel fetch, and both track the background load:

  inflight at click   playable                          session
  0-4  (n=11)        493-1788 (10/11 under 3s)          140-1299
  5-10 (n=1)         3083                              663
  11-40 (n=3)        2549-3278                         850-2376

The one slow rep inside the low bucket (3120 ms) had session=707 ms but
master=350 ms and segment=404 ms - its own two worst legs, so it is a different
tail, not the same one.

CONCLUSION for the goal: the click path is CHEAP and MEASURED to be cheap. The
cost is entirely how much of the browser 6 sockets the app own background waves
hold when the user clicks. 11 of 15 reps landed under 3 s and 10 of those 11
had inflight <= 4. The remaining work is to keep inflight low at the click,
which is the shared budget (e465b5e) plus the priority window (0bd6a07) -
neither of which has a proved win, and both of which now have the measurement
that says what they are for.

---

## Round 7 — the VODRIP_NO_DAEMONS trap, and the first honest numbers

**Every failing measurement in rounds 100-106 was taken with
`VODRIP_NO_DAEMONS=1` set.** That is a debug flag, and it changes the behaviour
of the server. The user never sets it. So the failures were mine, not the app's.

The arms, same frozen build, same box, same harness, ONE variable:

| arm | warm wave | VODRIP_NO_DAEMONS | result |
|---|---|---|---|
| frozen | on  | **set** | server stops answering; panel 25 s / 11.7 s / 2 s errors |
| frozen | off | **set** | server stable 220 s |
| source | on  | unset | server stable 190 s; panel 0.02 s |
| **frozen** | **on** | **unset** | **server stable 190 s; panel 0.04-0.17 s** |

The warm wave is a hiccup (health drops out at t+20s once, then recovers), not a
killer. The flag is the killer.

### The A/B with six live previews, real conditions

Frozen v1.0.60+panel-fix, SIX previews, `VODRIP_NO_DAEMONS` **unset**, CYCLES=6,
12 reps, two connection-budget arms (sync2 vs sync3):

| rep | playable | outcome |
|---|---|---|
| 0  |  5245 ms | playing |
| 1  | 37728 ms | playing |
| 2  | 46468 ms | playing |
| 3  | 13108 ms | playing |
| 4  |  1537 ms | playing |
| 5  |  2490 ms | playing |
| 6  |  2182 ms | playing |
| 7  |  2485 ms | playing |
| 8  |  1550 ms | playing |
| 9  |  1474 ms | playing |
| 10 |  2485 ms | playing |
| 11 |  1847 ms | playing |

**12 of 12 played**, against **11 of 11 timeouts** in the same harness with the
flag set. After the first-load reps (0-3, which include session, player and window
warm-up), **8 of 8 consecutive reps are <= 2.49 s**.

Stated plainly rather than smoothed: the first four reps of a cold session are
NOT under 3 s. Reps 1 and 2 are 37.7 s and 46.5 s. That is the first-load cost,
not a steady-state one, and it is the remaining target.

### The rule this leaves behind

**Never measure latency with `VODRIP_NO_DAEMONS=1`.** It is not a lighter app, it
is a different app: the server stops answering under load while the process stays
alive, which looks exactly like an app defect and is not one.


---

## Round 8 — what the four slow reps are actually made of

With `VODRIP_NO_DAEMONS` UNSET (see round 7), 12 reps, six previews, this is the
per-request breakdown the harness prints. Read it as two separate defects, not one:

| rep | playable | session | hls/master.m3u8 |
|---|---|---|---|
| 0 |  5245 ms |  2969 ms |   817 ms |
| 1 | 37728 ms |  5289 ms | **10004 ms** |
| 2 | 46468 ms | 16210 ms | **10004 ms** |
| 3 | 13108 ms | 11822 ms |    17 ms |
| 4 |  1537 ms |   250 ms |     9 ms |
| 5 |  2490 ms |  1029 ms |     6 ms |
| 6 |  2182 ms |   359 ms |   337 ms |
| 7 |  2485 ms |  1365 ms |     9 ms |
| 8 |  1550 ms |   339 ms |     7 ms |
| 9 |  1474 ms |   395 ms |     6 ms |
| 10 | 2485 ms |  1094 ms |    11 ms |
| 11 | 1847 ms |   458 ms |     6 ms |

**Defect A - a cold `session` costs 3-16 s and then drops to 250-1400 ms.** Reps 4
onward are all under 2.5 s total, so steady state is met. The cold cost is the
session POST without a warm resolve.

**Defect B - `hls/master.m3u8` takes EXACTLY 10004 ms, twice, and 6-9 ms every
other time.** Two requests at 10.004 s is not a natural network duration; a round
number that repeats is a timeout or a fixed retry, and it should be found in the
code before anything else is blamed.

Also visible and worth its own look: the `panel/twitch/...` requests in reps 1 and
2 arrive SERIALISED - each starts only after the previous one returns - and run
4923, 8726, 6731, 4505, 2010 ms in rep 1. Six previews each polling the panel is
the connection-pool pressure measured in earlier rounds; with the panel limit now
at 2000 these are still slow, so the limit was not their cost either.

### The remaining target, precisely

1. Find the 10004 ms: grep the HLS master fetch for a 10 s timeout or retry.
2. Warm the session before the first click, or make the first session POST cheap.
3. Everything after rep 3 is already <= 2.49 s across 8 consecutive reps.

### The 10 004 ms is very likely the SQLite busy_timeout — leading hypothesis

Found by grepping for a round 10 s in the services layer:

```python
# backend/services/archive_db.py:527-530
conn = sqlite3.connect(str(path), timeout=10.0, check_same_thread=False)
conn.execute("PRAGMA busy_timeout=10000")
```

and the module documents this exact failure mode at `archive_db.py:658`:

    WAL-busy write holds _lock while sqlite spins busy_timeout (10s) — so ...

So the reading is: the `hls/master.m3u8` request reads `archive.db`, a concurrent
WRITE holds the lock, and the read spins for the FULL `busy_timeout` before either
succeeding or giving up. 10 004 ms is 10 000 ms of waiting plus 4 ms of work, and
it appears exactly twice, which is what a fixed timeout looks like.

NOT PROVEN, and labelled as such: I have not yet shown the write that holds the
lock, nor instrumented the read to confirm it parks in `busy_timeout`. The two
facts on record are (a) the duration is 10 004 ms to the millisecond, and (b) the
only 10 s constant on that path is the busy_timeout. That is a strong lead, not a
measurement.

The same mechanism would explain the cold `session` too, since the session POST
also reads the archive, and it is why the cost collapses to 250-1400 ms once the
startup writers have settled.

### How to confirm or kill it

1. Instrument the master handler to record how long it waits on the DB handle
   versus how long it spends doing work after acquiring it. If the 10 s is in the
   wait, this is confirmed.
2. Find the concurrent writer at that moment. The startup warm wave writes; so do
   the archive daemons. A/B the master fetch with the wave off versus on.
3. If confirmed, the fix is not "lower the timeout" — that converts a slow success
   into a fast failure. It is to stop the reader and the writer contending, or to
   serve the master from cache while a write is in flight.

### Narrowed: a FRESH THREAD first read, during the startup write window

`archive_db.py:653-659` documents this exact mode, in the docstring of
`_ensure_schema_ready`:

    Fast path (no locks): once _schema_ready is True for the current path it is
    never reset while that path stays active ... Without this, every fresh
    thread first read serialises behind _lock - and a WAL-busy write holds _lock
    while sqlite spins busy_timeout (10s) - so even off-loop readers would wedge
    behind it (the dev-backend wedge).

and `_get_read_conn()` states the mitigation: per-thread READ connections, so
"concurrent reads never serialize on _lock and a long read never blocks the write
path".

So the mechanism the code names, and the one the measurement fits, is:

  1. a thread whose FIRST read arrives while `_schema_ready` is not yet True for
     the current path takes `_lock`;
  2. a WAL-busy WRITE holds `_lock` while sqlite spins the 10 s `busy_timeout`;
  3. the read waits the full 10 s, which is what the 10 004 ms samples are.

This fits the shape of the data in a way the earlier version did not: the 10 s
samples appear ONLY in reps 1-2, early, and never again. Once the schema is ready
the fast path skips the lock entirely, so steady state cannot wedge this way. The
cold `session` of 3-16 s is explained by the same first-read cost.

STILL A HYPOTHESIS. It is now a hypothesis with a named code path and a stated
mechanism, which is a different thing from a measurement. What would settle it:

  * instrument the master handler to log whether it took `_lock`, and how long it
    waited, versus the work after acquiring the connection;
  * or log `_schema_ready` and the lock wait at the moment of the slow call.

Either is a small, local change - no rebuild of the bundle needed if measured
against the source build, which is already known to serve the same endpoint in
0.02 s once warm.


---

## Round 9 — the panel lane regressed, and the paired rerun proves it

Commit 7210443 routed the panel poll through the shared budget lane, motivated by
the concurrent probe (six previews open, only one playing in 47 s). The rebuild
carrying it measured, sequential harness, CYCLES=6, NO_DAEMONS unset:

  WITH the lane (7210443):
    sync3 median=13489 ms  vals=[17164,13489,7554,22384,13106,10148]
    sync2 median=12957 ms  vals=[12679,18076,6082,15975,3163,12957]
    reps <= 3 s: 0 of 12. session alone: 1893-13396 ms.

vs the same harness on the build WITHOUT it (round 8: sync3 median 2485 ms,
sync2 median 2490 ms, 8/8 consecutive <= 2.49 s).

CONFESSING THE HOLE BEFORE CLOSING IT: the two runs overlapped a YouTube
extract-failure storm (app.log: 50-70 `extract fail` vs ~300 ok,
`preview_race_exhausted` / `all_fallbacks_exhausted`), so unpaired in time they
could have blamed the storm instead of the lane. The paired repeat was therefore
run: revert the lane (commit 53a4147), rebuild, same harness, same storm.

  REVERTED, same storm:
    sync3 median=1841 ms  vals=[null,2074,1841,2201,1785,1403]   (rep 0: 45 s timeout, first load)
    sync2 median=2517 ms  vals=[16024,4529,2517,2059,1520,1881]  (rep 1: 16 s outlier)
    11 of 12 playing. The healthy pattern is back, immediately.

The storm was present in BOTH runs (measured before and during), so it does not
explain a 5-7x reversal on revert. The regression was the lane, not the
weather. The revert stands and is pushed.

MECHANISM, still labelled hypothesis (never instrumented): the session POST is
raw apiPost, but createPreviewSessionWithRetry() calls beginPreviewPriorityWindow(),
which pulls the shared budget to 1 for the click window. Panel tasks on that same
gate then hold the single priority slot while their fetch runs, and the session
POST - unlaned, browser-FIFO - waits behind it. That would move the bottleneck
from a background flood (unbudgeted raw polls) to a foreground gate (budgeted
shared slot) during the very window reserved for the session. What is proven is
only the paired reversal above.

LESSON REPEATED FROM ROUND 7: the fix that "obviously follows" from a measurement
still needs its own measurement. The lane had a red-armed unit law and still lost
5-7x in the real path, because the unit law tested gating, not the priority
interaction. A law that tests the mechanism you hoped for, rather than the one
that matters, is the dressed-up version of "a test that cannot fail".

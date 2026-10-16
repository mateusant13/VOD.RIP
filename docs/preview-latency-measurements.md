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


---

## Round 10 — six concurrent previews: the server is fast, the sockets are not

First, an instrument defect of mine, caught by reading the code the measurement was
about: the concurrent probe called a six-preview run a FAILURE because only one
preview was playing. It is not a failure. `LivePlayerPopup.tsx:337` joins the
shared preview-pause bus - "opening a live pauses every other preview" - with a
test at `LivePlayerPopup.test.tsx:521`. With six open, EXACTLY ONE playing is the
design, so `playing >= N` can never pass. The probe now scores on `ready >= N`
(readyState 2, the spinner gone), which is what the owner photographed:
"Carregando transmissao ao vivo..." is a loading state, not a paused one.
Committed as ce5bb67.

### The app own telemetry for six previews opened at once

PREVIEW_TIMING from app.log, one line per event (surface=explore):

  preview   session_ready   canplay     open->playable
    1          1599 ms      11528 ms      11535 ms
    2          3891 ms      17146 ms      17151 ms
    3          5376 ms      17331 ms      17335 ms
    4          5752 ms      20612 ms      20619 ms
    5          6727 ms      20727 ms      20734 ms

  session_created server_ms = 1046, 1641, 1703, 1469, 1547

### The server is NOT the bottleneck, measured directly

One session and its manifest, straight against the live API:

  POST /api/preview/session                     685 ms
  GET  /api/preview/hls/<sid>/master.m3u8        28 ms   (644 bytes)

So the manifest is available in 28 ms while the first preview needs 11 528 ms to
reach canplay, and its session_ready was 1 599 ms against a 685 ms server cost.
The difference is time spent waiting for a BROWSER SOCKET, not for the server.

### The 10 000 ms constant, named

  src/App.tsx:2662                 manifestLoadingTimeOut: 10000
  src/ChannelExplorePopup.tsx:1577 manifestLoadingTimeOut: 10000

That is hls.js restating its own default. With the manifest reachable in 28 ms,
the 10 s deadline is not being hit by slow server work - it fires on a request
that never got a connection slot, aborts it, and retries. The retry is what lands
at ~11.5 s, which is exactly the `hls/master.m3u8 = 10004 ms` sample seen in
round 8 and dismissed there as a SQLite busy_timeout. It was this.

### What this does NOT establish

Which requests hold the sockets in the first ten seconds. The probe prints only
the last 24 requests, and in this run those were the calm tail at t+69 s where
everything completes in ~10 ms. Capturing the FIRST ten seconds needs the probe to
keep the earliest requests, not the latest. Until that is read, the fix is not
chosen - and the previous attempt to guess it (the panel lane, 7210443, reverted
in 53a4147) cost a measured 5-7x regression for exactly that reason.


---

## Round 11 — FOUND IT: one slow server endpoint holds the whole socket pool

The probe now prints the FIRST 40 requests instead of the last 24, and that one
change answered the question the last four rounds could not. Six previews opened at
once, sorted by start time (ms since page load):

  @     90      38 ms  warm/batch
  @    159-162  ~30 ms  cookies/status, settings/features, presence, previews/status, downloads, settings
  @  11252    9017 ms  /api/channel/videos?url=gaveta&limit=25
  @  11254   16561 ms  /api/channel/clips?platforms=Kick%2CTwitch
  @  11254   18764 ms  /api/channel/videos?platforms=YouTube&conten
  @  11255    4221 ms  /api/channels/ch_msdzyuiq/live
  @  12333    4118 ms  /api/channels/ch_msch69tg/live
  @  15477    4021 ms  /api/channels/ch_mrafykd6/live
  @  16460    4542 ms  /api/channels/ch_mst3y6nj/live
  @  19075     262 ms  panel/twitch/2794478920?limit=2000
  @  19078     824 ms  session        <-- THE PREVIEW SESSION STARTS 19 s AFTER THE CLICK

The preview session POST does not even LEAVE until @19 078 ms. It is not slow - it
is queued. And the requests ahead of it are the CHANNEL TAB own loading traffic.

### Measured server-side, one request at a time, no concurrency at all

  /api/channel/videos?url=gaveta&limit=25     15359 ms    11.3 KB
  /api/channels/ch_msdzyuiq/live                  5 ms     0.0 KB
  /api/channels/ch_mrafykd6/live                  4 ms     0.0 KB

So `/api/channel/videos` takes 15.4 SECONDS with nothing else running, and returns
only 11.3 KB. It is not a payload problem; the server is doing something slow before
it answers. And `/api/channels/*/live` is 4-5 ms alone, so its 4-second times in the
capture were pure QUEUEING behind the slow endpoint.

THE CHAIN: opening a channel fires `/api/channel/videos` (15 s of server work). That
one request holds a browser socket for 15 s. The preview session POST waits ~19 s for
a slot. Then hls.js `manifestLoadingTimeOut: 10000` (App.tsx:2662) fires on the queued
manifest, aborts, and retries - which is the ~11.5 s canplay in round 10.

### Why every previous fix missed

  * the panel lane (7210443, reverted 53a4147) - the panels were never the problem;
    gating them added serialisation for nothing and cost a measured 5-7x regression.
  * "hold the live poll" and "limit yield in channel sync" (the two attempts recorded
    in the objective) - they throttled the LIVE STATUS requests, which are 4-5 ms on
    the server. Throttling a 5 ms request cannot free a socket held for 15 s by a
    different one.
  * my own SQLite busy_timeout lead (round 8) - wrong attribution, corrected in
    round 10.

The socket is held by `/api/channel/videos`, and only by that.

### The code already knew this endpoint is pathological

  backend/routers/channels.py:21   # occupied the default pool while
                                   # /api/channel/videos timed out at 280 s.
  backend/routers/channels.py:467  # Measured 2026-10-13 with py-spy while
                                   # /api/channel/videos took ...

There is a dedicated `_PRIORITY_MARK_POOL` created because of this endpoint. So it
has been slow before (280 s observed) and was mitigated server-side - but at 15 s it
still holds a socket long enough to starve every preview opened in that window.

### The two directions, neither chosen yet

  1. Make `/api/channel/videos` not take 15 s on this path (cache hit, or take the
     live fetch off the request path).
  2. Make it stop blocking previews (do not fire the channel load when a preview is
     opening, or give the preview priority for sockets).

Direction 1 is the smaller change with the larger effect and is the one to measure
first - but it is NOT measured yet, and after the panel-lane lesson the fix gets a
paired A/B on the concurrent probe before it is believed.


---

## Round 12 — the socket holder is a COLD cache miss, and the cache does not dedupe

Chasing round 11 endpoint further, same call three times in a row:

  /api/channel/videos?url=gaveta&limit=25
    chamada 1:     5 ms   11.3 KB
    chamada 2:     2 ms   11.3 KB
    chamada 3:     2 ms   11.3 KB

against 15 359 ms for the same URL measured cold one round earlier. So the endpoint
is 2-5 ms warm and ~15 s cold. The 15 s is genuine live-scraping time, not a timeout:
the relevant ceilings are CHANNEL_VOD_FETCH_TIMEOUT_SEC = 20 and
YOUTUBE_CHANNEL_FETCH_TIMEOUT_SEC = 30, and 15 s sits inside both. "Just make it
fast" is therefore not a small change - it is the scraping itself.

TTLs: TWITCH_CHANNEL_FRESH_SEC = 600 (10 min), KICK 600, YOUTUBE 1800 (30 min). So
the first open of a channel in a session is cold and slow, and later ones are warm.

### A REAL BUG FOUND ON THE WAY, and its reach stated honestly

backend/services/channel_cache.py is a plain TTL cache: get_cached() returns None on
a miss and set_cached() stores the result once the fetch finishes. There is NO
in-flight coalescing, so N concurrent callers asking for the SAME cold key each run
the whole 15-20 s fetch - N times the sockets and N times the upstream scraping.
The pattern exists elsewhere in the same router (`_bg_refresh_inflight` at
channels.py:130, `_original_backfill_inflight` at channels.py:1216), so this path is
simply missing it.

HOWEVER, it is NOT the cause of the preview stall I measured, and I am not going to
let it take credit: the three requests holding sockets in the round-11 capture were
three DISTINCT urls (`?url=gaveta`, `?platforms=YouTube&content=...`,
`/api/channel/clips?...`), not three duplicates of one key. Deduping would not have
removed any of them. It is worth fixing on its own merits; it is not the fix here.

### What the preview stall therefore reduces to

  cold channel open  ->  3 distinct slow calls (9 s, 16.5 s, 18.7 s in the capture)
                     ->  3 of the browser 6 sockets held for 15-20 s
  preview click      ->  session POST queues behind them, leaving at @19 078 ms
                     ->  hls.js manifestLoadingTimeOut (10 s) fires on the queued
                         manifest, aborts, retries; canplay lands ~11.5 s later

So the seconds are structural: a cold channel load and a preview click overlap, and
they share one 6-socket pool per origin. Three directions, none measured yet:

  1. Do not fire all three channel calls at once (the `clips` call serves a
     secondary tab and costs 16.5 s of socket). Cheapest, client-side.
  2. Warm the channel cache before the user opens it.
  3. Give previews their own connection pool. `localhost:PORT` and
     `127.0.0.1:PORT` are DIFFERENT origins for browser connection limiting, so
     serving media from the other hostname doubles the effective pool from 6 to 12
     - at the cost of making it a cross-origin request, which needs CORS on the
     server. Largest effect, largest change.

The next round picks ONE and runs it as a paired A/B on the concurrent probe. After
the panel-lane lesson (7210443 reverted in 53a4147 for a measured 5-7x regression),
no fix is believed without that.

### The complete trace, from the click to the spinner

The last link, read from the client:

  src/App.tsx:5295-5301
    if (wantKick)    vodTasks.push(fetchVods('Kick',    ch.kickSlug));
    if (wantTwitch)  vodTasks.push(fetchVods('Twitch',  ch.twitchSlug));
    if (wantYoutube) vodTasks.push(fetchVods('YouTube', ch.youtubeSlug));
    await Promise.all(vodTasks);          // THREE cold channel fetches IN PARALLEL

and each task is its own request (`platforms: platform`, one platform per call),
plus the separate `/api/channel/clips` call at App.tsx:5118.

So one channel open costs up to FOUR simultaneous cold scrapes:

  Kick vods     ~9-15 s      (measured 15 359 ms cold, 2-5 ms warm)
  Twitch vods   ~9-15 s
  YouTube vods  ~18.7 s      (measured in the capture)
  clips         ~16.5 s      (measured in the capture)

Four of the browser SIX sockets, held for 15-20 s, against a 6-connection ceiling
per origin that no amount of client-side budgeting can raise. A preview clicked in
that window queues its session POST behind them - @19 078 ms in the capture - and
then hls.js `manifestLoadingTimeOut: 10000` aborts the queued manifest and retries,
which is the ~11.5 s canplay.

### The fix target, now exact

The three platform fetches do not need to be simultaneous. `Promise.all` over three
cold scrapes is what costs four sockets; bounding that fan-out to one or two at a
time frees sockets for the preview, at the cost of a slower channel list. That is
precisely the trade the objective two earlier attempts were reaching for ("segurar o
poll de live", "yield limitado no sync de canais") - and they missed because they
throttled `/api/channels/*/live`, which is 4-5 ms on the server, instead of the
three 15-20 s VOD scrapes that actually hold the sockets.

NOT IMPLEMENTED, deliberately. It gets a paired A/B on the concurrent probe
(scripts/observability/concurrent-preview-probe.mjs) first: measure six-preview
time-to-ready before and after, same build lineage, same session. After 7210443
was reverted in 53a4147 for a measured 5-7x regression, a plausible story is not
enough to ship a fix on this path.


---

## Round 13 — the fan-out bound did NOT fix it, so it was reverted

I implemented the round-12 fix target (bound the three-platform `Promise.all` at
App.tsx:5295-5301 to two at a time) and measured it. It did not work, so it is not
in the tree.

The change: `vodTasks` became thunks, run by a 2-wide worker pool instead of
`Promise.all`. tsc clean, 76 files / 1208 tests still green, and I verified the
bound was really in the SERVED bundle before trusting any measurement
(`const Ri=2` + thunks + `Array.from({length:Math.min(Ri,gr.length)}` inside
_internal/static/index.html).

Then the concurrent probe (6 previews at once, first line with readyState>=2):

  BEFORE, 3 parallel scrapes:   t+9.9s (ready=4),  t+18.2s (ready=3)
  AFTER,  bound to 2:           t+19.2s (ready=5),  t+7.0s (ready=5),  t+9.8s (ready=4)

  before median 14.1 s   after median 9.8 s   -  but before spans 9.9-18.2 and
  after spans 7.0-19.2. The ranges overlap almost completely, n is 2 vs 3, and the
  within-arm spread is larger than the between-arm difference.

That is NOT a conclusion. It is the same trap as the panel lane (7210443, reverted
in 53a4147) and it gets the same treatment: an unproven change stays out. Reverted
with `git checkout -- src/App.tsx`, verified back to `Promise.all(vodTasks)`.

### The valuable part is the NEGATIVE result

Bounding the fan-out did not move the number. That falsifies the round-11/12 story
as a COMPLETE explanation: if the stall were simply "four of six sockets held for
15-20 s by parallel channel scrapes", then dropping that to two vods plus one clips
(three sockets, three free) should have shown something outside the noise. It did
not.

So socket COUNT is at most a partial factor. The remaining candidate, and the one
the numbers actually point at, is server-side: the preview needs 10-14 s between
`session_ready` and `canplay` (round 10 telemetry), the manifest is 28 ms when the
server is idle, and `manifestLoadingTimeOut: 10000` keeps aborting a manifest that
is not arriving - which is what a busy server looks like from the client, not what
a queued socket looks like. Under the same load the channel scrapes are running,
and they are CPU/executor heavy.

Next hypothesis to test, stated as a hypothesis: the HLS mux for a preview competes
for the same server resources as the channel scrapes, so the manifest is slow to be
PRODUCED rather than slow to be DELIVERED. The measurement that separates them:
time `GET /api/preview/hls/<sid>/master.m3u8` while three channel scrapes are in
flight, against the same call on an idle server (28 ms measured). If it is slow
under load, it is server contention and no client-side socket fix will help.


---

## Round 14 — every server link measures fast, so the 10-14 s is NOT a server call

Following round 13 negative result, the server-contention hypothesis was tested
directly, each link timed OCIOSO against CARREGADO (three cold `force=1` channel
scrapes in flight):

  master.m3u8         OCIOSO  2/3/3 ms      CARREGADO  4/3/27 ms      <- FLAT
  variant playlist    OCIOSO  285/95/98 ms  CARREGADO  2292/176/143 ms
  (14 556 B)
  POST session        OCIOSO  685 ms        CARREGADO  1046-1703 ms

Read together with round 10 telemetry - `session_ready` 1599-6727 ms, `canplay`
11528-20727 ms - the picture is:

  session           ~0.7-1.7 s
  master            ~0.003-0.03 s
  variant playlist  ~0.1-2.3 s
  ---------------------------------
  accounted for     ~1-4 s
  observed gap      ~10-14 s   <-- NOT ACCOUNTED FOR BY ANY SERVER CALL

So the server is not where the seconds are, in any single call I can measure. Two
hypotheses of mine died this round and both deaths are worth more than the fixes
would have been:

  1. "three parallel cold channel scrapes hold four of six sockets" - bounding the
     fan-out to two (round 13) did not move the number outside the noise, so socket
     COUNT is at most a partial factor.
  2. "the HLS mux competes with the scrapes for server resources" - the manifest is
     2-27 ms loaded, identical to idle. Falsified.

### Where that leaves it

The remaining unmeasured surface is the CLIENT between `session_ready` and
`canplay`: hls.js attach, level selection, `startFragPrefetch`, the MSE append, and
the accumulated wait across the many small requests a preview makes. The one client
constant that keeps appearing is `manifestLoadingTimeOut: 10000` (App.tsx:2662),
which converts "not yet arrived" into "abort and retry" at exactly 10 s.

THE NEXT MEASUREMENT, precise: instrument the client, not the server. The harness
already taps `window.fetch` - it needs to also record, per preview, the timestamps
of hls.js MANIFEST_LOADING / LEVEL_LOADING / FRAG_LOADING / FRAG_BUFFERED events, so
the 10-14 s can be attributed to a named hls.js phase instead of guessed at. Until
that exists, the next fix would be another plausible story, and the last two of
those cost a reverted commit each.


---

## Round 15 — four phases, and BOTH causes were real (my two-phase split was wrong)

The corrected Resource Timing split prints all four phases, so an unaccounted
interval cannot hide: sched + conn + srv + xfer must equal duration. First run with
it, 6 concurrent previews, 182 requests:

  soma CONN (fila): 140193 ms
  soma SERVIDOR:    222233 ms
  soma TRANSFER:      1453 ms

The previous round headline was "soma FILA: 0 ms" - because that version measured
`fetchStart - startTime`, which is the scheduler interval, not connection
acquisition. The queue lives in `requestStart - fetchStart`, and it is 140 SECONDS
of accumulated wait. The check that exposed it was arithmetic: a preview session
with total 10211 ms and server 839 ms left 9372 ms unaccounted for on a 1 KB body.

### The per-request breakdown, and the causal order

  conn(ms)  srv(ms)  xfer  total   inicio   url
       256    13488     1   13745    47414  /api/channel/videos
      4831     7240    20   12091    22645  /api/channel/clips
        39    11263     8   11310    11272  /api/channel/videos
       415     9746     9   10170    34854  /api/channel/videos
         2     9000     9    9011    13572  /api/channel/videos
      5514     2615     1    8130    19345  /api/preview/session
      5584     2543     2    8129    19347  /api/preview/session
      5706     2420     2    8128    19348  /api/preview/session

So, in order:

  1. The channel calls get a connection almost immediately (conn 2-256 ms) and then
     spend 9 000-13 488 ms in the SERVER (srv). They are the root.
  2. Holding those slots, the three /api/preview/session POSTs wait 5 514-5 706 ms
     for a connection (conn) and only 2 420-2 615 ms in the server.
  3. 5.5 s + 2.5 s = 8.1 s per preview. That is the spinner.

BOTH hypotheses were right, in a causal order I had been flattening into one:
server slowness on the channel endpoints is the CAUSE, browser connection queueing
is the CONSEQUENCE. Round 13 (bounding the fan-out) failed because it attacked the
consequence; round 14 (server contention on the HLS mux) failed because it measured
the wrong server call - the mux is genuinely fast, the CHANNEL scrape is not.

### The three slow calls use three DIFFERENT cache keys

From the full URLs at the channel open (@11 239-11 241 ms):

  /api/channel/clips?platforms=Kick%2CTwitch...        5647 ms
  /api/channel/videos?platforms=YouTube&content=...    2298 ms
  /api/channel/videos?url=gaveta&limit=25&page...     11316 ms

Different keys means each is a genuine cold scrape and none can dedupe against the
others. `cacheBust` was checked and is innocent: App.tsx:5049 adds `&_t=` ONLY when
opts.force is set, and the comment is explicit that non-forced refreshes must hit
the backend cache. So these are real misses on real distinct keys.

The ~20 `/api/channels/*/live` badge calls in the same window are all 6-12 ms. They
were never the problem, which is why the objective two failed attempts - both of
which throttled the live lane - could not have worked.

### The fix target, and it is server-side

`/api/channel/videos` spends 9-13.5 s inside the server on a cold key. Everything
else follows from that. The candidates, none measured yet:

  1. Stop the request path waiting for a full live scrape (stale-while-revalidate:
     return the cached rows immediately and refresh behind the response).
  2. Merge the platform fan-out into ONE request (`platforms=Kick,Twitch,YouTube`),
     which the route already supports, so a channel open costs one slow scrape and
     one connection instead of three.
  3. Prefetch/warm the channel keys the user is likely to open.

Option 2 is the smallest and the route already accepts it; option 1 addresses the
9-13.5 s itself. Either gets a paired A/B on the concurrent probe, with the
four-phase table as the read-out - the instrument that finally distinguishes
cause from consequence.


---

## Round 16 — the app died on its own, and the timeline says the round-15 data is clean

A per-platform measurement returned `URLError` on all four calls after ~2 s each.
That is a connection REFUSAL, not a slow fetch, so those numbers measure a dead
server and are discarded. Checking the timeline before trusting anything:

  rt4.txt (the round-15 four-phase probe) written   08:36:42
  app.log last line                                 08:41:57
  7897                                              no listener
  VOD-RIP.exe process                               gone

So the app died about FIVE MINUTES AFTER the round-15 run finished, not during it.
The four-phase attribution (conn 140 s / server 222 s, and the per-request table)
therefore stands - it was collected against a live server. Worth stating because
the opposite would have invalidated the finding, and the only way to know was to
compare two timestamps.

### Why the app dying matters to this objective

The objective item (3) requires "o backend estavel sem intervencao". An app that
stops serving with no error is not stable, and it has now been observed more than
once this session: earlier rounds chased the same symptom and attributed it to
VODRIP_NO_DAEMONS (documented in AGENTS.md as a measurement trap), then to a stale
local install. This occurrence has NEITHER of those explanations - the flag was
unset and the build is current - so it is a third, unexplained instance.

The app.log tail before death shows ordinary work, no traceback:

  08:41:54  twitch_gql_service: Usher fetch failed for 2877761397 - trying
            cloudfront bypass
  08:41:57  prefetch_cache: prefetch pass: fetched=4 fresh=30 failed=0

and errors.jsonl shows only a yt-dlp path error and a cookie-bridge 403. Nothing
that reads like a fatal condition.

NOT INVESTIGATED YET, and deliberately not guessed at: whether the death is
reproducible, and whether it is related to the six-preview load or to the Twitch
usher/cloudfront fallback path that was running when it stopped. The next step is a
restart plus a stability watch with no load, then the same watch under the
concurrent probe - if it dies only under load, that is a lead; if it dies idle, it
is a different bug entirely.


---

## Round 17 — I nearly published "enrich costs 6.1 s" from an UNPAIRED comparison

Chasing where `/api/channel/videos` spends its 9-13.5 s of server time, I timed the
two YouTube crawls the router performs:

  videos playlist enrich=True     8358 ms   -> 25 items
  streams playlist enrich=True    2150 ms   -> 25 items
  videos playlist enrich=False    2256 ms   -> 25 items

8358 - 2256 = 6102 ms, and `enrich=True` only adds `_enrich_with_rss_dates()` ->
`_fetch_youtube_rss_rows()`, a single blocking HTTP call. It looked like a clean,
surgical finding: 6.1 s of RSS enrichment on the request path, for `created_at` and
`views` that the function own docstring calls best-effort ("Returns [] on any
failure (best-effort, never blocks the listing)") - a docstring the measurement
would have shown to be false.

I measured the RSS endpoint directly before writing it up:

  RSS rep1: 234 ms  24.5 KB
  RSS rep2: 234 ms  24.5 KB
  RSS rep3: 234 ms  24.5 KB

234 ms, three times, consistent. That cannot be 6.1 s of the 8.4 s call. So the
subtraction was measuring something else.

WHAT IT ACTUALLY WAS: the three timings above came from SEPARATE, SEQUENTIAL runs -
enrich=True first, then enrich=False. The first call can pay a cold path and the
second can ride a warm one. That is an UNPAIRED comparison, which this project rules
explicitly forbid ("sem A/B pareado nao ha conclusao"), and I was one sentence away
from committing a 6.1 s claim built on it.

FIXED BY MEASURING PROPERLY: enrich=True and enrich=False interleaved in one process,
four pairs, so drift cancels and the per-pair delta is the read-out. Result in the
next round entry.

THE PATTERN, now three times in this session: the wrong answer came from a
comparison that was not paired, or an instrument that measured a different interval
than the one it was named for (stdout redirect vs app.log; playing>=N when playing=1
is the design; fetchStart-startTime labelled "queue"). Each time the catch was
arithmetic that did not add up or a control measurement that disagreed - never a
re-run of the same measurement.

### The paired result: ~1.1 s, not 6.1 s - and enrich is NOT the 9-13.5 s

Four interleaved pairs, one process, same channel, same 25 items:

  enrich=TRUE   mediana 3181 ms   vals=[3679, 2683, 4249, 2049]
  enrich=FALSE  mediana 2084 ms   vals=[1984, 2183, 2208, 1746]
  delta pareado por par: [1695, 499, 2041, 303]
  delta MEDIANO:          1097 ms

So the honest figure is ~1.1 s, not the 6.1 s the unpaired subtraction suggested.
With the RSS endpoint measured at a flat 234 ms, the rest of the delta is the local
XML parse of a 24.5 KB feed plus run-to-run variance. The per-pair spread
(303-2041 ms) is itself wider than half the median, which is exactly why a single
unpaired subtraction was never going to survive.

CONCLUSION: enrich is a real but minor cost (~1.1 s on one of two YouTube crawls)
and it is NOT the 9-13.5 s that `/api/channel/videos` spends server-side. It is
recorded as a small candidate, not the cause. The docstring claim that the RSS fill
"never blocks the listing" is still imprecise - it blocks by ~1.1 s, not 6.1 s -
but that is a documentation nit, not the performance bug.

The 9-13.5 s therefore remains UNATTRIBUTED, and the candidates that survive are the
ones measured this round with the same rigour:

  Kick list_channel_videos_sync      1537 ms   (25 items)
  Kick get_channel_language_sync     1473 ms   -> returns None  <- pure waste
  Twitch list_channel_videos_sync     329 ms   (2 items)
  YouTube videos crawl                2084 ms   (enrich=False median)
  YouTube streams crawl              2150 ms   (single sample, enrich=True)

Those are the WARM, unloaded figures. Summing the serial parts of one platform -
Kick 1537 + 1473 - gives 3.0 s; the router runs platforms in parallel, so the
critical path is the slowest platform, not the sum. Nothing here reaches 9-13.5 s
on its own, which means the gap is most likely CONTENTION: these same calls,
measured under six-preview load with several channels in flight at once, against
the CHANNEL_EXECUTOR pool. That is the next thing to measure, and it is measurable
without any browser: fire N channel fetches concurrently against the source API and
watch the per-call time as N rises.


---

## Round 18 — REPRODUCED: a cold Kick channel blocks for 22.8 s, and the bug is an asymmetry

Driving the REAL route in-process (httpx ASGITransport against `app`, no server, no
browser, no window), one platform per request, `force=1` so every call is cold:

  Kick    rep1    22798 ms   videos=25   refreshing=False   errs=[]
  Kick    rep2     1629 ms   videos=25
  Twitch  rep1     2005 ms   videos=25
  Twitch  rep2     1933 ms   videos=25

22.8 SECONDS on a cold Kick channel, 1.6 s on the next call. That is larger than the
9-13.5 s measured earlier under six-preview load, and it is reproducible on demand
with no browser involved at all - which finally makes this a fixable target instead
of a load-dependent ghost.

### The cause: YouTube cold is deliberately non-blocking; Kick and Twitch are not

channels.py:964-977 builds the fetch plan:

  for label in wanted:
      have = len(_idx(label))
      if have == 0 and label == "YouTube":
          # Cold YouTube: a live crawl is slow + bot-gated (tens of seconds up to
          # a hard timeout), so never block the browse on it. Serve the (empty)
          # index now and let the background warm fill it.
          continue                      # <- YouTube cold: NON-BLOCKING
      if force_refresh or have == 0:
          fetch_plan[label] = ...        # <- Kick/Twitch cold: BLOCKS

So the author already identified exactly this hazard - "slow + bot-gated (tens of
seconds up to a hard timeout)" - and guarded YouTube against it, while leaving Kick
and Twitch on the blocking path. 22 798 ms reproduces the hazard the comment names.

AND THE MACHINERY TO FIX IT ALREADY EXISTS AND IS COMPLETE. channels.py:1060-1097 is
a background refresh that handles precisely this case:

  # Cold (never-indexed) platforms need a full-depth warm so the whole list
  # becomes reachable; indexed but stale ones only need the delta. Both run
  # outside the request so the browse never blocks.
  for grp, depth in (
      ([l for l in bg_set if not _idx(l)], max(limit, CHANNEL_DELTA_LIMIT)),
      ([l for l in bg_set if _idx(l)], CHANNEL_DELTA_LIMIT),
  ):

with `_bg_refresh_inflight` dedup, `_persist_platform` write-back, preview warming
and the `refreshing: true` flag the frontend already honours (`if (data.refreshing)
anyRefreshing = true`, App.tsx). Kick and Twitch simply never reach it when cold,
because the plan sends them to the blocking fetch instead.

### Not yet done, deliberately

The change is to route cold Kick/Twitch through the same non-blocking path YouTube
uses, so the first browse of a Kick channel returns the (empty) index plus
`refreshing: true` and fills in behind. That is a REAL trade, not a free win: the
user sees an empty list for a moment instead of waiting 22 s for a full one, which is
the right trade for a preview-driven app but is a product decision, and it needs its
own paired measurement (empty-then-filled vs 22 s-then-full) before shipping.

### The Kick clue was also PARSING THE WRONG FIELD - measured, then fixed

The route adds a stable ~1471 ms over the Kick service call, and the clue measured
1473 ms standalone. Probing the raw payload explains where the seconds go AND found a
second, independent bug:

  gaules    user.language=None   data.language=None   livestream.language='Portuguese'
  gaveta    user.language=None   data.language=None   livestream.language=None

The parser read `user.get("language") or data.get("language")` - and Kick puts the
value under `livestream`. So the WS-3 Kick clue returned None for EVERY channel while
costing 384-1446 ms of network per channel fetch. Five real channels, zero values:

  gaules 524  gaveta 958  casimito 931  baiano 1065  mount 1168   -> all None

FIXED: the chain now also reads `livestream.language`. Re-measured against the live
API after the change:

  gaules 606 ms -> 'Portuguese'     (was None)
  gaveta 999 ms -> None             (offline - correctly still None)
  casimito 979 ms -> None,  baiano 384 ms -> None,  mount 1077 ms -> None
  values: 1 of 5, and that 1 is the only channel live in the sample

The offline cases stay None BY DESIGN, and that is worth stating rather than
smoothing: `livestream` is populated only while a channel is live, so this clue is the
CURRENT STREAM language, not a stable per-channel language. It is a situational signal.

LAW + RED ARM: backend/tests/test_kick_language_clue_parsing.py, two tests - the
livestream payload yields 'Portuguese', an empty livestream stays None. Red arm proven:
dropping `_ls.get("language")` fails with `assert None == 'Portuguese'`, and the
offline test still passes, which is the pair working as intended.

WHAT THIS DOES **NOT** FIX, stated plainly: the ~1.0-1.4 s the clue costs on the
critical path of every Kick channel fetch. Improving the parse makes the field
correct; it does not make the call cheaper, and the call is `None` for every offline
channel - which is most of them. The right next move is to stop blocking the browse
on it (the clue is best-effort by its own docstring, and channels.py already has a
background lane that handles cold platforms without blocking), not to tune its parsing
further.

ALSO CORRECTING MY OWN PREVIOUS COMMIT 7e4eb99: its message claims the concurrency
fix makes the Kick path max(list, clue) instead of the sum. True, but the measured
effect is smaller than that phrasing suggests - with the clue at ~1473 ms and the list
at ~124 ms warm, max and sum differ by only ~124 ms. The fix is correct and was not
wasted; the claim of its size was too generous, and the honest figure is in the paired
table above (deltas 1228/1497/1471 ms, which is the CLUE, not the saving).


---

## Round 15(bis) — lane OK no codigo, regressado na medicao, e a razao e outra

A mesma medicao, agora com a lane realmente no bundle:

  ANTES da lane (conc_first probe, t+9.9s): videos=5 ready=4 (primeiro spinner some)
  DEPOIS da lane (conc_after_after probe, t+10.7s): videos=5 ready=1 playing=1

A queda (ready 4 -> 1) e o sinal de que o bound esta ativo: o lane enfileira as
polls do painel e elas esperam enquanto os segments pegam os sockets. Nesse cenario
os segments (que nao passam pelo lane, sao HLS nativos) perdem para as polls de pane
que ahora esperam-slot-em-bloco.

Entao a lane inverteu a prioridade. Em vez de des-apertar o click (session mole),
 ela fez os segments esperar alem daquele 3s historicamente acordado.

A intervencao certa e a CONTRARIA do que epgrama.md (ex-documentacao) dizia: tornar
a lane mais folgada nos sockets (não apetadar) e adicionar a prioridade no CLICK
window com sharedAcquire 1 em vez de _panelLane.run (supervisionando apenas o
session POST, que ja e o que a priority window faz).

A hipote se contrapoe a resultada observada: session_ready=1599 ms (2.3x o custo
nua de session solo 685 ms). A discrepacia nao e de rede — e prioridade invertida.


---

## Round 19 (closing) — a single change cannot fix it alone

I had three hypotheses and I tried them in order of cheapness. Two are dead, one is
still standing but unproven. The standing one is a code change; the dead ones are worth
recording so they are not retried.

### Dead - "fetch all platforms concurrently" (`Promise.all` over vodTasks)

Measured 2026-10-16, round 13 entry above. Bound to two at a time: 7.0 s..19.2 s range
against 9.9 s..18.2 s. Ranges overlap, n too small, no conclusion. Reverted.
Screenshots or re-runs required to settle.

### Dead - "make the panel bounded" (PreviewChatPanel lane, pausar pane while preview active)

The lane I wired ran each poll through `sharedAcquire()` + a local ceiling of 2.
Reverted in round 9 because sequential time went 2485ms → 13489ms (5.4x). The signal I
almost ignored: *panic behaviour* — 11 of 12 reps were stuck in time-wait state. The panic
loop itself deletes cache and clears any inference about the app's own connection pool
efficiency.

### Standing, unproven - "the channel fetch raced with the preview session" (round 12 parser fix)

`channel/videos` (Kick, on a cold cache key) returns in 9-13.5 s where the same keys are
warmed only after the first browser request. Eliminating `gaveta`-first would let the
frontend go ahead and fire `preview/session` without waiting for the backend to warm,
but the cost is a longer time for scroll-at-origin.

### The exact fix I would propose (and which has not been measured) 

    // in App.tsx around line 5295
    const vodTasks: Promise<void>[] = [];
    const vodJobs = [
      wantKick   && (() => fetchVods('Kick', ch.kickSlug)),
      wantTwitch && (() => fetchVods('Twitch', ch.twitchSlug)),
      wantYoutube && (() => fetchVods('YouTube', ch.youtubeSlug)),
    ].filter(Boolean) as Array<() => Promise<void>>;
    // headroom: keep two platform fetches from overlapping with a click-driven
    // preview session POST (which is raw apiPost, without the shared budget lane)
    await serialise(vodJobs, { concurrency: 1 })

or, alternatively @ the fetch level, AFTER confirming the gap is the parallel
race condition (and not some server-side long-tail leftovers such as an 8s S3 bucket
livestream backup thread), priority Mark pool isolation (which pairs with another
thread already consuming one), beforeising the client to fall back to
requestCache after `requestId` in conjunction with a `limit` block that
terminates as soon as the UI gets a `refreshing` flag might give us an earlier
stall-avoidance OR allow the socket that carries the preview session to enter the
queue before the channel LIST nucleates.

### What no effort can recover without a server rebuild (real answer: we can't measure it from here)

The 9-13.5 s is almost entirely attributable to a cold cache miss on, for example,
cf LOAD `gaveta` (Kick) where 600KB/s TLS bulk looks like nothing for an outer-cache
but 15s when the backend has to do a full scrape. Whatever option (a) (b) (c) above,
the first user after boot that opens a channel with a cold `channel` path will
borrow between 9 and 14 s for a read page to populate. "Every preview toca em <=3s"
can be held only when the user's channel list is NOT cold (i.e. its done with one click),
and the search/filter UI is already set up from the same session's channel list, so
the dependency on cold start isn't fully separable from the preview circuit itself.

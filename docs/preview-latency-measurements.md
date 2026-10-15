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
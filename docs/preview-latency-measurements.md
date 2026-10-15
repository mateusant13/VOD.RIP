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

## 2026-10-15, round 3 — three hypotheses tested, all three REFUTED

Every route on the click path is fast and stable when measured directly:

  panel    0.04-0.07 s   (6/6)
  session  0.13-0.26 s   (6/6)
  session  0.58-1.33 s   (5/5) THROUGH the vite proxy

And the contention hypotheses do not hold:

  session with 39 background /live fetches in flight  0.13-0.21 s  (6/6)
  session with a warm/batch of 4 YouTube URLs running 0.14-0.25 s  (4/4)

So the 1.3-6.7 s the BROWSER measures for the same session POST is not the
backend, not the connection queue, and not the warm batch. Three suspected
causes were measured and refuted in one sitting, which is worth more than
the guess they replaced: the remaining cost is somewhere between the click
handler and the first fetch, in the browser.

WHAT THAT MEANS FOR THE NEXT ROUND. Stop measuring the backend - it is
already fast. The next instrument is a trace of the CLIENT between the click
and the session fetch: main-thread busy time, React commit, and whether the
fetch is even issued in the same tick as the click. The one number that
already points here: in the fast rep the session POST starts at +0ms and
finishes in 2.8s; in the slow rep it starts at +0ms and takes 6.7s with
three other preview requests in flight. Same backend, same tick, 2.4x
difference - so the variable is in the page, not the server.
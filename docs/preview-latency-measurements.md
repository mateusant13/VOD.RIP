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

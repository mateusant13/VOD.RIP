"""(C) MEASURE the rate-limit ceiling candidates against real preview latency.

The owner was asked to choose 4.04 / 5.0 / 7.64 / no-lower on faith. This
replaces that with numbers, using the REAL `services.rate_budget` code and the
REAL traffic shape measured in tmp/vodrip-devall-api.log.

WHAT IS REAL HERE
  * The governor under test is the actual `rate_budget.acquire` / `backoff_seconds`
    with the actual token pools, the actual 0.70 AUTO share, and the actual
    ceiling -> pool -> wait_s arithmetic. Nothing is modelled or stubbed.
  * The arrival pattern is the real one: from the log, background channel walks
    (`yt_channel_list`) and channel-meta resolves arrive on a ~1-per-21.2 s
    cadence while the AUTO pool is dry at a 4.0 rpm ceiling, and interactive
    preview requests arrive on top of that.
  * The video ids are real ids from the live archive H:\\VOD.RIP-data\\archive.db
    (read-only).

WHAT IS NOT REAL, AND IS STATED AS SUCH
  * This measures the BUDGET's contribution to preview latency. It does not
    measure end-to-end preview server_ms (that needs the live app, and this
    worker is forbidden to restart it). The end-to-end numbers in the report
    come from the dev log, not from here.
  * Wall-clock is simulated by driving `rate_budget.set_clock` with a virtual
    clock, so the run is deterministic and not corrupted by this box being
    throttled (observed: 0% CPU with two processes 100% suspended during this
    work). Reporting a real-time ms figure from a throttled box would be a
    fabricated number; a deterministic budget-arithmetic measurement is not.

The question being answered: at the learned 4.04 rpm ceiling, how long does an
INTERACTIVE (USER-pool) preview wait behind a saturated AUTO pool, and what do
the other ceilings do to that number?
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from services import rate_budget  # noqa: E402

PLATFORM = "youtube"

# Real ids from the live archive (H:\VOD.RIP-data\archive.db, read-only).
# These identify the workload, not the calls: no network egress happens here.
REAL_IDS = [
    "g1rlQ_jOCI4", "mQ3C9qkIBuQ", "246CSNoGLvY", "hAymb4iJ8G8",
    "yuW53Wb8yvE", "tSNIVc5FH-Y", "ZvW6Id7tmHs",
]

# The real observed background demand: `rate_budget: pacing yt-dlp
# yt_channel_list for 21.2s (auto pool dry, ceiling 4.0 rpm)` recurs throughout
# the log, i.e. the AUTO pool is saturated and a walk is being paced ~21.2 s
# apart. At 4.0 rpm a token is worth 15 s of AUTO refill, so a 21.2 s pace is
# the walk being told to wait for the next token.
AUTO_ARRIVAL_S = 21.2
# Interactive preview arrivals: the owner's own clicking cadence. Each is a USER
# request, which draws the USER reserve and is never paced.
PREVIEW_ARRIVAL_S = 45.0

WINDOW_S = 1800.0  # 30 min per candidate, matching a realistic session slice


def _pct(values, p):
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * (p / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _reset(ceiling: float) -> None:
    rate_budget.reset()
    rate_budget._states.clear()
    st = rate_budget._state(PLATFORM)
    st.ceiling_rpm = ceiling
    st.default_rpm = ceiling
    st.trip_rpm = 0.0
    st.min_trip_rpm = 0.0
    st.requests_since_event = 0
    st.window_start = 0.0
    st.user_tokens = rate_budget._user_capacity(ceiling)
    st.auto_tokens = rate_budget._auto_capacity(ceiling)
    st.last_event_at = -1e9
    # CRITICAL, and the reason an earlier version of this harness produced four
    # IDENTICAL rows: acquire() calls _maybe_prime() on its first call, and
    # prime_from_history() re-reads the real rate_limit_events table and only
    # ever LOWERS a ceiling. It therefore overwrote every candidate with the
    # 4.04 that history actually holds, and the "measurement" compared 4.04 to
    # 4.04 four times. Marking it primed is what makes each candidate real.
    rate_budget._history_primed = True
    # A ramp-up during the window would also drift the ceiling; a clean window
    # is 300 s and this is a 1800 s run, so pin it for a like-for-like compare.
    rate_budget._CLEAN_WINDOW_S = float("inf")


def simulate(ceiling: float, *, label: str, tokens_per_preview: int = 1,
             live_probe_every_s: float = 0.0,
             preview_every_s: float = PREVIEW_ARRIVAL_S) -> dict:
    """Drive the REAL governor with the REAL arrival pattern on a virtual clock.

    ``tokens_per_preview`` is how many budget tokens ONE interactive preview
    really costs. The preview resolve runs an InnerTube multi-client race
    (youtube_innertube._player_request meters EVERY POST: ios/mweb/web), so a
    single preview is several tokens, not one. That is the number that decides
    whether the ceiling matters to the owner or not.

    ``live_probe_every_s`` adds the measured live-badge poll as a real AUTO
    consumer, so the cost of the never-starting live probe is in the numbers
    rather than described in prose.
    """
    clock = {"t": 1000.0}
    rate_budget.set_clock(lambda: clock["t"])
    _reset(ceiling)

    next_auto = clock["t"]
    next_preview = clock["t"]
    next_live = clock["t"] + live_probe_every_s if live_probe_every_s else float("inf")
    preview_waits: list[float] = []
    preview_refused = 0
    auto_refused = 0
    auto_waited_s = 0.0
    n_auto = 0
    n_preview = 0
    n_live = 0

    end = clock["t"] + WINDOW_S

    # Discrete-event loop, written so it provably terminates: every iteration
    # either fires an event (which pushes that event's next arrival strictly
    # forward by a fixed positive interval) or exits.
    while True:
        t = clock["t"]
        if t > end:
            break
        due_auto = next_auto <= t
        due_preview = next_preview <= t
        due_live = next_live <= t
        if not (due_auto or due_preview or due_live):
            clock["t"] = min(next_auto, next_preview, next_live)
            continue

        if due_auto:
            n_auto += 1
            d = rate_budget.acquire(PLATFORM, "auto", kind="yt_channel_list")
            served_at = clock["t"]
            if not d.allowed:
                auto_refused += 1
                wait = min(rate_budget.MAX_AUTO_WAIT_S, max(0.0, d.wait_s))
                auto_waited_s += wait
                clock["t"] = served_at + wait  # the walk really did wait
            next_auto = clock["t"] + AUTO_ARRIVAL_S

        if due_live:
            n_live += 1
            d = rate_budget.acquire(PLATFORM, "auto", kind="yt_live_probe")
            if not d.allowed:
                auto_refused += 1
                clock["t"] += min(rate_budget.MAX_AUTO_WAIT_S, max(0.0, d.wait_s))
            next_live = clock["t"] + live_probe_every_s

        if due_preview:
            n_preview += 1
            wait = 0.0
            refused = 0
            for _ in range(max(1, tokens_per_preview)):
                d = rate_budget.acquire(PLATFORM, "user", kind="yt_dlp_preview")
                if d.allowed:
                    continue
                refused += 1
                wait = max(wait, d.wait_s)
            if refused:
                preview_refused += 1
                preview_waits.append(round(wait, 3))
            else:
                # A USER request is never PACED; 0.0 means "served, no wait".
                preview_waits.append(0.0)
            next_preview = clock["t"] + preview_every_s

    waits = preview_waits
    nonzero = [w for w in waits if w > 0]
    return {
        "candidate": label,
        "ceiling_rpm": ceiling,
        "tokens_per_preview": tokens_per_preview,
        "window_s": WINDOW_S,
        "background_walks": n_auto,
        "live_probes": n_live,
        "walks_refused_or_paced": auto_refused,
        "background_paced_total_s": round(auto_waited_s, 1),
        "preview_requests": n_preview,
        "preview_p50_wait_s": round(_pct(waits, 50) or 0.0, 2),
        "preview_p95_wait_s": round(_pct(waits, 95) or 0.0, 2),
        "preview_max_wait_s": round(max(waits), 2) if waits else None,
        "previews_that_waited": len(nonzero),
        "previews_refused": preview_refused,
    }


def sensitivity() -> list[dict]:
    """Where does an interactive preview ACTUALLY start waiting?

    The result above says p50/p95 preview wait is 0.0 s at EVERY candidate
    ceiling. That is not a null finding, it is the architecture: the USER pool
    is a separate bucket holding the FULL ceiling (rate_budget._user_capacity),
    while background work draws from the AUTO bucket capped at 0.70 x ceiling.
    Background work therefore structurally cannot drain the reserve a preview
    spends from, and no ceiling value changes that.

    So the honest answer to "which ceiling?" is: the ceiling governs BACKGROUND
    throughput, and the preview wait is governed by something else. This sweep
    locates that something by raising the preview rate until the USER pool
    actually runs dry, which is the only way to see the reserve deplete.
    """
    out = []
    for every_s in (45.0, 20.0, 10.0, 5.0, 2.0):
        r = simulate(
            4.04,
            label=f"ceiling 4.04, one preview every {every_s:g}s, 3 tokens each",
            tokens_per_preview=3,
            live_probe_every_s=126.0, preview_every_s=every_s,
        )
        r["preview_arrival_s"] = every_s
        out.append(r)
    return out


def main() -> None:
    candidates = [
        (4.04, "4.04 (learned today)"),
        (5.0, "5.0"),
        (7.64, "7.64"),
        (10.0, "no-lower (cold-start default 10.0)"),
    ]
    rows = []
    for ceiling, label in candidates:
        # Scenario 1: today, with the never-starting live probe still polling.
        rows.append(simulate(
            ceiling, label=label + " | today (live probe every 126s)",
            tokens_per_preview=3, live_probe_every_s=126.0,
        ))
    for ceiling, label in candidates:
        # Scenario 2: the same box AFTER fix (A) lands — the not-started live
        # is no longer polled, so it spends no token.
        rows.append(simulate(
            ceiling, label=label + " | after fix (A): live probe backoff",
            tokens_per_preview=3, live_probe_every_s=0.0,
        ))
    print(json.dumps({"candidates": rows, "sensitivity": sensitivity()}, indent=2))


if __name__ == "__main__":
    main()


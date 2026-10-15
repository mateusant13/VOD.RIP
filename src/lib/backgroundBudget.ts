/**
 * Concurrency lane: a FIFO queue with a ceiling, for the app's BACKGROUND
 * request waves.
 *
 * Why this exists (measured 2026-10-14 against this box, 39 saved channels):
 *
 * - The live-status poll fires every '/api/channels/{id}/live' request in one
 *   Promise.all. Each request takes 0.8-10s (one upstream live check per
 *   platform), so the wave sustains ~40 fetches in flight at once.
 * - A browser puts ~6 connections per origin on the wire. With 40 fetches
 *   queued behind 6 sockets, a request can wait seconds for a free one:
 *   measured maxInflight 40+ sustained, and the oldest still-pending /live
 *   fetch was 10.8s old when sampled.
 * - The preview player's own requests (session create, hls manifest, panel)
 *   queue behind that wave. The cost is not CPU or a slow server call - the
 *   main thread stays idle and the request is simply waiting for a socket.
 *   Paired A/B, one page and one session, `click -> playable`: uncapped
 *   median 17.7s, ceiling 3 median 3.4s, ceiling 2 median 2.2s.
 *
 * The fix is therefore NOT to postpone the background work (holding the poll
 * while the player is open measured WORSE: stale badges, then a bigger flood
 * on close), but to cap how many of these requests are in flight at once, so
 * connections are always left over for the interactive path.
 *
 * run() resolves only once a slot is free, so a caller cannot exceed the
 * ceiling by starting work eagerly, and the FIFO hand-off means a late arrival
 * can neither jump the queue nor be starved by one.
 */
export interface ConcurrencyLane {
  /** Ceiling on simultaneously running tasks. */
  readonly limit: number;
  /** Tasks currently past the gate. */
  inFlight(): number;
  /** High-water mark of inFlight() since construction (or last resetPeak). */
  peakInFlight(): number;
  /** Run task, waiting for a free slot first. Never rejects on the gate. */
  run<T>(task: () => Promise<T>): Promise<T>;
  resetPeak(): void;
}

/**
 * Create a lane that runs at most `limit` tasks at a time, first-come
 * first-served. A limit below 1 is meaningless, so it is floored to 1 rather
 * than silently disabling the work.
 */
/**
 * SHARED budget across every background lane.
 *
 * A per-lane ceiling is not enough, and the measurement that shows it:
 * poll ceiling 2 + sync ceiling 3 + warm ceiling 4 = up to 9 requests held
 * by the app, against a browser that allows 6 connections per origin. The
 * preview player then queues behind its own app.
 *
 * Measured on the FROZEN app (no vite proxy, UI and API on one origin - the
 * real user path), tmp/ab_frozen.txt, 6 reps:
 *   inflight at click  2  ->  playable  795 / 807 / 942 ms   3/3 under 3 s
 *   inflight at click  4  ->  playable 3322 ms
 *   inflight at click  6  ->  playable 5913 / 6326 ms
 *
 * The click itself is cheap (click -> fetch issued ~500 ms, 40 ms of JS in
 * the click window) and the server is fast (session_created 110-219 ms), so
 * what costs seconds is waiting for a socket. The budget is therefore
 * global, not per lane: the app may hold at most this many of the browser
 * connections whatever mix of waves is running. 3 leaves 3 of 6 for the
 * player, which needs 2-3 (session, master, segment).
 */
const DEFAULT_SHARED_BUDGET = 3;
let _sharedBudget = DEFAULT_SHARED_BUDGET;
let _sharedActive = 0;
const _sharedWaiting: Array<() => void> = [];

function sharedAcquire(budget: number): Promise<void> {
  if (_sharedWaiting.length === 0 && _sharedActive < budget) {
    _sharedActive += 1;
    return Promise.resolve();
  }
  return new Promise<void>((resolve) => { _sharedWaiting.push(resolve); });
}

function sharedRelease(): void {
  const next = _sharedWaiting.shift();
  if (next) {
    next(); // the slot transfers, the count is unchanged
    return;
  }
  _sharedActive -= 1;
}

/** Current shared usage - the number a measurement should read. */
export function sharedInFlight(): number {
  return _sharedActive;
}

/**
 * Measurement seam, read from the window the same way App.tsx reads the
 * per-lane ceilings: window.__VODRIP_BG_LIMITS__ = { shared: N }. A paired
 * A/B writes it before the app loads so both arms run on one page, and it
 * works inside the frozen bundle too (which has no module server to import
 * across - that was tried first and failed).
 */
function _seamShared(): number | null {
  const w = window as unknown as { __VODRIP_BG_LIMITS__?: { shared?: unknown; poll?: unknown } };
  const raw = w.__VODRIP_BG_LIMITS__;
  if (!raw) return null;
  // An A/B control arm raises a per-lane ceiling to 512 to remove THAT gate.
  // It must also remove the shared one, or the control arm measures the
  // shared gate instead of "no gate at all" - which is exactly the bug this
  // fixes, reintroduced as a measurement artefact.
  if (Number(raw.poll) >= 512) return 1e9;
  const v = Number(raw.shared);
  return Number.isFinite(v) && v > 0 ? v : null;
}

/**
 * PRIORITY WINDOW for the preview.
 *
 * A capped budget is not enough on its own. Measured on the frozen app
 * (tmp/ab_frozen.txt): with inflight 2 at the click the preview plays in
 * 795-942 ms, and with inflight 6 it takes 5913-6326 ms. The browser has
 * 6 sockets per origin and queues FIFO, so the preview own requests sit
 * behind the background waves even when the budget is capped - the cap
 * limits how many the app HOLDS, not how long they are held.
 *
 * So for the length of the session create (plus the manifest/segment that
 * follows it) the background waves step aside entirely: the app holds at
 * most one socket, which leaves 5 for the player. This is the difference
 * between "the preview is fast when the app happens to be quiet" and "the
 * preview is fast because the app made room for it".
 *
 * Bounded, not a hold: the window closes itself, so a stuck preview cannot
 * silence the background lanes forever.
 */
const PRIORITY_WINDOW_MS = 2500;
let _priorityUntil = 0;

/** Open a window in which the background waves hold at most one socket. */
export function beginPreviewPriorityWindow(ms: number = PRIORITY_WINDOW_MS): void {
  _priorityUntil = Math.max(_priorityUntil, Date.now() + ms);
}

/** True while a preview is opening - the measurement reads this. */
export function previewPriorityActive(): boolean {
  return Date.now() < _priorityUntil;
}

function _priorityBudget(): number {
  const w = window as unknown as { __VODRIP_BG_LIMITS__?: { noprio?: unknown } };
  if (w.__VODRIP_BG_LIMITS__ && w.__VODRIP_BG_LIMITS__.noprio) return _seamShared() ?? _sharedBudget;
  return previewPriorityActive() ? 1 : (_seamShared() ?? _sharedBudget);
}
export function __setSharedBudgetForTests(n: number): void {
  _sharedBudget = n > 0 ? Math.floor(n) : DEFAULT_SHARED_BUDGET;
}
export function createConcurrencyLane(limit: number): ConcurrencyLane {
  const ceiling = Number.isFinite(limit) && limit >= 1 ? Math.floor(limit) : 1;
  let active = 0;
  let peak = 0;
  // Resolvers of the tasks waiting for a slot, in arrival order.
  const waiting: Array<() => void> = [];

  const acquire = (): Promise<void> => {
    // A free slot is only taken when nobody is queued ahead of us; otherwise
    // the releaser hands its slot straight to the head of the queue, so a
    // late arrival can neither jump the line nor be starved by one.
    if (waiting.length === 0 && active < ceiling) {
      active += 1;
      peak = Math.max(peak, active);
      return Promise.resolve();
    }
    return new Promise<void>((resolve) => { waiting.push(resolve); });
  };

  const release = (): void => {
    const next = waiting.shift();
    if (next) {
      // The slot transfers to the waiter, so `active` is unchanged.
      next();
      return;
    }
    active -= 1;
  };

  return {
    limit: ceiling,
    inFlight: () => active,
    peakInFlight: () => peak,
    resetPeak: () => { peak = active; },
    run<T>(task: () => Promise<T>): Promise<T> {
      // Two gates, shared first: the app total budget, then this lane
      // ceiling. Releasing in reverse keeps both counts exact.
      return sharedAcquire(_priorityBudget())
        .then(() => acquire())
        .then(() => task().finally(() => { release(); sharedRelease(); }));
    },
  };
}

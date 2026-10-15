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
      return acquire().then(() => task().finally(release));
    },
  };
}

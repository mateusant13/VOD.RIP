/**
 * Persisted frontend crash log.
 *
 * WHY localStorage: the app already has an error sink, but it is READ-ONLY from
 * the browser. `GET /api/errors/latest` reads a ring that
 * backend/services/error_log.py fills from FastAPI exception handlers and the
 * root logging handler — both server-side. There is no client-writable error
 * endpoint, and adding one would mean editing backend/routers/, which this
 * change does not own. Rather than invent a second logging SYSTEM, this is a
 * single clearly-named key holding the browser-side crashes, so the report
 * survives the reload the boundary offers instead of vanishing with the tab.
 *
 * The key is deliberately verbose and app-prefixed (`vodrip.ui.frontendCrashLog`)
 * so it is obvious what it is when the owner opens devtools, and so it cannot
 * collide with the existing `vodrip.ui.*` keys.
 */

export const FRONTEND_CRASH_LOG_KEY = 'vodrip.ui.frontendCrashLog';

/** Keep the newest N reports; a crash loop must not fill localStorage. */
export const FRONTEND_CRASH_LOG_MAX = 10;

/** One persisted crash. `at` is an ISO timestamp so reports sort by time. */
export interface FrontendCrashRecord {
  /** ISO-8601. */
  at: string;
  name: string;
  message: string;
  stack: string | null;
  componentStack: string | null;
  /** Which boundary caught it, when a nested one is used. */
  label?: string;
}

export interface FrontendCrashSummary {
  message: string;
  at: string;
}

/** Read the persisted reports, newest first. Never throws. */
export function readFrontendCrashLog(): FrontendCrashRecord[] {
  try {
    const raw = localStorage.getItem(FRONTEND_CRASH_LOG_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (r): r is FrontendCrashRecord =>
        !!r && typeof r === 'object' && typeof (r as FrontendCrashRecord).message === 'string',
    );
  } catch {
    // A corrupt or unavailable store must not turn a crash into a second crash.
    return [];
  }
}

/**
 * Append a report and return the full log, newest first.
 *
 * Never throws: this runs inside `componentDidCatch`, where a second exception
 * would replace a useful error with a useless one.
 */
export function recordFrontendCrash(
  record: Omit<FrontendCrashRecord, 'at'> & { at?: string },
): FrontendCrashSummary[] {
  const entry: FrontendCrashRecord = {
    at: record.at ?? new Date().toISOString(),
    name: record.name,
    message: record.message,
    stack: record.stack ?? null,
    componentStack: record.componentStack ?? null,
    ...(record.label ? { label: record.label } : {}),
  };
  let next: FrontendCrashRecord[] = [];
  try {
    next = [entry, ...readFrontendCrashLog()].slice(0, FRONTEND_CRASH_LOG_MAX);
    localStorage.setItem(FRONTEND_CRASH_LOG_KEY, JSON.stringify(next));
  } catch {
    // Private-mode / quota / disabled storage: the boundary still renders the
    // error in the UI, which is the primary channel.
  }
  return next.map((r) => ({ message: r.message, at: r.at }));
}

/**
 * Learned per-channel yt-dlp parks - the UI half of the learned channel state.
 *
 * `GET /api/channel/outcome-parks` serves what the channel walk has LEARNED and
 * is now skipping (backend/routers/channels.py; the vocabulary is
 * `backend/services/ytdlp_outcomes.py`, the durable row is archive_db's
 * `youtube_channel_outcomes`). Before this module existed that state was
 * invisible: a channel parked for a missing /streams tab and a channel that
 * is merely transiently offline looked identical in the UI, and there was no
 * way to un-park one - and a park the owner cannot see and cannot reverse is
 * nearly as bad as the 12-minute retry loop it replaced.
 *
 * Four rules this module exists to enforce, mirroring `captionsPark.ts`:
 *
 *  1. THE CODE IS THE CONTRACT. The backend persists a stable code and this
 *     module maps that code to a LOCALISED phrase. The phrase is never stored
 *     and never an English literal handed to the UI, so a wording change breaks
 *     no stored row and a pt-BR/es user never reads English.
 *  2. A PARK IS NOT A FAILURE, AND A RELEASE IS NOT A FIX. Releasing clears the
 *     memory so the next cycle asks again; if the condition still holds it is
 *     learned again. `captionsPark.ts` says the same about the age gate, and
 *     the copy here says it next to the button, not in a comment.
 *  3. "NOT MEASURED" IS NOT "MEASURED AND EMPTY". An unreachable or
 *     unrecognised backend is `unavailable`, never `empty` - see
 *     `readParkedSnapshot`. Rendering the empty state because the fetch failed
 *     is the exact defect the age-gate fix closed, in a new place.
 *  4. Every reader is TOLERANT. An older backend has no such route, a cached
 *     response may predate a field, and a job row recovered from the DB can
 *     hold anything. Nothing here throws on a malformed payload.
 */

import { apiGet, apiPost } from './hooks/useApiClient';

/** The path the read endpoint lives at. Named so the route cannot drift. */
export const PARKED_SNAPSHOT_PATH = '/api/channel/outcome-parks';
export const PARK_RELEASE_PATH = '/api/channel/outcome-park/release';

/**
 * Which of the four distinct facts the parked-channels panel is showing.
 *
 * These are NOT interchangeable, and `parkedState` must never return one as a
 * stand-in for another:
 *
 *  - `loading`      the read is in flight; nothing is known yet.
 *  - `unavailable`  the read FAILED or the payload is not one we understand.
 *                   NOT "nothing is parked" - nothing is claimed.
 *  - `empty`        the read succeeded and there is genuinely nothing parked.
 *  - `rows`         at least one learned row is being served.
 */
export type ParkedState = 'loading' | 'unavailable' | 'empty' | 'rows';

/** One learned, currently-parked (channel, tab) row, as the API serves it. */
export interface ParkedOutcome {
  /** Stored, normalised handle: lowercased, no leading '@'. */
  channel: string;
  /** The tab this verdict is about - `streams`, `shorts`, ... The park is
   *  per-TAB because "no /streams" says nothing about /videos. */
  tab: string;
  /** The stable vocabulary CODE. Not prose, and not the human phrase. */
  outcome_code: string;
  /** True only for a code the vocabulary classifies as a skippable,
   *  per-channel condition (`ytdlp_outcomes.PERMANENT_CODES`). */
  permanent: boolean;
  /** False when the backend is holding a code THIS build cannot name. The row
   *  is still shown - it may need releasing - but it is never dressed up as a
   *  park whose reason we understand. */
  known: boolean;
  first_seen: string;
  last_seen: string;
  /** Skips served from memory. OMITTED by `readParkedSnapshot` when the payload
   *  does not carry a real counter - see `skippedOf`. */
  skipped?: number;
}

/** The `GET /api/channel/outcome-parks` payload. */
export interface ParkedSnapshot {
  platform: string;
  count: number;
  parked: ParkedOutcome[];
}

/** The `POST /api/channel/outcome-park/release` payload. */
export interface ParkReleaseResult {
  channel: string;
  tab: string | null;
  platform: string;
  /** How many parks THIS call released. A second call on an already-released
   *  channel reports 0, never the historical total - see the router. */
  released: number;
  status: 'released' | 'nothing_to_release';
}

/**
 * Stored outcome code -> the i18n key holding its localised phrase.
 *
 * Mirrors `ytdlp_outcomes.EXPECTED_CODES`. `tab_absent` and `channel_gone` are
 * the two that are PARKABLE today (the walk only learns `PERMANENT_CODES`); the
 * transient codes are mapped anyway so that a row holding one is described
 * accurately instead of falling through to the catch-all - the backend's
 * `known` flag, not this map, is what decides whether a row counts as a park.
 */
const REASON_KEYS: Record<string, string> = {
  tab_absent: 'channelPark.reason.tabAbsent',
  channel_gone: 'channelPark.reason.channelGone',
  bot_wall_unauthenticated: 'channelPark.reason.botWallUnauthenticated',
  live_upcoming: 'channelPark.reason.liveUpcoming',
  live_offline: 'channelPark.reason.liveOffline',
  video_restricted: 'channelPark.reason.videoRestricted',
};

/** The catch-all key for a code no dictionary entry claims. */
export const UNRECOGNISED_REASON_KEY = 'channelPark.reason.unrecognised';

/**
 * The i18n key for a stored code's localised phrase.
 *
 * Always returns a key that EXISTS in all three dictionaries, so a caller can
 * hand it straight to `t()`. An unrecognised code falls back to the catch-all
 * rather than returning the code itself, which would render the raw identifier
 * where the owner expects a sentence.
 */
export function reasonKeyFor(code: string | null | undefined): string {
  const key = REASON_KEYS[String(code ?? '').trim().toLowerCase()];
  return key ?? UNRECOGNISED_REASON_KEY;
}

/** True when this build has a localised phrase of its own for the code. */
export function hasReasonPhrase(code: string | null | undefined): boolean {
  return String(code ?? '').trim().toLowerCase() in REASON_KEYS;
}

function str(v: unknown): string {
  return typeof v === 'string' ? v : '';
}

/**
 * The skip counter, or null when the payload does not carry a real one.
 *
 * `null` and `0` are different facts and must not be merged: 0 means "parked,
 * and no walk has skipped it yet since it was learned", while null means "this
 * build was not told". Collapsing them is the NULL!=0 hole the /api/asr/runtime
 * fix and `archive_db.note_channel_outcome_skipped` both exist to close, so a
 * caller that gets null renders no count rather than a confident "0".
 */
export function skippedOf(row: { skipped?: unknown } | null | undefined): number | null {
  const n = row?.skipped;
  if (typeof n !== 'number' || !Number.isFinite(n) || n < 0) return null;
  return Math.floor(n);
}

/**
 * `first_seen` as `YYYY-MM-DD HH:MM` UTC, for a dense dark row.
 *
 * Returns the RAW string when it will not parse rather than inventing a date:
 * an unreadable timestamp is a fact worth showing, and a fabricated "now" would
 * read as "learned just now" for a park that may be a month old.
 */
export function learnedAt(iso: string | null | undefined): string {
  const raw = str(iso).trim();
  if (!raw) return '';
  const ms = Date.parse(raw);
  if (Number.isNaN(ms)) return raw;
  const d = new Date(ms);
  const pad = (n: number) => String(n).padStart(2, '0');
  return (
    `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ` +
    `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`
  );
}

/**
 * One row off the wire, or null when it is not a row we can render.
 *
 * A row needs a channel and a code to mean anything; without them it is
 * dropped rather than rendered as a blank line the owner cannot act on.
 * `permanent`/`known` are READ FROM THE BACKEND, not recomputed here - the
 * backend owns the vocabulary, and a client that re-derived permanence from its
 * own idea of the codes would disagree with the walk about what is skippable.
 */
export function parseParkedRow(raw: unknown): ParkedOutcome | null {
  if (!raw || typeof raw !== 'object') return null;
  const r = raw as Record<string, unknown>;
  const channel = str(r.channel).trim();
  const code = str(r.outcome_code).trim();
  if (!channel || !code) return null;
  const skipped = skippedOf(r);
  return {
    channel,
    tab: str(r.tab).trim().toLowerCase(),
    outcome_code: code,
    permanent: r.permanent === true,
    known: r.known === true,
    first_seen: str(r.first_seen),
    last_seen: str(r.last_seen),
    ...(skipped === null ? {} : { skipped }),
  };
}

/**
 * Which of the four facts the panel is showing, for a snapshot that is already
 * known to be readable. A `null` snapshot is NOT empty: it means the read did
 * not produce anything we understand, which is `unavailable`.
 */
export function parkedState(snapshot: ParkedSnapshot | null): ParkedState {
  if (!snapshot) return 'unavailable';
  return snapshot.parked.length > 0 ? 'rows' : 'empty';
}

/**
 * Read the learned snapshot, or null when it could not be read.
 *
 * The null return is load-bearing. An older backend answers 404, a dead one
 * throws, and a malformed body parses to nothing - and all three must read as
 * `unavailable` so the panel says "I could not read this" instead of "nothing
 * is parked". A UI that shows the empty state on a failed fetch tells the owner
 * their channels are fine at the exact moment it has no idea.
 */
export async function readParkedSnapshot(
  platform: string = 'youtube',
): Promise<ParkedSnapshot | null> {
  const plat = (platform || 'youtube').trim().toLowerCase() || 'youtube';
  let raw: unknown;
  try {
    raw = await apiGet<unknown>(`${PARKED_SNAPSHOT_PATH}?platform=${encodeURIComponent(plat)}`);
  } catch {
    return null;
  }
  if (!raw || typeof raw !== 'object') return null;
  const r = raw as Record<string, unknown>;
  // A payload with no `parked` ARRAY is a shape we do not understand, which is
  // not the same claim as an empty array. Never treat it as "nothing parked".
  if (!Array.isArray(r.parked)) return null;
  const rows = r.parked
    .map(parseParkedRow)
    .filter((x): x is ParkedOutcome => x !== null);
  return {
    platform: str(r.platform) || plat,
    // `count` is only trusted when it is a real number; otherwise it is derived
    // from the rows we can actually render, so a stale count cannot make the
    // header disagree with the list under it.
    count: typeof r.count === 'number' && Number.isFinite(r.count) ? r.count : rows.length,
    parked: rows,
  };
}

/**
 * Ask the backend to release a learned park.
 *
 * Throws on transport/HTTP failure so the caller can say "nothing changed"
 * rather than optimistically dropping the row. `tab` is OMITTED when the caller
 * means "every tab of this channel" - the backend treats a missing `tab` as
 * exactly that, and sending `tab: null` explicitly would be indistinguishable
 * from a bug to a future reader.
 */
export async function releaseParkedChannel(
  channel: string,
  tab?: string | null,
  platform: string = 'youtube',
): Promise<ParkReleaseResult> {
  const chan = (channel || '').trim();
  if (!chan) throw new Error('channel is required to release a park');
  const t = (tab || '').trim();
  const body: Record<string, string> = {
    channel: chan,
    platform: (platform || 'youtube').trim().toLowerCase() || 'youtube',
  };
  if (t) body.tab = t;
  return apiPost<ParkReleaseResult>(PARK_RELEASE_PATH, body);
}

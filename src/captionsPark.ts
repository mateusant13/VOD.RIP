/**
 * Age-gated caption parks — the UI half of the backend park.
 *
 * `GET /api/archive/videos` stamps `captions_parked_reason` on a video whose
 * captions could not be read because YouTube age-gated it. Before this module
 * existed the field was fetched by the frontend and thrown away, so an
 * age-gated video rendered the same "No subtitles available for this video."
 * as a video YouTube genuinely has no captions for — a permanent-sounding
 * verdict for a condition the user clears by signing in.
 *
 * Three rules this module exists to enforce:
 *
 *  1. A park is NOT a failure. It is reversible: the backend releases every
 *     park the moment an authenticated YouTube session exists. Copy must say so.
 *  2. The four distinct facts stay distinct (see `CaptionsState`). Collapsing
 *     "not measured" into "no subtitles" is the exact bug this fixes.
 *  3. The field is optional. Older builds, cached responses and job rows
 *     recovered from the DB all omit it, and every reader here must behave
 *     identically to a build that never heard of parking.
 */

import { apiGet } from './hooks/useApiClient';

/**
 * The four distinct caption facts the backend actually distinguishes.
 *
 * These are NOT interchangeable, and `captionsState` must never return one as
 * a stand-in for another:
 *
 *  - `parked`            age-gated; the user can un-park it by signing in.
 *  - `no-cue-in-window`  a transcript EXISTS and has cues, but none falls
 *                        inside the current trimmed window. The video is fine.
 *  - `no-caption-track`  YouTube was asked and served no caption track at all.
 *  - `no-transcript`     the archive holds no transcript for this video.
 */
export type CaptionsState =
  | 'parked'
  | 'no-cue-in-window'
  | 'no-caption-track'
  | 'no-transcript';

/** Facts the caption display can be derived from. */
export interface CaptionsFacts {
  /** Backend's per-video park reason. Absent/blank on older builds. */
  parkedReason?: string | null;
  /** Archive panel payload's `has_transcript`. */
  hasTranscript: boolean;
  /** `/api/subtitles` `has_subtitles`; null/undefined = not measured. */
  hasSubtitles?: boolean | null;
  /** Caption/transcript cues that fall inside the current (trimmed) window. */
  cuesInWindow: number;
  /** URL-only YouTube preview (subtitles fetched live, not from the archive). */
  subtitlesOnly: boolean;
}

/**
 * Read `captions_parked_reason` off a video row, tolerantly.
 *
 * Returns null for a missing key, a non-string, or a blank string — all of
 * which mean "not parked" and must leave the caller's existing rendering
 * untouched. Never throws on a malformed row.
 */
export function parkedReasonOf(row: unknown): string | null {
  if (!row || typeof row !== 'object') return null;
  const raw = (row as { captions_parked_reason?: unknown }).captions_parked_reason;
  if (typeof raw !== 'string') return null;
  const trimmed = raw.trim();
  return trimmed ? trimmed : null;
}

/**
 * The two park flavours the backend words differently, plus a catch-all.
 *
 * The backend sends free-text English prose, not an enum, so the UI derives
 * the variant from the wording to pick a LOCALIZED sentence. The verbatim
 * reason is still rendered alongside it — this only chooses the translation,
 * it never replaces the authoritative text.
 */
export type ParkVariant = 'no-session' | 'rejected-session' | 'unknown';

export function parkVariant(reason: string | null | undefined): ParkVariant {
  if (!reason) return 'unknown';
  const low = reason.toLowerCase();
  if (low.includes('session was rejected') || low.includes('rejected')) return 'rejected-session';
  if (low.includes('no signed-in') || low.includes('no signed')) return 'no-session';
  return 'unknown';
}

/**
 * Which of the four facts the caption display is currently showing.
 *
 * `parked` wins outright: it is the only state the user can clear, and it
 * explains the absence rather than merely reporting it. `no-cue-in-window`
 * outranks the two "nothing at all" states because a transcript with cues in
 * it is a fundamentally different fact from one that has none.
 */
export function captionsState(f: CaptionsFacts): CaptionsState {
  if (f.parkedReason) return 'parked';
  if (f.cuesInWindow > 0) return 'no-cue-in-window';
  if (f.subtitlesOnly) return 'no-caption-track';
  return 'no-transcript';
}

/**
 * The sweep's `age_parked` counter, normalized.
 *
 * Absent (older backend), null, or garbage all read as 0, which the caller
 * renders as "no clause at all" — so an older build's summary line stays
 * byte-identical to today.
 */
export function ageParkedCount(job: { age_parked?: number | null } | null | undefined): number {
  const n = job?.age_parked;
  if (typeof n !== 'number' || !Number.isFinite(n) || n <= 0) return 0;
  return Math.floor(n);
}

/**
 * Find this video's park reason among `/api/archive/videos` rows.
 *
 * Scoped to the platform (and channel when known) so a preview does not pull
 * the whole archive. Returns null — never throws — on any failure, so a
 * missing park is indistinguishable from an unreachable backend and both
 * leave existing behaviour intact.
 */
export async function fetchParkedReason(
  platform: string | null | undefined,
  videoId: string | null | undefined,
  channel?: string | null,
): Promise<string | null> {
  const plat = (platform || '').trim().toLowerCase();
  const vid = (videoId || '').trim();
  if (!plat || !vid) return null;
  const params = new URLSearchParams({ platform: plat });
  const chan = (channel || '').trim();
  if (chan) params.set('channel', chan);
  try {
    const res = await apiGet<{ videos?: unknown[] }>(`/api/archive/videos?${params.toString()}`);
    const rows = Array.isArray(res?.videos) ? res.videos : [];
    for (const row of rows) {
      const r = row as { video_id?: unknown };
      if (String(r?.video_id ?? '') !== vid) continue;
      // First match wins; the backend stamps at most one reason per video.
      const reason = parkedReasonOf(row);
      if (reason) return reason;
    }
    return null;
  } catch {
    return null;
  }
}

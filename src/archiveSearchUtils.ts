/**
 * Pure helpers for the archive search UI (ArchiveSearchPopup).
 * No DOM, no network — everything here is unit-testable.
 */

export interface ArchiveSearchHit {
  /** 'title' = local video-title match; 'youtube' = remote channel-search hit. */
  kind: 'transcript' | 'message' | 'title' | 'youtube';
  platform: string;
  /** Every platform where the same canonical VOD exists (dedupe membership),
   *  always including `platform`. Absent on older backend responses —
   *  consumers fall back to [platform] (see hitPlatforms). */
  platforms?: string[];
  video_id: string;
  offset_sec: number;
  text: string;
  /** Chat author (message hits only): displayed name or @handle as stored. */
  author?: string | null;
  score: number;
  /** Extras from the owning videos row (null when no video row exists). */
  channel?: string | null;
  title?: string | null;
  /** WS-4: original (non-auto-translated) title when the API supplied it. */
  originalTitle?: string | null;
  date?: string | null;
  video_kind?: string | null;
  /** Transcript language tag ('pt' | 'en' | other code); null for chat rows. */
  lang?: string | null;
  /** WS-3: detected channel language of the hit's channel (null = unknown). */
  channel_language?: string | null;
  /** Remote hits only. */
  duration_sec?: number | null;
  duration_string?: string | null;
  thumbnail_url?: string | null;
  /** Concept (embedding) hit — true only for the semantic search pass. */
  semantic?: boolean;
  /** True when the hit matched fewer than all query tokens (closest-match). */
  partial?: boolean;
}

export interface ArchiveVideoRow {
  platform: string;
  video_id: string;
  channel?: string | null;
  title?: string | null;
  /** WS-4: original (non-auto-translated) title when the API supplied it. */
  originalTitle?: string | null;
  started_at?: string | null;
  ended_at?: string | null;
  duration_sec?: number | null;
  archive_path?: string | null;
  canonical_key?: string | null;
  status?: string | null;
  kind?: string | null;
}

export const ARCHIVE_PLATFORMS = ['youtube', 'twitch', 'kick'] as const;
export const ARCHIVE_KINDS = ['vod', 'clip', 'short', 'live'] as const;
export type ArchiveKind = (typeof ARCHIVE_KINDS)[number];

/**
 * Kind values offered as search filter chips — LIVE is hidden so search is
 * VOD-only (kindLabel('live') still renders badges on stored hits).
 */
export const ARCHIVE_FILTER_KINDS = ARCHIVE_KINDS.filter((k) => k !== 'live');

export const ARCHIVE_SOURCES = ['both', 'transcript', 'chat'] as const;
export type ArchiveSource = (typeof ARCHIVE_SOURCES)[number];

/** Transcript language filter values sent to /api/archive/search?lang=… */
export const ARCHIVE_LANGS = ['pt', 'en'] as const;
export type ArchiveLang = (typeof ARCHIVE_LANGS)[number];

export const ARCHIVE_LANG_LABELS: Record<string, string> = {
  pt: 'PT-BR',
  en: 'EN',
};

export const ARCHIVE_KIND_LABELS: Record<ArchiveKind, string> = {
  vod: 'VOD',
  clip: 'CLIP',
  short: 'SHORT',
  live: 'LIVE',
};

/** i18n keys for the source-filter labels. The query PARAM values stay
 *  stable (both|transcript|chat — buildSearchUrl sends them untouched);
 *  only the display words changed (STREAMER → speech, BOTH → both). */
export const ARCHIVE_SOURCE_LABELS: Record<ArchiveSource, string> = {
  both: 'both',
  transcript: 'speech',
  chat: 'chat',
};

/** Upper-case label for a kind value; unknown/empty stays as-is. */
export function kindLabel(kind: string | null | undefined): string {
  if (!kind) return '';
  const label = ARCHIVE_KIND_LABELS[kind as ArchiveKind];
  return label ?? String(kind);
}

/**
 * Every platform where the hit's canonical VOD exists, PRIMARY platform
 * first. The backend `platforms` field (dedupe membership) is optional and
 * may arrive in dedupe-view order — normalize to primary-first, deduped,
 * lowercase so callers can rely on the order (logo row, tie-breaks).
 */
export function hitPlatforms(hit: Pick<ArchiveSearchHit, 'platform' | 'platforms'>): string[] {
  const list = Array.isArray(hit.platforms) && hit.platforms.length > 0 ? hit.platforms : [hit.platform];
  const ordered: string[] = [];
  for (const p of [(hit.platform || '').toLowerCase(), ...list]) {
    const k = (p || '').toLowerCase();
    if (k && !ordered.includes(k)) ordered.push(k);
  }
  return ordered;
}

/** True for a real calendar date in YYYY-MM-DD (2026-02-30 is invalid). */
export function isValidDateParam(s: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(s)) return false;
  const [y, m, d] = s.split('-').map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d));
  return dt.getUTCFullYear() === y && dt.getUTCMonth() === m - 1 && dt.getUTCDate() === d;
}

/** Local-timezone YYYY-MM-DD for "today" (the date pickers show local dates). */
export function todayIso(): string {
  const d = new Date();
  const m = `${d.getMonth() + 1}`.padStart(2, '0');
  const day = `${d.getDate()}`.padStart(2, '0');
  return `${d.getFullYear()}-${m}-${day}`;
}

/** Local calendar date of `d` as a UTC-midnight timestamp (day granularity). */
function localDayNumber(d: Date): number {
  return Date.UTC(d.getFullYear(), d.getMonth(), d.getDate());
}

/**
 * Relative "X ago" label for an ISO timestamp vs the current local date
 * (same local-timezone discipline as todayIso). Ladder: today → yesterday
 * → 2–13 days → 2–5 weeks → 1–11 months → years. Null/empty/invalid input
 * → null; future dates read as "today" (defensive).
 *
 * `now` is injectable so tests are deterministic; production uses new Date().
 */
export function formatRelativeDate(iso: string | null | undefined, now: Date = new Date()): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  const days = Math.round((localDayNumber(now) - localDayNumber(d)) / 86_400_000);
  if (days <= 0) return 'today';
  if (days === 1) return 'yesterday';
  if (days <= 13) return `${days} days ago`;
  if (days <= 35) return `${Math.floor(days / 7)} weeks ago`; // 2–5 weeks, always plural
  // Whole calendar months; a day-of-month later than today's means the
  // latest month isn't complete yet (a >35-day gap always spans ≥1 month).
  let months = (now.getFullYear() - d.getFullYear()) * 12 + (now.getMonth() - d.getMonth());
  if (d.getDate() > now.getDate()) months -= 1;
  if (months < 12) return months === 1 ? '1 month ago' : `${months} months ago`;
  const years = Math.floor(months / 12);
  return years === 1 ? '1 year ago' : `${years} years ago`;
}

export interface ArchiveSearchFilterParams {
  query: string;
  /** Empty/omitted = all channels; comma-joined slugs match ANY of them. */
  channel?: string | null;
  /** Empty = all platforms; multiple join as comma-separated. */
  platforms?: readonly string[] | null;
  /** Empty = all kinds; multiple join as comma-separated. */
  kinds?: readonly string[] | null;
  /** 'both' (default) | 'chat' | 'transcript' — omitted from the URL when 'both'. */
  source?: ArchiveSource | null;
  /** Scope the search to a single archived video id; omitted when unset. */
  videoId?: string | null;
  /** YYYY-MM-DD inclusive bounds on started_at; empty = unset. */
  dateFrom?: string | null;
  dateTo?: string | null;
  /** Transcript language filter ('pt' | 'en'); omitted when unset. */
  lang?: string | null;
  /** Chat author filter — comma-separate multiple users ("a,b" matches ANY;
   *  '@' tolerated — YouTube stores the @handle, Twitch/Kick the displayed
   *  name). With an empty query the backend returns those authors' whole
   *  history, newest first. Omitted when unset. */
  username?: string | null;
  limit?: number;
  /** Explicitly opt OUT of the backend's auto channel-scope (hint=0). */
  hint?: boolean;
  /** Concept search: embedding pass over transcript segments (semantic=1). */
  semantic?: boolean;
}

/** Query-string for GET /api/archive/search — omits unset filters. */
export function buildSearchUrl(p: ArchiveSearchFilterParams): string {
  const params = new URLSearchParams();
  params.set('q', p.query.trim());
  if (p.channel) params.set('channel', p.channel);
  const platforms = (p.platforms ?? []).filter(Boolean);
  if (platforms.length > 0) params.set('platform', platforms.join(','));
  const kinds = (p.kinds ?? []).filter(Boolean);
  if (kinds.length > 0) params.set('kind', kinds.join(','));
  if (p.source && p.source !== 'both') params.set('source', p.source);
  if (p.videoId) params.set('video_id', p.videoId);
  const dateFrom = p.dateFrom && isValidDateParam(p.dateFrom) ? p.dateFrom : null;
  const dateTo = p.dateTo && isValidDateParam(p.dateTo) ? p.dateTo : null;
  if (dateFrom) params.set('date_from', dateFrom);
  if (dateTo) params.set('date_to', dateTo);
  if (p.lang) params.set('lang', p.lang);
  const username = (p.username ?? '').trim().replace(/^@/, '');
  if (username) params.set('username', username);
  if (p.hint === false) params.set('hint', '0');
  if (p.semantic) params.set('semantic', '1');
  params.set('limit', String(p.limit ?? 30));
  return `/api/archive/search?${params.toString()}`;
}

export interface ArchiveChatMessage {
  platform: string;
  video_id: string;
  offset_sec: number;
  user_id?: string;
  username: string;
  text: string;
  badges?: string | null;
  emotes?: string | null;
  /** Platform chat username color (#RRGGBB); null = palette fallback. */
  color?: string | null;
  ts?: string | null;
  /** Collapsed duplicate run length (1 = a single message, N = N identical). */
  spam_count?: number;
}

/** Background indexing work kicked by a search (backend fires it lazily). */
export interface ArchiveEnrichEntry {
  platform: string;
  video_id: string;
  kind: 'transcript' | 'chat_backfill';
  channel?: string;
  title?: string;
}

/** GET /api/archive/search response (enriching/channel_hint always present). */
export interface ArchiveSearchResponse {
  hits: ArchiveSearchHit[];
  enriching: ArchiveEnrichEntry[];
  /** Set when the first query token auto-scoped the search to a channel. */
  channel_hint?: string;
}

/** Seconds → mm:ss (h:mm:ss past an hour). Negative/NaN clamp to 00:00. */
export function formatArchiveOffset(sec: number): string {
  const s = Number.isFinite(sec) ? Math.max(0, Math.floor(sec)) : 0;
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const rem = s % 60;
  const pad = (n: number) => n.toString().padStart(2, '0');
  return h > 0 ? `${h}:${pad(m)}:${pad(rem)}` : `${pad(m)}:${pad(rem)}`;
}

/**
 * Case-insensitive, word-boundary matches of every query word in `text`,
 * returned as `[start, end)` ranges (merged, sorted). Non-word chars inside
 * a query word (e.g. "part-1") still match literally — boundaries only apply
 * around the whole word, so "chat" never highlights inside "chatter".
 */
export function highlightQuerySpans(text: string, query: string): Array<{ start: number; end: number }> {
  if (!text) return [];
  const words = (query || '').toLowerCase().split(/\s+/).filter((w) => w.length > 0);
  if (words.length === 0) return [];
  const ranges: Array<{ start: number; end: number }> = [];
  const lower = text.toLowerCase();
  for (const word of words) {
    const esc = word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const re = new RegExp(`(^|[^a-z0-9])(${esc})(?![a-z0-9])`, 'gi');
    let m: RegExpExecArray | null;
    while ((m = re.exec(lower)) !== null) {
      // Group 2 start = match index + length of the boundary char (group 1).
      const start = m.index + m[1].length;
      ranges.push({ start, end: start + word.length });
      if (m[0].length === 0) re.lastIndex += 1; // paranoid: never loop forever
    }
  }
  if (ranges.length === 0) return [];
  ranges.sort((a, b) => a.start - b.start);
  const merged: Array<{ start: number; end: number }> = [ranges[0]];
  for (let i = 1; i < ranges.length; i += 1) {
    const prev = merged[merged.length - 1];
    const cur = ranges[i];
    if (cur.start <= prev.end) {
      if (cur.end > prev.end) prev.end = cur.end;
    } else {
      merged.push(cur);
    }
  }
  return merged;
}

/** Offset of the first query match in `text`, or -1. */
export function firstMatchIndex(text: string, query: string): number {
  const spans = highlightQuerySpans(text, query);
  return spans.length > 0 ? spans[0].start : -1;
}

/**
 * Compact snippet around the first match: up to `radius` chars on each side.
 * Elides with '…' when text was cut. No match → leading window of the text.
 */
export function snippetAroundMatch(text: string, query: string, radius = 48): string {
  if (!text) return '';
  const idx = firstMatchIndex(text, query);
  if (idx < 0) {
    return text.length > radius * 2 ? `${text.slice(0, radius * 2).trimEnd()}…` : text;
  }
  const start = Math.max(0, idx - radius);
  const end = Math.min(text.length, idx + query.trim().length + radius);
  const prefix = start > 0 ? '…' : '';
  const suffix = end < text.length ? '…' : '';
  return `${prefix}${text.slice(start, end).trim()}${suffix}`;
}

/** Messages strictly before the hit offset vs at/after it (marker line slot). */
export function groupChatWindow(
  messages: ArchiveChatMessage[],
  hitOffsetSec: number,
): { before: ArchiveChatMessage[]; after: ArchiveChatMessage[] } {
  const before: ArchiveChatMessage[] = [];
  const after: ArchiveChatMessage[] = [];
  for (const m of messages) {
    const off = Number.isFinite(m.offset_sec) ? m.offset_sec : 0;
    if (off < hitOffsetSec) before.push(m);
    else after.push(m);
  }
  return { before, after };
}

/**
 * Previewable URL for an archived VOD. Mirrors channelUtils.buildVodUrl for
 * the three archive platforms; the kick URL needs the channel slug, so an
 * unknown channel falls back to the plain /videos/ route.
 *
 * Returns '' for watchdog synthetic ids (youtube-live-<slug>-<epoch-ms>):
 * those rows have no real video on the platform (the URL would 404), so
 * callers skip the preview flow. Type stays string so every existing caller
 * keeps compiling; guards check falsiness.
 */
export function buildArchiveVodUrl(platform: string, videoId: string, channel?: string | null): string {
  const p = (platform || '').toLowerCase();
  const id = videoId.trim();
  if (/^[a-z]+-live-[a-z0-9_]+-\d+$/.test(id)) return '';
  if (p === 'twitch') return `https://www.twitch.tv/videos/${id.replace(/^v/, '')}`;
  if (p === 'youtube') return `https://www.youtube.com/watch?v=${id}`;
  const slug = (channel || '').trim();
  return slug ? `https://kick.com/${slug}/videos/${id}` : `https://kick.com/videos/${id}`;
}

/** One openable platform of a hit's canonical VOD — App picks among these. */
export interface ArchiveOpenTarget {
  platform: string;
  video: ArchiveVideoRow | undefined;
}

/**
 * Resolvable per-platform open targets for a hit, primary first. Sibling
 * platforms (same canonical VOD) resolve their video row via canonical_key
 * from the caller's video map; a sibling with no matching row is skipped
 * (no video_id → no playable URL). The primary target always exists — its
 * video row may be absent, in which case App falls back to hit.video_id.
 */
export function resolveOpenTargets(
  hit: ArchiveSearchHit,
  videos: Record<string, ArchiveVideoRow>,
): ArchiveOpenTarget[] {
  const primary = (hit.platform || '').toLowerCase();
  const primaryVideo = videos[`${primary}:${hit.video_id}`];
  const targets: ArchiveOpenTarget[] = [{ platform: primary, video: primaryVideo }];
  if (!primaryVideo?.canonical_key) return targets;
  const seen = new Set([primary]);
  for (const p of hitPlatforms(hit)) {
    if (seen.has(p)) continue;
    const sibling = Object.values(videos).find(
      (v) => (v.platform || '').toLowerCase() === p
        && v.canonical_key && v.canonical_key === primaryVideo.canonical_key,
    );
    if (sibling) {
      seen.add(p);
      targets.push({ platform: p, video: sibling });
    }
  }
  return targets;
}

/** Least-opened platform target — session playback balancing across the
 *  mirrors of a canonical VOD (the backend balances transcription
 *  extraction; this balances playback). Ties resolve to the earlier
 *  candidate (primary-first order); unknown counts read as 0. */
export function pickLeastOpenedTarget(
  targets: readonly ArchiveOpenTarget[],
  openCounts: Record<string, number>,
): ArchiveOpenTarget {
  return targets.reduce((best, t) =>
    (openCounts[t.platform] ?? 0) < (openCounts[best.platform] ?? 0) ? t : best,
    targets[0],
  );
}

/**
 * Pure helpers for the archive search UI (ArchiveSearchPopup).
 * No DOM, no network — everything here is unit-testable.
 */

export interface ArchiveSearchHit {
  /** 'title' = local video-title match; 'youtube' = remote channel-search hit. */
  kind: 'transcript' | 'message' | 'title' | 'youtube';
  platform: string;
  video_id: string;
  offset_sec: number;
  text: string;
  score: number;
  /** Extras from the owning videos row (null when no video row exists). */
  channel?: string | null;
  title?: string | null;
  date?: string | null;
  video_kind?: string | null;
  /** Transcript language tag ('pt' | 'en' | other code); null for chat rows. */
  lang?: string | null;
  /** Remote hits only. */
  duration_sec?: number | null;
  duration_string?: string | null;
  thumbnail_url?: string | null;
}

export interface ArchiveVideoRow {
  platform: string;
  video_id: string;
  channel?: string | null;
  title?: string | null;
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

/** UI labels for the source filter — STREAMER is the user-facing word for transcript. */
export const ARCHIVE_SOURCE_LABELS: Record<ArchiveSource, string> = {
  both: 'BOTH',
  transcript: 'STREAMER',
  chat: 'CHAT',
};

/** Upper-case label for a kind value; unknown/empty stays as-is. */
export function kindLabel(kind: string | null | undefined): string {
  if (!kind) return '';
  const label = ARCHIVE_KIND_LABELS[kind as ArchiveKind];
  return label ?? String(kind);
}

/** True for a real calendar date in YYYY-MM-DD (2026-02-30 is invalid). */
export function isValidDateParam(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const [y, m, d] = value.split('-').map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d));
  return dt.getUTCFullYear() === y && dt.getUTCMonth() === m - 1 && dt.getUTCDate() === d;
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
  limit?: number;
  /** Explicitly opt OUT of the backend's auto channel-scope (hint=0). */
  hint?: boolean;
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
  if (p.hint === false) params.set('hint', '0');
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

/**
 * Channel and video utility functions extracted from App.tsx.
 */

import { parseVideoTs, parseHmsDurationString, fmtDuration, fmtDateAndAgo, fmtViews } from './formatters';
import type { ChannelVideo, SavedChannel, VideoInfo } from './types';

export function bestAvailableQuality(info: VideoInfo): string {
  if (info.qualities?.length) {
    return info.qualities[0].toLowerCase();
  }
  return 'source';
}

export function detectUrlPlatform(u: string): 'kick' | 'twitch' | null {
  const l = u.toLowerCase();
  if (l.includes('kick.com')) return 'kick';
  if (l.includes('twitch.tv')) return 'twitch';
  return null;
}

export function isClipUrl(u: string): boolean {
  const l = u.toLowerCase();
  if (l.includes('clips.twitch.tv')) return true;
  if (l.includes('twitch.tv') && l.includes('/clip/')) return true;
  if (l.includes('kick.com') && l.includes('/clips/')) return true;
  return false;
}

export function channelVideoDurationSec(v: ChannelVideo): number | null {
  if (v.duration != null && v.duration > 0) return Math.floor(v.duration);
  if (v.duration_string) return parseHmsDurationString(v.duration_string);
  return null;
}

/** Full VOD length for trim sliders — never derived from the current trim end. */

export function videoInfoDurationSec(info: VideoInfo | null | undefined): number {
  if (!info) return 3600;
  if (info.duration != null && info.duration > 0) return Math.floor(info.duration);
  const parsed = info.duration_string ? parseHmsDurationString(info.duration_string) : null;
  return parsed != null && parsed > 0 ? parsed : 3600;
}

export function isLikelyClip(v: ChannelVideo): boolean {
  if (v.content_kind === 'clip') {
    if (v.duration != null && v.duration > CLIP_MAX_DURATION_SEC) return false;
    return true;
  }
  if (v.platform === 'Kick' && v.id.toLowerCase().startsWith('clip_')) return true;
  const url = (v.url || '').toLowerCase();
  if (url.includes('/videos/') && !url.includes('/clips/') && !url.includes('/clip/')) {
    return false;
  }
  if (url.includes('/clips/') || url.includes('clips.twitch.tv') || url.includes('/clip/')) {
    if (v.duration != null && v.duration > CLIP_MAX_DURATION_SEC) return false;
    return true;
  }
  // Twitch GQL may return clip pages as twitch.tv/{login}/clip/{slug}
  if (v.platform === 'Twitch' && /\/clip\/[^/]+/i.test(url)) {
    if (v.duration != null && v.duration > CLIP_MAX_DURATION_SEC) return false;
    return true;
  }
  return false;
}

export function channelVideoKey(v: ChannelVideo): string {
  return `${v.platform}:${v.id}`;
}

export function mapApiChannelItem(v: ChannelVideo & { thumbnail?: string | null }): ChannelVideo {
  return {
    ...v,
    thumbnail_url: v.thumbnail_url ?? v.thumbnail ?? null,
  };
}

/** Merge feeds newest-first; incoming wins on duplicate ids (metadata refresh). */

export function mergeVodLists(existing: ChannelVideo[], incoming: ChannelVideo[]): ChannelVideo[] {
  const map = new Map<string, ChannelVideo>();
  for (const v of incoming.map(mapApiChannelItem)) {
    map.set(channelVideoKey(v), v);
  }
  for (const v of existing) {
    const k = channelVideoKey(v);
    if (!map.has(k)) map.set(k, v);
  }
  return Array.from(map.values()).sort(
    (a, b) => parseVideoTs(b.created_at) - parseVideoTs(a.created_at),
  );
}

/** Merge clip feeds; incoming wins on duplicate ids. Sorted by views desc. */

export function mergeClipLists(existing: ChannelVideo[], incoming: ChannelVideo[]): ChannelVideo[] {
  const map = new Map<string, ChannelVideo>();
  for (const v of incoming.map(mapApiChannelItem).filter(isLikelyClip)) {
    map.set(channelVideoKey(v), v);
  }
  for (const v of existing.filter(isLikelyClip)) {
    const k = channelVideoKey(v);
    if (!map.has(k)) map.set(k, v);
  }
  return Array.from(map.values()).sort(
    (a, b) => (Number(b.views) || 0) - (Number(a.views) || 0),
  );
}

export function channelClipsMissing(
  ch: SavedChannel,
  kickOn: boolean,
  twitchOn: boolean,
): boolean {
  const clips = ch.clipVideos ?? [];
  if (!ch.clipsFetched && clips.length === 0) return true;
  if (kickOn && ch.kickSlug?.trim() && !clips.some((v) => v.platform === 'Kick')) return true;
  if (twitchOn && ch.twitchSlug?.trim() && !clips.some((v) => v.platform === 'Twitch')) {
    return true;
  }
  return false;
}

/** Mirror of `channelClipsMissing` for the VODs cache. */

export function channelVodsMissing(
  ch: SavedChannel,
  kickOn: boolean,
  twitchOn: boolean,
): boolean {
  // Treat a never-fetched channel and a channel whose last VOD fetch
  // returned an empty list the same way: the data is missing for the
  // platforms the user has enabled and we should re-fetch on demand.
  if (!ch.updatedAt && (ch.vodVideos?.length ?? 0) === 0) return true;
  if (kickOn && ch.kickSlug?.trim() && !ch.vodVideos?.some((v) => v.platform === 'Kick')) return true;
  if (twitchOn && ch.twitchSlug?.trim() && !ch.vodVideos?.some((v) => v.platform === 'Twitch')) return true;
  return false;
}

export function buildVodUrl(v: ChannelVideo): string {
  const isTw = v.platform === 'Twitch';
  const isClip = isLikelyClip(v);
  const twitchId = isTw && v.id.startsWith('v') ? v.id.slice(1) : v.id;
  if (v.url) {
    const u = v.url;
    if (!isTw && isClip && u.includes('/videos/') && !u.includes('/clips/')) {
      const slug = v.channel || u.match(/kick\.com\/([^/]+)/i)?.[1] || '';
      return `https://kick.com/${slug}/clips/${v.id}`;
    }
    return u;
  }
  if (isTw) {
    return isClip
      ? `https://clips.twitch.tv/${twitchId}`
      : `https://www.twitch.tv/videos/${twitchId}`;
  }
  return isClip
    ? `https://kick.com/${v.channel || ''}/clips/${v.id}`
    : `https://kick.com/${v.channel || ''}/videos/${v.id}`;
}

export function parseChannelInput(raw: string): { displayName: string; kickSlug: string; twitchSlug: string } {
  const trimmed = raw.trim();
  if (!trimmed) return { displayName: '', kickSlug: '', twitchSlug: '' };
  const lower = trimmed.toLowerCase();
  if (lower.includes('kick.com')) {
    const m = trimmed.match(/kick\.com\/([^/?#]+)/i);
    const slug = m?.[1] || trimmed;
    return { displayName: slug, kickSlug: slug, twitchSlug: slug };
  }
  if (lower.includes('twitch.tv')) {
    const m = trimmed.match(/twitch\.tv\/([^/?#]+)/i);
    const slug = m?.[1] || trimmed;
    return { displayName: slug, kickSlug: slug, twitchSlug: slug };
  }
  const slug = trimmed.replace(/^https?:\/\//, '').split('/').pop()?.split('?')[0] || trimmed;
  return { displayName: slug, kickSlug: slug, twitchSlug: slug };
}

/** Settings/section captions — not <label> so clicks never focus nearby inputs. */

export function channelPlatformErrors(ch: SavedChannel, mode: 'vods' | 'clips'): Record<string, string> {
  return mode === 'clips' ? (ch.clipErrors ?? {}) : (ch.vodErrors ?? (ch as SavedChannel & { errors?: Record<string, string> }).errors ?? {});
}

export function formatChannelErrorMessage(
  ch: SavedChannel,
  mode: 'vods' | 'clips',
  kickEnabled: boolean,
  twitchEnabled: boolean,
): string | null {
  const errs = channelPlatformErrors(ch, mode);
  const errKeys = Object.keys(errs).filter((k) => {
    if (!errs[k]) return false;
    if (k === 'Kick' && !kickEnabled) return false;
    if (k === 'Twitch' && !twitchEnabled) return false;
    return true;
  });
  if (errKeys.length === 0) return null;
  const hasItems = mode === 'clips'
    ? (ch.clipVideos?.length ?? 0) > 0
    : (ch.vodVideos?.length ?? 0) > 0;
  return hasItems
    ? `Partial results — ${errKeys.map((k) => `${k}: ${errs[k]}`).join(' | ')}`
    : errKeys.map((k) => `${k}: ${errs[k]}`).join(' | ');
}

export function normalizeSavedChannel(ch: SavedChannel): SavedChannel {
  const { videos: legacy, ...rest } = ch;
  let vodVideos = ch.vodVideos;
  let clipVideos = ch.clipVideos;
  if (vodVideos === undefined && clipVideos === undefined && Array.isArray(legacy)) {
    vodVideos = legacy.filter((v) => !isLikelyClip(v));
    clipVideos = legacy.filter(isLikelyClip);
  }
  const legacyErrors = (ch as SavedChannel & { errors?: Record<string, string> }).errors ?? {};
  return {
    ...rest,
    vodVideos: vodVideos ?? [],
    clipVideos: clipVideos ?? [],
    vodErrors: ch.vodErrors ?? legacyErrors,
    clipErrors: ch.clipErrors ?? {},
    clipsFetched: ch.clipsFetched ?? (clipVideos?.length ?? 0) > 0,
    loading: false,
  };
}

export function loadSavedChannels(): SavedChannel[] {
  try {
    const raw = localStorage.getItem(CHANNELS_STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.map((ch) => normalizeSavedChannel(ch as SavedChannel));
  } catch {
    return [];
  }
}

export function persistChannels(channels: SavedChannel[]) {
  const toStore = channels.map(({ loading: _loading, ...ch }) => ch);
  localStorage.setItem(CHANNELS_STORAGE_KEY, JSON.stringify(toStore));
}

/** Insert-before index (0..rowCount) from pointer Y — stable while the list is not reordered mid-drag. */

export function channelInsertIndex(listEl: HTMLElement, clientY: number): number {
  const rows = [...listEl.querySelectorAll<HTMLElement>('[data-channel-row]')];
  if (!rows.length) return 0;

  let bestIndex = rows.length;
  let bestDist = Infinity;
  for (let i = 0; i <= rows.length; i++) {
    let boundaryY: number;
    if (i === 0) {
      boundaryY = rows[0].getBoundingClientRect().top;
    } else if (i === rows.length) {
      boundaryY = rows[rows.length - 1].getBoundingClientRect().bottom;
    } else {
      const above = rows[i - 1].getBoundingClientRect();
      const below = rows[i].getBoundingClientRect();
      boundaryY = (above.bottom + below.top) / 2;
    }
    const dist = Math.abs(clientY - boundaryY);
    if (dist < bestDist) {
      bestDist = dist;
      bestIndex = i;
    }
  }
  return bestIndex;
}

export function reorderChannelsById(
  channels: SavedChannel[],
  channelId: string,
  insertBefore: number,
): SavedChannel[] {
  const from = channels.findIndex((c) => c.id === channelId);
  if (from < 0) return channels;
  let to = Math.max(0, Math.min(insertBefore, channels.length));
  if (from < to) to -= 1;
  if (from === to) return channels;
  const next = [...channels];
  const [item] = next.splice(from, 1);
  next.splice(to, 0, item);
  return next;
}

export function channelVodSubline(v: ChannelVideo): string {
  const parts: string[] = [];
  const when = fmtDateAndAgo(v.created_at);
  if (when) parts.push(when);
  const durSec = channelVideoDurationSec(v);
  if (durSec != null) parts.push(fmtDuration(durSec));
  if (v.views != null && Number(v.views) > 0) {
    parts.push(`${fmtViews(Number(v.views))} views`);
  }
  return parts.join(' · ');
}

export function detectVideoPlatform(info: VideoInfo | null, url: string): 'kick' | 'twitch' | null {
  const p = info?.platform?.toLowerCase();
  if (p === 'kick') return 'kick';
  if (p === 'twitch') return 'twitch';
  return detectUrlPlatform(url);
}

export function preferHeightFromQuality(quality: string): number {
  const q = (quality || 'source').toLowerCase();
  if (q === 'source') return 10_000;
  const m = q.match(/(\d+)/);
  return m ? parseInt(m[1], 10) : 1080;
}

export function pickSizeForQuality(
  sizes: Record<string, number>,
  quality: string,
): number | undefined {
  const q = (quality || 'source').toLowerCase();
  if (sizes[quality] || sizes[q]) return sizes[quality] ?? sizes[q];
  const preferH = preferHeightFromQuality(quality);
  let bestKey: string | null = null;
  let bestH = -1;
  for (const key of Object.keys(sizes)) {
    const m = key.match(/(\d+)/);
    const h = m ? parseInt(m[1], 10) : (key.toLowerCase() === 'source' ? 10_000 : 0);
    if (preferH >= 10_000) {
      if (h > bestH) {
        bestH = h;
        bestKey = key;
      }
    } else if (h <= preferH && h > bestH) {
      bestH = h;
      bestKey = key;
    }
  }
  if (bestKey) return sizes[bestKey];
  if (preferH < 10_000) {
    for (const key of Object.keys(sizes)) {
      const m = key.match(/(\d+)/);
      const h = m ? parseInt(m[1], 10) : 0;
      if (h >= preferH) return sizes[key];
    }
  }
  return sizes.source ?? Math.max(...Object.values(sizes));
}

export function estimateDownloadBytes(
  videoInfo: VideoInfo | null,
  quality: string,
  clipSec: number,
  fullDurationSec: number,
): number {
  if (clipSec <= 0) return 0;
  const fullDur = Math.max(clipSec, fullDurationSec || clipSec);
  const sizes = videoInfo?.size_by_quality;
  if (sizes && Object.keys(sizes).length > 0) {
    const fullBytes = pickSizeForQuality(sizes, quality);
    if (fullBytes && fullBytes > 0) {
      return Math.round(fullBytes * (clipSec / fullDur));
    }
  }
  if (videoInfo?.estimated_bytes && fullDur > 0) {
    return Math.round(videoInfo.estimated_bytes * (clipSec / fullDur));
  }
  const mbPerMin = LEGACY_MB_PER_MIN[quality] || 70;
  return Math.round((clipSec / 60) * mbPerMin * 1024 * 1024);
}

/**
 * Returns a cap-adjusted trim window when the estimated download exceeds 1 GB.
 * Returns null when no cap is needed.
 */
export function capDownloadToMaxBytes(
  estimatedBytes: number,
  durationSec: number,
  trimStartSec: number,
  trimEndSec: number,
): { trimEnd: number; estimatedBytes: number } | null {
  if (estimatedBytes <= MAX_DOWNLOAD_BYTES) return null;
  const clipSec = Math.max(1, trimEndSec - trimStartSec);
  const ratio = MAX_DOWNLOAD_BYTES / estimatedBytes;
  const newClipSec = Math.floor(clipSec * ratio);
  const newTrimEnd = trimStartSec + newClipSec;
  if (newTrimEnd <= trimStartSec) return null;
  return {
    trimEnd: newTrimEnd,
    estimatedBytes: Math.floor(estimatedBytes * ratio),
  };
}


export function loadStoredChannelUi(): {
  kick: boolean;
  twitch: boolean;
  content: 'vods' | 'clips';
} {
  try {
    const raw = localStorage.getItem(CHANNEL_UI_STORAGE_KEY);
    if (!raw) return { kick: true, twitch: true, content: 'vods' };
    const p = JSON.parse(raw) as {
      kick?: boolean;
      twitch?: boolean;
      content?: string;
    };
    return {
      kick: p.kick !== false,
      twitch: p.twitch !== false,
      content: p.content === 'clips' ? 'clips' : 'vods',
    };
  } catch {
    return { kick: true, twitch: true, content: 'vods' };
  }
}

export const CHANNEL_INITIAL_VISIBLE = 5;

export const CHANNEL_EXPAND_STEP = 10;

export const CHANNEL_FETCH_LIMIT = 100;
/** Cheap head fetch on page load — merge only ids not already cached. */

export const CHANNEL_INCREMENTAL_LIMIT = 25;

export const CHANNELS_STORAGE_KEY = 'vodrip_saved_channels';

export const CHANNEL_UI_STORAGE_KEY = 'vodrip_channel_ui';

export const MAX_SAVED_CHANNELS = 10;
/** Highest quality from API list, or source when none listed (Kick). */

export const CLIP_MAX_DURATION_SEC = 60;

/** 1 GB download cap — when estimated size exceeds this, the UI warns. */
export const MAX_DOWNLOAD_BYTES = 1_073_741_824;


export const LEGACY_MB_PER_MIN: Record<string, number> = {
  source: 112, '1080p60': 112, '1080p': 75,
  '720p60': 44, '720p': 42, '480p': 24, '360p': 14,
};

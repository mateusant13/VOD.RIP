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

export function detectUrlPlatform(u: string): 'kick' | 'twitch' | 'youtube' | null {
  const l = u.toLowerCase();
  if (l.includes('kick.com')) return 'kick';
  if (l.includes('twitch.tv')) return 'twitch';
  if (l.includes('youtube.com') || l.includes('youtu.be')) return 'youtube';
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
  youtubeOn = false,
): boolean {
  const clips = ch.clipVideos ?? [];
  if (!ch.clipsFetched && clips.length === 0) return true;
  if (kickOn && ch.kickSlug?.trim() && !clips.some((v) => v.platform === 'Kick')) return true;
  if (twitchOn && ch.twitchSlug?.trim() && !clips.some((v) => v.platform === 'Twitch')) {
    return true;
  }
  if (youtubeOn && ch.youtubeSlug?.trim() && !clips.some((v) => v.platform === 'YouTube')) return true;
  return false;
}

/** YouTube stream archives (/streams) — stored in vodVideos with content_kind stream. */
export function channelStreamsMissing(
  ch: SavedChannel,
  youtubeOn: boolean,
): boolean {
  if (!youtubeOn || !ch.youtubeSlug?.trim()) return false;
  if (!ch.streamsFetched) return true;
  return false;
}

/** Mirror of `channelClipsMissing` for the VODs cache. */

export function channelVodsMissing(
  ch: SavedChannel,
  kickOn: boolean,
  twitchOn: boolean,
  youtubeOn = false,
): boolean {
  if (!ch.updatedAt && (ch.vodVideos?.length ?? 0) === 0) return true;
  if (kickOn && ch.kickSlug?.trim() && !ch.vodVideos?.some((v) => v.platform === 'Kick')) return true;
  if (twitchOn && ch.twitchSlug?.trim() && !ch.vodVideos?.some((v) => v.platform === 'Twitch')) return true;
  if (youtubeOn && ch.youtubeSlug?.trim()
    && !ch.vodVideos?.some((v) => v.platform === 'YouTube' && v.content_kind !== 'stream')) {
    return true;
  }
  return false;
}

/** Resolve Twitch/Kick thumbnail templates with width/height placeholders. */
export function resolveVideoThumbnail(
  url: string | null | undefined,
  width = 160,
  height = 90,
): string | null {
  if (!url?.trim()) return null;
  const w = String(width);
  const h = String(height);
  return url
    .replace(/%\{width\}/g, w)
    .replace(/%\{height\}/g, h)
    .replace(/\{width\}/g, w)
    .replace(/\{height\}/g, h);
}

/** Cached channel VOD/clip thumbnail for a URL (buildVodUrl match). */
export function findCachedVideoThumbnail(
  videoUrl: string,
  channels: SavedChannel[],
): string | null {
  const needle = videoUrl.trim().toLowerCase();
  if (!needle) return null;
  for (const ch of channels) {
    const videos = [...(ch.vodVideos ?? []), ...(ch.clipVideos ?? [])];
    for (const v of videos) {
      if (buildVodUrl(v).trim().toLowerCase() === needle) {
        return v.thumbnail_url ?? null;
      }
    }
  }
  return null;
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
  if (v.platform === 'YouTube') {
    return v.url || `https://www.youtube.com/watch?v=${v.id}`;
  }
  return isClip
    ? `https://kick.com/${v.channel || ''}/clips/${v.id}`
    : `https://kick.com/${v.channel || ''}/videos/${v.id}`;
}

/** Display name stored on SavedChannel — derived from per-platform slugs. */
export function deriveChannelDisplayName(
  kickSlug: string,
  twitchSlug: string,
  youtubeSlug = '',
): string {
  const parts: string[] = [];
  const seen = new Set<string>();
  for (const raw of [twitchSlug, kickSlug, youtubeSlug]) {
    const s = raw.trim();
    if (!s) continue;
    const key = s.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    parts.push(s);
  }
  return parts.join(' / ');
}

/** Slugs for a VOD/clip URL — Twitch /videos/{id} needs channelLogin from API metadata. */
export function slugFromVideoUrl(
  url: string,
  platform: 'kick' | 'twitch' | 'youtube' | null,
  uploader?: string | null,
  channelLogin?: string | null,
): { kickSlug: string; twitchSlug: string; youtubeSlug: string } {
  const trimmed = url.trim();
  const lower = trimmed.toLowerCase();
  const login = (channelLogin || uploader || '').trim();
  const all = (slug: string) => ({ kickSlug: slug, twitchSlug: slug, youtubeSlug: slug });

  if (platform === 'youtube' || lower.includes('youtube.com') || lower.includes('youtu.be')) {
    const fromUrl = youtubeSlugFromChannelUrl(trimmed);
    if (fromUrl) return { kickSlug: '', twitchSlug: '', youtubeSlug: fromUrl };
    const handle = trimmed.match(/youtube\.com\/@([^/?#]+)/i)?.[1];
    const slug = handle || login;
    if (slug) return all(slug);
    return { kickSlug: '', twitchSlug: '', youtubeSlug: '' };
  }

  if (platform === 'kick' || lower.includes('kick.com')) {
    const path = trimmed.match(/kick\.com\/([^/?#]+)(?:\/([^/?#]+))?/i);
    const seg1 = path?.[1] ?? '';
    const seg2 = (path?.[2] ?? '').toLowerCase();
    if (seg1 && !['videos', 'clips'].includes(seg1.toLowerCase())) {
      if (!seg2 || seg2 === 'videos' || seg2 === 'clips') {
        return all(seg1);
      }
    }
    if (login) return all(login);
    return { kickSlug: '', twitchSlug: '', youtubeSlug: '' };
  }

  if (platform === 'twitch' || lower.includes('twitch.tv')) {
    const loginPath = trimmed.match(/twitch\.tv\/([^/?#]+)\/clip\//i);
    const loginSeg = loginPath?.[1]?.toLowerCase();
    if (loginSeg && !['videos', 'clip', 'directory', 'clips'].includes(loginSeg)) {
      return all(loginPath![1]);
    }
    if (login) return all(login);
    return { kickSlug: '', twitchSlug: '', youtubeSlug: '' };
  }

  if (login) return all(login);
  return { kickSlug: '', twitchSlug: '', youtubeSlug: '' };
}

export function isChannelAlreadySaved(
  kickSlug: string,
  twitchSlug: string,
  channels: SavedChannel[],
  youtubeSlug = '',
): boolean {
  const k = kickSlug.trim().toLowerCase();
  const t = twitchSlug.trim().toLowerCase();
  const y = youtubeSlug.trim().toLowerCase();
  if (!k && !t && !y) return false;
  return channels.some((ch) => {
    const ck = (ch.kickSlug || '').toLowerCase();
    const ct = (ch.twitchSlug || '').toLowerCase();
    const cy = (ch.youtubeSlug || '').toLowerCase();
    for (const slug of [k, t, y]) {
      if (!slug) continue;
      if (slug === ck || slug === ct || slug === cy) return true;
    }
    return false;
  });
}

/** Parse YouTube channel handle/id from a channel URL (not watch/shorts links). */
export function youtubeSlugFromChannelUrl(raw: string): string {
  const trimmed = raw.trim();
  const lower = trimmed.toLowerCase();
  if (!lower.includes('youtube.com') && !lower.includes('youtu.be')) return '';
  const handle = trimmed.match(/youtube\.com\/@([^/?#]+)/i)?.[1];
  if (handle) return handle;
  const channelId = trimmed.match(/youtube\.com\/channel\/(UC[^/?#]+)/i)?.[1];
  if (channelId) return channelId;
  const custom = trimmed.match(/youtube\.com\/c\/([^/?#]+)/i)?.[1];
  if (custom) return custom;
  const user = trimmed.match(/youtube\.com\/user\/([^/?#]+)/i)?.[1];
  if (user) return user;
  return '';
}

export function parseChannelInput(raw: string): {
  displayName: string;
  kickSlug: string;
  twitchSlug: string;
  youtubeSlug: string;
} {
  const trimmed = raw.trim();
  if (!trimmed) return { displayName: '', kickSlug: '', twitchSlug: '', youtubeSlug: '' };
  const lower = trimmed.toLowerCase();
  if (lower.includes('kick.com')) {
    const m = trimmed.match(/kick\.com\/([^/?#]+)/i);
    const slug = m?.[1] || trimmed;
    return { displayName: slug, kickSlug: slug, twitchSlug: slug, youtubeSlug: slug };
  }
  if (lower.includes('twitch.tv')) {
    const m = trimmed.match(/twitch\.tv\/([^/?#]+)/i);
    const slug = m?.[1] || trimmed;
    return { displayName: slug, kickSlug: slug, twitchSlug: slug, youtubeSlug: slug };
  }
  if (lower.includes('youtube.com') || lower.includes('youtu.be')) {
    const yt = youtubeSlugFromChannelUrl(trimmed);
    if (yt) return { displayName: yt, kickSlug: '', twitchSlug: '', youtubeSlug: yt };
    const tail = trimmed.replace(/^https?:\/\//, '').split('/').pop()?.split('?')[0] || trimmed;
    if (['videos', 'shorts', 'streams', 'featured', 'playlists', 'watch'].includes(tail.toLowerCase())) {
      return { displayName: '', kickSlug: '', twitchSlug: '', youtubeSlug: '' };
    }
    return { displayName: tail, kickSlug: '', twitchSlug: '', youtubeSlug: tail };
  }
  const slug = trimmed.replace(/^https?:\/\//, '').split('/').pop()?.split('?')[0] || trimmed;
  return { displayName: slug, kickSlug: slug, twitchSlug: slug, youtubeSlug: '' };
}

/** Settings/section captions — not <label> so clicks never focus nearby inputs. */

export function channelPlatformErrors(ch: SavedChannel, mode: 'vods' | 'clips' | 'streams'): Record<string, string> {
  return mode === 'clips' ? (ch.clipErrors ?? {}) : (ch.vodErrors ?? (ch as SavedChannel & { errors?: Record<string, string> }).errors ?? {});
}

/** Hide cookie/auth jargon from channel list banners. */
export function isHiddenChannelPlatformError(msg: string): boolean {
  const low = (msg || '').toLowerCase();
  return low.includes('cookie') || low.includes('dpapi') || low.includes('po_token')
    || low.includes('sign in to');
}

export function formatChannelErrorMessage(
  ch: SavedChannel,
  mode: 'vods' | 'clips' | 'streams',
  kickEnabled: boolean,
  twitchEnabled: boolean,
  youtubeEnabled = false,
): string | null {
  const errs = channelPlatformErrors(ch, mode);
  const errKeys = Object.keys(errs).filter((k) => {
    if (!errs[k]) return false;
    if (isHiddenChannelPlatformError(errs[k])) return false;
    if (k === 'Kick' && !kickEnabled) return false;
    if (k === 'Twitch' && !twitchEnabled) return false;
    if (k === 'YouTube' && !youtubeEnabled) return false;
    return true;
  });
  if (errKeys.length === 0) return null;
  const hasItems = mode === 'clips'
    ? (ch.clipVideos?.length ?? 0) > 0
    : mode === 'streams'
      ? (ch.vodVideos ?? []).some((v) => v.content_kind === 'stream')
      : (ch.vodVideos ?? []).some((v) => v.content_kind !== 'stream');
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
    youtubeSlug: ch.youtubeSlug ?? '',
    vodVideos: vodVideos ?? [],
    clipVideos: clipVideos ?? [],
    vodErrors: ch.vodErrors ?? legacyErrors,
    clipErrors: ch.clipErrors ?? {},
    clipsFetched: ch.clipsFetched ?? (clipVideos?.length ?? 0) > 0,
    streamsFetched: ch.streamsFetched ?? (vodVideos?.some((v) => v.content_kind === 'stream') ?? false),
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

export function detectVideoPlatform(info: VideoInfo | null, url: string): 'kick' | 'twitch' | 'youtube' | null {
  const p = info?.platform?.toLowerCase();
  if (p === 'kick') return 'kick';
  if (p === 'twitch') return 'twitch';
  if (p === 'youtube') return 'youtube';
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
  audioOnly = false,
): number {
  if (clipSec <= 0) return 0;
  const fullDur = Math.max(clipSec, fullDurationSec || clipSec);
  let raw = 0;
  const sizes = videoInfo?.size_by_quality;
  if (sizes && Object.keys(sizes).length > 0) {
    const fullBytes = pickSizeForQuality(sizes, quality);
    if (fullBytes && fullBytes > 0) {
      raw = Math.round(fullBytes * (clipSec / fullDur));
    }
  } else if (videoInfo?.estimated_bytes && fullDur > 0) {
    raw = Math.round(videoInfo.estimated_bytes * (clipSec / fullDur));
  } else {
    const mbPerMin = LEGACY_MB_PER_MIN[quality] || 70;
    raw = Math.round((clipSec / 60) * mbPerMin * 1024 * 1024);
  }
  if (audioOnly) {
    // ponytail: ~128kbps MP3 vs typical 1080p video — rough 8% of video estimate
    raw = Math.round(raw * 0.08);
  }
  return raw > 0 ? Math.round(raw * 0.97) : 0;
}

/**
 * Returns a cap-adjusted trim window when the estimated download exceeds 1 GB.
 * Returns null when no cap is needed.
 */
export function capDownloadToMaxBytes(
  estimatedBytes: number,
  _durationSec: number,
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
  youtube: boolean;
  content: 'vods' | 'clips' | 'streams';
} {
  try {
    const raw = localStorage.getItem(CHANNEL_UI_STORAGE_KEY);
    if (!raw) return { kick: true, twitch: true, youtube: true, content: 'vods' };
    const p = JSON.parse(raw) as {
      kick?: boolean;
      twitch?: boolean;
      youtube?: boolean;
      content?: string;
    };
    const content = p.content === 'clips' ? 'clips' : p.content === 'streams' ? 'streams' : 'vods';
    return {
      kick: p.kick !== false,
      twitch: p.twitch !== false,
      youtube: p.youtube !== false,
      content,
    };
  } catch {
    return { kick: true, twitch: true, youtube: true, content: 'vods' };
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

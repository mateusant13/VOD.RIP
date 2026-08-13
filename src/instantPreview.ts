/**
 * Instant-preview registry — the backend's local 6s clips for each saved
 * channel's most recent VOD (see backend InstantPreview worker).
 *
 * The UI fetches GET /api/previews/status once on mount (and after every
 * channel-list save), caches the map briefly, and matches an opened VOD URL
 * against it — exact `vod_url` first, then `vod_id`/`video_id` extracted from
 * the URL. A match lets the preview surface play the local clip instantly
 * while the remote session boots. Every failure degrades to an empty map, so
 * the normal preview flow is never blocked.
 */

export interface InstantPreviewEntry {
  channel_id: string;
  platform: 'twitch' | 'kick' | 'youtube';
  title: string;
  vod_url: string;
  vod_id: string;
  video_id: string | null;
  generated_at: string;
  /** Local MP4 served with HTTP Range support — absolute or app-relative. */
  media_url: string;
}

export interface InstantPreviewsResponse {
  previews: InstantPreviewEntry[];
}

/** How long a fetched status response is considered fresh. */
export const INSTANT_PREVIEWS_TTL_MS = 30_000;

let entries: InstantPreviewEntry[] = [];
let fetchedAt = 0;
let inflight: Promise<void> | null = null;

function normalizeUrl(url: string): string {
  return (url || '').trim().toLowerCase().replace(/\/+$/, '');
}

/**
 * Platform video id extracted from an opened VOD URL — mirrors the backend's
 * id shapes (Twitch numeric, YouTube 11-char, Kick uuid).
 */
export function videoIdFromOpenedUrl(url: string): string | null {
  const u = (url || '').trim();
  if (!u) return null;
  const twitch = /twitch\.tv\/videos\/(\d+)/i.exec(u) || /^(\d{6,})$/.exec(u);
  if (twitch) return twitch[1];
  const yt =
    /[?&]v=([\w-]{6,})/.exec(u) ||
    /youtu\.be\/([\w-]{6,})/.exec(u) ||
    /\/shorts\/([\w-]{6,})/.exec(u);
  if (yt) return yt[1];
  const kick = /kick\.com\/[^/]+\/videos\/([\da-f]{8}-(?:[\da-f]{4}-){3}[\da-f]{12})/i.exec(u);
  if (kick) return kick[1];
  return null;
}

/**
 * Best match for an opened VOD URL: exact (normalized) `vod_url` first, then
 * `vod_id`/`video_id` when the URL spelling differs (e.g. bare Twitch id,
 * youtu.be short link).
 */
export function findInstantPreview(openedUrl: string): InstantPreviewEntry | null {
  const needle = normalizeUrl(openedUrl);
  if (!needle) return null;
  for (const e of entries) {
    if (normalizeUrl(e.vod_url) === needle) return e;
  }
  const id = videoIdFromOpenedUrl(openedUrl);
  if (id) {
    const needleId = id.toLowerCase();
    for (const e of entries) {
      if (e.vod_id && e.vod_id.toLowerCase() === needleId) return e;
      if (e.video_id && e.video_id.toLowerCase() === needleId) return e;
    }
  }
  return null;
}

/**
 * (Re)fetch the instant-preview status. `force` bypasses the brief TTL — used
 * right after the channel list is saved so freshly generated clips are picked
 * up. Any failure resolves silently to an empty map.
 */
export function refreshInstantPreviews(force = false): Promise<void> {
  const now = Date.now();
  if (!force && fetchedAt && now - fetchedAt < INSTANT_PREVIEWS_TTL_MS) {
    return Promise.resolve();
  }
  if (inflight) return inflight;
  inflight = fetch('/api/previews/status', { cache: 'no-cache' })
    .then((res) => {
      if (!res.ok) return null;
      return res.json().catch(() => null) as Promise<Partial<InstantPreviewsResponse> | null>;
    })
    .then((body) => {
      const list = Array.isArray(body?.previews) ? body.previews : [];
      entries = list.filter(
        (e) => e && typeof e.channel_id === 'string' && typeof e.media_url === 'string',
      );
      fetchedAt = Date.now();
    })
    .catch(() => {
      // Backend not up yet / endpoint absent — degrade to an empty map.
      entries = [];
      fetchedAt = Date.now();
    })
    .finally(() => {
      inflight = null;
    });
  return inflight;
}

/** Current registry (test/debug helper; consumers use findInstantPreview). */
export function getInstantPreviews(): InstantPreviewEntry[] {
  return entries;
}

/** Test helper — clears module cache state. */
export function resetInstantPreviewsForTests(): void {
  entries = [];
  fetchedAt = 0;
  inflight = null;
}

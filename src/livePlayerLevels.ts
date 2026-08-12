/**
 * Pure helpers for the live player (LivePlayerPopup): quality-level filtering
 * (360p–1080p with original hls.js indices, 360p default), seek/time
 * decisions, and the DVR REPLAY archive context (entry-channel slug + newest
 * in-progress VOD). No DOM/hls.js imports so vitest can cover them without
 * mounting a player.
 */

import { buildVodUrl, isPublicVideo } from './channelUtils';
import { edgeAffectsNorth, edgeAffectsWest, widthDeltaFromEdge } from './layoutUtils';
import type { PanelSize, SavedChannel } from './types';
import type { ResizeEdge } from './explorePopupUtils';

export interface LiveLevelLike {
  index: number;
  height: number;
  bitrate?: number;
}

export interface FilteredLiveLevels {
  /** Filtered levels — `index` is the ORIGINAL hls.levels index. */
  levels: LiveLevelLike[];
  /** Original index of the default (360-preferred) level, or -1. */
  defaultIndex: number;
}

export interface FilterLiveLevelsOpts {
  /** Policy allow-list — when set, ONLY these heights may be offered
   *  (e.g. YouTube live: [360] anonymous, [360, 720, 1080] with cookies).
   *  Twitch/Kick live pass no allowHeights — everything ≥360p up to source
   *  (the highest manifest level, which may exceed 1080p) stays offered. */
  allowHeights?: number[];
}

const LIVE_LEVEL_MIN_HEIGHT = 360;
const LIVE_DEFAULT_HEIGHT = 360;

/**
 * DVR REPLAY archive context for the live player popup: the platform slug for
 * the open-channel link and the channel's newest public in-progress VOD
 * (replay source). Derived from the ENTRY's own channel — never the selected
 * channel, since the live button lives on every row and the clicked channel
 * may not be selected.
 */
export function liveArchiveContext(
  channel: SavedChannel | null | undefined,
  platform: string | undefined,
): { channelSlug: string | undefined; vodUrl: string | undefined } {
  const plat = (platform || '').toLowerCase();
  const slug = plat === 'twitch'
    ? channel?.twitchSlug
    : plat === 'kick' ? channel?.kickSlug : channel?.youtubeSlug;
  const newestVod = [...(channel?.vodVideos ?? [])]
    .filter((v) => (v.platform || '').toLowerCase() === plat
      && v.content_kind !== 'clip' && isPublicVideo(v))
    .sort((a, b) => (b.created_at ?? '').localeCompare(a.created_at ?? ''))[0];
  return { channelSlug: slug, vodUrl: newestVod ? buildVodUrl(newestVod) : undefined };
}

/**
 * Filter hls.levels to the policy-allowed set, keeping ORIGINAL indices so
 * hls.currentLevel / hls.loadLevel can be set with them.
 *
 * Default (no allowHeights): 360p up to source — the highest manifest level,
 * which for Twitch/Kick may exceed 1080p.
 *
 * With allowHeights: only the listed tiers are offered (YouTube live: 360/720/
 * 1080, or 360-only when anonymous). When no level matches (manifest lacks the
 * policy tiers, e.g. only 480p), fall back to the lowest level ≥360 so the
 * menu is never empty — the backend clamp still caps the served stream for
 * YouTube sessions.
 */
export function filterLiveLevels(
  levels: LiveLevelLike[],
  opts: FilterLiveLevelsOpts = {},
): FilteredLiveLevels {
  if (!levels.length) return { levels: [], defaultIndex: -1 };
  const allow = opts.allowHeights?.length ? new Set(opts.allowHeights) : null;
  const allowed = allow
    ? levels.filter((l) => l.height >= LIVE_LEVEL_MIN_HEIGHT && allow.has(l.height))
    : levels.filter((l) => l.height >= LIVE_LEVEL_MIN_HEIGHT);
  let source = allowed;
  if (allow && !allowed.length) {
    // Policy tiers absent from the manifest — offer the lowest in-range level
    // (never show anything above the policy set as a fallback).
    const minOk = levels
      .filter((l) => l.height >= LIVE_LEVEL_MIN_HEIGHT)
      .sort((a, b) => a.height - b.height);
    source = minOk.length ? [minOk[0]] : [levels[0]];
  } else if (!allowed.length) {
    source = levels;
  }

  // Default: closest to 360 (exact 360 wins), tie broken by index order.
  let defaultIndex = source[0].index;
  let bestDist = Number.POSITIVE_INFINITY;
  for (const l of source) {
    const dist = Math.abs(l.height - LIVE_DEFAULT_HEIGHT);
    if (dist < bestDist) {
      bestDist = dist;
      defaultIndex = l.index;
    }
  }
  return { levels: source, defaultIndex };
}

export interface ReplaySeekDecision {
  /** True when the target is inside the current snapshot — native seek. */
  inSnapshot: boolean;
}

/**
 * Map the player's currentTime to broadcast-relative seconds for the live
 * timestamp display. The live HLS timeline is a moving window (or an
 * Infinity-duration remap), so its origin is not the broadcast start; the
 * archive duration is. Because the live edge corresponds to the archive edge,
 * `current ≈ total − (edge − currentTime)` regardless of the timeline origin:
 * at the edge it equals the archive duration and it ticks 1:1 with playback.
 * Falls back to the raw player time when either side is unknown.
 */
export function liveBroadcastPositionSec(
  archiveDurationSec: number,
  liveEdgeSec: number,
  currentTimeSec: number,
): number {
  if (!(archiveDurationSec > 0) || !(liveEdgeSec > 0)) {
    return Math.max(0, currentTimeSec);
  }
  return Math.max(0, archiveDurationSec - Math.max(0, liveEdgeSec - currentTimeSec));
}

/**
 * Decide how a REPLAY-mode rail drag should behave:
 * - inside the current ENDLIST snapshot (target < duration) → native seek;
 * - at/past the snapshot edge → re-snapshot (fresh cache-busted playlist that
 *   includes segments appended since the last snapshot) and startLoad there.
 */
/**
 * Sum EXTINF durations in a (replay snapshot) playlist — the archive's
 * current length in seconds. 0 when the text has no EXTINF lines.
 */
export function parsePlaylistTotalSec(m3u8: string): number {
  let total = 0;
  for (const m of m3u8.matchAll(/#EXTINF:\s*([0-9]+(?:\.[0-9]+)?)/g)) {
    total += parseFloat(m[1]);
  }
  return total;
}

export function replaySeekTarget(
  targetSec: number,
  snapshotDurationSec: number,
): ReplaySeekDecision {
  const finite = Number.isFinite(snapshotDurationSec) && snapshotDurationSec > 0;
  return { inSnapshot: finite && targetSec <= snapshotDurationSec - 0.5 };
}

/**
 * Concurrent live-player cap: append *next* unless *items* already holds
 * *max* entries. With a *keyOf* extractor the same key is deduped FIRST —
 * the existing item is returned so the caller can focus it instead of
 * spawning a duplicate popup + backend session. Returns the new list (or the
 * unchanged one when blocked/deduped).
 */
export function appendLivePopup<T>(
  items: T[],
  next: T,
  max: number,
  keyOf?: (item: T) => string,
): { items: T[]; blocked: boolean; existing?: T } {
  if (keyOf) {
    const key = keyOf(next);
    const existing = items.find((it) => keyOf(it) === key);
    if (existing) return { items, blocked: false, existing };
  }
  if (items.length >= max) return { items, blocked: true };
  return { items: [...items, next], blocked: false };
}

// ---------------------------------------------------------------------------
// Aspect-locked popup resize
// ---------------------------------------------------------------------------

export interface LivePanelAspectClamp {
  minW: number;
  minH: number;
  maxW: number;
  maxH: number;
}

/**
 * Aspect-locked size for the live player popup during a resize drag.
 *
 * The popup is a flex column (fixed header + video area), and the live chat
 * panel may dock right of the video, shrinking its width. For the video to
 * fill its area with NO letterboxing the VIDEO AREA must keep the stream's
 * aspect: (h − headerH) = (w − chatW) / aspect. `startPanelResizeDrag`
 * hands us the raw (already edge-calc'd, generically clamped) size — we
 * reconstruct the pointer deltas from it, re-derive the width via
 * `widthDeltaFromEdge` (the same math the main preview uses), then derive
 * the height from the width. When the height hits a clamp the width is
 * re-derived from it (two-way lock), so growing the panel grows it exactly
 * like the video and shrinking behaves consistently.
 */
export function livePanelSizeFromAspect(
  edge: ResizeEdge,
  startSize: PanelSize,
  current: PanelSize,
  aspect: number,
  headerH: number,
  chatW: number,
  clamp: LivePanelAspectClamp,
): PanelSize {
  // `current` came from calcPanelSizeFromEdge, which INVERTS the pointer
  // delta on west/north edges (w = startW − dx, h = startH − dy). Recover
  // the raw pointer deltas so widthDeltaFromEdge sees the same sign
  // convention the main preview's resize path uses.
  const rawDx = edgeAffectsWest(edge) ? startSize.w - current.w : current.w - startSize.w;
  const rawDy = edgeAffectsNorth(edge) ? startSize.h - current.h : current.h - startSize.h;
  const deltaW = widthDeltaFromEdge(edge, rawDx, rawDy, Math.max(0.01, aspect));
  let w = startSize.w + deltaW;
  w = Math.min(clamp.maxW, Math.max(clamp.minW, w));

  let h = headerH + Math.max(0, w - chatW) / Math.max(0.01, aspect);
  h = Math.round(h);
  if (h > clamp.maxH) {
    h = clamp.maxH;
    w = Math.round(chatW + (clamp.maxH - headerH) * Math.max(0.01, aspect));
    w = Math.min(clamp.maxW, Math.max(clamp.minW, w));
  } else if (h < clamp.minH) {
    h = clamp.minH;
    w = Math.round(chatW + (clamp.minH - headerH) * Math.max(0.01, aspect));
    w = Math.min(clamp.maxW, Math.max(clamp.minW, w));
  }
  return { w, h };
}

// ---------------------------------------------------------------------------
// Fast clip (livestream popup CLIP button)
// ---------------------------------------------------------------------------

/** Fast-clip cooldown: one clip per window; a second click is ignored. */
export const FAST_CLIP_COOLDOWN_MS = 5000;
/** Fast-clip duration bounds — the seconds input clamps to this range. */
export const FAST_CLIP_MIN_SEC = 1;
export const FAST_CLIP_MAX_SEC = 60;
export const FAST_CLIP_DEFAULT_SEC = 30;

/** Seconds remaining in the clip cooldown at `nowMs`, or 0 when free. */
export function clipCooldownRemaining(lastClipAtMs: number, nowMs: number, cooldownMs = FAST_CLIP_COOLDOWN_MS): number {
  if (lastClipAtMs <= 0) return 0;
  return Math.max(0, lastClipAtMs + cooldownMs - nowMs);
}

/** Clamp the seconds input to the 1..60 fast-clip range. */
export function clampClipSeconds(value: number): number {
  if (!Number.isFinite(value)) return FAST_CLIP_DEFAULT_SEC;
  return Math.min(FAST_CLIP_MAX_SEC, Math.max(FAST_CLIP_MIN_SEC, Math.round(value)));
}

/**
 * Live chat room slug from the entry URL (twitch.tv/<login>,
 * kick.com/<slug>, youtube.com/@handle) — falls back to the platform slug
 * the archive context resolved. Used to open the per-viewer chat stream.
 */
export function liveChatSlugFromUrl(url: string, platform: string | undefined): string | undefined {
  try {
    const u = new URL(url);
    const host = u.hostname.toLowerCase();
    const path = u.pathname.split('/').filter(Boolean);
    const plat = (platform || '').toLowerCase();
    if (plat === 'twitch' || host.includes('twitch.tv')) return path[0] || undefined;
    if (plat === 'kick' || host.includes('kick.com')) return path[0] || undefined;
    if (plat === 'youtube' || host.includes('youtube.com') || host === 'youtu.be') return path[0] || undefined;
    return path[0] || undefined;
  } catch {
    return undefined;
  }
}

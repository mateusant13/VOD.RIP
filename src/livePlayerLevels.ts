/**
 * Pure helpers for the live player (LivePlayerPopup): quality-level filtering
 * (360p–1080p with original hls.js indices, 360p default), seek/time
 * decisions, and the DVR REPLAY archive context (entry-channel slug + newest
 * in-progress VOD). No DOM/hls.js imports so vitest can cover them without
 * mounting a player.
 */

import { buildVodUrl, isPublicVideo } from './channelUtils';
import type { SavedChannel } from './types';

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

/**
 * Pure helpers for the live player (LivePlayerPopup): quality-level filtering
 * (360p–1080p with original hls.js indices, 360p default) and seek/time
 * decisions. Kept dependency-free so vitest can cover them without mounting
 * hls.js or a video element.
 */

export interface LiveLevelLike {
  index: number;
  height: number;
  bitrate?: number;
}

export interface FilteredLiveLevels {
  /** Filtered levels — `index` is the ORIGINAL hls.levels index. */
  levels: LiveLevelLike[];
  /** Original index of the default (480p-preferred) level, or -1. */
  defaultIndex: number;
}

const LIVE_LEVEL_MIN_HEIGHT = 360;
const LIVE_LEVEL_MAX_HEIGHT = 1080;
const LIVE_DEFAULT_HEIGHT = 360;

/**
 * Filter hls.levels to 360p–1080p, keeping ORIGINAL indices so
 * hls.currentLevel / hls.loadLevel can be set with them.
 *
 * Fallback ladder: when no level is in range (e.g. only 160p/1080p60/2160p),
 * keep the closest in-range levels rather than showing nothing — never show
 * >1080 or <360 unless NOTHING is in range, in which case the full list is
 * shown so the menu is never empty.
 */
export function filterLiveLevels(levels: LiveLevelLike[]): FilteredLiveLevels {
  if (!levels.length) return { levels: [], defaultIndex: -1 };
  const inRange = levels.filter(
    (l) => l.height >= LIVE_LEVEL_MIN_HEIGHT && l.height <= LIVE_LEVEL_MAX_HEIGHT,
  );
  const source = inRange.length > 0 ? inRange : levels;

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

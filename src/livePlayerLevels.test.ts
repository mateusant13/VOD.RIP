import { describe, expect, it } from 'vitest';
import { filterLiveLevels, liveBroadcastPositionSec, parsePlaylistTotalSec, replaySeekTarget } from './livePlayerLevels';

describe('filterLiveLevels', () => {
  const full = (heights: number[], bitrates = heights.map((h) => h * 1000)) =>
    heights.map((height, i) => ({ index: i, height, bitrate: bitrates[i] }));

  it('keeps every level ≥360 up to source (no 1080 cap, twitch/kick live)', () => {
    const { levels } = filterLiveLevels(full([160, 360, 480, 720, 1080, 2160]));
    expect(levels.map((l) => l.height)).toEqual([360, 480, 720, 1080, 2160]);
    expect(levels.map((l) => l.index)).toEqual([1, 2, 3, 4, 5]);
  });

  it('defaults to the level closest to 360', () => {
    const { defaultIndex } = filterLiveLevels(full([720, 1080]));
    expect(defaultIndex).toBe(0); // 720 (dist 360) < 1080 (dist 720)
  });

  it('prefers exact 360 over 480/720', () => {
    const { defaultIndex } = filterLiveLevels(full([360, 480, 720]));
    expect(defaultIndex).toBe(0);
  });

  it('falls back to the lowest in-range level when 360 is absent', () => {
    const { defaultIndex } = filterLiveLevels(full([480, 720, 1080]));
    expect(defaultIndex).toBe(0); // 480 (dist 120) < 720 (dist 360)
  });

  it('drops sub-360 levels even when no other level exists', () => {
    // 160 + 1440: 1440 (source) stays, 160 is below the 360 floor.
    const { levels, defaultIndex } = filterLiveLevels(full([160, 1440]));
    expect(levels.map((l) => l.height)).toEqual([1440]);
    expect(defaultIndex).toBe(1);
  });

  it('never shows an empty menu', () => {
    const { levels, defaultIndex } = filterLiveLevels(full([2160, 4320]));
    expect(levels.length).toBe(2);
    expect(defaultIndex).toBe(0);
  });

  it('handles empty input', () => {
    expect(filterLiveLevels([])).toEqual({ levels: [], defaultIndex: -1 });
  });

  it('labels carry bitrate (menu uses label downstream)', () => {
    const { levels } = filterLiveLevels(full([480, 1080], [2_500_000, 6_000_000]));
    expect(levels[0].bitrate).toBe(2_500_000);
    expect(levels[1].bitrate).toBe(6_000_000);
  });

  it('no allowHeights keeps source levels above 1080 (twitch/kick live)', () => {
    const { levels } = filterLiveLevels(full([360, 720, 1080, 1440, 2160]));
    expect(levels.map((l) => l.height)).toEqual([360, 720, 1080, 1440, 2160]);
    expect(levels.map((l) => l.index)).toEqual([0, 1, 2, 3, 4]);
  });

  it('allowHeights=[360] caps the menu to 360p (youtube live anonymous)', () => {
    const { levels, defaultIndex } = filterLiveLevels(full([360, 720, 1080]), { allowHeights: [360] });
    expect(levels.map((l) => l.height)).toEqual([360]);
    expect(levels.map((l) => l.index)).toEqual([0]);
    expect(defaultIndex).toBe(0);
  });

  it('allowHeights=[360,720,1080] drops in-between tiers like 480 (youtube live + cookies)', () => {
    const { levels, defaultIndex } = filterLiveLevels(
      full([360, 480, 720, 1080, 1440]),
      { allowHeights: [360, 720, 1080] },
    );
    expect(levels.map((l) => l.height)).toEqual([360, 720, 1080]);
    expect(levels.map((l) => l.index)).toEqual([0, 2, 3]);
    expect(defaultIndex).toBe(0);
  });

  it('allowHeights with no matching level falls back to the lowest in-range level (never empty, never over-policy)', () => {
    const { levels } = filterLiveLevels(full([480, 720]), { allowHeights: [360] });
    expect(levels.map((l) => l.height)).toEqual([480]);
  });

  it('allowHeights ignores sub-360 levels even when in the list', () => {
    const { levels } = filterLiveLevels(full([144, 360, 720]), { allowHeights: [144, 360] });
    expect(levels.map((l) => l.height)).toEqual([360]);
  });

  it('defaults to the level closest to 360 within the allowed set', () => {
    const { levels, defaultIndex } = filterLiveLevels(
      full([360, 480, 720, 1080]),
      { allowHeights: [360, 720, 1080] },
    );
    expect(levels.map((l) => l.height)).toEqual([360, 720, 1080]);
    expect(defaultIndex).toBe(0);
  });

  it('no allowHeights and no level ≥360 falls back to the full list (never empty)', () => {
    const { levels } = filterLiveLevels(full([160, 144]));
    expect(levels.map((l) => l.height)).toEqual([160, 144]);
  });
});

describe('parsePlaylistTotalSec', () => {
  const pl = (extinfs: string) => `#EXTM3U\n#EXT-X-TARGETDURATION:6\n${extinfs}#EXT-X-ENDLIST\n`;

  it('sums EXTINF durations (int + float)', () => {
    expect(parsePlaylistTotalSec(pl('#EXTINF:6.000,\nseg1.ts\n#EXTINF:6.0,\nseg2.ts\n#EXTINF:4.5,\nseg3.ts\n'))).toBe(16.5);
  });

  it('returns 0 for playlists without EXTINF', () => {
    expect(parsePlaylistTotalSec('#EXTM3U\n#EXT-X-ENDLIST\n')).toBe(0);
    expect(parsePlaylistTotalSec('')).toBe(0);
  });

  it('ignores non-duration EXT tags', () => {
    expect(parsePlaylistTotalSec(pl('#EXT-X-PROGRAM-DATE-TIME:2026-08-02T21:00:00Z\n#EXTINF:6.0,\ns.ts\n'))).toBe(6);
  });
});

describe('replaySeekTarget', () => {
  it('native-seeks inside the snapshot', () => {
    expect(replaySeekTarget(30, 120)).toEqual({ inSnapshot: true });
  });

  it('re-snapshots at the edge (grew since last snapshot)', () => {
    expect(replaySeekTarget(120, 120)).toEqual({ inSnapshot: false });
    expect(replaySeekTarget(200, 120)).toEqual({ inSnapshot: false });
  });

  it('re-snapshots when duration is unknown', () => {
    expect(replaySeekTarget(10, 0)).toEqual({ inSnapshot: false });
    expect(replaySeekTarget(10, Number.POSITIVE_INFINITY)).toEqual({ inSnapshot: false });
  });
});

describe('liveBroadcastPositionSec', () => {
  it('maps the player time to broadcast time via the live edge', () => {
    // 2h archive, edge 3s behind archive end, player at the edge
    expect(liveBroadcastPositionSec(7200, 7197, 7197)).toBe(7200);
    // 10 min back in the broadcast
    expect(liveBroadcastPositionSec(7200, 7197, 6597)).toBe(6600);
  });

  it('handles a window-based timeline (origin at window start)', () => {
    expect(liveBroadcastPositionSec(7200, 87, 87)).toBe(7200);
    expect(liveBroadcastPositionSec(7200, 87, 27)).toBe(7140);
  });

  it('falls back to raw player time when archive or edge is unknown', () => {
    expect(liveBroadcastPositionSec(0, 87, 42)).toBe(42);
    expect(liveBroadcastPositionSec(7200, 0, 42)).toBe(42);
    expect(liveBroadcastPositionSec(0, 0, 42)).toBe(42);
  });

  it('clamps negative results', () => {
    expect(liveBroadcastPositionSec(30, 90, 10)).toBe(0);
  });
});

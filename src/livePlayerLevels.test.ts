import { describe, expect, it } from 'vitest';
import { filterLiveLevels, replaySeekTarget } from './livePlayerLevels';

describe('filterLiveLevels', () => {
  const full = (heights: number[], bitrates = heights.map((h) => h * 1000)) =>
    heights.map((height, i) => ({ index: i, height, bitrate: bitrates[i] }));

  it('keeps only 360-1080 with original indices', () => {
    const { levels } = filterLiveLevels(full([160, 360, 480, 720, 1080, 2160]));
    expect(levels.map((l) => l.height)).toEqual([360, 480, 720, 1080]);
    expect(levels.map((l) => l.index)).toEqual([1, 2, 3, 4]);
  });

  it('defaults to the level closest to 480', () => {
    const { defaultIndex } = filterLiveLevels(full([360, 720, 1080]));
    expect(defaultIndex).toBe(0); // 360 (dist 120) < 720 (dist 240)
  });

  it('prefers exact 480 over 360/720', () => {
    const { defaultIndex } = filterLiveLevels(full([360, 480, 720]));
    expect(defaultIndex).toBe(1);
  });

  it('falls back to closest in-range levels when none are in 360-1080', () => {
    // 160 + 1440: nothing in range → full list, closest to 480 wins (160)
    const { levels, defaultIndex } = filterLiveLevels(full([160, 1440]));
    expect(levels.map((l) => l.height)).toEqual([160, 1440]);
    expect(defaultIndex).toBe(0);
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

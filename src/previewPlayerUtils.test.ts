import { describe, expect, it } from 'vitest';
import {
  applyPolicyHeights,
  resolveHlsPreviewLevels,
  resolveProgressivePreviewLevels,
  youtubePreviewAllowHeights,
} from './previewPlayerUtils';

describe('youtubePreviewAllowHeights (quality policy)', () => {
  it('VOD previews stay 360p only', () => {
    expect(youtubePreviewAllowHeights({ isLive: false, anonymous: false })).toEqual([360]);
  });

  it('live + anonymous session stays 360p only', () => {
    expect(youtubePreviewAllowHeights({ isLive: true, anonymous: true })).toEqual([360]);
  });

  it('live + user cookies allows up to 1080p', () => {
    expect(youtubePreviewAllowHeights({ isLive: true, anonymous: false })).toEqual([360, 720, 1080]);
  });
});

describe('applyPolicyHeights', () => {
  it('keeps only the allowed heights', () => {
    expect(applyPolicyHeights([360, 720, 1080], [360])).toEqual([360]);
  });

  it('falls back to the lowest tier when nothing matches', () => {
    expect(applyPolicyHeights([480, 720], [360])).toEqual([480]);
  });

  it('passes through without allowHeights', () => {
    expect(applyPolicyHeights([360, 720], undefined)).toEqual([360, 720]);
  });
});

describe('resolveProgressivePreviewLevels policy cap', () => {
  it('youtube VOD: 360p-only even when API lists higher tiers', () => {
    const { mapped } = resolveProgressivePreviewLevels({
      variantHeights: [360, 720, 1080],
      qualityLabels: ['360p', '720p', '1080p'],
      initialHeight: 360,
      allowHeights: [360],
    });
    expect(mapped.map((m) => m.height)).toEqual([360]);
  });

  it('youtube live + cookies: full 360/720/1080 ladder offered', () => {
    const { mapped } = resolveProgressivePreviewLevels({
      variantHeights: [360, 720, 1080],
      initialHeight: 360,
      allowHeights: [360, 720, 1080],
    });
    expect(mapped.map((m) => m.height)).toEqual([360, 720, 1080]);
  });

  it('non-youtube previews keep every variant (no allowHeights)', () => {
    const { mapped } = resolveProgressivePreviewLevels({
      variantHeights: [360, 720, 1080],
      initialHeight: 720,
    });
    expect(mapped.map((m) => m.height)).toEqual([360, 720, 1080]);
  });

  it('policy cap with no API data still offers 360', () => {
    const { mapped } = resolveProgressivePreviewLevels({
      initialHeight: 360,
      allowHeights: [360],
    });
    expect(mapped.map((m) => m.height)).toEqual([360]);
  });
});

describe('resolveHlsPreviewLevels policy cap', () => {
  const levels = [
    { height: 360, bitrate: 500_000 },
    { height: 720, bitrate: 2_000_000 },
    { height: 1080, bitrate: 5_000_000 },
  ];

  it('caps the menu to 360p while keeping ORIGINAL hls.levels indices', () => {
    const { mapped, defaultIndex } = resolveHlsPreviewLevels(levels, {
      initialHeight: 360,
      allowHeights: [360],
    });
    expect(mapped.map((m) => m.height)).toEqual([360]);
    expect(mapped.map((m) => m.index)).toEqual([0]);
    expect(defaultIndex).toBe(0);
  });

  it('allows the full ladder for youtube live + cookies', () => {
    const { mapped } = resolveHlsPreviewLevels(levels, {
      initialHeight: 360,
      allowHeights: [360, 720, 1080],
    });
    expect(mapped.map((m) => m.height)).toEqual([360, 720, 1080]);
  });

  it('keeps higher tiers when no policy cap is passed', () => {
    const { mapped } = resolveHlsPreviewLevels(levels, { initialHeight: 720 });
    expect(mapped.map((m) => m.height)).toEqual([360, 720, 1080]);
  });

  it('index stays valid when 720 is filtered out (360→1080 jump)', () => {
    const { mapped } = resolveHlsPreviewLevels(
      [{ height: 360, bitrate: 1 }, { height: 1080, bitrate: 9 }],
      { initialHeight: 360, allowHeights: [360, 1080] },
    );
    expect(mapped.map((m) => ({ h: m.height, i: m.index }))).toEqual([
      { h: 360, i: 0 },
      { h: 1080, i: 1 },
    ]);
  });

  it('policy filter with nothing matching offers only the lowest tier', () => {
    const { mapped } = resolveHlsPreviewLevels(
      [{ height: 480, bitrate: 1 }, { height: 720, bitrate: 9 }],
      { initialHeight: 360, allowHeights: [360] },
    );
    expect(mapped.map((m) => m.height)).toEqual([480]);
    expect(mapped[0].index).toBe(0);
  });

  it('fallback heights are capped too', () => {
    const { mapped } = resolveHlsPreviewLevels(
      [{ height: 0, bitrate: 1 }],
      { initialHeight: 360, fallbackHeights: [360, 720, 1080], allowHeights: [360] },
    );
    expect(mapped.map((m) => m.height)).toEqual([360]);
  });
});

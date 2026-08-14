import { describe, expect, it } from 'vitest';
import {
  filterLiveLevels,
  liveArchiveContext,
  liveBroadcastPositionSec,
  liveChatSlugFromUrl,
  livePanelSizeFromAspect,
  parsePlaylistTotalSec,
  qualityLevelForPolicy,
  replaySeekTarget,
} from './livePlayerLevels';
import type { SavedChannel } from './types';

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

describe('liveArchiveContext', () => {
  const vod = (platform: string, id: string, createdAt: string, opts: Partial<import('./types').ChannelVideo> = {}): import('./types').ChannelVideo => ({
    id,
    platform,
    title: `VOD ${id}`,
    duration: 3600,
    created_at: createdAt,
    views: 100,
    thumbnail_url: null,
    url: `https://example.com/${platform}/${id}`,
    channel: 'srdogg',
    content_kind: 'vod',
    ...opts,
  });

  const CHANNEL: SavedChannel = {
    id: 'ch-srdogg',
    displayName: 'srdogg / srdoglol',
    kickSlug: 'srdoglol',
    twitchSlug: 'srdogg',
    youtubeSlug: 'srdogyt',
    vodVideos: [
      // Members-only newest twitch VOD — must NOT be picked as replay source.
      vod('Twitch', 'v999', '2026-08-03T20:00:00Z', { availability: 'subscriber_only' }),
      vod('Twitch', 'v123', '2026-08-03T19:00:00Z'),
      vod('Twitch', 'clip_x', '2026-08-03T18:00:00Z', { content_kind: 'clip', url: 'https://clips.twitch.tv/foo' }),
      vod('Kick', 'k42', '2026-08-02T10:00:00Z'),
      vod('YouTube', 'yt1', '2026-08-01T10:00:00Z'),
    ],
    clipVideos: [],
    updatedAt: '2026-08-03T00:00:00Z',
  };

  it('resolves slug + newest public VOD for the entry platform (not the selected channel)', () => {
    const { channelSlug, vodUrl } = liveArchiveContext(CHANNEL, 'Twitch');
    expect(channelSlug).toBe('srdogg');
    // v999 is members-only and the clip is excluded — v123 wins.
    expect(vodUrl).toBe('https://example.com/Twitch/v123');
  });

  it('picks the per-platform slug (kick/youtube)', () => {
    expect(liveArchiveContext(CHANNEL, 'Kick').channelSlug).toBe('srdoglol');
    expect(liveArchiveContext(CHANNEL, 'Kick').vodUrl).toBe('https://example.com/Kick/k42');
    expect(liveArchiveContext(CHANNEL, 'YouTube').channelSlug).toBe('srdogyt');
    expect(liveArchiveContext(CHANNEL, 'YouTube').vodUrl).toBe('https://example.com/YouTube/yt1');
  });

  it('keeps the slug but drops vodUrl when the platform has no public VOD', () => {
    const onlyClips = { ...CHANNEL, vodVideos: [vod('Twitch', 'clip_x', '2026-08-03T18:00:00Z', { content_kind: 'clip', url: 'https://clips.twitch.tv/foo' })] };
    expect(liveArchiveContext(onlyClips, 'Twitch')).toEqual({ channelSlug: 'srdogg', vodUrl: undefined });
  });

  it('degrades gracefully without a channel (popup still plays from entry.url)', () => {
    expect(liveArchiveContext(undefined, 'Twitch')).toEqual({ channelSlug: undefined, vodUrl: undefined });
  });
});

describe('livePanelSizeFromAspect', () => {
  const CLAMP = { minW: 320, minH: 200, maxW: 1280, maxH: 800 };
  // 16:9 stream, 44px header, no chat docked.
  const start = { w: 480, h: 44 + 480 / (16 / 9) }; // aspect-locked start
  const aspect = 16 / 9;

  it('growing the south edge grows the width too (video keeps 16:9)', () => {
    const next = livePanelSizeFromAspect('s', start, { w: 480, h: start.h + 90 }, aspect, 44, 0, CLAMP);
    // Δw = Δh * aspect → video area stays exactly 16:9 (h is rounded).
    expect(next.w).toBe(480 + Math.round(90 * aspect));
    expect(Math.abs((next.h - 44) - next.w / aspect)).toBeLessThan(1.01);
  });

  it('growing the east edge grows the height to match', () => {
    const next = livePanelSizeFromAspect('e', start, { w: 640, h: start.h }, aspect, 44, 0, CLAMP);
    expect(next.w).toBe(640);
    expect(Math.abs((next.h - 44) - 640 / aspect)).toBeLessThan(1.01);
  });

  it('shrinking stays consistent (north edge)', () => {
    // Drag north by 60: panel shrinks but the video area keeps the aspect.
    const next = livePanelSizeFromAspect('n', start, { w: 480, h: start.h - 60 }, aspect, 44, 0, CLAMP);
    expect(next.h).toBe(start.h - 60);
    expect(Math.abs((next.h - 44) - (next.w / aspect))).toBeLessThan(1.01);
  });

  it('reserves the chat panel width from the video area', () => {
    // chatW=260: video area = w - 260 must keep 16:9.
    const next = livePanelSizeFromAspect('e', start, { w: 640, h: start.h }, aspect, 44, 260, CLAMP);
    expect(Math.abs((next.h - 44) - (640 - 260) / aspect)).toBeLessThan(1.01);
  });

  it('clamps to maxH and re-derives the width (two-way lock)', () => {
    // Portrait stream: at maxH the width (h−44)*aspect stays under maxW, so
    // the HEIGHT clamp binds and the width is re-derived from it.
    const tallAspect = 9 / 16;
    const tallStart = { w: 320, h: 44 + 320 / tallAspect };
    const next = livePanelSizeFromAspect('s', tallStart, { w: 320, h: 9999 }, tallAspect, 44, 0, CLAMP);
    expect(next.h).toBe(CLAMP.maxH);
    expect(Math.abs(next.w - (CLAMP.maxH - 44) * tallAspect)).toBeLessThan(1.01);
  });

  it('clamps to minW even when the pointer says smaller', () => {
    const next = livePanelSizeFromAspect('e', start, { w: 100, h: start.h }, aspect, 44, 0, CLAMP);
    expect(next.w).toBe(CLAMP.minW);
    // Height re-derived from the min width keeps the aspect.
    expect(Math.abs((next.h - 44) - CLAMP.minW / aspect)).toBeLessThan(1.01);
  });
});


describe('liveChatSlugFromUrl', () => {
  it('parses the room slug per platform', () => {
    expect(liveChatSlugFromUrl('https://www.twitch.tv/srdogg', 'twitch')).toBe('srdogg');
    expect(liveChatSlugFromUrl('https://kick.com/srdoglol', 'kick')).toBe('srdoglol');
    expect(liveChatSlugFromUrl('https://www.youtube.com/@srdogyt/live', 'youtube')).toBe('@srdogyt');
    // Platform tag wins over a mismatched host — first path segment IS the slug.
    expect(liveChatSlugFromUrl('https://example.com/whatever', 'twitch')).toBe('whatever');
  });

  it('extracts the login from Twitch HLS master URLs (live entries)', () => {
    expect(liveChatSlugFromUrl('https://usher.ttvnw.net/api/channel/hls/cellbit.m3u8?allow_source=true', 'twitch')).toBe('cellbit');
    expect(liveChatSlugFromUrl('https://usher.ttvnw.net/api/channel/hls/jynxzi.m3u8', 'Twitch')).toBe('jynxzi');
    expect(liveChatSlugFromUrl('https://cdn.ttvnw.net/chunked/hls/srdogg.m3u8', 'twitch')).toBe('srdogg');
  });

  it('falls back to the host when the platform tag is missing', () => {
    expect(liveChatSlugFromUrl('https://kick.com/srdoglol', undefined)).toBe('srdoglol');
    expect(liveChatSlugFromUrl('https://www.twitch.tv/srdogg', '')).toBe('srdogg');
  });

  it('returns undefined for garbage', () => {
    expect(liveChatSlugFromUrl('not-a-url', 'twitch')).toBeUndefined();
    expect(liveChatSlugFromUrl('', 'kick')).toBeUndefined();
  });
});

describe('qualityLevelForPolicy', () => {
  const withHeights = (heights: number[]) => heights.map((height) => ({ height }));
  const withBitrates = (bitrates: number[]) => bitrates.map((bitrate) => ({ bitrate }));
  const ladder = (heights: number[], bitrates = heights.map((h) => h * 1_000_000)) =>
    heights.map((height, i) => ({ height, bitrate: bitrates[i] }));

  it('single: picks the highest bitrate (SOURCE), ties broken by height', () => {
    expect(qualityLevelForPolicy(ladder([360, 720, 1080]), false)).toBe(2);
    // 720 vs 1080 at the same bitrate → higher height wins.
    expect(qualityLevelForPolicy([
      { height: 720, bitrate: 3_000_000 },
      { height: 1080, bitrate: 3_000_000 },
    ], false)).toBe(1);
    // Equal bitrate AND height → the first level wins (stable).
    expect(qualityLevelForPolicy([
      { height: 720, bitrate: 3_000_000 },
      { height: 720, bitrate: 3_000_000 },
    ], false)).toBe(0);
  });

  it('single: falls back to the highest height when bitrate info is absent', () => {
    expect(qualityLevelForPolicy(withHeights([360, 480, 1080]), false)).toBe(2);
  });

  it('single: falls back to level 0 when no info at all', () => {
    expect(qualityLevelForPolicy([{}, {}], false)).toBe(0);
  });

  it('single: bitrate-only manifests use bitrate directly', () => {
    expect(qualityLevelForPolicy(withBitrates([500_000, 6_000_000, 2_500_000]), false)).toBe(1);
  });

  it('multi: prefers the highest level ≤480, then ≤360', () => {
    expect(qualityLevelForPolicy(ladder([360, 480, 720, 1080]), true)).toBe(1); // 480
    expect(qualityLevelForPolicy(ladder([360, 720, 1080]), true)).toBe(0); // 360
    expect(qualityLevelForPolicy(ladder([160, 360, 480]), true)).toBe(2); // 480
  });

  it('multi: exactly 480 qualifies, exactly 360 is the second rung', () => {
    expect(qualityLevelForPolicy(ladder([480, 1080]), true)).toBe(0);
    expect(qualityLevelForPolicy(ladder([360]), true)).toBe(0);
    expect(qualityLevelForPolicy(ladder([360, 480]), true)).toBe(1);
  });

  it('multi: a 1080-only manifest (nothing fits the caps) falls to the lowest bitrate', () => {
    expect(qualityLevelForPolicy(ladder([1080]), true)).toBe(0);
    expect(qualityLevelForPolicy(ladder([1080, 1080], [6_000_000, 2_500_000]), true)).toBe(1);
  });

  it('multi: no height info at all → lowest bitrate (safest for bandwidth)', () => {
    expect(qualityLevelForPolicy(withBitrates([6_000_000, 1_000_000, 2_500_000]), true)).toBe(1);
  });

  it('multi: no height OR bitrate info → level 0', () => {
    expect(qualityLevelForPolicy([{}, {}, {}], true)).toBe(0);
  });

  it('multi: within one cap rung, the first level at the best height wins', () => {
    expect(qualityLevelForPolicy([
      { height: 360, bitrate: 1_000_000 },
      { height: 360, bitrate: 2_000_000 },
    ], true)).toBe(0);
  });

  it('handles empty input with -1', () => {
    expect(qualityLevelForPolicy([], false)).toBe(-1);
    expect(qualityLevelForPolicy([], true)).toBe(-1);
  });

  it('height-0 entries (source media playlist without RESOLUTION) count as no height info', () => {
    // Single: the only level is the pick. Multi: lowest bitrate among them.
    expect(qualityLevelForPolicy([{ height: 0, bitrate: 6_000_000 }], false)).toBe(0);
    expect(qualityLevelForPolicy([
      { height: 0, bitrate: 6_000_000 },
      { height: 0, bitrate: 1_000_000 },
    ], true)).toBe(1);
  });
});

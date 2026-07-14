/**
 * Unit tests for channelUtils.ts — channel/video helper functions.
 * Tests cover pure logic only; localStorage-dependent functions are excluded.
 */
import { describe, it, expect } from 'vitest';
import {
  isLikelyClip,
  channelVideoKey,
  mergeVodLists,
  mergeClipLists,
  channelClipsMissing,
  channelVodsMissing,
  channelStreamsMissing,
  mergeVodPlatformsFetched,
  channelPlatformVisibleSlice,
  channelPlatformCanExpand,
  channelLinkDraftFromParsed,
  channelLinkWillAddSummary,
  parseChannelInput,
  buildVodUrl,
  slugFromVideoUrl,
  youtubeSlugFromChannelUrl,
  isChannelAlreadySaved,
  deriveChannelDisplayName,
  resolveVideoThumbnail,
  findCachedVideoThumbnail,
  formatChannelErrorMessage,
  isHiddenChannelPlatformError,
  syncDurationFromPreviewSession,
  videoInfoDurationSec,
} from './channelUtils';
import type { ChannelVideo, SavedChannel, VideoInfo } from './types';

const makeVod = (overrides: Partial<ChannelVideo> = {}): ChannelVideo => ({
  id: 'v123',
  platform: 'Twitch',
  title: 'Test VOD',
  duration: 3600,
  created_at: '2024-01-15T00:00:00Z',
  views: 1000,
  thumbnail_url: 'https://example.com/thumb.jpg',
  url: 'https://twitch.tv/videos/123',
  channel: 'testchannel',
  ...overrides,
});

const makeClip = (overrides: Partial<ChannelVideo> = {}): ChannelVideo => ({
  id: 'clip_abc',
  platform: 'Kick',
  title: 'Test Clip',
  duration: 30,
  created_at: '2024-01-15T00:00:00Z',
  views: 500,
  thumbnail_url: null,
  url: 'https://kick.com/testchannel/clips/abc',
  channel: 'testchannel',
  content_kind: 'clip',
  ...overrides,
});

describe('isLikelyClip', () => {
  it('returns true for clips by content_kind', () => {
    expect(isLikelyClip(makeClip())).toBe(true);
  });

  it('returns false for long clips with content_kind=clip but >60s', () => {
    const longClip = makeClip({ duration: 120 });
    expect(isLikelyClip(longClip)).toBe(false);
  });

  it('returns true for Kick clips with clip_ prefix', () => {
    const v = makeVod({
      platform: 'Kick',
      id: 'clip_xyz',
      url: 'https://kick.com/ch/videos/clip_xyz',
    });
    expect(isLikelyClip(v)).toBe(true);
  });

  it('returns false for normal VODs', () => {
    expect(isLikelyClip(makeVod())).toBe(false);
  });

  it('returns true for Twitch clip URLs with clip-appropriate duration', () => {
    const v = makeVod({
      platform: 'Twitch',
      url: 'https://clips.twitch.tv/ClipName',
      duration: 30,
    });
    expect(isLikelyClip(v)).toBe(true);
  });

  it('returns false for Twitch clips that exceed max duration', () => {
    const v = makeVod({
      platform: 'Twitch',
      url: 'https://clips.twitch.tv/ClipName',
      duration: 120,
    });
    expect(isLikelyClip(v)).toBe(false);
  });
});

describe('channelVideoKey', () => {
  it('creates platform:id key', () => {
    expect(channelVideoKey(makeVod())).toBe('Twitch:v123');
    expect(channelVideoKey(makeClip())).toBe('Kick:clip_abc');
  });
});

describe('mergeVodLists', () => {
  it('merges with incoming winning on duplicates', () => {
    const existing = [makeVod({ title: 'Old Title' })];
    const incoming = [makeVod({ title: 'New Title' })];
    const result = mergeVodLists(existing, incoming);
    expect(result).toHaveLength(1);
    expect(result[0].title).toBe('New Title');
  });

  it('includes items from both lists', () => {
    const existing = [makeVod({ id: 'v1', title: 'VOD 1' })];
    const incoming = [makeVod({ id: 'v2', title: 'VOD 2' })];
    const result = mergeVodLists(existing, incoming);
    expect(result).toHaveLength(2);
  });

  it('preserves created_at when incoming row is partial', () => {
    const existing = [makeVod({
      id: 'yt1',
      platform: 'YouTube',
      created_at: '2024-01-15T00:00:00Z',
      views: 100,
    })];
    const incoming = [makeVod({
      id: 'yt1',
      platform: 'YouTube',
      created_at: null,
      views: 200,
    })];
    const merged = mergeVodLists(existing, incoming);
    expect(merged[0].created_at).toBe('2024-01-15T00:00:00Z');
    expect(merged[0].views).toBe(200);
  });

  it('sorts newest first by created_at', () => {
    const older = makeVod({ id: 'v1', created_at: '2024-01-01T00:00:00Z' });
    const newer = makeVod({ id: 'v2', created_at: '2024-06-01T00:00:00Z' });
    const result = mergeVodLists([older], [newer]);
    expect(result[0].id).toBe('v2');
    expect(result[1].id).toBe('v1');
  });
});

describe('mergeClipLists', () => {
  it('filters out non-clips from incoming', () => {
    const clip = makeClip({ id: 'c1' });
    const vod = makeVod({ id: 'v1' });
    const result = mergeClipLists([], [clip, vod]);
    expect(result).toHaveLength(1);
    expect(result[0].id).toBe('c1');
  });

  it('sorts by views descending', () => {
    const popular = makeClip({ id: 'c1', views: 1000 });
    const lessPop = makeClip({ id: 'c2', views: 100 });
    const result = mergeClipLists([lessPop], [popular]);
    expect(result[0].id).toBe('c1');
    expect(result[1].id).toBe('c2');
  });
});

describe('mergeVodPlatformsFetched', () => {
  it('marks platform fetched after completed attempt even when empty', () => {
    const out = mergeVodPlatformsFetched(
      {},
      { kickSlug: 'a', twitchSlug: 'b', youtubeSlug: '' },
      [],
      {},
      { Kick: true, Twitch: true },
    );
    expect(out.Kick).toBe(true);
    expect(out.Twitch).toBe(true);
  });

  it('marks platform fetched when rows or errors arrive', () => {
    const v = makeVod({ platform: 'Twitch', id: '1' });
    const out = mergeVodPlatformsFetched({}, { kickSlug: 'a', twitchSlug: 'b', youtubeSlug: '' }, [v], {});
    expect(out.Twitch).toBe(true);
    expect(out.Kick).toBeUndefined();
  });
});

describe('channelLinkDraftFromParsed', () => {
  it('prefills all platforms from bare handle', () => {
    const draft = channelLinkDraftFromParsed(parseChannelInput('surtepi'));
    expect(draft.kickSlug).toBe('surtepi');
    expect(draft.twitchSlug).toBe('surtepi');
    expect(draft.youtubeSlug).toBe('surtepi');
    expect(draft.kickEnabled && draft.twitchEnabled && draft.youtubeEnabled).toBe(true);
  });

  it('detects youtube source and guesses kick/twitch', () => {
    const draft = channelLinkDraftFromParsed(
      parseChannelInput('https://youtube.com/@surtepi'),
      'https://youtube.com/@surtepi',
    );
    expect(draft.detectedFrom).toBe('youtube');
    expect(draft.kickSlug).toBeTruthy();
    expect(draft.twitchSlug).toBeTruthy();
  });

  it('summarizes will-add line', () => {
    const draft = channelLinkDraftFromParsed(parseChannelInput('surtepi'));
    expect(channelLinkWillAddSummary(draft)).toContain('surtepi');
  });
});

describe('channelPlatformVisibleSlice', () => {
  const oldDate = new Date(Date.now() - 60 * 86_400_000).toISOString();
  const newDate = new Date().toISOString();
  const oldVod = (id: string): ChannelVideo => makeVod({
    id,
    platform: 'Kick',
    title: id,
    created_at: oldDate,
    url: `https://kick.com/x/videos/${id}`,
    channel: 'x',
  });
  const newVod = (id: string): ChannelVideo => ({
    ...oldVod(id),
    created_at: newDate,
  });

  it('shows only recent items until beyondRecent', () => {
    const videos = [newVod('n1'), oldVod('o1'), oldVod('o2')];
    expect(channelPlatformVisibleSlice(videos, 5, false, false).map((v) => v.id)).toEqual(['n1']);
    expect(channelPlatformVisibleSlice(videos, 5, true, false).map((v) => v.id)).toEqual(['n1', 'o1', 'o2']);
  });

  it('shows next items from full list when beyondRecent', () => {
    const videos = [newVod('n1'), oldVod('o1'), oldVod('o2')];
    expect(channelPlatformVisibleSlice(videos, 2, true, false).map((v) => v.id)).toEqual(['n1', 'o1']);
  });

  it('canExpand when older items exist beyond recent window', () => {
    const videos = [newVod('n1'), oldVod('o1')];
    expect(channelPlatformCanExpand(videos, 1, false, false)).toBe(true);
  });
});

describe('channelClipsMissing', () => {
  const ch = (overrides: Partial<SavedChannel> = {}): SavedChannel => ({
    id: 'ch1',
    displayName: 'Test',
    kickSlug: 'test_kick',
    twitchSlug: 'test_twitch',
    youtubeSlug: '',
    vodVideos: [],
    clipVideos: [],
    updatedAt: '2024-01-01T00:00:00Z',
    ...overrides,
  });

  it('returns true when no clips fetched and none exist', () => {
    expect(channelClipsMissing(ch(), true, true)).toBe(true);
  });

  it('returns false when clips exist for both platforms', () => {
    const state = ch({
      clipsFetched: true,
      clipVideos: [
        makeClip({ platform: 'Kick' }),
        makeClip({ platform: 'Twitch', id: 'twitch_clip' }),
      ],
    });
    expect(channelClipsMissing(state, true, true)).toBe(false);
  });

  it('returns true when Kick clips missing but Kick enabled', () => {
    const state = ch({
      clipsFetched: true,
      clipPlatformsFetched: { Twitch: true },
      clipVideos: [makeClip({ platform: 'Twitch', id: 'twitch_clip' })],
    });
    expect(channelClipsMissing(state, true, true)).toBe(true);
  });

  it('returns false when Kick was fetched but channel has no Kick clips', () => {
    const state = ch({
      clipsFetched: true,
      clipPlatformsFetched: { Kick: true, Twitch: true },
      clipVideos: [makeClip({ platform: 'Twitch', id: 'twitch_clip' })],
    });
    expect(channelClipsMissing(state, true, true)).toBe(false);
  });
});

describe('channelVodsMissing', () => {
  const ch = (overrides: Partial<SavedChannel> = {}): SavedChannel => ({
    id: 'ch1',
    displayName: 'Test',
    kickSlug: 'test_kick',
    twitchSlug: 'test_twitch',
    youtubeSlug: '',
    vodVideos: [],
    clipVideos: [],
    updatedAt: '2024-01-01T00:00:00Z',
    ...overrides,
  });

  it('returns false when VODs exist for both platforms', () => {
    const state = ch({
      vodVideos: [
        makeVod({ platform: 'Kick' }),
        makeVod({ platform: 'Twitch', id: 'twvod' }),
      ],
    });
    expect(channelVodsMissing(state, true, true)).toBe(false);
  });
});

describe('buildVodUrl', () => {
  it('uses the existing URL for Twitch VODs', () => {
    const v = makeVod({ url: 'https://twitch.tv/videos/123456' });
    expect(buildVodUrl(v)).toBe('https://twitch.tv/videos/123456');
  });

  it('builds Kick clip URL from channel + id', () => {
    const v = makeClip({ url: '', channel: 'testuser' });
    expect(buildVodUrl(v)).toBe('https://kick.com/testuser/clips/clip_abc');
  });

  it('builds Kick VOD URL from channel + id', () => {
    const v = makeVod({
      platform: 'Kick',
      id: 'vod_xyz',
      url: '',
      channel: 'testuser',
    });
    expect(buildVodUrl(v)).toBe('https://kick.com/testuser/videos/vod_xyz');
  });

  it('strips v prefix for Twitch VODs', () => {
    const v = makeVod({ id: 'v123456', url: '' });
    expect(buildVodUrl(v)).toBe('https://www.twitch.tv/videos/123456');
  });
});

describe('slugFromVideoUrl', () => {
  it('extracts Kick slug from VOD URL', () => {
    const s = slugFromVideoUrl('https://kick.com/xqc/videos/abc', 'kick');
    expect(s).toEqual({ kickSlug: 'xqc', twitchSlug: 'xqc', youtubeSlug: 'xqc' });
  });

  it('uses channel login for Twitch /videos/ URLs', () => {
    const s = slugFromVideoUrl('https://twitch.tv/videos/1', 'twitch', 'Display', 'cellbit');
    expect(s).toEqual({ kickSlug: 'cellbit', twitchSlug: 'cellbit', youtubeSlug: 'cellbit' });
  });

  it('extracts YouTube handle from watch URL metadata', () => {
    const s = slugFromVideoUrl('https://youtube.com/watch?v=abc', 'youtube', 'Cellbit', 'cellbit');
    expect(s.youtubeSlug).toBe('cellbit');
  });
});

describe('deriveChannelDisplayName', () => {
  it('returns single slug when only one platform', () => {
    expect(deriveChannelDisplayName('cellbit', '', '')).toBe('cellbit');
    expect(deriveChannelDisplayName('', 'xqc', '')).toBe('xqc');
    expect(deriveChannelDisplayName('', '', 'mkbhd')).toBe('mkbhd');
  });

  it('joins different slugs without duplicates', () => {
    expect(deriveChannelDisplayName('xqc', 'cellbit', '')).toBe('cellbit / xqc');
    expect(deriveChannelDisplayName('cellbit', 'cellbit', 'cellbit')).toBe('cellbit');
  });
});

describe('resolveVideoThumbnail', () => {
  it('substitutes width/height placeholders', () => {
    expect(resolveVideoThumbnail('https://x/%{width}x%{height}', 48, 36)).toBe('https://x/48x36');
    expect(resolveVideoThumbnail('https://x/{width}x{height}', 160, 90)).toBe('https://x/160x90');
  });

  it('returns null for empty input', () => {
    expect(resolveVideoThumbnail(null)).toBeNull();
    expect(resolveVideoThumbnail('  ')).toBeNull();
  });
});

describe('findCachedVideoThumbnail', () => {
  const channels: SavedChannel[] = [{
    id: '1',
    displayName: 'F',
    kickSlug: 'foo',
    twitchSlug: 'bar',
    youtubeSlug: '',
    vodVideos: [makeVod({ url: 'https://twitch.tv/videos/999', thumbnail_url: 'https://cdn/thumb.jpg' })],
    clipVideos: [],
    updatedAt: '',
  }];

  it('finds thumbnail by buildVodUrl match', () => {
    expect(findCachedVideoThumbnail('https://twitch.tv/videos/999', channels)).toBe('https://cdn/thumb.jpg');
  });

  it('returns null when no match', () => {
    expect(findCachedVideoThumbnail('https://other', channels)).toBeNull();
  });
});

describe('isChannelAlreadySaved', () => {
  const channels: SavedChannel[] = [{
    id: '1',
    displayName: 'F',
    kickSlug: 'Foo',
    twitchSlug: 'bar',
    youtubeSlug: '',
    vodVideos: [],
    clipVideos: [],
    updatedAt: '',
  }];

  it('matches kick slug case-insensitively', () => {
    expect(isChannelAlreadySaved('foo', '', channels)).toBe(true);
  });

  it('returns false for unknown slug', () => {
    expect(isChannelAlreadySaved('other', '', channels)).toBe(false);
  });
});

describe('youtubeSlugFromChannelUrl', () => {
  it('parses @handle, /channel/UC, and /c/ URLs', () => {
    expect(youtubeSlugFromChannelUrl('https://youtube.com/@LinusTechTips')).toBe('LinusTechTips');
    expect(youtubeSlugFromChannelUrl('https://youtube.com/channel/UC1234567890')).toBe('UC1234567890');
    expect(youtubeSlugFromChannelUrl('https://youtube.com/c/LinusTechTips/videos')).toBe('LinusTechTips');
  });

  it('ignores watch URLs', () => {
    expect(youtubeSlugFromChannelUrl('https://www.youtube.com/watch?v=dQw4w9WgXcQ')).toBe('');
  });
});

describe('channelStreamsMissing', () => {
  const base: SavedChannel = {
    id: '1',
    displayName: 'yt',
    kickSlug: '',
    twitchSlug: '',
    youtubeSlug: 'UCtest',
    vodVideos: [],
    clipVideos: [],
    updatedAt: '',
    streamsFetched: true,
  };

  it('stops refetching after streamsFetched even when list is empty', () => {
    expect(channelStreamsMissing(base, true)).toBe(false);
  });

  it('requests fetch before streamsFetched', () => {
    expect(channelStreamsMissing({ ...base, streamsFetched: false }, true)).toBe(true);
  });
});

describe('isHiddenChannelPlatformError', () => {
  it('hides all channel platform errors from UI', () => {
    expect(isHiddenChannelPlatformError('failed to load cookies')).toBe(true);
    expect(isHiddenChannelPlatformError('Stream VOD fetch timed out — try again')).toBe(true);
    expect(formatChannelErrorMessage(
      {
        id: '1', displayName: 'x', kickSlug: '', twitchSlug: '', youtubeSlug: 'yt',
        vodVideos: [makeVod({ id: 'a', platform: 'YouTube', title: 't', duration: 1, created_at: '', views: 0 })],
        clipVideos: [], updatedAt: '',
        vodErrors: { YouTube: 'Stream VOD fetch timed out — try again' },
      },
      'streams', false, false, true,
    )).toBeNull();
  });
});

describe('videoInfoDurationSec', () => {
  it('returns 0 when duration unknown', () => {
    expect(videoInfoDurationSec(null)).toBe(0);
    expect(videoInfoDurationSec({ title: 'x' } as VideoInfo)).toBe(0);
  });
});

describe('syncDurationFromPreviewSession', () => {
  it('replaces 7200 placeholder with real duration', () => {
    const out = syncDurationFromPreviewSession(212, 0, 7200);
    expect(out).toEqual({ start: 0, end: 212, duration: 212 });
  });
  it('keeps client crop_end when session duration is higher', () => {
    const out = syncDurationFromPreviewSession(50, 0, 50);
    expect(out).toEqual({ start: 0, end: 50, duration: 50 });
  });
});

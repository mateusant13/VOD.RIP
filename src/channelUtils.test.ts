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
  buildVodUrl,
  slugFromVideoUrl,
  isChannelAlreadySaved,
} from './channelUtils';
import type { ChannelVideo, SavedChannel } from './types';

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

describe('channelClipsMissing', () => {
  const ch = (overrides: Partial<SavedChannel> = {}): SavedChannel => ({
    id: 'ch1',
    displayName: 'Test',
    kickSlug: 'test_kick',
    twitchSlug: 'test_twitch',
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
      clipVideos: [makeClip({ platform: 'Twitch', id: 'twitch_clip' })],
    });
    expect(channelClipsMissing(state, true, true)).toBe(true);
  });
});

describe('channelVodsMissing', () => {
  const ch = (overrides: Partial<SavedChannel> = {}): SavedChannel => ({
    id: 'ch1',
    displayName: 'Test',
    kickSlug: 'test_kick',
    twitchSlug: 'test_twitch',
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
    expect(s).toEqual({ kickSlug: 'xqc', twitchSlug: 'xqc' });
  });

  it('uses channel login for Twitch /videos/ URLs', () => {
    const s = slugFromVideoUrl('https://twitch.tv/videos/1', 'twitch', 'Display', 'login');
    expect(s.twitchSlug).toBe('login');
  });
});

describe('isChannelAlreadySaved', () => {
  const channels: SavedChannel[] = [{
    id: '1',
    displayName: 'F',
    kickSlug: 'Foo',
    twitchSlug: 'bar',
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

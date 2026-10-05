/**
 * The Shorts / Clips split.
 *
 * These are the acceptance tests for the reported defect: "when YouTube and
 * another platform are selected, separate shorts and clips. Don't leave
 * everything in the clips filter."
 *
 * The fixtures model what the backend ACTUALLY returns for `content=clips`:
 * backend/routers/channels.py merges the YouTube shorts playlist with the
 * Twitch and Kick clip crawls and stamps `content_kind: "clip"` on every row.
 * So in the real payload a YouTube Short and a Twitch clip are indistinguishable
 * by content_kind — which is why the URL is the discriminator under test.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import {
  CHANNEL_CONTENT_FILTERS,
  backendContentForFilter,
  isChannelContentFilter,
  isClipLikeFilter,
  isShortsVideo,
  normalizeChannelContentFilter,
  selectChannelContent,
  splitShortsAndClips,
} from './channelContentFilter';
import { CHANNEL_UI_STORAGE_KEY, loadStoredChannelUi } from './channelUtils';
import type { ChannelVideo } from './types';

const row = (o: Partial<ChannelVideo> & { id: string; url: string }): ChannelVideo => ({
  platform: 'YouTube',
  title: 'row',
  created_at: '2024-05-01T00:00:00Z',
  views: 10,
  ...o,
} as ChannelVideo);

/** The merged clips payload exactly as the backend builds it. */
const CLIP_PAYLOAD: ChannelVideo[] = [
  // A YouTube Short. content_kind is 'clip' — same value a Twitch clip gets.
  row({ id: 'yt-short-1', url: 'https://www.youtube.com/shorts/dQw4w9WgXcQ', content_kind: 'clip' }),
  // A YouTube clip from the /clips tab: NOT a short, despite content_kind 'clip'.
  row({ id: 'yt-clip-1', url: 'https://www.youtube.com/watch?v=abc123DEF_-', content_kind: 'clip' }),
  row({ id: 'tw-clip-1', platform: 'Twitch', url: 'https://clips.twitch.tv/EagerOtterGoal', content_kind: 'clip' }),
  row({ id: 'kick-clip-1', platform: 'Kick', url: 'https://kick.com/someone/clips/clip_xyz', content_kind: 'clip' }),
];

const VOD_PAYLOAD: ChannelVideo[] = [
  row({ id: 'yt-vod-1', url: 'https://www.youtube.com/watch?v=zzzzzzzzzzz', content_kind: 'vod' }),
  row({ id: 'yt-stream-1', url: 'https://www.youtube.com/watch?v=yyyyyyyyyyy', content_kind: 'stream' }),
  // A Short mis-filed into the VOD list by an older /videos fetch.
  row({ id: 'yt-short-2', url: 'https://www.youtube.com/shorts/aaaaaaaaaaa', content_kind: 'vod' }),
];

const lists = { clipVideos: CLIP_PAYLOAD, vodVideos: VOD_PAYLOAD };
const opts = { youtubePlatformOnly: false, keyOf: (v: ChannelVideo) => v.url };
const select = (filter: Parameters<typeof selectChannelContent>[0]) =>
  selectChannelContent(filter, lists, opts);

describe('isShortsVideo - the URL is the discriminator', () => {
  it('classifies a YouTube /shorts/ URL as a Short', () => {
    expect(isShortsVideo({ url: 'https://www.youtube.com/shorts/dQw4w9WgXcQ' })).toBe(true);
  });

  it('the URL wins when content_kind disagrees - /shorts/ + content_kind "clip" is a Short', () => {
    // This is the normal shape of every YouTube short in the clips payload.
    expect(isShortsVideo({ url: 'https://www.youtube.com/shorts/dQw4w9WgXcQ', content_kind: 'clip' })).toBe(true);
  });

  it('the URL wins when content_kind says "vod" but the URL is /shorts/', () => {
    expect(isShortsVideo({ url: 'https://www.youtube.com/shorts/aaaaaaaaaaa', content_kind: 'vod' })).toBe(true);
  });

  it('content_kind "clip" WITHOUT a /shorts/ URL is a Clip, not a Short', () => {
    // The disagreement case in the other direction, and the one that matters:
    // if content_kind were the discriminator, every Twitch/Kick clip below
    // would be listed as a Short.
    expect(isShortsVideo({ url: 'https://clips.twitch.tv/EagerOtterGoal', content_kind: 'clip' })).toBe(false);
    expect(isShortsVideo({ url: 'https://kick.com/someone/clips/clip_xyz', content_kind: 'clip' })).toBe(false);
    expect(isShortsVideo({ url: 'https://www.youtube.com/watch?v=abc123DEF_-', content_kind: 'clip' })).toBe(false);
  });

  it('is host-anchored: a non-YouTube /shorts/ path is not a Short', () => {
    expect(isShortsVideo({ url: 'https://example.com/shorts/abc123DEF_-' })).toBe(false);
  });

  it('youtu.be/<id> is the short form of a WATCH url, never a Short', () => {
    expect(isShortsVideo({ url: 'https://youtu.be/dQw4w9WgXcQ' })).toBe(false);
  });

  it('a missing or unparseable url is not a Short (it stays reachable in Clips)', () => {
    expect(isShortsVideo({ url: '' })).toBe(false);
    expect(isShortsVideo({ url: null })).toBe(false);
    expect(isShortsVideo({})).toBe(false);
    expect(isShortsVideo({ url: 'not a url' })).toBe(false);
  });
});

describe('splitShortsAndClips - disjoint and total', () => {
  it('partitions the payload with nothing duplicated and nothing lost', () => {
    const { shorts, clips } = splitShortsAndClips(CLIP_PAYLOAD);
    expect(shorts.map((v) => v.id)).toEqual(['yt-short-1']);
    expect(clips.map((v) => v.id)).toEqual(['yt-clip-1', 'tw-clip-1', 'kick-clip-1']);
    expect(shorts.length + clips.length).toBe(CLIP_PAYLOAD.length);
  });
});

describe('selectChannelContent - YouTube AND another platform selected', () => {
  it('the Shorts filter contains ONLY shorts', () => {
    const ids = select('shorts').map((v) => v.id);
    expect(ids).toContain('yt-short-1');
    // A Short mis-filed into the VOD list is still a Short, not lost.
    expect(ids).toContain('yt-short-2');
    // The regression: none of these may appear under Shorts.
    expect(ids).not.toContain('tw-clip-1');
    expect(ids).not.toContain('kick-clip-1');
    expect(ids).not.toContain('yt-clip-1');
    for (const v of select('shorts')) expect(isShortsVideo(v)).toBe(true);
  });

  it('the Clips filter contains NO shorts', () => {
    const rows = select('clips');
    const ids = rows.map((v) => v.id);
    expect(ids).toEqual(['yt-clip-1', 'tw-clip-1', 'kick-clip-1']);
    for (const v of rows) expect(isShortsVideo(v)).toBe(false);
  });

  it('every clip-shaped row lands in exactly one of the two filters', () => {
    // Nothing is duplicated across the two lists and nothing vanishes: the
    // Shorts list is a genuine split of the cached rows, not a filter that
    // shadows or re-fetches them.
    const shortIds = new Set(select('shorts').map((v) => v.id));
    const clipIds = new Set(select('clips').map((v) => v.id));
    for (const id of shortIds) expect(clipIds.has(id)).toBe(false);
    expect([...shortIds, ...clipIds].sort()).toEqual(
      ['kick-clip-1', 'tw-clip-1', 'yt-clip-1', 'yt-short-1', 'yt-short-2'].sort(),
    );
  });

  it('the Shorts filter stays correct in YouTube-only mode too', () => {
    const ytOnly = selectChannelContent('shorts', lists, { ...opts, youtubePlatformOnly: true });
    expect(ytOnly.map((v) => v.id).sort()).toEqual(['yt-short-1', 'yt-short-2']);
  });
});

describe('the filter survives a reload', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('accepts every declared filter value', () => {
    for (const f of CHANNEL_CONTENT_FILTERS) {
      expect(isChannelContentFilter(f)).toBe(true);
      expect(normalizeChannelContentFilter(f)).toBe(f);
    }
  });

  it('still coerces junk to vods instead of adopting an unknown filter', () => {
    expect(normalizeChannelContentFilter(undefined)).toBe('vods');
    expect(normalizeChannelContentFilter('nope')).toBe('vods');
    expect(normalizeChannelContentFilter(7)).toBe('vods');
    expect(isChannelContentFilter('nope')).toBe(false);
  });

  it('localStorage restores "shorts" instead of silently reverting to vods', () => {
    // THE reload bug. loadStoredChannelUi used to enumerate clips/vods/streams
    // inline, so a persisted `shorts` was coerced to `vods` on every launch and
    // the filter looked like it had never been set.
    localStorage.setItem(
      CHANNEL_UI_STORAGE_KEY,
      JSON.stringify({ kick: true, twitch: true, youtube: true, content: 'shorts' }),
    );
    expect(loadStoredChannelUi().content).toBe('shorts');
  });

  it('still restores the older filter values', () => {
    for (const f of ['vods', 'clips', 'streams', 'shorts'] as const) {
      localStorage.setItem(CHANNEL_UI_STORAGE_KEY, JSON.stringify({ content: f }));
      expect(loadStoredChannelUi().content).toBe(f);
    }
  });
});

describe('backendContentForFilter - the backend only knows three values', () => {
  it('shorts is served by the clips payload, not a value of its own', () => {
    // backend/routers/channels.py treats any `content` that is neither
    // "clips" nor "streams" as vods, so sending `shorts` would fetch VODs.
    expect(backendContentForFilter('shorts')).toBe('clips');
    expect(backendContentForFilter('clips')).toBe('clips');
    expect(backendContentForFilter('streams')).toBe('streams');
    expect(backendContentForFilter('vods')).toBe('vods');
  });

  it('shorts shares the clips payload bookkeeping', () => {
    expect(isClipLikeFilter('shorts')).toBe(true);
    expect(isClipLikeFilter('clips')).toBe(true);
    expect(isClipLikeFilter('streams')).toBe(false);
    expect(isClipLikeFilter('vods')).toBe(false);
  });
});

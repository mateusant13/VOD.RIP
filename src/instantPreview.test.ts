import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  findInstantPreview,
  getInstantPreviews,
  refreshInstantPreviews,
  resetInstantPreviewsForTests,
  videoIdFromOpenedUrl,
  INSTANT_PREVIEWS_TTL_MS,
  type InstantPreviewEntry,
} from './instantPreview';

const ENTRY: InstantPreviewEntry = {
  channel_id: 'c1',
  platform: 'twitch',
  title: 'Some VOD',
  vod_url: 'https://www.twitch.tv/videos/123456789',
  vod_id: '123456789',
  video_id: null,
  generated_at: '2026-08-13T00:00:00Z',
  media_url: '/api/previews/c1/media',
};

const YT_ENTRY: InstantPreviewEntry = {
  ...ENTRY,
  channel_id: 'c2',
  platform: 'youtube',
  vod_url: 'https://www.youtube.com/watch?v=AbCdEfGhIjK',
  vod_id: '',
  video_id: 'AbCdEfGhIjK',
  media_url: '/api/previews/c2/media',
};

const KICK_ENTRY: InstantPreviewEntry = {
  ...ENTRY,
  channel_id: 'c3',
  platform: 'kick',
  vod_url: 'https://kick.com/cellbit/videos/3f2b0c1a-1f5e-4b3a-9c8d-1a2b3c4d5e6f',
  vod_id: '3f2b0c1a-1f5e-4b3a-9c8d-1a2b3c4d5e6f',
  video_id: null,
  media_url: '/api/previews/c3/media',
};

function mockFetchOk(body: unknown): void {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
    ok: true,
    json: () => Promise.resolve(body),
  }));
}

function mockFetchFail(): void {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false }));
}

describe('videoIdFromOpenedUrl', () => {
  it('extracts Twitch ids from full URLs and bare numbers', () => {
    expect(videoIdFromOpenedUrl('https://www.twitch.tv/videos/123456789')).toBe('123456789');
    expect(videoIdFromOpenedUrl('https://twitch.tv/videos/123456789?t=1h2m')).toBe('123456789');
    expect(videoIdFromOpenedUrl('123456789')).toBe('123456789');
  });

  it('extracts YouTube ids from watch/shorts/youtu.be URLs', () => {
    expect(videoIdFromOpenedUrl('https://www.youtube.com/watch?v=AbCdEfGhIjK')).toBe('AbCdEfGhIjK');
    expect(videoIdFromOpenedUrl('https://youtu.be/AbCdEfGhIjK')).toBe('AbCdEfGhIjK');
    expect(videoIdFromOpenedUrl('https://www.youtube.com/shorts/AbCdEfGhIjK')).toBe('AbCdEfGhIjK');
  });

  it('extracts Kick uuids', () => {
    expect(videoIdFromOpenedUrl('https://kick.com/cellbit/videos/3f2b0c1a-1f5e-4b3a-9c8d-1a2b3c4d5e6f'))
      .toBe('3f2b0c1a-1f5e-4b3a-9c8d-1a2b3c4d5e6f');
  });

  it('returns null for clip URLs and nonsense', () => {
    expect(videoIdFromOpenedUrl('https://clips.twitch.tv/SlugHere')).toBeNull();
    expect(videoIdFromOpenedUrl('https://kick.com/x/videos/not-a-uuid')).toBeNull();
    expect(videoIdFromOpenedUrl('')).toBeNull();
  });
});

describe('findInstantPreview', () => {
  beforeEach(() => {
    mockFetchOk({ previews: [ENTRY, YT_ENTRY, KICK_ENTRY] });
  });
  afterEach(() => {
    resetInstantPreviewsForTests();
    vi.unstubAllGlobals();
  });

  it('matches the exact vod_url (case/trailing-slash tolerant)', async () => {
    await refreshInstantPreviews(true);
    expect(findInstantPreview('https://www.twitch.tv/videos/123456789')?.channel_id).toBe('c1');
    expect(findInstantPreview('https://www.TWITCH.TV/videos/123456789/')?.channel_id).toBe('c1');
  });

  it('falls back to vod_id when the URL spelling differs (bare Twitch id)', async () => {
    await refreshInstantPreviews(true);
    expect(findInstantPreview('123456789')?.channel_id).toBe('c1');
  });

  it('falls back to video_id for YouTube short links', async () => {
    await refreshInstantPreviews(true);
    expect(findInstantPreview('https://youtu.be/AbCdEfGhIjK')?.channel_id).toBe('c2');
    expect(findInstantPreview('https://www.youtube.com/watch?v=abcdefghijk')?.channel_id).toBe('c2');
  });

  it('matches Kick vod_url and uuid fallback', async () => {
    await refreshInstantPreviews(true);
    expect(findInstantPreview('https://kick.com/cellbit/videos/3f2b0c1a-1f5e-4b3a-9c8d-1a2b3c4d5e6f')?.channel_id).toBe('c3');
  });

  it('returns null when nothing matches', async () => {
    await refreshInstantPreviews(true);
    expect(findInstantPreview('https://www.twitch.tv/videos/999')).toBeNull();
    expect(findInstantPreview('https://clips.twitch.tv/SlugHere')).toBeNull();
    expect(findInstantPreview('')).toBeNull();
  });
});

describe('refreshInstantPreviews', () => {
  beforeEach(() => resetInstantPreviewsForTests());
  afterEach(() => {
    resetInstantPreviewsForTests();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('populates the registry from a valid response', async () => {
    mockFetchOk({ previews: [ENTRY] });
    await refreshInstantPreviews();
    expect(getInstantPreviews()).toHaveLength(1);
    expect(getInstantPreviews()[0].media_url).toBe('/api/previews/c1/media');
  });

  it('degrades to an empty map on a non-OK response (endpoint absent)', async () => {
    mockFetchFail();
    await refreshInstantPreviews();
    expect(getInstantPreviews()).toEqual([]);
  });

  it('degrades to an empty map on a network error — never rejects', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    await expect(refreshInstantPreviews()).resolves.toBeUndefined();
    expect(getInstantPreviews()).toEqual([]);
  });

  it('filters malformed entries (missing channel_id/media_url)', async () => {
    mockFetchOk({ previews: [ENTRY, { channel_id: 'x' }, { media_url: '/y' }, null] });
    await refreshInstantPreviews(true);
    expect(getInstantPreviews()).toHaveLength(1);
  });

  it('caches within the TTL and refetches on force', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(1_000_000);
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ previews: [ENTRY] }) });
    vi.stubGlobal('fetch', fetchMock);
    await refreshInstantPreviews();
    await refreshInstantPreviews();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    vi.setSystemTime(1_000_000 + INSTANT_PREVIEWS_TTL_MS);
    await refreshInstantPreviews(true); // force skips TTL
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('treats the empty result as cacheable too (no refetch spam)', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(1_000_000);
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ previews: [] }) });
    vi.stubGlobal('fetch', fetchMock);
    await refreshInstantPreviews();
    await refreshInstantPreviews();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

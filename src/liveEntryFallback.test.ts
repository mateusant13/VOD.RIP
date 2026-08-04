import { describe, expect, it } from 'vitest';
import { nextLiveEntry, type LiveFallbackEntry } from './liveEntryFallback';

const mkEntry = (url: string, overrides?: Partial<LiveFallbackEntry>): LiveFallbackEntry => ({
  url,
  ...overrides,
});

describe('nextLiveEntry', () => {
  it('advances to the next entry in the list', () => {
    const entries = [mkEntry('kick.com/a'), mkEntry('twitch.tv/a')];
    expect(nextLiveEntry(entries, 0)).toBe(entries[1]);
  });

  it('advances through the middle of a multi-entry list', () => {
    const entries = [mkEntry('a'), mkEntry('b'), mkEntry('c')];
    expect(nextLiveEntry(entries, 1)).toBe(entries[2]);
  });

  it('returns null at the last entry — no wrap-back to a failed entry', () => {
    const entries = [mkEntry('a'), mkEntry('b')];
    expect(nextLiveEntry(entries, 1)).toBeNull();
  });

  it('returns null for a single-entry list (no fallback available)', () => {
    expect(nextLiveEntry([mkEntry('a')], 0)).toBeNull();
  });

  it('returns null for an empty list', () => {
    expect(nextLiveEntry([], 0)).toBeNull();
  });

  it('returns null for out-of-range or negative indices', () => {
    const entries = [mkEntry('a'), mkEntry('b')];
    expect(nextLiveEntry(entries, -1)).toBeNull();
    expect(nextLiveEntry(entries, 2)).toBeNull();
    expect(nextLiveEntry(entries, 99)).toBeNull();
  });

  it('preserves entry identity and metadata (platform/title/headers)', () => {
    const entries = [
      mkEntry('kick.com/a', { platform: 'kick', title: 'L1', headers: { h: '1' } }),
      mkEntry('twitch.tv/a', { platform: 'twitch', title: 'L2' }),
    ];
    const next = nextLiveEntry(entries, 0);
    expect(next).toEqual(entries[1]);
    expect(next?.platform).toBe('twitch');
    expect(next?.headers).toBeUndefined();
  });

  it('accepts structurally compatible richer entries (App LiveEntry shape)', () => {
    const entries = [
      { url: 'u1', platform: 'kick', title: 't1', headers: {}, is_live: true, type: 'stream', viewer_count: 42 },
      { url: 'u2', platform: 'twitch', title: 't2', headers: {}, is_live: true, type: 'stream' },
    ];
    const next = nextLiveEntry(entries, 0);
    expect(next?.url).toBe('u2');
    expect((next as { viewer_count?: number } | null)?.viewer_count).toBeUndefined();
  });
});

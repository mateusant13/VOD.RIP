// @vitest-environment node
import { describe, it, expect } from 'vitest';
import { archiveVideoIdFromUrl, isNativeArchiveVideoId } from './archiveScope';

describe('archiveVideoIdFromUrl', () => {
  it('extracts YouTube watch v= ids', () => {
    expect(archiveVideoIdFromUrl('https://www.youtube.com/watch?v=dQw4w9WgXcQ')).toBe('dQw4w9WgXcQ');
    expect(archiveVideoIdFromUrl('https://youtube.com/watch?v=abc123DEF_-&t=42')).toBe('abc123DEF_-');
  });

  it('extracts youtu.be and shorts ids', () => {
    expect(archiveVideoIdFromUrl('https://youtu.be/dQw4w9WgXcQ')).toBe('dQw4w9WgXcQ');
    expect(archiveVideoIdFromUrl('https://www.youtube.com/shorts/abc123DEF_-')).toBe('abc123DEF_-');
  });

  it('extracts numeric Twitch VOD ids (v-prefix form too)', () => {
    expect(archiveVideoIdFromUrl('https://www.twitch.tv/videos/2117068816')).toBe('2117068816');
    expect(archiveVideoIdFromUrl('https://m.twitch.tv/videos/2117068816')).toBe('2117068816');
  });

  it('extracts Kick video uuids', () => {
    expect(archiveVideoIdFromUrl('https://kick.com/some-channel/videos/9f7e3f0a-1b2c-4d5e-8f90-ab12cd34ef56')).toBe('9f7e3f0a-1b2c-4d5e-8f90-ab12cd34ef56');
  });

  it('returns null for non-video or ambiguous URLs', () => {
    expect(archiveVideoIdFromUrl('https://www.twitch.tv/somechannel')).toBeNull();
    expect(archiveVideoIdFromUrl('https://www.youtube.com/watch?v=')).toBeNull();
    expect(archiveVideoIdFromUrl('https://clips.twitch.tv/AmazingSlug123')).toBeNull();
    expect(archiveVideoIdFromUrl('https://kick.com/some-channel')).toBeNull();
    expect(archiveVideoIdFromUrl('not a url')).toBeNull();
    expect(archiveVideoIdFromUrl('')).toBeNull();
  });
});

describe('isNativeArchiveVideoId', () => {
  it('accepts native ids', () => {
    expect(isNativeArchiveVideoId('2117068816')).toBe(true);
    expect(isNativeArchiveVideoId('dQw4w9WgXcQ')).toBe(true);
  });

  it('rejects URL-like synthetic ids', () => {
    expect(isNativeArchiveVideoId('https://www.twitch.tv/videos/2117068816')).toBe(false);
    expect(isNativeArchiveVideoId(null)).toBe(false);
    expect(isNativeArchiveVideoId(undefined)).toBe(false);
    expect(isNativeArchiveVideoId('')).toBe(false);
  });
});

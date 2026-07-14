import { describe, expect, it } from 'vitest';
import { youtubeVideoIdFromUrl } from './youtubeEmbed';

describe('youtubeVideoIdFromUrl', () => {
  it('parses common YouTube URL shapes', () => {
    expect(youtubeVideoIdFromUrl('https://www.youtube.com/watch?v=abc_123-XYZ&t=4')).toBe('abc_123-XYZ');
    expect(youtubeVideoIdFromUrl('https://youtu.be/abc_123-XYZ')).toBe('abc_123-XYZ');
    expect(youtubeVideoIdFromUrl('https://youtube.com/shorts/abc_123-XYZ')).toBe('abc_123-XYZ');
    expect(youtubeVideoIdFromUrl('https://youtube.com/live/abc_123-XYZ')).toBe('abc_123-XYZ');
  });
});

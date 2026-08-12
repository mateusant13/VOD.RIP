import { describe, expect, it } from 'vitest';
import { previewDownloadTree } from './downloadLayout';

describe('previewDownloadTree', () => {
  it('flat layout keeps every file in the root', () => {
    const rows = previewDownloadTree('D:\\Videos', 'flat');
    expect(rows.every((p) => p.startsWith('D:\\Videos\\'))).toBe(true);
    expect(rows.some((p) => p.includes('\\VODs\\'))).toBe(false);
  });

  it('typed layout uses subfolders', () => {
    const rows = previewDownloadTree('D:/Videos', 'typed');
    expect(rows).toContain('D:/Videos/VODs/full-vod.mp4');
    expect(rows).toContain('D:/Videos/Twitch clips/twitch-clip.mp4');
    expect(rows).toContain('D:/Videos/Cuts/trim-cut.mp4');
  });
});

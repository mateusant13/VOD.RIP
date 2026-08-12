/** Download folder layout: one flat folder, or typed subfolders. */

export type DownloadLayout = 'flat' | 'typed';

export const DOWNLOAD_SUBDIRS = {
  vods: 'VODs',
  cuts: 'Cuts',
  clips: 'Clips',
  twitchClips: 'Twitch clips',
  live: 'Live',
  audio: 'Audio',
  chat: 'Chat',
} as const;

export function previewDownloadTree(root: string, layout: DownloadLayout): string[] {
  const base = (root || 'Downloads').replace(/[\\/]+$/, '');
  const sep = base.includes('\\') ? '\\' : '/';
  const join = (...parts: string[]) => [base, ...parts].join(sep);
  if (layout === 'flat') {
    return [
      join('full-vod.mp4'),
      join('full-vod.txt'),
      join('trim-cut.mp4'),
      join('clip.mp4'),
      join('twitch-clip.mp4'),
      join('live-stream.mp4'),
      join('audio.mp3'),
      join('vod.chat.txt'),
    ];
  }
  return [
    join(DOWNLOAD_SUBDIRS.vods, 'full-vod.mp4'),
    join(DOWNLOAD_SUBDIRS.vods, 'full-vod.txt'),
    join(DOWNLOAD_SUBDIRS.cuts, 'trim-cut.mp4'),
    join(DOWNLOAD_SUBDIRS.clips, 'clip.mp4'),
    join(DOWNLOAD_SUBDIRS.twitchClips, 'twitch-clip.mp4'),
    join(DOWNLOAD_SUBDIRS.live, 'live-stream.mp4'),
    join(DOWNLOAD_SUBDIRS.audio, 'audio.mp3'),
    join(DOWNLOAD_SUBDIRS.chat, 'vod.chat.txt'),
  ];
}

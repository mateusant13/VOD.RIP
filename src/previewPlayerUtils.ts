export const PREVIEW_MAIN_DEFAULT_HEIGHT = 480;
export const PREVIEW_EXPLORE_DEFAULT_HEIGHT = 360;
export const PREVIEW_CLIP_DEFAULT_HEIGHT = 360;

export function isClipPreviewUrl(u: string): boolean {
  const l = u.toLowerCase();
  if (l.includes('clips.twitch.tv')) return true;
  if (l.includes('twitch.tv') && l.includes('/clip/')) return true;
  if (l.includes('kick.com') && l.includes('/clips/')) return true;
  return false;
}

/** Normalize preview session response — Twitch clips are progressive MP4, not HLS. */
export function resolvePreviewPlayback(
  pageUrl: string,
  session: { playback_url?: string; master_url: string; kind?: string },
): { url: string; kind: 'hls' | 'progressive' } {
  const progressive = isClipPreviewUrl(pageUrl) || session.kind === 'progressive';
  const url = session.playback_url || session.master_url;
  return { url, kind: progressive ? 'progressive' : 'hls' };
}

/** Load proxied MP4 into a <video> — use <source type="video/mp4"> so .m3u8 paths still play. */
export function attachProgressivePreview(video: HTMLVideoElement, playbackUrl: string): void {
  video.innerHTML = '';
  video.removeAttribute('src');
  const source = document.createElement('source');
  source.src = playbackUrl;
  source.type = 'video/mp4';
  video.appendChild(source);
  video.load();
}

export function detachProgressivePreview(video: HTMLVideoElement): void {
  video.pause();
  video.innerHTML = '';
  video.removeAttribute('src');
  video.load();
}

export function channelSlugFromMediaUrl(u: string): string | null {
  const kick = u.match(/kick\.com\/([^/?#]+)/i);
  if (kick && !['videos', 'clips'].includes(kick[1].toLowerCase())) return kick[1];
  const tw = u.match(/twitch\.tv\/([^/?#]+)/i);
  if (tw && !['videos', 'clip', 'directory', 'clips'].includes(tw[1].toLowerCase())) return tw[1];
  return null;
}

export function suggestClipDownloadName(
  title: string | null | undefined,
  uploader: string | null | undefined,
  mediaUrl: string,
): string {
  const clipper = uploader?.trim() || channelSlugFromMediaUrl(mediaUrl) || 'channel';
  const clipTitle = title?.trim() || 'Untitled';
  return `${clipper} - ${clipTitle} (clip)`;
}

export interface PreviewLevelOption {
  index: number;
  height: number;
  label: string;
}

type HlsLevelLike = {
  height?: number;
  width?: number;
  bitrate?: number;
  url?: string | string[];
  name?: string;
  attrs?: { RESOLUTION?: string };
};

export function previewLevelLabel(height: number, bitrate?: number, isSourceLevel = false): string {
  if (!height) return 'Auto';
  const res = `${height}p`;
  if (isSourceLevel) return `source/${res}`;
  const kbps = bitrate ? Math.round(bitrate / 1000) : 0;
  return kbps > 0 ? `${res} · ${kbps}k` : res;
}

export function levelIndexForHeight(levels: PreviewLevelOption[], target: number): number {
  if (!levels.length) return 0;
  const matches = levels.filter((l) => l.height === target);
  if (matches.length) return matches[0].index;
  const below = levels.filter((l) => l.height > 0 && l.height < target);
  if (below.length) return below[below.length - 1].index;
  const above = levels.filter((l) => l.height > target);
  if (above.length) return above[0].index;
  return levels[0].index;
}

export function inferLevelHeight(level: HlsLevelLike): number {
  if (level.height && level.height > 0) return level.height;
  const res = level.attrs?.RESOLUTION;
  if (res) {
    const m = res.match(/x(\d+)/i);
    if (m) return parseInt(m[1], 10);
  }
  const urlRaw = level.url;
  const url = (Array.isArray(urlRaw) ? urlRaw[0] : urlRaw) || level.name || '';
  const urlM = url.match(/\/(\d{3,4})p\d*\//i) || url.match(/(\d{3,4})p/i);
  if (urlM) return parseInt(urlM[1], 10);
  return 0;
}

export function mapHlsLevels(
  levels: HlsLevelLike[],
  defaultHeight: number,
): { mapped: PreviewLevelOption[]; defaultIndex: number } {
  const raw = levels.map((l, i) => ({
    index: i,
    height: inferLevelHeight(l),
    bitrate: l.bitrate,
  }));
  const maxHeight = raw.reduce((max, l) => Math.max(max, l.height), 0);
  const mapped: PreviewLevelOption[] = raw.map((l) => ({
    index: l.index,
    height: l.height,
    label: previewLevelLabel(l.height, l.bitrate, l.height === maxHeight && maxHeight > 0),
  }));
  mapped.sort((a, b) => a.height - b.height);
  const defaultIndex = mapped.length ? levelIndexForHeight(mapped, defaultHeight) : 0;
  return { mapped, defaultIndex };
}

/** Map HLS levels for the quality menu; synthesize one entry for single-quality streams (e.g. Kick clips). */
export function resolvePreviewLevels(
  levels: HlsLevelLike[],
  defaultHeight: number,
): { mapped: PreviewLevelOption[]; defaultIndex: number } {
  const result = mapHlsLevels(levels, defaultHeight);
  if (result.mapped.length) {
    if (result.mapped.every((l) => !l.height)) {
      const height = defaultHeight;
      return {
        mapped: result.mapped.map((l) => ({
          ...l,
          height,
          label: previewLevelLabel(height, undefined, true),
        })),
        defaultIndex: 0,
      };
    }
    return result;
  }
  return {
    mapped: [{
      index: 0,
      height: defaultHeight,
      label: previewLevelLabel(defaultHeight, undefined, true),
    }],
    defaultIndex: 0,
  };
}

export function applyHlsQualityLevel(
  hls: { levels: unknown[]; loadLevel: number; nextLevel: number },
  levelIndex: number,
  forceLoad = false,
): void {
  if (levelIndex < 0 || levelIndex >= hls.levels.length) return;
  if (forceLoad) hls.loadLevel = levelIndex;
  else hls.nextLevel = levelIndex;
}

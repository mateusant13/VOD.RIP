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

const PREVIEW_HEIGHT_STEPS = [240, 360, 480, 720, 1080] as const;

/** Snap CSS player height to a stream tier — avoids fetching 1080p for a tiny panel. */
export function snapPreviewHeight(cssPx: number): number {
  if (!Number.isFinite(cssPx) || cssPx <= 0) return PREVIEW_CLIP_DEFAULT_HEIGHT;
  for (const step of PREVIEW_HEIGHT_STEPS) {
    if (cssPx <= step + 48) return step;
  }
  return 1080;
}

/** Max stream height that matches the on-screen player box (logical CSS pixels). */
export function measurePlayerHeightCap(element: HTMLElement | null, aspect = 16 / 9): number {
  if (!element) return PREVIEW_MAIN_DEFAULT_HEIGHT;
  const r = element.getBoundingClientRect();
  let h = r.height;
  if (h < 48 && r.width > 0) h = r.width / aspect;
  return snapPreviewHeight(h);
}

/** First preview load: fast default (360p clips / 480p VOD), never above player cap. */
export function initialPreviewPreferHeight(isClip: boolean, playerCap: number): number {
  const desired = isClip ? PREVIEW_CLIP_DEFAULT_HEIGHT : PREVIEW_MAIN_DEFAULT_HEIGHT;
  return Math.min(desired, playerCap);
}

export function parseQualityHeights(qualities: string[]): number[] {
  const heights = qualities
    .map((q) => {
      const m = q.match(/(\d+)/);
      return m ? parseInt(m[1], 10) : 0;
    })
    .filter((h) => h > 0);
  return [...new Set(heights)].sort((a, b) => a - b);
}

/** Quality menu entries; highest available height is labelled source/… */
export function mapHeightsToPreviewLevels(heights: number[]): PreviewLevelOption[] {
  if (!heights.length) return [];
  const maxH = Math.max(...heights);
  return heights.map((height, index) => ({
    index,
    height,
    label: previewLevelLabel(height, undefined, height === maxH),
  }));
}

/** Highest quality label for download / URL info — never tied to preview playback. */
export function maxQualityLabelFromList(qualities: string[]): string {
  const heights = parseQualityHeights(qualities);
  if (heights.length) return `${heights[heights.length - 1]}p`;
  return '1080p';
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

export function mergeVariantHeights(
  ...sources: Array<number[] | undefined | null>
): number[] {
  const set = new Set<number>();
  for (const src of sources) {
    for (const h of src ?? []) {
      if (h > 0) set.add(h);
    }
  }
  return [...set].sort((a, b) => a - b);
}

function mapInferredHlsLevels(
  raw: Array<{ index: number; height: number; bitrate?: number }>,
  initialHeight: number,
): { mapped: PreviewLevelOption[]; defaultIndex: number } {
  const withHeight = raw.filter((l) => l.height > 0);
  if (!withHeight.length) {
    return { mapped: [], defaultIndex: 0 };
  }
  const maxH = Math.max(...withHeight.map((l) => l.height));
  const mapped = withHeight
    .map((l) => ({
      index: l.index,
      height: l.height,
      label: previewLevelLabel(l.height, l.bitrate, l.height === maxH),
    }))
    .sort((a, b) => a.height - b.height);
  return {
    mapped,
    defaultIndex: levelIndexForHeight(mapped, initialHeight),
  };
}

/**
 * Build the preview quality menu for HLS. Menu tiers come from the manifest and/or
 * API variant_heights — never from the player-size cap alone (that only picks initial playback).
 */
export function resolveHlsPreviewLevels(
  hlsLevels: HlsLevelLike[],
  opts: { initialHeight: number; fallbackHeights?: number[] },
): { mapped: PreviewLevelOption[]; defaultIndex: number } {
  const fallback = mergeVariantHeights(opts.fallbackHeights);
  const initialHeight = opts.initialHeight;

  if (hlsLevels.length > 0) {
    const raw = hlsLevels.map((l, i) => ({
      index: i,
      height: inferLevelHeight(l),
      bitrate: l.bitrate,
    }));

    if (raw.every((l) => !l.height)) {
      if (fallback.length === hlsLevels.length) {
        raw.forEach((l, i) => { l.height = fallback[i]; });
      } else if (hlsLevels.length === 1 && fallback.length > 1) {
        const mapped = mapHeightsToPreviewLevels(fallback);
        return {
          mapped,
          defaultIndex: levelIndexForHeight(mapped, initialHeight),
        };
      } else if (fallback.length > 0) {
        const mapped = mapHeightsToPreviewLevels(fallback);
        if (mapped.length === hlsLevels.length) {
          mapped.forEach((m, i) => { m.index = i; });
        }
        return {
          mapped,
          defaultIndex: levelIndexForHeight(mapped, initialHeight),
        };
      }
    }

    const fromHls = mapInferredHlsLevels(raw, initialHeight);
    if (fromHls.mapped.length) return fromHls;
  }

  if (fallback.length > 0) {
    const mapped = mapHeightsToPreviewLevels(fallback);
    return {
      mapped,
      defaultIndex: levelIndexForHeight(mapped, initialHeight),
    };
  }

  if (hlsLevels.length > 0) {
    const mapped = hlsLevels.map((_l, i) => ({
      index: i,
      height: 0,
      label: `Level ${i + 1}`,
    }));
    return { mapped, defaultIndex: 0 };
  }

  return { mapped: [], defaultIndex: 0 };
}

/** @deprecated Use resolveHlsPreviewLevels */
export function resolvePreviewLevels(
  levels: HlsLevelLike[],
  defaultHeight: number,
  fallbackHeights?: number[],
): { mapped: PreviewLevelOption[]; defaultIndex: number } {
  return resolveHlsPreviewLevels(levels, {
    initialHeight: defaultHeight,
    fallbackHeights,
  });
}

/** Progressive / API-only quality menu (never collapse to active playback height only). */
export function resolveProgressivePreviewLevels(
  opts: {
    variantHeights?: number[];
    qualityLabels?: string[];
    initialHeight: number;
  },
): { mapped: PreviewLevelOption[]; defaultIndex: number } {
  const heights = mergeVariantHeights(
    opts.variantHeights,
    parseQualityHeights(opts.qualityLabels ?? []),
  );
  const mapped = mapHeightsToPreviewLevels(heights);
  if (!mapped.length) {
    return { mapped: [], defaultIndex: 0 };
  }
  return {
    mapped,
    defaultIndex: levelIndexForHeight(mapped, opts.initialHeight),
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

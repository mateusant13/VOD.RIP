export const PREVIEW_MAIN_DEFAULT_HEIGHT = 480;
export const PREVIEW_EXPLORE_DEFAULT_HEIGHT = 360;

export interface PreviewLevelOption {
  index: number;
  height: number;
  label: string;
}

type HlsLevelLike = {
  height?: number;
  width?: number;
  bitrate?: number;
  url?: string;
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
  const url = level.url || level.name || '';
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

export function applyHlsQualityLevel(
  hls: { levels: unknown[]; loadLevel: number; nextLevel: number },
  levelIndex: number,
  forceLoad = false,
): void {
  if (levelIndex < 0 || levelIndex >= hls.levels.length) return;
  if (forceLoad) hls.loadLevel = levelIndex;
  else hls.nextLevel = levelIndex;
}

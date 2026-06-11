/** Responsive UI scale — reference layout is 1280×800 (default app window). */

const REF_W = 1280;
const REF_H = 800;

export type ViewportTier = 'narrow' | 'normal' | 'wide';

/** Slight lift over the original fixed-size UI (~5% at 1280×800, up to ~10% on large displays). */
const BASE_SCALE = 1.05;
const MAX_SCALE = 1.1;

export function computeUiScale(width: number, height: number): number {
  const areaRatio = Math.sqrt((width * height) / (REF_W * REF_H));
  const extra = Math.max(0, areaRatio - 1) * 0.06;
  const scale = BASE_SCALE + extra;
  return Math.min(MAX_SCALE, Math.max(1, Math.round(scale * 100) / 100));
}

export function viewportTier(width: number): ViewportTier {
  if (width < 1080) return 'narrow';
  if (width >= 2000) return 'wide';
  return 'normal';
}

export function applyUiScale(
  width = window.innerWidth,
  height = window.innerHeight,
): number {
  const scale = computeUiScale(width, height);
  const root = document.documentElement;
  root.style.setProperty('--ui-scale', String(scale));
  root.dataset.viewport = viewportTier(width);
  return scale;
}

export function readUiScale(): number {
  if (typeof window === 'undefined') return 1;
  const raw = getComputedStyle(document.documentElement).getPropertyValue('--ui-scale').trim();
  const parsed = parseFloat(raw);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : computeUiScale(window.innerWidth, window.innerHeight);
}

export function initUiScale(): () => void {
  applyUiScale();
  let timer: number | undefined;
  const onResize = () => {
    if (timer) window.clearTimeout(timer);
    timer = window.setTimeout(() => applyUiScale(), 80);
  };
  window.addEventListener('resize', onResize);
  return () => {
    if (timer) window.clearTimeout(timer);
    window.removeEventListener('resize', onResize);
  };
}

export function panelMaxWidthCap(): number {
  if (typeof window === 'undefined') return 1000;
  return window.innerWidth >= 2000 ? 1120 : 1000;
}

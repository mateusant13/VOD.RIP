import { useRef, useCallback, useEffect } from 'react';

/**
 * Pointer-capture draggable gutter between two panels.
 * - onPointerDown → setPointerCapture → onPointerMove (rAF) → onPointerUp
 * - Enforces minima: sidebar >=240, queue >=260, preview col >=280, player >=400
 * - Persists to localStorage vodrip.layout.* and restores on reload
 * - Responsive stack below 900px via CSS (frame.css)
 * - No layout thrash: rAF coalescing + willChange + direct DOM writes, React state only on pointerup
 * ponytail: keyboard alternative via arrow keys (16px/32px with Shift); upgrade = full roving-tabindex + per-edge resize handles
 */

const MINIMA: Record<string, number> = {
  sidebar: 240,
  queue: 260,
  previewCol: 280,
  player: 400,
  main: 260,
};

export const PANEL_MINIMA = MINIMA;

function readPersisted(key: string, fallback: number): number {
  try {
    const raw = localStorage.getItem(`vodrip.layout.${key}`);
    if (raw != null) {
      const n = Number(raw);
      if (Number.isFinite(n)) return n;
    }
  } catch {}
  return fallback;
}

function writePersisted(key: string, value: number) {
  try {
    localStorage.setItem(`vodrip.layout.${key}`, String(Math.round(value)));
  } catch {}
}

type Props = {
  leftRef?: React.RefObject<HTMLElement | null>;
  rightRef?: React.RefObject<HTMLElement | null>;
  persistKey?: string;
  minLeft?: number;
  minRight?: number;
  orientation?: 'vertical' | 'horizontal';
  onResize?: (delta: number) => void;
};

export default function PanelResizer({
  leftRef,
  rightRef,
  persistKey = 'previewCol',
  minLeft,
  minRight,
  orientation = 'vertical',
  onResize,
}: Props) {
  const startXRef = useRef(0);
  const startWRef = useRef(0);
  const rafRef = useRef<number | null>(null);
  const pendingDxRef = useRef(0);
  const activeElRef = useRef<HTMLElement | null>(null);

  const ensureRestored = useCallback((el: HTMLElement | null) => {
    if (!el || !persistKey) return;
    const persisted = readPersisted(persistKey, NaN);
    if (Number.isFinite(persisted)) {
      const min = minLeft ?? MINIMA[persistKey] ?? MINIMA.previewCol ?? 280;
      el.style.width = `${Math.max(min, persisted)}px`;
    }
  }, [persistKey, minLeft]);

  // restore persisted width on mount / reload
  useEffect(() => {
    const el = leftRef?.current ?? null;
    if (el) ensureRestored(el);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const flush = useCallback(() => {
    const el = activeElRef.current ?? leftRef?.current ?? null;
    if (!el) return;
    const dx = pendingDxRef.current;
    const startW = startWRef.current;
    const minL = minLeft ?? MINIMA[persistKey] ?? 280;
    const minR = minRight ?? 240;
    let nextW = startW + dx;
    nextW = Math.max(minL, nextW);
    const container = el.parentElement;
    if (container && rightRef?.current) {
      const containerW = container.clientWidth;
      const gap = 12;
      if (containerW - nextW - gap < minR) {
        nextW = containerW - minR - gap;
        nextW = Math.max(minL, nextW);
      }
    }
    el.style.width = `${nextW}px`;
    el.style.willChange = 'width';
    if (persistKey) writePersisted(persistKey, nextW);
    onResize?.(nextW - startW);
    rafRef.current = null;
  }, [leftRef, rightRef, persistKey, minLeft, minRight, onResize]);

  const onPointerDown = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    const leftEl = leftRef?.current ?? null;
    if (!leftEl) return;
    e.preventDefault();
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    activeElRef.current = leftEl;
    startXRef.current = e.clientX;
    startWRef.current = leftEl.offsetWidth;
    pendingDxRef.current = 0;
    ensureRestored(leftEl);
    leftEl.style.willChange = 'width';
    document.body.style.userSelect = 'none';
    document.body.style.cursor = orientation === 'vertical' ? 'col-resize' : 'row-resize';

    const handleMove = (ev: PointerEvent) => {
      if (ev.pointerId !== e.pointerId) return;
      pendingDxRef.current = ev.clientX - startXRef.current;
      if (rafRef.current == null) {
        rafRef.current = requestAnimationFrame(flush);
      }
    };
    const handleUp = (ev: PointerEvent) => {
      if (ev.pointerId !== e.pointerId) return;
      if (rafRef.current != null) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
      pendingDxRef.current = ev.clientX - startXRef.current;
      flush();
      try { (e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId); } catch {}
      document.body.style.userSelect = '';
      document.body.style.cursor = '';
      if (leftEl) leftEl.style.willChange = '';
      window.removeEventListener('pointermove', handleMove);
      window.removeEventListener('pointerup', handleUp);
      window.removeEventListener('pointercancel', handleUp);
      activeElRef.current = null;
    };
    window.addEventListener('pointermove', handleMove);
    window.addEventListener('pointerup', handleUp);
    window.addEventListener('pointercancel', handleUp);
  }, [leftRef, orientation, flush, ensureRestored, persistKey]);

  const onKeyDown = useCallback((e: React.KeyboardEvent<HTMLDivElement>) => {
    const leftEl = leftRef?.current ?? null;
    if (!leftEl) return;
    const step = e.shiftKey ? 32 : 16;
    let dx = 0;
    if (e.key === 'ArrowLeft') dx = -step;
    else if (e.key === 'ArrowRight') dx = step;
    else return;
    e.preventDefault();
    const minL = minLeft ?? MINIMA[persistKey] ?? 280;
    const minR = minRight ?? MINIMA[persistKey] ?? 280;
    let nextW = leftEl.offsetWidth + dx;
    nextW = Math.max(minL, nextW);
    const container = leftEl.parentElement;
    if (container && rightRef?.current) {
      const containerW = container.clientWidth;
      const gap = 12;
      if (containerW - nextW - gap < minR) nextW = containerW - minR - gap;
      nextW = Math.max(minL, nextW);
    }
    leftEl.style.width = `${nextW}px`;
    if (persistKey) writePersisted(persistKey, nextW);
    onResize?.(dx);
  }, [leftRef, rightRef, persistKey, minLeft, minRight, onResize]);

  return (
    <div
      role="separator"
      aria-orientation={orientation}
      tabIndex={0}
      onPointerDown={onPointerDown}
      onKeyDown={onKeyDown}
      data-panel-resize={persistKey}
      title="Drag to resize — arrow keys also work"
      style={{
        flexShrink: 0,
        width: orientation === 'vertical' ? 8 : '100%',
        height: orientation === 'vertical' ? 'auto' : 8,
        cursor: orientation === 'vertical' ? 'col-resize' : 'row-resize',
        alignSelf: 'stretch',
        touchAction: 'none',
        background: 'transparent',
        borderRadius: 2,
      }}
      className="panel-resizer-gutter hover:bg-white/10 focus-visible:outline focus-visible:outline-1 focus-visible:outline-white"
    />
  );
}

export { readPersisted as readLayoutPersisted, writePersisted as writeLayoutPersisted };

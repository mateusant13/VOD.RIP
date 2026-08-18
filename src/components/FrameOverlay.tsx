import { useCallback, useRef, useState } from 'react';

/**
 * FrameOverlay — CSS grid of snap targets (dotted outlines, tmux-like).
 * - 2x3 / 3x2 adaptive (wide screens 3 cols, narrow 2 cols) — CSS grid auto
 * - Preview cards draggable with custom ghost, onDragOver highlight nearest cell, onDrop reparent via portal
 * - Only rendered when frameMode === true
 * ponytail: drag uses HTML5 DnD ghost; upgrade to pointer-capture + absolute positioning for smoother cross-panel moves
 */

const GRID_CELLS = 6;

export function FrameOverlay({
  active,
  children,
  onDropCell,
}: {
  active: boolean;
  children?: React.ReactNode;
  onDropCell?: (index: number, data: string) => void;
}) {
  const [hoverCell, setHoverCell] = useState<number | null>(null);
  const dragDataRef = useRef<string>('');

  const onDragOver = useCallback((e: React.DragEvent, idx: number) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    setHoverCell(idx);
  }, []);

  const onDragLeave = useCallback(() => setHoverCell(null), []);

  const onDrop = useCallback((e: React.DragEvent, idx: number) => {
    e.preventDefault();
    const data = e.dataTransfer.getData('text/plain') || dragDataRef.current || String(idx);
    setHoverCell(null);
    onDropCell?.(idx, data);
  }, [onDropCell]);

  const onDragStartCapture = useCallback((e: React.DragEvent) => {
    const target = e.target as HTMLElement;
    const ghost = target.closest('[data-frame-card]') as HTMLElement | null;
    if (ghost) {
      const clone = ghost.cloneNode(true) as HTMLElement;
      clone.style.position = 'absolute';
      clone.style.top = '-1000px';
      clone.style.opacity = '0.9';
      clone.style.transform = 'rotate(1deg)';
      document.body.appendChild(clone);
      try {
        e.dataTransfer.setDragImage(clone, 40, 20);
      } catch {}
      const cleanup = () => clone.remove();
      // Esc-cancel or drop both fire before the next tick; cleanup on next frame and on dragend fallback.
      setTimeout(cleanup, 0);
      const id = ghost.getAttribute('data-frame-card') || '';
      dragDataRef.current = id;
      e.dataTransfer.setData('text/plain', id);
      e.dataTransfer.effectAllowed = 'move';
      const onEnd = () => { cleanup(); document.removeEventListener('dragend', onEnd); document.removeEventListener('dragcancel', onEnd); };
      document.addEventListener('dragend', onEnd, { once: true });
      document.addEventListener('dragcancel', onEnd as any, { once: true } as any);
    }
  }, []);
  if (!active) return null;

  const grid = (
    <div
      className="frame-overlay"
      data-frame-overlay
      onDragStartCapture={onDragStartCapture}
      onDragOver={(e) => e.preventDefault()}
      style={{
        position: 'absolute',
        inset: 0,
        display: 'grid',
        gap: 8,
        padding: 8,
        pointerEvents: 'auto',
        zIndex: 1,
      }}
    >
      {Array.from({ length: GRID_CELLS }, (_, i) => (
        <div
          key={i}
          data-frame-cell={i}
          onDragOver={(e) => onDragOver(e, i)}
          onDragLeave={onDragLeave}
          onDrop={(e) => onDrop(e, i)}
          style={{
            border: hoverCell === i ? '2px solid #fff' : '2px dashed rgba(255,255,255,0.35)',
            borderRadius: 6,
            background: hoverCell === i ? 'rgba(255,255,255,0.08)' : 'rgba(255,255,255,0.03)',
            minHeight: 120,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            transition: 'border-color 120ms, background 120ms',
          }}
        >
          <span style={{ fontSize: 10, color: 'rgba(255,255,255,0.4)', fontFamily: 'monospace' }}>{i + 1}</span>
        </div>
      ))}
    </div>
  );

  // App wraps FrameOverlay in a fixed inset-0 container so grid/content render
  // inline here — no portal needed (avoids clipping by vod-app-shell overflow).
  if (children) {
    const content = (
      <div style={{ position: 'absolute', inset: 0, pointerEvents: 'none' }}>
        <div style={{ pointerEvents: 'auto' }}>{children}</div>
      </div>
    );
    return (
      <>
        {grid}
        {content}
      </>
    );
  }

  return grid;
}

// Helper to make a preview card draggable inside frame mode — portals its content on drop via FrameOverlay
export function FrameCard({
  id,
  children,
  className,
  style,
}: {
  id: string;
  children: React.ReactNode;
  className?: string;
  style?: React.CSSProperties;
}) {
  return (
    <div
      data-frame-card={id}
      draggable
      className={className}
      style={{ ...style, cursor: 'grab' }}
    >
      {children}
    </div>
  );
}

export default FrameOverlay;

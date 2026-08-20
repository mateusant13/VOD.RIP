import { useCallback, useEffect, useState } from 'react';
import { FRAME_GRID_CELLS } from '../frameLayout';
import { EXPLORE_POPUP_Z } from '../layoutUtils';

/**
 * FrameOverlay — CSS grid of snap targets (dotted outlines, tmux-like).
 * - 2x3 / 3x2 adaptive (wide screens 3 cols, narrow 2 cols) — CSS grid auto
 * - Preview cards draggable with custom ghost, onDragOver highlight nearest cell, onDrop reparent via portal
 * - Only rendered when frameMode === true
 * - Grid is click-through by default; becomes interactive only during an active HTML5 drag
 *   so the underlying channel list / popups remain fully clickable in frame mode.
 * ponytail: drag uses HTML5 DnD ghost; upgrade to pointer-capture + absolute positioning for smoother cross-panel moves
 */

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
  const [dragging, setDragging] = useState(false);

  // Track global HTML5 drag lifecycle — grid only becomes interactive while a drag is active.
  const endDrag = useCallback(() => {
    setDragging(false);
    setHoverCell(null);
    delete document.body.dataset.frameDragging;
  }, []);

  useEffect(() => {
    if (!active) {
      endDrag();
      return;
    }
    // Recover from a prior crash/tab kill that left the body flag set.
    delete document.body.dataset.frameDragging;
    const onStart = () => {
      setDragging(true);
      document.body.dataset.frameDragging = '1';
    };
    const onEnd = () => endDrag();
    document.addEventListener('dragstart', onStart, true);
    document.addEventListener('dragend', onEnd, true);
    document.addEventListener('drop', onEnd, true);
    return () => {
      endDrag();
      document.removeEventListener('dragstart', onStart, true);
      document.removeEventListener('dragend', onEnd, true);
      document.removeEventListener('drop', onEnd, true);
    };
  }, [active, endDrag]);

  const onDragOver = useCallback((e: React.DragEvent, idx: number) => {
    e.preventDefault();
    if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
    setHoverCell(idx);
  }, []);

  const onDragLeave = useCallback(() => setHoverCell(null), []);

  const onDrop = useCallback((e: React.DragEvent, idx: number) => {
    e.preventDefault();
    e.stopPropagation();
    const data = e.dataTransfer.getData('text/plain') || String(idx);
    endDrag();
    onDropCell?.(idx, data);
  }, [onDropCell, endDrag]);

  if (!active) return null;

  const grid = (
    <div
      className="frame-overlay"
      data-frame-overlay
      style={{
        position: 'absolute',
        inset: 0,
        display: 'grid',
        gap: 8,
        padding: 8,
        // ponytail: upgrade to IntersectionObserver-driven visibility for smoother UX
        pointerEvents: dragging ? 'auto' : 'none',
        opacity: dragging ? 1 : 0,
        zIndex: dragging ? EXPLORE_POPUP_Z + 20 : 1,
      }}
    >
      {Array.from({ length: FRAME_GRID_CELLS }, (_, i) => (
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

/**
 * Wraps a preview card to be draggable in frame mode.
 * Sets `data-frame-card` for the overlay's ghost/drop identification.
 * Only makes the card draggable when the `draggable` prop is true (set by the
 * parent based on `frameMode`).
 */
export function FrameCard({
  id,
  draggable = false,
  children,
  className,
  style,
  onDragStart,
}: {
  id: string;
  draggable?: boolean;
  children: React.ReactNode;
  className?: string;
  style?: React.CSSProperties;
  onDragStart?: (e: React.DragEvent) => void;
}) {
  return (
    <div
      data-frame-card={id}
      draggable={draggable}
      className={className}
      style={draggable ? { ...style, cursor: 'grab' } : style}
      onDragStart={onDragStart}
    >
      {children}
    </div>
  );
}

export default FrameOverlay;

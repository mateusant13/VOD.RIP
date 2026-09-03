import { useCallback, useEffect, useRef, useState } from 'react';
import { FRAME_GRID_CELLS, encodeFrameDragPopupId, getFrameCellRect } from '../frameLayout';
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

/**
 * How long an in-flight drag may go quiet before the drag flag self-clears.
 * 600ms comfortably exceeds the browser's dragover cadence (~350ms), so a
 * live drag keeps refreshing the deadline and never gets cut off.
 */
export const FRAME_DRAG_DESPAWN_MS = 600;

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

  // Popup body/video drags are POINTER drags (startFloatingPanelDrag), which never
  // fire an HTML5 dragstart. In frame mode the explore popup dispatches
  // 'explore-frame-arm' so the grid arms for the primary gesture too; we track the
  // cursor over the 6 cells by geometry and snap on pointerup.
  const pointerArmIdRef = useRef<string | null>(null);

  // handleDropCell (App.tsx) changes identity whenever visibleChannelVideos or
  // explorePopups churn (channel refresh, popup open/close). If it were a dep of
  // the listener effect below, that churn mid-drag would re-run the effect and its
  // cleanup would disarm the grid + clear pointerArmIdRef — snapping would silently
  // never happen ("grid appears then vanishes, no snap"). Keep the latest onDropCell
  // in a ref so the listeners register once per active flip and survive churn.
  const onDropCellRef = useRef(onDropCell);
  useEffect(() => {
    onDropCellRef.current = onDropCell;
  });

  /** Which frame cell (by shared grid geometry, not hit-testing) contains (x, y). */
  const cellIndexAt = useCallback((x: number, y: number): number | null => {
    for (let i = 0; i < FRAME_GRID_CELLS; i++) {
      const r = getFrameCellRect(i);
      if (x >= r.x && x <= r.x + r.w && y >= r.y && y <= r.y + r.h) return i;
    }
    return null;
  }, []);

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
    // Watchdog: HTML5 dragend sometimes never fires when the drag source is
    // unmounted mid-drag (channel-list refresh, preview close/collapse, dedupe)
    // or dropped outside the window. Without a deadline, the body
    // `data-frame-dragging` flag sticks and frame.css freezes every floating
    // popup (`pointer-events: none`) until Frame is toggled off/on. Each
    // dragover pushes the deadline out so a genuine drag never gets cut off;
    // a stale flag self-clears ~the interval after the last activity, and
    // any pointerdown (the user grabbing something else) clears it instantly.
    let deadline: number | null = null;
    const armDespawn = () => {
      if (deadline != null) window.clearTimeout(deadline);
      deadline = window.setTimeout(() => {
        deadline = null;
        endDrag();
      }, FRAME_DRAG_DESPAWN_MS);
    };
    const disarm = () => {
      if (deadline != null) window.clearTimeout(deadline);
      deadline = null;
      endDrag();
    };
          const onStart = () => {
        setDragging(true);
        document.body.dataset.frameDragging = '1';
        armDespawn();
      };
      const onDragOverAnywhere = () => {
        if (deadline != null) armDespawn();
      };
      // Pointer-drag co-op (explore popup body/video). The arm event carries the
      // popup id; the grid stays armed until pointerup (no 600ms deadline, since a
      // held pointer-drag has no dragover cadence). Geometry-based hover/snap, so it
      // works even though the dragged popup paints above the cells.
      const onPointerArm = (ev: Event) => {
        const id = (ev as CustomEvent<{ id?: string }>).detail?.id;
        if (!id) return;
        pointerArmIdRef.current = id;
        setDragging(true);
        setHoverCell(null);
        document.body.dataset.frameDragging = '1';
      };
      const onPointerMoveAnywhere = (ev: PointerEvent) => {
        if (pointerArmIdRef.current === null) return;
        setHoverCell(cellIndexAt(ev.clientX, ev.clientY));
      };
      const onPointerRelease = (ev: PointerEvent) => {
        const id = pointerArmIdRef.current;
        if (id === null) return;
        pointerArmIdRef.current = null;
        const idx = cellIndexAt(ev.clientX, ev.clientY);
        endDrag();
        if (idx != null) onDropCellRef.current?.(idx, encodeFrameDragPopupId(id));
      };
      document.addEventListener('dragstart', onStart, true);
      document.addEventListener('dragover', onDragOverAnywhere, true);
      document.addEventListener('dragend', disarm, true);
      document.addEventListener('drop', disarm, true);
      document.addEventListener('pointerdown', disarm, true);
      document.addEventListener('explore-frame-arm', onPointerArm);
      document.addEventListener('pointermove', onPointerMoveAnywhere, true);
      document.addEventListener('pointerup', onPointerRelease, true);
      document.addEventListener('pointercancel', onPointerRelease, true);
      return () => {
        disarm();
        pointerArmIdRef.current = null;
        document.removeEventListener('dragstart', onStart, true);
        document.removeEventListener('dragover', onDragOverAnywhere, true);
        document.removeEventListener('dragend', disarm, true);
        document.removeEventListener('drop', disarm, true);
        document.removeEventListener('pointerdown', disarm, true);
        document.removeEventListener('explore-frame-arm', onPointerArm);
        document.removeEventListener('pointermove', onPointerMoveAnywhere, true);
        document.removeEventListener('pointerup', onPointerRelease, true);
        document.removeEventListener('pointercancel', onPointerRelease, true);
      };
    }, [active, endDrag, cellIndexAt]);

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
        // Tiling guide appears only mid-drag (mirrors tmux tiling: zones
        // reveal at drag time, not on toggle) and is click-through until a
        // drag starts so the base channel cards stay grabbable underneath.
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

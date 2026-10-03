import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  FRAME_GRID_CELLS,
  FRAME_WINDOW_ATTR,
  encodeFrameDragPopupId,
  frameCellIndexAt,
  getFrameCellRect,
} from '../frameLayout';
import { EXPLORE_POPUP_Z } from '../layoutUtils';

/**
 * FrameOverlay — the 6-cell snap grid (dotted outlines, tmux-like).
 * - 2x3 / 3x2 adaptive (wide screens 3 cols, narrow 2 cols)
 * - Any floating window whose root carries `data-frame-window="<key>"` snaps
 *   into the grid: the arm is read off the DOM at pointerdown, so a window
 *   CANNOT "forget to arm" — carrying the attribute is the whole contract.
 * - Cells are absolutely positioned from the same getFrameCellRect() math the
 *   hit-test uses, so the pixels and the snap math can never disagree.
 * - Only rendered when frameMode === true
 * - The guide is visible (faint) whenever frame mode is on so the user can
 *   see where to aim, and brightens during a drag. It stays click-through
 *   unless a drag is in flight.
 */

/**
 * How long an in-flight drag may go quiet before the drag flag self-clears.
 * 600ms comfortably exceeds the browser's dragover cadence (~350ms), so a
 * live drag keeps refreshing the deadline and never gets cut off.
 */
export const FRAME_DRAG_DESPAWN_MS = 600;

/**
 * Idle guide opacity. The grid used to be `opacity: 0` until a drag started,
 * so a user in frame mode had no visible target to aim at and the feature
 * read as broken. Faint-but-present fixes that without competing with the
 * video underneath.
 */
export const FRAME_IDLE_OVERLAY_OPACITY = 0.3;

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
  // Bumped on resize so the absolutely-positioned cells re-read the viewport.
  const [viewportTick, setViewportTick] = useState(0);

  // Popup body/video drags are POINTER drags (startFloatingPanelDrag), which
  // never fire an HTML5 dragstart. The armed window key is read straight off
  // the pointerdown target, so every window family snaps through one path.
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

  useEffect(() => {
    if (!active) return;
    let raf = 0;
    const onResize = () => {
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = 0;
        setViewportTick((t) => t + 1);
      });
    };
    window.addEventListener('resize', onResize);
    return () => {
      window.removeEventListener('resize', onResize);
      if (raf) cancelAnimationFrame(raf);
    };
  }, [active]);

  /** Cell rects rendered as pixels — the same source the hit-test reads. */
  const cellRects = useMemo(() => {
    void viewportTick; // re-read window.innerWidth/innerHeight after a resize
    return Array.from({ length: FRAME_GRID_CELLS }, (_, i) => getFrameCellRect(i));
  }, [viewportTick]);

  /**
   * Which cell (x, y) snaps to: exact hit, else the NEAREST cell within the
   * snap threshold. Reads live viewport dims, so it can never drift from the
   * rects the user is aiming at.
   */
  const cellIndexAt = useCallback(
    (x: number, y: number): number | null => frameCellIndexAt(x, y),
    [],
  );

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
      // Pointer-drag co-op (any floating window body/video/header). The armed
      // key comes from the `data-frame-window` attribute on the pointerdown
      // target, so a window cannot fail to snap by forgetting to arm: this
      // capture-phase listener runs BEFORE the popup's own React handler and
      // before any dragstart, which is what makes the ordering deterministic.
      // Pointer-drags have no dragover cadence, so the grid stays armed with
      // no despawn deadline until pointerup. Hover/snap are geometric, so it
      // works even though the dragged window paints above the cells.
      const onPointerDownCapture = (ev: PointerEvent) => {
        const target = ev.target as (Element & { closest?: (s: string) => Element | null }) | null;
        const host = target?.closest?.(`[${FRAME_WINDOW_ATTR}]`) as HTMLElement | null;
        const key = host?.getAttribute(FRAME_WINDOW_ATTR);
        if (!key) {
          disarm();
          return;
        }
        if (deadline != null) window.clearTimeout(deadline);
        deadline = null;
        pointerArmIdRef.current = key;
        setDragging(true);
        setHoverCell(null);
        document.body.dataset.frameDragging = '1';
      };
      // Backwards-compatible fallback for windows that still dispatch the
      // bespoke event instead of (or before) carrying the attribute.
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
      document.addEventListener('pointerdown', onPointerDownCapture, true);
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
        document.removeEventListener('pointerdown', onPointerDownCapture, true);
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
        // The guide is ALWAYS visible in frame mode (faint) so there is a
        // target to aim at before any drag starts, and clicks through to the
        // app underneath until a drag is actually in flight.
        pointerEvents: dragging ? 'auto' : 'none',
        opacity: dragging ? 1 : FRAME_IDLE_OVERLAY_OPACITY,
        zIndex: dragging ? EXPLORE_POPUP_Z + 20 : 1,
      }}
    >
      {cellRects.map((r, i) => (
        <div
          key={i}
          data-frame-cell={i}
          onDragOver={(e) => onDragOver(e, i)}
          onDragLeave={onDragLeave}
          onDrop={(e) => onDrop(e, i)}
          style={{
            // Positioned from the SAME getFrameCellRect() the hit-test uses —
            // no CSS grid, no minHeight, so pixels == snap math by
            // construction. boxSizing keeps the 2px border inside the rect.
            position: 'absolute',
            left: r.x,
            top: r.y,
            width: r.w,
            height: r.h,
            boxSizing: 'border-box',
            border: hoverCell === i ? '2px solid #fff' : '2px dashed rgba(255,255,255,0.35)',
            borderRadius: 6,
            background: hoverCell === i ? 'rgba(255,255,255,0.08)' : 'rgba(255,255,255,0.03)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            // Transitions off while dragging (no hover lag mid-gesture).
            transition: dragging ? 'none' : 'border-color 120ms, background 120ms',
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

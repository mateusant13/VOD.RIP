import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { act, fireEvent, render } from '@testing-library/react';
import FrameOverlay, { FRAME_DRAG_DESPAWN_MS, FRAME_IDLE_OVERLAY_OPACITY } from './FrameOverlay';
import { getFrameCellRect } from '../frameLayout';

describe('FrameOverlay', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    delete document.body.dataset.frameDragging;
  });

  afterEach(() => {
    vi.useRealTimers();
    delete document.body.dataset.frameDragging;
  });

  it('is click-through but VISIBLE while frame mode is idle (no active drag)', () => {
    const { container } = render(<FrameOverlay active onDropCell={vi.fn()} />);
    const overlay = container.querySelector('[data-frame-overlay]') as HTMLElement;
    expect(overlay).not.toBeNull();
    expect(overlay.style.pointerEvents).toBe('none');
    // Idle guide stays faintly visible so the user can see the snap target
    // before any drag starts (it used to be opacity 0 and read as broken).
    expect(overlay.style.opacity).toBe(String(FRAME_IDLE_OVERLAY_OPACITY));
    expect(document.body.dataset.frameDragging).toBeUndefined();
  });

  it('becomes interactive and visible during an HTML5 drag', () => {
    const { container } = render(<FrameOverlay active onDropCell={vi.fn()} />);
    const overlay = container.querySelector('[data-frame-overlay]') as HTMLElement;

    fireEvent.dragStart(document, { bubbles: true, cancelable: true });

    expect(overlay.style.pointerEvents).toBe('auto');
    expect(overlay.style.opacity).toBe('1');
    expect(document.body.dataset.frameDragging).toBe('1');
  });

  it('clears drag state and hover highlight on dragend', () => {
    const { container } = render(<FrameOverlay active onDropCell={vi.fn()} />);
    const overlay = container.querySelector('[data-frame-overlay]') as HTMLElement;
    const cell = container.querySelector('[data-frame-cell="2"]') as HTMLElement;

    fireEvent.dragStart(document, { bubbles: true, cancelable: true });
    fireEvent.dragOver(cell, { bubbles: true, cancelable: true, dataTransfer: { dropEffect: '' } });
    fireEvent.dragEnd(document, { bubbles: true, cancelable: true });

    expect(overlay.style.pointerEvents).toBe('none');
    expect(overlay.style.opacity).toBe(String(FRAME_IDLE_OVERLAY_OPACITY));
    expect(document.body.dataset.frameDragging).toBeUndefined();
    expect(cell.style.border).toContain('dashed');
  });
  it('keeps the drag alive while dragover keeps arriving (no premature clear)', () => {
    const { container } = render(<FrameOverlay active onDropCell={vi.fn()} />);
    const overlay = container.querySelector('[data-frame-overlay]') as HTMLElement;

    fireEvent.dragStart(document, { bubbles: true, cancelable: true });
    for (let i = 0; i < 3; i++) {
      vi.advanceTimersByTime(FRAME_DRAG_DESPAWN_MS - 100);
      fireEvent.dragOver(document, { bubbles: true, cancelable: true });
    }
    expect(overlay.style.pointerEvents).toBe('auto');
    expect(document.body.dataset.frameDragging).toBe('1');
  });

  it('self-clears a stale drag flag when dragend never fires', () => {
    const { container } = render(<FrameOverlay active onDropCell={vi.fn()} />);
    const overlay = container.querySelector('[data-frame-overlay]') as HTMLElement;

    fireEvent.dragStart(document, { bubbles: true, cancelable: true });
    act(() => {
      vi.advanceTimersByTime(FRAME_DRAG_DESPAWN_MS + 50);
    });

    expect(document.body.dataset.frameDragging).toBeUndefined();
    expect(overlay.style.opacity).toBe(String(FRAME_IDLE_OVERLAY_OPACITY));
    expect(overlay.style.pointerEvents).toBe('none');
  });

  it('clears the drag flag immediately on pointerdown', () => {
    const { container } = render(<FrameOverlay active onDropCell={vi.fn()} />);
    const overlay = container.querySelector('[data-frame-overlay]') as HTMLElement;

    fireEvent.dragStart(document, { bubbles: true, cancelable: true });
    fireEvent.pointerDown(document, { bubbles: true });

    expect(document.body.dataset.frameDragging).toBeUndefined();
    expect(overlay.style.pointerEvents).toBe('none');
  });

  it('invokes onDropCell and resets drag state on drop', () => {
    const onDropCell = vi.fn();
    const { container } = render(<FrameOverlay active onDropCell={onDropCell} />);
    const overlay = container.querySelector('[data-frame-overlay]') as HTMLElement;
    const cell = container.querySelector('[data-frame-cell="1"]') as HTMLElement;

    fireEvent.dragStart(document, { bubbles: true, cancelable: true });
    fireEvent.drop(cell, {
      bubbles: true,
      cancelable: true,
      dataTransfer: { getData: () => 'vodrip-frame:popup-1' },
    });

    expect(onDropCell).toHaveBeenCalledWith(1, 'vodrip-frame:popup-1');
    expect(overlay.style.pointerEvents).toBe('none');
    expect(document.body.dataset.frameDragging).toBeUndefined();
  });

  it('arms on explore-frame-arm and snaps on pointerup over a cell (body/video drag)', () => {
    window.innerWidth = 1280; // 3 columns (frameGridColumns > 1100)
    window.innerHeight = 720;
    const onDropCell = vi.fn();
    const { container } = render(<FrameOverlay active onDropCell={onDropCell} />);
    const overlay = container.querySelector('[data-frame-overlay]') as HTMLElement;

    // Explore popup dispatches this when the user grabs the popup body/video.
    fireEvent(document, new CustomEvent('explore-frame-arm', { detail: { id: 'my-popup' } }));

    expect(overlay.style.pointerEvents).toBe('auto');
    expect(overlay.style.opacity).toBe('1');
    expect(document.body.dataset.frameDragging).toBe('1');

    // Hover into cell 2 (col 2, row 0): x in [856, 1272], y in [8, 356].
    fireEvent.pointerMove(document, { bubbles: true, clientX: 900, clientY: 100 });
    const cell2 = container.querySelector('[data-frame-cell="2"]') as HTMLElement;
    expect(cell2.style.border).toContain('2px solid');

    // Release over the same cell snaps it.
    fireEvent.pointerUp(document, { bubbles: true, clientX: 900, clientY: 100 });

    expect(onDropCell).toHaveBeenCalledWith(2, 'vodrip-frame:my-popup');
    expect(overlay.style.pointerEvents).toBe('none');
    expect(document.body.dataset.frameDragging).toBeUndefined();
  });

  it('does not snap when a pointer-drag releases outside every cell', () => {
    window.innerWidth = 1280;
    window.innerHeight = 720;
    const onDropCell = vi.fn();
    render(<FrameOverlay active onDropCell={onDropCell} />);

    fireEvent(document, new CustomEvent('explore-frame-arm', { detail: { id: 'pop' } }));
    // Release far outside the 3-col/2-row grid (e.g. overflowing x=5000).
    fireEvent.pointerUp(document, { bubbles: true, clientX: 5000, clientY: 8000 });

    expect(onDropCell).not.toHaveBeenCalled();
    expect(document.body.dataset.frameDragging).toBeUndefined();
  });

  it('keeps an armed pointer-drag across an onDropCell identity change (mid-drag churn)', () => {
    window.innerWidth = 1280;
    window.innerHeight = 720;
    const first = vi.fn();
    const second = vi.fn();
    const { container, rerender } = render(<FrameOverlay active onDropCell={first} />);
    const overlay = container.querySelector('[data-frame-overlay]') as HTMLElement;

    fireEvent(document, new CustomEvent('explore-frame-arm', { detail: { id: 'p1' } }));
    expect(overlay.style.pointerEvents).toBe('auto');

    // Simulate App churn (channel refresh / popup open-close) swapping the handler
    // mid-drag. The listeners must survive — no disarm, no pointerArmIdRef clear.
    rerender(<FrameOverlay active onDropCell={second} />);
    expect(overlay.style.pointerEvents).toBe('auto');
    expect(document.body.dataset.frameDragging).toBe('1');

    fireEvent.pointerMove(document, { bubbles: true, clientX: 900, clientY: 100 });
    fireEvent.pointerUp(document, { bubbles: true, clientX: 900, clientY: 100 });

    // The snap resolves against the LATEST handler.
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledWith(2, 'vodrip-frame:p1');
    expect(overlay.style.pointerEvents).toBe('none');
    expect(document.body.dataset.frameDragging).toBeUndefined();
  });

  it('recovers from a stale body frameDragging flag on mount', () => {
    document.body.dataset.frameDragging = '1';
    render(<FrameOverlay active onDropCell={vi.fn()} />);
    expect(document.body.dataset.frameDragging).toBeUndefined();
  });

  /** A floating window root: the ONLY thing snap integration needs of it. */
  function mountFrameWindow(key: string): HTMLElement {
    const el = document.createElement('div');
    el.setAttribute('data-frame-window', key);
    document.body.appendChild(el);
    return el;
  }

  it('arms from the data-frame-window attribute alone (no bespoke arm event)', () => {
    window.innerWidth = 1280; // 3 columns
    window.innerHeight = 720;
    const onDropCell = vi.fn();
    const { container } = render(<FrameOverlay active onDropCell={onDropCell} />);
    const overlay = container.querySelector('[data-frame-overlay]') as HTMLElement;
    // A live player: never dispatched 'explore-frame-arm', and before this
    // change it was structurally unable to snap at all.
    const win = mountFrameWindow('live:3');

    fireEvent.pointerDown(win, { bubbles: true, cancelable: true });

    expect(overlay.style.pointerEvents).toBe('auto');
    expect(document.body.dataset.frameDragging).toBe('1');

    // Cell 2 spans x 856..1272, y 8..356 at this viewport.
    fireEvent.pointerMove(document, { bubbles: true, clientX: 900, clientY: 100 });
    fireEvent.pointerUp(document, { bubbles: true, clientX: 900, clientY: 100 });

    expect(onDropCell).toHaveBeenCalledWith(2, 'vodrip-frame:live:3');
    expect(overlay.style.pointerEvents).toBe('none');
    win.remove();
  });

  it('does not arm for a pointerdown outside any frame window', () => {
    window.innerWidth = 1280;
    window.innerHeight = 720;
    const onDropCell = vi.fn();
    const { container } = render(<FrameOverlay active onDropCell={onDropCell} />);
    const overlay = container.querySelector('[data-frame-overlay]') as HTMLElement;
    const outside = document.createElement('button');
    document.body.appendChild(outside);

    fireEvent.pointerDown(outside, { bubbles: true, cancelable: true });
    fireEvent.pointerMove(document, { bubbles: true, clientX: 900, clientY: 100 });
    fireEvent.pointerUp(document, { bubbles: true, clientX: 900, clientY: 100 });

    expect(onDropCell).not.toHaveBeenCalled();
    expect(overlay.style.pointerEvents).toBe('none');
    expect(document.body.dataset.frameDragging).toBeUndefined();
    outside.remove();
  });

  it('snaps to the NEAREST cell when released in a gap between cells', () => {
    window.innerWidth = 1280; // 3 cols x 2 rows
    window.innerHeight = 720;
    const onDropCell = vi.fn();
    render(<FrameOverlay active onDropCell={onDropCell} />);
    const win = mountFrameWindow('explore:e1');

    fireEvent.pointerDown(win, { bubbles: true, cancelable: true });
    // The 8px row gap: cell 0 spans y 8..356 and cell 3 spans y 364..712, so
    // y=360 hits NO cell. A strict rect test returned null here and the drop
    // did nothing at all ("I dropped it and nothing happened").
    fireEvent.pointerUp(document, { bubbles: true, clientX: 100, clientY: 360 });

    expect(onDropCell).toHaveBeenCalledWith(0, 'vodrip-frame:explore:e1');
    win.remove();
  });

  it('snaps within FRAME_SNAP_THRESHOLD_PX of the grid but not far outside it', () => {
    window.innerWidth = 1280;
    window.innerHeight = 720;
    const onDropCell = vi.fn();
    render(<FrameOverlay active onDropCell={onDropCell} />);
    const win = mountFrameWindow('live:1');

    // 4px above the grid (cell 0 starts at y=8) — inside the threshold.
    fireEvent.pointerDown(win, { bubbles: true, cancelable: true });
    fireEvent.pointerUp(document, { bubbles: true, clientX: 100, clientY: 4 });
    expect(onDropCell).toHaveBeenCalledWith(0, 'vodrip-frame:live:1');

    // 108px above the grid — beyond the threshold, so genuinely no snap.
    fireEvent.pointerDown(win, { bubbles: true, cancelable: true });
    fireEvent.pointerUp(document, { bubbles: true, clientX: 100, clientY: -100 });
    expect(onDropCell).toHaveBeenCalledTimes(1);
    win.remove();
  });

  it('positions each cell from the shared geometry (pixels match the math)', () => {
    window.innerWidth = 1280;
    window.innerHeight = 720;
    const { container } = render(<FrameOverlay active onDropCell={vi.fn()} />);
    const cell = container.querySelector('[data-frame-cell="2"]') as HTMLElement;

    // Same source the hit-test reads, so the rendered rect can never drift
    // from the snap math (the old CSS grid + min-height:120 could).
    expect(cell.style.left).toBe(`${getFrameCellRect(2, 1280, 720).x}px`);
    expect(cell.style.top).toBe(`${getFrameCellRect(2, 1280, 720).y}px`);
    expect(cell.style.width).toBe(`${getFrameCellRect(2, 1280, 720).w}px`);
    expect(cell.style.height).toBe(`${getFrameCellRect(2, 1280, 720).h}px`);
    expect(cell.style.position).toBe('absolute');
  });
});
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { act, fireEvent, render } from '@testing-library/react';
import FrameOverlay, { FRAME_DRAG_DESPAWN_MS } from './FrameOverlay';

describe('FrameOverlay', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    delete document.body.dataset.frameDragging;
  });

  afterEach(() => {
    vi.useRealTimers();
    delete document.body.dataset.frameDragging;
  });

  it('is click-through and invisible while frame mode is idle (no active drag)', () => {
    const { container } = render(<FrameOverlay active onDropCell={vi.fn()} />);
    const overlay = container.querySelector('[data-frame-overlay]') as HTMLElement;
    expect(overlay).not.toBeNull();
    expect(overlay.style.pointerEvents).toBe('none');
    expect(overlay.style.opacity).toBe('0');
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
    expect(overlay.style.opacity).toBe('0');
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
    expect(overlay.style.opacity).toBe('0');
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
});
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import type { PointerEvent as ReactPointerEvent } from 'react';
import {
  makeRafMoveLoop,
  startFloatingPanelDrag,
  startExplorePanelWidthResize,
  suspendPanelTransitions,
  restorePanelTransitions,
} from './explorePopupUtils';
import { startPanelResizeDrag } from './layoutUtils';

// The drag helpers attach raw pointermove/pointerup listeners and use
// setPointerCapture — jsdom implements both, but shim defensively so this
// suite also runs on older jsdom versions.
const captureProto = HTMLElement.prototype as unknown as {
  setPointerCapture?: () => void;
  releasePointerCapture?: () => void;
};
beforeEach(() => {
  if (!captureProto.setPointerCapture) {
    captureProto.setPointerCapture = () => {};
  }
  if (!captureProto.releasePointerCapture) {
    captureProto.releasePointerCapture = () => {};
  }
  document.body.innerHTML = '';
});

afterEach(() => {
  document.body.innerHTML = '';
});

/** jsdom PointerEvent → the drag helpers' React pointer-event shape. The
 *  helpers only read preventDefault/stopPropagation/currentTarget/pointerId/
 *  clientX/clientY — all present on the DOM event — so the cast is structural. */
function asDragEvent<T extends HTMLElement>(e: Event): ReactPointerEvent<T> {
  return e as unknown as ReactPointerEvent<T>;
}

/** Fake DOM pointer event; falls back to MouseEvent when jsdom lacks PointerEvent. */
function makePointer(type: string, x: number, y: number, pointerId = 1): Event {
  const hasPointerEvent = typeof PointerEvent !== 'undefined';
  const Ctor = hasPointerEvent ? PointerEvent : MouseEvent;
  const ev = new Ctor(type, { clientX: x, clientY: y, bubbles: true, cancelable: true });
  if (!hasPointerEvent) {
    Object.defineProperty(ev, 'pointerId', { value: pointerId });
  }
  return ev;
}

const nextFrame = () =>
  new Promise<void>((resolve) => {
    requestAnimationFrame(() => resolve());
  });

/** Appends a handle + panel and returns a function that starts a drag on the handle. */
function mountDragSurface() {
  const handle = document.createElement('div');
  const panel = document.createElement('div');
  document.body.appendChild(handle);
  document.body.appendChild(panel);
  panel.style.width = '300px';
  panel.style.height = '200px';
  return { handle, panel };
}

describe('makeRafMoveLoop', () => {
  it('coalesces many moves into one apply per frame, applying the latest coordinates', async () => {
    const apply = vi.fn();
    const loop = makeRafMoveLoop(apply);
    loop.onMove(10, 20);
    loop.onMove(30, 40);
    loop.onMove(50, 60);
    expect(apply).not.toHaveBeenCalled();
    await nextFrame();
    expect(apply).toHaveBeenCalledTimes(1);
    expect(apply).toHaveBeenCalledWith(50, 60);
    // Nothing pending → no further applies.
    await nextFrame();
    expect(apply).toHaveBeenCalledTimes(1);
  });

  it('flushSync applies the pending move immediately and cancels the scheduled frame', async () => {
    const apply = vi.fn();
    const loop = makeRafMoveLoop(apply);
    loop.onMove(10, 20);
    loop.onMove(70, 80);
    loop.flushSync();
    expect(apply).toHaveBeenCalledTimes(1);
    expect(apply).toHaveBeenCalledWith(70, 80);
    await nextFrame();
    expect(apply).toHaveBeenCalledTimes(1);
  });
});

describe('startPanelResizeDrag', () => {
  it('writes the DOM once per frame, not once per pointermove; commits once on pointerup', async () => {
    const { handle, panel } = mountDragSurface();
    const sizeRef = { current: { w: 300, h: 200 } };
    const setSize = vi.fn();
    const onResizeMove = vi.fn();
    const onResizeEnd = vi.fn();

    handle.addEventListener('pointerdown', (e) => {
      startPanelResizeDrag(asDragEvent<HTMLDivElement>(e), 'e', sizeRef, setSize, {
        panelEl: panel,
        maxW: 1000,
        maxH: 800,
        onResizeMove,
        onResizeEnd,
      });
    });
    handle.dispatchEvent(makePointer('pointerdown', 300, 200));

    // 30 moves in the same frame — a 240Hz-class burst.
    for (let i = 0; i < 30; i++) {
      handle.dispatchEvent(makePointer('pointermove', 300 + i * 10, 200, 1));
    }
    expect(onResizeMove).not.toHaveBeenCalled();
    expect(panel.style.transition).toBe('none'); // transitions killed for the drag

    await nextFrame();
    expect(onResizeMove).toHaveBeenCalledTimes(1);
    expect(sizeRef.current).toEqual({ w: 590, h: 200 });
    expect(panel.style.width).toBe('590px');

    await nextFrame();
    expect(onResizeMove).toHaveBeenCalledTimes(1); // nothing pending

    handle.dispatchEvent(makePointer('pointerup', 590, 200, 1));
    expect(setSize).toHaveBeenCalledTimes(1);
    expect(setSize.mock.calls[0][0]).toEqual({ w: 590, h: 200 });
    expect(onResizeEnd).toHaveBeenCalledTimes(1);
    expect(panel.style.transition).toBe(''); // restored
    expect(panel.style.willChange).toBe('');
  });

  it('clamps the drag inside max bounds on the final commit', async () => {
    const { handle, panel } = mountDragSurface();
    const sizeRef = { current: { w: 300, h: 200 } };
    const setSize = vi.fn();

    handle.addEventListener('pointerdown', (e) => {
      startPanelResizeDrag(asDragEvent<HTMLDivElement>(e), 'e', sizeRef, setSize, {
        panelEl: panel,
        maxW: 320,
        maxH: 800,
      });
    });
    handle.dispatchEvent(makePointer('pointerdown', 300, 200));
    handle.dispatchEvent(makePointer('pointermove', 900, 200, 1));
    await nextFrame();
    expect(panel.style.width).toBe('320px');
    handle.dispatchEvent(makePointer('pointerup', 900, 200, 1));
    expect(setSize.mock.calls[0][0]).toEqual({ w: 320, h: 200 });
  });
});

describe('startExplorePanelWidthResize', () => {
  it('coalesces moves, keeps the panel inside the viewport, commits once on pointerup', async () => {
    const { handle, panel } = mountDragSurface();
    const widthRef = { current: 400 };
    const setWidth = vi.fn();
    const onResizeMove = vi.fn();

    handle.addEventListener('pointerdown', (e) => {
      startExplorePanelWidthResize(asDragEvent<HTMLDivElement>(e), 'e', widthRef, setWidth, {
        panelEl: panel,
        aspect: 16 / 9,
        clampWidth: (w) => Math.max(200, Math.min(1000, w)),
        posRef: { current: { x: 100, y: 100 } },
        setPos: vi.fn(),
        onResizeMove,
      });
    });
    handle.dispatchEvent(makePointer('pointerdown', 100, 100));
    for (let i = 0; i < 10; i++) {
      handle.dispatchEvent(makePointer('pointermove', 100 + i * 8, 100, 1));
    }
    await nextFrame();
    expect(onResizeMove).toHaveBeenCalledTimes(1);
    expect(widthRef.current).toBe(472);
    expect(panel.style.width).toBe('472px');

    handle.dispatchEvent(makePointer('pointerup', 172, 100, 1));
    expect(setWidth).toHaveBeenCalledTimes(1);
    expect(setWidth.mock.calls[0][0]).toBe(472);
  });
});

describe('startFloatingPanelDrag', () => {
  it('coalesces moves and commits the final clamped position on pointerup', async () => {
    const { handle, panel } = mountDragSurface();
    const posRef = { current: { x: 100, y: 100 } };
    const setPos = vi.fn();

    handle.addEventListener('pointerdown', (e) => {
      startFloatingPanelDrag(asDragEvent<HTMLElement>(e), posRef, setPos, panel);
    });
    handle.dispatchEvent(makePointer('pointerdown', 50, 50));
    for (let i = 0; i < 20; i++) {
      handle.dispatchEvent(makePointer('pointermove', 50 + i * 5, 50 + i * 5, 1));
    }
    await nextFrame();
    expect(posRef.current).toEqual({ x: 195, y: 195 });
    expect(panel.style.left).toBe('195px');
    expect(panel.style.top).toBe('195px');

    handle.dispatchEvent(makePointer('pointerup', 195, 195, 1));
    expect(setPos).toHaveBeenCalledTimes(1);
    expect(setPos.mock.calls[0][0]).toEqual({ x: 195, y: 195 });
    expect(panel.style.willChange).toBe('');
  });
});

describe('suspendPanelTransitions', () => {
  it('kills and restores the transition on the dragged element', () => {
    const el = document.createElement('div');
    el.style.transition = 'width 0.3s ease';
    const prev = suspendPanelTransitions(el);
    expect(el.style.transition).toBe('none');
    expect(prev).toBe('width 0.3s ease');
    restorePanelTransitions(el, prev);
    expect(el.style.transition).toBe('width 0.3s ease');
  });
});

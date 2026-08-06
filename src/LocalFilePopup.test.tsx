import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import LocalFilePopup, { type LocalFilePopupItem } from './LocalFilePopup';

const ITEM: LocalFilePopupItem = {
  id: 'l1',
  filePath: 'C:\\VODs\\clip.mp4',
  title: 'Clip A',
  platform: 'twitch',
};

function renderPopup() {
  const onClose = vi.fn();
  const view = render(
    <LocalFilePopup
      item={ITEM}
      zIndex={10}
      stackIndex={0}
      onClose={onClose}
      onBringToFront={vi.fn()}
      onOpenHit={vi.fn()}
      savedChannels={[]}
    />,
  );
  return { ...view, onClose };
}

// The resize/drag helpers attach raw pointermove/pointerup listeners and use
// setPointerCapture — shim defensively (same as panelDrag.test.ts).
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
});

/** Fake DOM pointer event; falls back to MouseEvent when jsdom lacks PointerEvent. */
function makePointer(type: string, x: number, y: number): Event {
  const hasPointerEvent = typeof PointerEvent !== 'undefined';
  const Ctor = hasPointerEvent ? PointerEvent : MouseEvent;
  return new Ctor(type, { clientX: x, clientY: y, bubbles: true, cancelable: true });
}

const nextFrame = () =>
  new Promise<void>((resolve) => {
    requestAnimationFrame(() => resolve());
  });

/** Panel root rendered by LocalFilePopup (the only `.fixed` element in the test). */
function popupPanel(container: HTMLElement): HTMLElement {
  const panel = container.querySelector('div.fixed');
  if (!panel) throw new Error('popup panel not found');
  return panel as HTMLElement;
}

describe('LocalFilePopup', () => {
  it('renders exactly 8 resize handles ([data-panel-resize])', () => {
    renderPopup();
    expect(document.querySelectorAll('[data-panel-resize]')).toHaveLength(8);
  });

  it('renders the local video element and the close button fires onClose', () => {
    const { container, onClose } = renderPopup();
    expect(container.querySelector('video')).not.toBeNull();
    fireEvent.click(screen.getByTitle('Close'));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('west-edge resize moves the panel so the opposite (east) edge stays fixed', async () => {
    const { container } = renderPopup();
    const panel = popupPanel(container);

    // jsdom has no layout: offsetWidth/offsetHeight are always 0, which the
    // drag clamp feeds on. Advertise the panel's real size so the drag lands
    // on a deterministic mid-screen position instead of the mount's
    // bottom-right corner (where any west growth would hit the on-screen
    // clamp and mask the edge-fixed assertion).
    Object.defineProperty(panel, 'offsetWidth', { configurable: true, value: 288 });
    Object.defineProperty(panel, 'offsetHeight', { configurable: true, value: 202 });

    // Drag the header to a known position: (978, 722) + (200, 100) → clamped
    // to 1024-46-288=690 / 768-46-202=520.
    const header = panel.querySelector('.cursor-grab') as HTMLElement;
    await act(async () => {
      header.dispatchEvent(makePointer('pointerdown', 500, 500));
      header.dispatchEvent(makePointer('pointermove', 700, 600));
      await nextFrame();
      header.dispatchEvent(makePointer('pointerup', 700, 600));
    });
    expect(panel.style.left).toBe('690px');
    expect(panel.style.top).toBe('520px');

    // West handle = 4th of the 8 [data-panel-resize] strips (n, s, e, w, ...).
    const west = panel.querySelectorAll('[data-panel-resize]')[3] as HTMLElement;
    const startW = Number(panel.style.width);

    await act(async () => {
      west.dispatchEvent(makePointer('pointerdown', 300, 300));
      west.dispatchEvent(makePointer('pointermove', 250, 300)); // dx = -50
      await nextFrame();
      west.dispatchEvent(makePointer('pointerup', 250, 300));
    });

    expect(Number(panel.style.width)).toBe(startW + 50);
    // Panel slid west by the same 50px — the east edge (left + width) is fixed.
    expect(panel.style.left).toBe('640px'); // 690 + (288 - 338)
    expect(panel.style.top).toBe('520px'); // vertical edge untouched
  });
});

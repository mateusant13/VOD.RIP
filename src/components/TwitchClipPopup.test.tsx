import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import type * as PreviewPlayerUtils from '../previewPlayerUtils';
import TwitchClipPopup, { CLIP_PANEL_MIN_H, CLIP_PANEL_MIN_W } from './TwitchClipPopup';

// The popup POSTs a preview session on mount — fail it fast so the component
// settles in its error state (no HLS construction, no retry timers). The
// error message must match previewPlayerUtils' fatal regex ('private').
vi.mock('../previewPlayerUtils', async (importOriginal) => {
  const actual = await importOriginal<typeof PreviewPlayerUtils>();
  return {
    ...actual,
    createPreviewSessionWithRetry: vi.fn().mockRejectedValue(new Error('private video')),
    playPreviewWithAudio: vi.fn().mockResolvedValue(undefined),
  };
});

// The resize helpers attach raw pointermove/pointerup listeners and use
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

function renderPopup() {
  return render(
    <TwitchClipPopup
      url="https://www.twitch.tv/videos/123456789"
      broadcasterLogin="somebody"
      vodId="123456789"
      playheadSec={120}
      vodTitle="jantando o guiven parte 1"
      vodDurationSec={600}
      zIndex={10}
      onClose={vi.fn()}
    />,
  );
}

/** The popup renders through createPortal(document.body) — query the body. */
function popupPanel(): HTMLElement {
  const panel = document.body.querySelector('[data-twitch-clip-popup]');
  if (!panel) throw new Error('clip popup panel not found');
  return panel as HTMLElement;
}

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

// The widest button row (Clip trim row) — mirror of the derivation comment in
// TwitchClipPopup.tsx. jsdom has no layout, so this asserts the CSS CONTRACT:
// the min size must be >= what the row needs (label 36 + 2 gaps 16 + duration
// buttons ~100 + length readout 44 + a usable rail ~100 + px-2 padding 16).
const WIDEST_ROW_REQUIREMENT = 36 + 8 + 100 + 8 + 44 + 100 + 16;
// Min height needs header (~40) + video at min width (320 * 9/16 = 180) +
// the trim section (~129) to fit uncut.
const CONTENT_HEIGHT_REQUIREMENT = 40 + 180 + 129;

describe('TwitchClipPopup create button', () => {
  it('renders the Create clip button icon-only with a11y label and title', () => {
    renderPopup();
    const btn = screen.getByRole('button', { name: 'Create clip' });
    expect(btn.querySelector('svg')).toBeTruthy();
    expect(btn.textContent?.trim()).toBe('');
    expect(btn).toHaveAttribute('aria-label', 'Create clip');
    expect(btn).toHaveAttribute('title');
  });
});

describe('TwitchClipPopup min-size contract', () => {
  it('min width guarantees the widest button row fits uncut', () => {
    expect(CLIP_PANEL_MIN_W).toBeGreaterThanOrEqual(WIDEST_ROW_REQUIREMENT);
  });

  it('min height guarantees header + video + trim buttons fit uncut', () => {
    expect(CLIP_PANEL_MIN_H).toBeGreaterThanOrEqual(CONTENT_HEIGHT_REQUIREMENT);
  });

  it('renders the min-size contract as CSS min-width/min-height', () => {
    renderPopup();
    const panel = popupPanel();
    expect(panel).toHaveStyle({
      minWidth: `${CLIP_PANEL_MIN_W}px`,
      minHeight: `${CLIP_PANEL_MIN_H}px`,
    });
  });

  it('renders 8 directional resize handles (n/s/e/w + corners)', () => {
    renderPopup();
    expect(document.querySelectorAll('[data-panel-resize]')).toHaveLength(8);
  });
});

describe('TwitchClipPopup resize', () => {
  it('clamps the panel to the min contract DURING a shrink drag', async () => {
    renderPopup();
    const panel = popupPanel();
    // jsdom has no layout — the mount measure sees offsetHeight 0, so the
    // resize handler seeds size from the DOM as CLIP_PANEL_MIN_H; advertise
    // a plausible natural size to make the drag math deterministic.
    Object.defineProperty(panel, 'offsetHeight', { configurable: true, value: 430 });
    panel.style.width = '460px';
    panel.style.height = '430px';

    // East handle = 3rd of the 8 strips (n, s, e, w, ne, nw, se, sw). Dragging
    // west (-250px) would take the width to 210 — far below the min.
    const east = panel.querySelectorAll('[data-panel-resize]')[2] as HTMLElement;
    await act(async () => {
      east.dispatchEvent(makePointer('pointerdown', 500, 300));
      east.dispatchEvent(makePointer('pointermove', 250, 300)); // dx = -250
      await nextFrame();
      east.dispatchEvent(makePointer('pointerup', 250, 300));
    });
    // Clamped at the min, never below it.
    expect(panel.style.width).toBe(`${CLIP_PANEL_MIN_W}px`);

    // South handle = 2nd of the 8 strips. Dragging up (-250px) would take the
    // height to 180 — below the min.
    const south = panel.querySelectorAll('[data-panel-resize]')[1] as HTMLElement;
    await act(async () => {
      south.dispatchEvent(makePointer('pointerdown', 500, 300));
      south.dispatchEvent(makePointer('pointermove', 500, 50)); // dy = -250
      await nextFrame();
      south.dispatchEvent(makePointer('pointerup', 500, 50));
    });
    expect(panel.style.height).toBe(`${CLIP_PANEL_MIN_H}px`);
  });

  it('keeps the panel inside the viewport while resizing', async () => {
    renderPopup();
    const panel = popupPanel();
    // South-east corner = 8th of the 8 strips. A big drag lands on the
    // viewport cap (innerWidth 1024 - RESIZE_MARGIN 32 = 992; innerHeight
    // 768 - 32 = 736).
    const se = panel.querySelectorAll('[data-panel-resize]')[7] as HTMLElement;
    await act(async () => {
      se.dispatchEvent(makePointer('pointerdown', 500, 300));
      se.dispatchEvent(makePointer('pointermove', 500 + 2000, 300 + 2000));
      await nextFrame();
      se.dispatchEvent(makePointer('pointerup', 500 + 2000, 300 + 2000));
    });
    expect(panel.style.width).toBe(`${window.innerWidth - 32}px`);
    expect(panel.style.height).toBe(`${window.innerHeight - 32}px`);
  });
});

describe('TwitchClipPopup hover volume', () => {
  it('shows the volume slider on mouseEnter and hides on mouseLeave', async () => {
    renderPopup();
    const volWrapper = document.body.querySelector('[data-volume-menu]') as HTMLElement;
    expect(volWrapper).toBeTruthy();

    // Slider hidden by default
    expect(volWrapper.querySelector("input[type='range']")).toBeNull();

    // mouseEnter → slider visible
    fireEvent.mouseEnter(volWrapper);
    expect(volWrapper.querySelector("input[type='range']")).toBeTruthy();

    // mouseLeave → slider hidden
    fireEvent.mouseLeave(volWrapper);
    expect(volWrapper.querySelector("input[type='range']")).toBeNull();
  });

  it('click speaker toggles mute and slider shows muted value', async () => {
    renderPopup();
    const volWrapper = document.body.querySelector('[data-volume-menu]') as HTMLElement;
    const speakerBtn = volWrapper.querySelector('button') as HTMLButtonElement;
    expect(speakerBtn).toBeTruthy();

    // Click to mute
    fireEvent.click(speakerBtn);

    // Hover to reveal and check slider value
    fireEvent.mouseEnter(volWrapper);
    const slider = volWrapper.querySelector("input[type='range']") as HTMLInputElement;
    expect(slider).toBeTruthy();
    expect(Number(slider.value)).toBe(0);
  });
});

/** PanelResizeHandles must not measure itself into an infinite render loop.
 *
 * THE CRASH THIS PINS. In the running app this threw
 *   "Maximum update depth exceeded"
 * with the stack ending at PanelResizeHandles inside ChannelExplorePopup.
 * The component measured its containing block in a `useLayoutEffect` that had
 * NO dependency array and called three setState functions unconditionally —
 * so the effect ran after EVERY render, and each run committed a state update,
 * which caused another render.
 *
 * On its own that settles, because React bails out when a primitive is set to
 * the value it already holds. It did not settle here, and the reason is in
 * this very file: there is a transition system
 * (`disablePanelTransitions` / `restorePanelTransitions`), and
 * `getComputedStyle` on a TRANSITIONED `box-shadow` returns a DIFFERENT
 * interpolated value on every frame. Unconditional measure-then-setState
 * against a value that changes every frame is unbounded, and React unmounts
 * the tree.
 *
 * The test reproduces the real mechanism rather than a mock of it: the stubbed
 * `getComputedStyle` hands back a different box-shadow band on every call,
 * which is what a transition does. The component must sample the containing
 * block ONCE per containing block. With the old effect it samples on every
 * render; with the fix it samples once, so the call count is bounded.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, cleanup } from '@testing-library/react';
import { useState } from 'react';

import { PanelResizeHandles } from './explorePopupUtils';

const realGetComputedStyle = window.getComputedStyle;
// jsdom implements no layout, so `offsetParent` is ALWAYS null there and the
// measuring effect would return early — the assertions below would then pass
// for the wrong reason (zero measurements trivially satisfies "at most four").
// Standing in the parent element makes the real path run.
const realOffsetParent = Object.getOwnPropertyDescriptor(
  HTMLElement.prototype,
  'offsetParent',
);

let measureCalls = 0;

/** Emulates a CSS transition: a DIFFERENT box-shadow band on every read. */
function transitioningGetComputedStyle(el: Element): CSSStyleDeclaration {
  const base = realGetComputedStyle.call(window, el);
  measureCalls += 1;
  const band = measureCalls;
  return new Proxy(base, {
    get(target, prop) {
      if (prop === 'boxShadow') return `0 0 0 ${band}px rgba(0,0,0,0.9)`;
      if (prop === 'paddingTop' || prop === 'paddingRight' ||
          prop === 'paddingBottom' || prop === 'paddingLeft') return '12px';
      if (prop === 'overflow') return 'visible';
      if (prop === 'overflowClipMargin') return '0px';
      const value = Reflect.get(target, prop);
      return typeof value === 'function' ? value.bind(target) : value;
    },
  });
}

beforeEach(() => {
  measureCalls = 0;
  window.getComputedStyle = transitioningGetComputedStyle as typeof window.getComputedStyle;
  Object.defineProperty(HTMLElement.prototype, 'offsetParent', {
    configurable: true,
    get(this: HTMLElement) {
      return this.parentElement;
    },
  });
});

afterEach(() => {
  window.getComputedStyle = realGetComputedStyle;
  if (realOffsetParent) {
    Object.defineProperty(HTMLElement.prototype, 'offsetParent', realOffsetParent);
  } else {
    delete (HTMLElement.prototype as unknown as Record<string, unknown>).offsetParent;
  }
  cleanup();
  vi.restoreAllMocks();
});

/** A positioned parent, so the handle host has a real `offsetParent`. */
function Harness() {
  const [n, setN] = useState(0);
  return (
    <div style={{ position: 'fixed', left: 0, top: 0, width: 200, height: 200 }}>
      <button type="button" onClick={() => setN((v) => v + 1)}>bump {n}</button>
      <PanelResizeHandles onPointerDown={() => {}} />
    </div>
  );
}

describe('PanelResizeHandles', () => {
  it('renders the eight edges and corners', () => {
    const { container } = render(<Harness />);
    expect(container.querySelectorAll('[data-panel-resize]')).toHaveLength(8);
  });

  it('measures the containing block once, not once per render', () => {
    render(<Harness />);
    const afterMount = measureCalls;
    // React's own mount renders more than once; what must not happen is the
    // effect re-measuring after every commit.
    expect(afterMount).toBeGreaterThan(0);
    expect(afterMount).toBeLessThanOrEqual(4);
  });

  it('a parent re-render does not re-measure, so a transitioned shadow cannot loop', () => {
    const { getByText } = render(<Harness />);
    const afterMount = measureCalls;

    // Ten more commits from an unrelated state change in the PARENT. The
    // effect depends on the host element, which did not change, so none of
    // these may produce a measurement. With the old dependency-less effect
    // this count grew by ten and the values it committed differed every time,
    // which is the unbounded loop.
    for (let i = 0; i < 10; i += 1) {
      getByText(/bump/).click();
    }

    expect(measureCalls).toBe(afterMount);
  });
});

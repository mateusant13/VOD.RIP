/**
 * FullscreenGate — single in-flight guard for fullscreen toggles.
 *
 * The historical bug: toggle bodies checked `if (!document.fullscreenElement)`
 * synchronously, so two rapid clicks before the promise settled both saw the
 * same state → duplicate requestFullscreen()/exitFullscreen() on different
 * elements (container vs inner video) or enter/exit interleaving.
 *
 * The gate instead:
 *   - ignores every toggle while a transition is in flight,
 *   - decides direction from the CURRENT active element at call time,
 *   - settles via `sync()` (called from the `fullscreenchange` listener),
 *     with a timeout fallback so a denied/unsupported request can't wedge it.
 */

export interface FullscreenAdapter {
  enter(el: HTMLElement): Promise<void>;
  exit(): Promise<void>;
  activeElement(): Element | null;
}

export const nativeFullscreenAdapter: FullscreenAdapter = {
  enter: (el) => el.requestFullscreen(),
  exit: () => document.exitFullscreen(),
  activeElement: () => document.fullscreenElement,
};

export type FullscreenDirection = 'enter' | 'exit';

export interface FullscreenGate {
  /** Starts a transition unless one is in flight. Returns the direction started, or null. */
  toggle(el: HTMLElement | null): FullscreenDirection | null;
  /** True while an enter/exit promise has not yet settled (or the fallback timer runs). */
  isTransitioning(): boolean;
  /** Whether `el` is currently the fullscreen element. */
  isActive(el: HTMLElement | null): boolean;
  /** Call from the `fullscreenchange` listener — settles the in-flight flag. */
  sync(): void;
}

/** Safety net: if `fullscreenchange` never fires (denied/unsupported), unstick after this long. */
export const FULLSCREEN_SETTLE_FALLBACK_MS = 350;

export function createFullscreenGate(
  adapter: FullscreenAdapter = nativeFullscreenAdapter,
  shouldExit: (active: Element | null, el: HTMLElement) => boolean = (active, el) => active === el,
): FullscreenGate {
  let busy = false;
  let seq = 0;

  const settle = () => {
    if (busy) busy = false;
  };

  return {
    toggle(el) {
      if (busy || !el) return null;
      const active = adapter.activeElement();
      const direction = shouldExit(active, el) ? 'exit' : 'enter';
      const id = ++seq;
      busy = true;
      const pending = direction === 'enter' ? adapter.enter(el) : adapter.exit();
      pending
        .then(() => {
          // fullscreenchange normally follows and clears `busy` via sync();
          // this is only a fallback for browsers/denials that skip the event.
          window.setTimeout(() => { if (id === seq) settle(); }, FULLSCREEN_SETTLE_FALLBACK_MS);
        })
        .catch(() => { if (id === seq) settle(); });
      return direction;
    },
    isTransitioning() {
      return busy;
    },
    isActive(el) {
      return el != null && adapter.activeElement() === el;
    },
    sync() {
      seq += 1; // invalidate any pending fallback timer
      settle();
    },
  };
}

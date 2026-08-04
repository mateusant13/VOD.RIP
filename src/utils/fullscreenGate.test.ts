import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import {
  createFullscreenGate,
  FULLSCREEN_SETTLE_FALLBACK_MS,
  type FullscreenAdapter,
} from './fullscreenGate';

class FakeAdapter implements FullscreenAdapter {
  active: Element | null = null;
  enterCalls = 0;
  exitCalls = 0;
  /** When set, enter() waits for the test to resolve this manually. */
  deferEnter: ((() => void) | null) = null;
  /** When set, exit() waits for the test to resolve this manually. */
  deferExit: ((() => void) | null) = null;
  private failEnter = false;

  failNextEnter() {
    this.failEnter = true;
  }

  enter(el: HTMLElement) {
    this.enterCalls += 1;
    if (this.failEnter) {
      this.failEnter = false;
      return Promise.reject(new Error('denied'));
    }
    if (this.deferEnter) {
      return new Promise<void>((resolve) => { this.deferEnter = resolve; }).then(() => {
        this.active = el;
      });
    }
    return Promise.resolve().then(() => { this.active = el; });
  }

  exit() {
    this.exitCalls += 1;
    if (this.deferExit) {
      return new Promise<void>((resolve) => { this.deferExit = resolve; }).then(() => {
        this.active = null;
      });
    }
    return Promise.resolve().then(() => { this.active = null; });
  }

  activeElement() {
    return this.active;
  }
}

const el = () => document.createElement('div');

describe('fullscreenGate', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('enters fullscreen when nothing is active', async () => {
    const adapter = new FakeAdapter();
    const gate = createFullscreenGate(adapter);
    const node = el();
    expect(gate.toggle(node)).toBe('enter');
    expect(adapter.enterCalls).toBe(1);
    await Promise.resolve();
    expect(adapter.activeElement()).toBe(node);
    expect(gate.isActive(node)).toBe(true);
  });

  it('exits when the element is already fullscreen', async () => {
    const adapter = new FakeAdapter();
    const gate = createFullscreenGate(adapter);
    const node = el();
    adapter.active = node;
    expect(gate.toggle(node)).toBe('exit');
    expect(adapter.exitCalls).toBe(1);
    await Promise.resolve();
    expect(adapter.activeElement()).toBe(null);
  });

  it('ignores a second toggle while a transition is in flight (the double-click bug)', async () => {
    const adapter = new FakeAdapter();
    const gate = createFullscreenGate(adapter);
    const node = el();
    expect(gate.toggle(node)).toBe('enter'); // first click — busy
    expect(gate.isTransitioning()).toBe(true);
    expect(gate.toggle(node)).toBe(null); // second click ignored
    expect(adapter.enterCalls).toBe(1); // exactly one requestFullscreen
    await Promise.resolve();
    gate.sync(); // browser fires fullscreenchange
    expect(gate.isTransitioning()).toBe(false);
    expect(gate.toggle(node)).toBe('exit');
    expect(adapter.exitCalls).toBe(1);
  });

  it('rapid enter+exit interleave cannot happen — exit is not even attempted mid-enter', async () => {
    const adapter = new FakeAdapter();
    const gate = createFullscreenGate(adapter);
    const node = el();
    gate.toggle(node);
    expect(gate.toggle(node)).toBe(null); // not 'exit' — direction never flips mid-flight
    expect(adapter.exitCalls).toBe(0);
    await Promise.resolve();
    gate.sync();
    expect(gate.toggle(node)).toBe('exit');
  });

  it('rejects clear the busy flag so the next toggle retries', async () => {
    const adapter = new FakeAdapter();
    const gate = createFullscreenGate(adapter);
    const node = el();
    adapter.failNextEnter();
    expect(gate.toggle(node)).toBe('enter');
    await Promise.resolve(); // rejected promise settles (then→catch chain needs 2 hops)
    await Promise.resolve();
    expect(gate.isTransitioning()).toBe(false);
    expect(gate.toggle(node)).toBe('enter');
    expect(adapter.enterCalls).toBe(2);
  });

  it('fallback timer unsticks when fullscreenchange never fires', async () => {
    const adapter = new FakeAdapter();
    const gate = createFullscreenGate(adapter);
    const node = el();
    gate.toggle(node);
    expect(gate.isTransitioning()).toBe(true);
    await vi.advanceTimersByTimeAsync(FULLSCREEN_SETTLE_FALLBACK_MS + 10);
    expect(gate.isTransitioning()).toBe(false);
    expect(gate.toggle(node)).toBe('exit');
  });

  it('sync() invalidates a pending fallback timer so it cannot clear a NEW transition', async () => {
    const adapter = new FakeAdapter();
    const gate = createFullscreenGate(adapter);
    const node = el();

    // Transition 1 (enter) is deferred — its promise stays unresolved.
    adapter.deferEnter = () => {};
    expect(gate.toggle(node)).toBe('enter');
    expect(gate.isTransitioning()).toBe(true);

    // Browser fires fullscreenchange without the promise having settled.
    gate.sync();
    adapter.active = node; // the browser transition really did enter fullscreen
    expect(gate.isTransitioning()).toBe(false);

    // Transition 2 (exit) is also deferred; busy again, seq bumped.
    adapter.deferExit = () => {};
    expect(gate.toggle(node)).toBe('exit');
    expect(gate.isTransitioning()).toBe(true);

    // Transition 1's promise finally resolves → its fallback timer arms (stale id).
    const resolveT1 = adapter.deferEnter!;
    adapter.deferEnter = null;
    resolveT1();
    await Promise.resolve();
    await Promise.resolve();

    // Stale timer fires → must NOT clear transition 2's busy flag (transition 2
    // is still pending, so only the stale timer could have cleared it).
    await vi.advanceTimersByTimeAsync(FULLSCREEN_SETTLE_FALLBACK_MS + 10);
    expect(gate.isTransitioning()).toBe(true);

    // Transition 2 settles normally via fullscreenchange.
    const resolveT2 = adapter.deferExit!;
    adapter.deferExit = null;
    resolveT2();
    await Promise.resolve();
    await Promise.resolve();
    gate.sync();
    expect(gate.isTransitioning()).toBe(false);
    expect(adapter.activeElement()).toBe(null);
  });

  it('supports the live-popup semantics: exit when ANY element is fullscreen', async () => {
    const adapter = new FakeAdapter();
    const gate = createFullscreenGate(adapter, (active) => active != null);
    const popup = el();
    const other = el();
    adapter.active = other; // some other element (e.g. preview panel) is fullscreen
    expect(gate.toggle(popup)).toBe('exit');
    expect(adapter.exitCalls).toBe(1);
    await Promise.resolve();
    gate.sync();
    expect(gate.isActive(popup)).toBe(false);
    expect(gate.toggle(popup)).toBe('enter');
    await Promise.resolve();
    gate.sync();
    expect(gate.isActive(popup)).toBe(true);
  });

  it('toggle(null) is a no-op', () => {
    const adapter = new FakeAdapter();
    const gate = createFullscreenGate(adapter);
    expect(gate.toggle(null)).toBe(null);
    expect(adapter.enterCalls).toBe(0);
  });
});

/**
 * Preview-window ids on an insecure context.
 *
 * `crypto.randomUUID` is only exposed in a secure context. This test pins the
 * failure it replaces: the bare call THREW, at a call site that sits inside a
 * `setState` updater, so React surfaced it as a render-phase error - which with
 * no boundary above it unmounted the whole app (blank screen). The gesture that
 * reaches it is dragging a preview into a frame cell.
 */
import { describe, it, expect, afterEach } from 'vitest';
import { newWindowId } from './windowId';

const realCrypto = globalThis.crypto;

function setCrypto(value: unknown): void {
  Object.defineProperty(globalThis, 'crypto', { value, configurable: true, writable: true });
}

afterEach(() => {
  setCrypto(realCrypto);
});

describe('newWindowId', () => {
  it('uses crypto.randomUUID when the context is secure', () => {
    setCrypto({ randomUUID: () => 'from-random-uuid', getRandomValues: undefined });
    expect(newWindowId()).toBe('from-random-uuid');
  });

  it('does not throw when randomUUID is missing (insecure context)', () => {
    // The regression itself: this is what the app used to do.
    setCrypto({ getRandomValues: (a: Uint8Array) => a });
    expect(() => newWindowId()).not.toThrow();
  });

  it('still produces a distinct id per call without randomUUID', () => {
    // A stub that VARIES. The first version stubbed getRandomValues to a
    // no-op, so every call saw the same zero buffer and this assertion was
    // UNSATISFIABLE rather than informative - uniqueness cannot be asserted
    // from a constant source. The assertion below is unchanged; it now
    // actually exercises the getRandomValues path it claims to cover.
    let n = 0;
    setCrypto({
      getRandomValues: (a: Uint8Array) => {
        n += 1;
        a.fill(n & 0xff);
        return a;
      },
    });
    const ids = new Set(Array.from({ length: 50 }, () => newWindowId()));
    expect(ids.size).toBe(50);
  });

  it('the fallback is UUID-shaped so logs are not ambiguous', () => {
    setCrypto({ getRandomValues: (a: Uint8Array) => a });
    expect(newWindowId()).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-a[0-9a-f]{3}-[0-9a-f]{12}$/,
    );
  });

  it('falls back to a unique id when crypto is absent entirely', () => {
    setCrypto(undefined);
    expect(() => newWindowId()).not.toThrow();
    expect(newWindowId()).not.toBe(newWindowId());
  });

  it('is unique in a secure context too', () => {
    expect(new Set(Array.from({ length: 50 }, () => newWindowId())).size).toBe(50);
  });
});

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { PREVIEW_START_TIMEOUT_MS, PreviewStartTimeout } from './previewStartTimeout';

/**
 * Regression guard for the "Starting YouTube preview…" infinite spinner:
 * the start phase MUST always terminate — either the player becomes ready or
 * the timeout fires exactly once, with the correct retry stage, and aborts
 * the in-flight session-create fetch so a RETRY click never inherits a hung
 * POST (the in-flight dedup entry is freed by the abort rejection).
 */
describe('PreviewStartTimeout (preview start phase guard)', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('fires onTimeout with stage=session when the create POST never resolves', () => {
    const onTimeout = vi.fn(() => true);
    const guard = new PreviewStartTimeout('https://youtu.be/x', { onTimeout });
    guard.start();
    expect(guard.signal?.aborted).toBe(false);
    vi.advanceTimersByTime(PREVIEW_START_TIMEOUT_MS);
    expect(onTimeout).toHaveBeenCalledTimes(1);
    expect(onTimeout).toHaveBeenCalledWith('https://youtu.be/x', 'session');
  });

  it('fires exactly once — never repeats after the timeout', () => {
    const onTimeout = vi.fn(() => true);
    const guard = new PreviewStartTimeout('https://youtu.be/x', { onTimeout });
    guard.start();
    vi.advanceTimersByTime(PREVIEW_START_TIMEOUT_MS * 3);
    expect(onTimeout).toHaveBeenCalledTimes(1);
  });

  it('escalates to stage=playback once the create resolved but playback never started', () => {
    const onTimeout = vi.fn(() => true);
    const guard = new PreviewStartTimeout('https://youtu.be/x', { onTimeout });
    guard.start();
    guard.markCreateResolved();
    vi.advanceTimersByTime(PREVIEW_START_TIMEOUT_MS);
    expect(onTimeout).toHaveBeenCalledWith('https://youtu.be/x', 'playback');
  });

  it('never fires after markReady — a player that reached canplay ends the phase', () => {
    const onTimeout = vi.fn(() => true);
    const guard = new PreviewStartTimeout('https://youtu.be/x', { onTimeout });
    guard.start();
    guard.markReady();
    vi.advanceTimersByTime(PREVIEW_START_TIMEOUT_MS * 2);
    expect(onTimeout).not.toHaveBeenCalled();
  });

  it('aborts the in-flight create fetch when the timeout is handled', () => {
    const onTimeout = vi.fn(() => true);
    const guard = new PreviewStartTimeout('https://youtu.be/x', { onTimeout });
    guard.start();
    const signal = guard.signal!;
    vi.advanceTimersByTime(PREVIEW_START_TIMEOUT_MS);
    expect(signal.aborted).toBe(true);
  });

  it('does NOT abort when the timeout was superseded (onTimeout returns false)', () => {
    const onTimeout = vi.fn(() => false);
    const guard = new PreviewStartTimeout('https://youtu.be/x', { onTimeout });
    guard.start();
    const signal = guard.signal!;
    vi.advanceTimersByTime(PREVIEW_START_TIMEOUT_MS);
    expect(onTimeout).toHaveBeenCalledTimes(1);
    expect(signal.aborted).toBe(false);
  });

  it('settle() clears the pending timer and aborts the fetch (terminal error path)', () => {
    const onTimeout = vi.fn(() => true);
    const guard = new PreviewStartTimeout('https://youtu.be/x', { onTimeout });
    guard.start();
    const signal = guard.signal!;
    guard.settle();
    vi.advanceTimersByTime(PREVIEW_START_TIMEOUT_MS * 2);
    expect(onTimeout).not.toHaveBeenCalled();
    expect(signal.aborted).toBe(true);
  });

  it('a new start() supersedes the previous phase: only the new timer fires, the old fetch is left alone', () => {
    const onTimeout = vi.fn(() => true);
    const guard = new PreviewStartTimeout('https://youtu.be/x', { onTimeout });
    guard.start();
    const firstSignal = guard.signal!;
    guard.start(); // superseded phase — new phase owns the timer + controller
    expect(guard.signal).not.toBe(firstSignal);
    vi.advanceTimersByTime(PREVIEW_START_TIMEOUT_MS);
    expect(onTimeout).toHaveBeenCalledTimes(1);
    expect(firstSignal.aborted).toBe(false); // dedup may still serve the newer open
  });
});

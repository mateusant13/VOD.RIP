import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  noteUserUnpause,
  autoPauseOtherPreviews,
  pauseOtherPreviews,
  registerPreviewPlayback,
  resetPreviewPlaybackBusForTests,
  UNPAUSE_GUARD_MS,
} from './previewPlaybackBus';

describe('previewPlaybackBus auto-pause guard', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(1_000_000);
    resetPreviewPlaybackBusForTests();
  });

  afterEach(() => {
    resetPreviewPlaybackBusForTests();
    vi.useRealTimers();
  });

  const makePlayer = () => {
    const pause = vi.fn();
    const unregister = registerPreviewPlayback(pause);
    return { pause, unregister };
  };

  it('auto-pauses other players when no user unpause happened', () => {
    const { pause, unregister } = makePlayer();
    autoPauseOtherPreviews(1_000_000);
    expect(pause).toHaveBeenCalledTimes(1);
    unregister();
  });

  it('suppresses the load-complete auto-pause when the user unpaused within 2s', () => {
    const { pause, unregister } = makePlayer();
    noteUserUnpause(); // t = 1_000_000
    vi.setSystemTime(1_000_000 + 2000); // exactly the window edge
    autoPauseOtherPreviews(1_000_000);
    expect(pause).not.toHaveBeenCalled();
    unregister();
  });

  it('auto-pauses once the 2s window lapses', () => {
    const { pause, unregister } = makePlayer();
    noteUserUnpause(); // t = 1_000_000, before the load starts
    vi.setSystemTime(1_000_000 + 1000);
    const loadStartedAt = 1_001_000;
    vi.setSystemTime(loadStartedAt + UNPAUSE_GUARD_MS + 1);
    autoPauseOtherPreviews(loadStartedAt);
    expect(pause).toHaveBeenCalledTimes(1);
    unregister();
  });

  it('suppresses when the user unpaused ANY time while the preview was loading', () => {
    // Mini preview open + loading for 10s; user unpauses the main preview
    // during the load; the load-complete must NOT pause it (the "better
    // rule") — far beyond the 2s window.
    const loadStartedAt = 1_000_000;
    const { pause, unregister } = makePlayer();
    vi.setSystemTime(1_000_000 + 3_000);
    noteUserUnpause();
    vi.setSystemTime(1_000_000 + 10_000);
    autoPauseOtherPreviews(loadStartedAt);
    expect(pause).not.toHaveBeenCalled();
    unregister();
  });

  it('pauses when the unpause predates the load window (and the 2s window)', () => {
    const { pause, unregister } = makePlayer();
    noteUserUnpause(); // t = 1_000_000, before the preview started loading
    vi.setSystemTime(1_000_000 + 1000);
    const loadStartedAt = 1_001_000;
    vi.setSystemTime(1_001_000 + 10_000);
    autoPauseOtherPreviews(loadStartedAt);
    expect(pause).toHaveBeenCalledTimes(1);
    unregister();
  });

  it('keeps the user-initiated pause path unguarded', () => {
    const { pause, unregister } = makePlayer();
    noteUserUnpause();
    // A user clicking play in another preview still pauses the rest.
    pauseOtherPreviews();
    expect(pause).toHaveBeenCalledTimes(1);
    unregister();
  });
});

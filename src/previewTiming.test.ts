/**
 * The preview metric must measure the CLICK, not the popup.
 *
 * Measured 2026-10-12 from the user's own log: `preview_open click→open=29ms`
 * and `click→open=-1ms` while the click actually hung ~26 s. Three defects:
 *
 *  1. When no gesture was captured, clickToOpenMs is -1 and full_path reported
 *     `click→playable = open→playable` — a number wearing the name of a segment
 *     it did not contain. The metric read green as the experience got worse.
 *  2. The main surface never noted a gesture at all (openPreview runs from an
 *     effect, so the click origin was simply absent).
 *  3. A failed click emitted NO terminal event, so the log's last word was the
 *     fast preview_open line — a 26 s failure looked like a success.
 *
 * The law is click → first decodable frame. These tests pin the accounting.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';

const apiPost = vi.fn().mockResolvedValue({ ok: true });
vi.mock('./hooks/useApiClient', () => ({
  apiPost: (...a: unknown[]) => apiPost(...a),
}));

import { PreviewTiming, notePreviewGesture } from './previewTiming';

type Emitted = { event: string; detail?: string; open_ms?: number };
const events = (): Emitted[] => apiPost.mock.calls.map((c) => c[1] as Emitted);
const detailOf = (name: string): string => events().find((e) => e.event === name)?.detail ?? '';

let now = 1000;
vi.spyOn(performance, 'now').mockImplementation(() => now);

beforeEach(() => {
  now = 1000;
  // _gestureStartMs is module-global and consumed by markOpen; drain any
  // leftover so these tests stay order-independent.
  notePreviewGesture();
  new PreviewTiming('reset', 'main').markOpen('reset');
  apiPost.mockClear();
});

describe('preview timing honesty', () => {
  it('never labels open→playable as click→playable when no gesture was captured', () => {
    const t = new PreviewTiming('youtube', 'main');
    t.markOpen('https://example/watch');
    now += 500;
    t.markFirstPlayable();

    const d = detailOf('full_path');
    expect(d).toContain('open→playable=500');
    expect(d).toContain('click→playable=unknown');
    expect(d).not.toMatch(/click→playable=\d/);
    expect(d).toContain('click→open=unknown');
  });

  it('includes the click segment when a gesture was captured', () => {
    notePreviewGesture(); // the click, at t=1000
    now = 1030;
    const t = new PreviewTiming('youtube', 'main');
    t.markOpen('https://example/watch');
    now = 2530;
    t.markFirstPlayable();

    const d = detailOf('full_path');
    expect(d).toContain('click→open=30');
    expect(d).toContain('open→playable=1500');
    expect(d).toContain('click→playable=1530ms');
    expect(events().find((e) => e.event === 'full_path')?.open_ms).toBe(1530);
  });

  it('emits a terminal event when the click never reached a playable frame', () => {
    notePreviewGesture();
    now = 1000;
    const t = new PreviewTiming('youtube', 'explore');
    t.markOpen('https://example/watch');
    now = 27000;
    t.markFailed('session: This video is unavailable.');

    const e = events().find((x) => x.event === 'preview_failed');
    expect(e).toBeDefined();
    expect(e!.detail).toContain('open→fail=26000ms');
    expect(e!.detail).toContain('reason=session: This video is unavailable.');
  });

  it('does not call a post-playable stall a failed click', () => {
    const t = new PreviewTiming('youtube', 'main');
    t.markOpen('https://example/watch');
    t.markFirstPlayable();
    t.markFailed('playback');
    expect(events().some((e) => e.event === 'preview_failed')).toBe(false);
  });

  it('tells a retry of the same open from a new click', () => {
    const t = new PreviewTiming('youtube', 'explore');
    expect(t.isRetryOf('https://example/watch')).toBe(false);
    t.markOpen('https://example/watch');
    expect(t.isRetryOf('https://example/watch')).toBe(true);
    expect(t.isRetryOf('https://example/other')).toBe(false);
  });

  it('keeps the original click origin across a retry', () => {
    notePreviewGesture(); // click at t=1000
    now = 1100;
    const t = new PreviewTiming('youtube', 'explore');
    const key = 'https://example/watch';
    t.markOpen(key);
    // The popup effect re-runs on retry; the tracker must be reused, not reset.
    const reused = t.isRetryOf(key) ? t : new PreviewTiming('youtube', 'explore');
    expect(reused).toBe(t);
    now = 14100;
    reused.markFirstPlayable();
    expect(detailOf('full_path')).toContain('click→playable=13100ms');
  });
});

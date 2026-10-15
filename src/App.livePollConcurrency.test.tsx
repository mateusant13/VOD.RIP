/**
 * App-level contract for the BACKGROUND concurrency lanes (App.tsx,
 * LIVE_POLL_MAX_IN_FLIGHT + lib/backgroundBudget.ts).
 *
 * The bug this pins: a full channel grid's live-status poll used to fire every
 * /api/channels/{id}/live request in one Promise.all. Measured in a real
 * browser against the running app (2026-10-14, 39 saved channels): that wave
 * holds ~40 fetches in flight, each taking 0.8-10s, and the oldest is still
 * pending 10.8s after it was issued. The paired A/B of this ceiling measured
 * `click -> playable` medians of 17.7s uncapped vs 2.2s at a ceiling of 2.
 * The browser only puts 6 connections per
 * origin on the wire, so the preview player's own requests (session create, hls
 * manifest, panel) sit in the connection queue behind the wave — with an idle
 * main thread and no slow server call anywhere.
 *
 * So this test renders the real App, opens the Channels tab, and measures how
 * many /live requests the app is willing to keep in flight at once. The arm
 * (ceiling) is selected through window.__VODRIP_BG_LIMITS__, which is the same
 * seam the paired browser A/B uses, so both arms run the identical code path.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import App from './App';

type LaneLimits = { poll?: number };

const CHANNELS = Array.from({ length: 12 }, (_, i) => ({
  id: 'ch_' + i,
  displayName: 'chan ' + i,
  kickSlug: '',
  twitchSlug: '',
  youtubeSlug: 'chan' + i,
  vodVideos: [],
  clipVideos: [],
  vodErrors: {},
  clipErrors: {},
  updatedAt: '',
  loading: false,
}));

/** One deferred /live response per channel, so a wave cannot drain early. */
function installLiveTap() {
  const tap = {
    started: 0,
    inFlight: 0,
    peak: 0,
    settled: 0,
  };
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(typeof input === 'string' ? input : (input as URL).href ?? input);
    if (!/\/api\/channels\/[^/]+\/live/.test(url)) {
      // Everything else the app asks for on boot: answer cheaply and at once.
      return new Response(JSON.stringify({}), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    tap.started += 1;
    tap.inFlight += 1;
    tap.peak = Math.max(tap.peak, tap.inFlight);
    // 25ms is long enough that a whole wave of responses cannot land inside
    // one microtask, so the peak measures how many the APP is willing to have
    // on the wire at once - not how fast the stub answers.
    await new Promise<void>((resolve) => { setTimeout(resolve, 25); });
    tap.inFlight -= 1;
    tap.settled += 1;
    return new Response(JSON.stringify({ is_live: false, title: '', viewer_count: 0 }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  });
  vi.stubGlobal('fetch', fetchMock);
  return tap;
}

async function openChannelsTab() {
  const btn = await screen.findByRole('button', { name: /channels/i });
  await act(async () => { fireEvent.click(btn); });
}

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem('vodrip_saved_channels', JSON.stringify(CHANNELS));
});
afterEach(() => {
  vi.unstubAllGlobals();
  delete (window as unknown as { __VODRIP_BG_LIMITS__?: LaneLimits }).__VODRIP_BG_LIMITS__;
});

describe('App live-status poll: concurrency ceiling', () => {
  it('keeps at most 2 live-status requests in flight (the default ceiling)', async () => {
    const tap = installLiveTap();
    render(<App />);
    await openChannelsTab();

    // Wait until the app has actually committed to the wave: with the ceiling
    // in place the first two requests own the only slots, so the app never
    // issues more than 2. Let the queue drain by microtasks first.
    await waitFor(() => expect(tap.started).toBeGreaterThanOrEqual(2));
    await act(async () => { await new Promise((r) => setTimeout(r, 30)); });

    expect(tap.peak).toBeLessThanOrEqual(2);
    // The wave is NOT dropped: every channel still gets polled once the
    // earlier responses land.
    await waitFor(() => expect(tap.settled).toBe(CHANNELS.length), { timeout: 8000 });
    expect(tap.started).toBe(CHANNELS.length);
  });

  it('holds every channel in one Promise.all when the ceiling is removed (A/B control arm)', async () => {
    (window as unknown as { __VODRIP_BG_LIMITS__?: LaneLimits }).__VODRIP_BG_LIMITS__ = { poll: 512 };
    const tap = installLiveTap();
    render(<App />);
    await openChannelsTab();

    await waitFor(() => expect(tap.started).toBeGreaterThanOrEqual(8));
    await act(async () => { await new Promise((r) => setTimeout(r, 30)); });

    // Same page, same click, same code path — only the ceiling differs. This is
    // the measured "before": the whole grid is on the wire at once.
    expect(tap.peak).toBeGreaterThanOrEqual(8);
  });
});

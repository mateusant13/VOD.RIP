import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, configure, fireEvent, render, screen, waitFor } from '@testing-library/react';
import ArchiveSearchPopup from './ArchiveSearchPopup';
import { getLanguage, setLanguage } from '../i18n';
import type { SavedChannel } from '../types';

/**
 * The deep sweep's `age_parked` clause.
 *
 * The backend counts age-gated videos SEPARATELY from `no_transcript` because
 * they have a fix (sign in to YouTube) that the other "without captions"
 * reasons do not. Before this, the sweep reported the total only, so a sweep
 * that parked 3 videos looked identical to one where YouTube simply had no
 * captions — and the whole point of the park was invisible.
 */

// One populated render in this popup costs ~290ms of pure jsdom even idle.
configure({ asyncUtilTimeout: 5000 });

const SAVED_GAVETA: SavedChannel = {
  id: 'ch-gaveta',
  displayName: 'gaveta',
  kickSlug: '',
  twitchSlug: '',
  youtubeSlug: 'gaveta',
  vodVideos: [],
  clipVideos: [],
  updatedAt: '2026-08-01T00:00:00Z',
};

const SEARCH_DEBOUNCE_MS = 250;

const saved = getLanguage();
afterEach(() => {
  setLanguage(saved);
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

/** @param status  the terminal poll body; extra keys model older backends. */
function mockFetch(status: Record<string, unknown>) {
  const fn = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes('/api/archive/search/deep')) {
      const body = url.includes('/cancel')
        ? { ok: true }
        : url.includes('/api/archive/search/deep/job-1')
          ? status
          : { job_id: 'job-1' };
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    if (url.includes('/api/archive/search/remote')) {
      return new Response(JSON.stringify({ hits: [], error: null }), { status: 200 });
    }
    if (url.includes('/api/archive/search')) {
      return new Response(JSON.stringify({ hits: [] }), { status: 200 });
    }
    if (url.includes('/api/archive/videos')) {
      return new Response(JSON.stringify({ videos: [] }), { status: 200 });
    }
    return new Response(JSON.stringify({}), { status: 404 });
  });
  vi.stubGlobal('fetch', fn);
  return fn;
}

async function flushDebounce() {
  vi.useFakeTimers();
  try {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(SEARCH_DEBOUNCE_MS + 1);
    });
  } finally {
    vi.useRealTimers();
  }
}

/** Open the popup, type a query, confirm the sweep, and let it finish. */
async function runSweepToDone() {
  render(
    <ArchiveSearchPopup
      zIndex={7}
      onClose={() => {}}
      onOpenHit={() => {}}
      savedChannels={[SAVED_GAVETA]}
      initialChannel="gaveta"
    />,
  );
  const input = screen.getByPlaceholderText('SEARCH TRANSCRIPTS + CHAT...');
  fireEvent.change(input, { target: { value: 'vale da estranheza' } });
  await flushDebounce();
  await waitFor(() => expect(screen.getByRole('textbox', { name: /Type confirmar to enable/i })).toBeTruthy());

  const startBtn = () =>
    screen.getByRole('button', { name: /Start deep search/i }) as HTMLButtonElement;
  fireEvent.change(screen.getByRole('textbox', { name: /Type confirmar to enable/i }), {
    target: { value: 'confirmar' },
  });
  await waitFor(() => expect(startBtn().disabled).toBe(false));
  vi.useFakeTimers();
  await act(async () => {
    fireEvent.click(startBtn());
    await vi.advanceTimersByTimeAsync(0);
  });
  vi.useRealTimers();
}

const summary = () => document.querySelector('[data-testid="deep-summary"]')?.textContent ?? '';

/** A non-empty result set — the "N transcript matches · ..." branch only
 *  renders when the sweep actually found something. */
const DEEP_ROW = [
  {
    id: 'v1',
    title: 'VOD ROW',
    url: 'https://youtu.be/v1',
    date: null,
    ts: 2,
    snippet: 'vod snippet',
    video_kind: 'vod',
  },
];

describe('deep sweep — age_parked is reported apart from no_transcript', () => {
  it('names the parked count and the sign-in that releases it', async () => {
    mockFetch({
      status: 'done',
      scanned: 12,
      total: 12,
      no_transcript: 5,
      age_parked: 3,
      truncated: false,
      results: DEEP_ROW,
    });
    await runSweepToDone();

    await waitFor(() => expect(summary()).toContain('12 scanned'));
    // The pre-existing clause is untouched...
    expect(summary()).toContain('12 scanned · 5 without captions');
    // ...and the park is added as its own fact, not folded into the 5.
    expect(summary()).toContain('3 parked');
    expect(summary()).toContain('sign in to YouTube to release them');
  });

  it('leaves the summary byte-identical when age_parked is ABSENT (older backend)', async () => {
    mockFetch({
      status: 'done',
      scanned: 12,
      total: 12,
      no_transcript: 5,
      truncated: false,
      results: DEEP_ROW,
    });
    await runSweepToDone();

    await waitFor(() => expect(summary()).toContain('12 scanned'));
    expect(summary()).toBe('1 transcript matches · 12 scanned · 5 without captions');
    expect(summary()).not.toContain('parked');
  });

  it('adds no clause when age_parked is 0 or null', async () => {
    for (const ageParked of [0, null]) {
      mockFetch({
        status: 'done',
        scanned: 4,
        total: 4,
        no_transcript: 1,
        age_parked: ageParked,
        truncated: false,
        results: [],
      });
      const { unmount } = render(
        <ArchiveSearchPopup
          zIndex={7}
          onClose={() => {}}
          onOpenHit={() => {}}
          savedChannels={[SAVED_GAVETA]}
          initialChannel="gaveta"
        />,
      );
      const input = screen.getByPlaceholderText('SEARCH TRANSCRIPTS + CHAT...');
      fireEvent.change(input, { target: { value: 'vale da estranheza' } });
      await flushDebounce();
      await waitFor(() =>
        expect(screen.getByRole('textbox', { name: /Type confirmar to enable/i })).toBeTruthy(),
      );
      const startBtn = () =>
        screen.getByRole('button', { name: /Start deep search/i }) as HTMLButtonElement;
      fireEvent.change(screen.getByRole('textbox', { name: /Type confirmar to enable/i }), {
        target: { value: 'confirmar' },
      });
      await waitFor(() => expect(startBtn().disabled).toBe(false));
      vi.useFakeTimers();
      await act(async () => {
        fireEvent.click(startBtn());
        await vi.advanceTimersByTimeAsync(0);
      });
      vi.useRealTimers();

      await waitFor(() => expect(summary()).toContain('4 scanned'));
      expect(summary()).toBe('No transcript matches in 4 scanned videos');
      unmount();
    }
  });

  it('is translated in pt-BR and es', async () => {
    // Render and sweep in English (the placeholder is a translated string, so
    // switching first would need a per-locale query), then switch language and
    // let the summary re-render.
    mockFetch({
      status: 'done',
      scanned: 2,
      total: 2,
      no_transcript: 1,
      age_parked: 1,
      truncated: false,
      results: DEEP_ROW,
    });
    await runSweepToDone();
    await waitFor(() => expect(summary()).toContain('1 parked'));

    setLanguage('pt-BR');
    await waitFor(() => expect(summary()).toContain('1 pausado'));
    expect(summary()).toContain('entre no YouTube para liberá-los');

    setLanguage('es');
    await waitFor(() => expect(summary()).toContain('1 pausados'));
    expect(summary()).toContain('inicia sesión en YouTube para liberarlos');
  });
});

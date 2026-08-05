import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import ArchiveSearchPopup from './ArchiveSearchPopup';
import type { SavedChannel } from '../types';

const ARCHIVE_VIDEOS = {
  videos: [
    { platform: 'twitch', video_id: 'v1', channel: 'srdogg', title: 'VOD A' },
    { platform: 'kick', video_id: 'k1', channel: 'srdoglol', title: 'VOD B' },
    { platform: 'youtube', video_id: 'yt1', channel: 'srdogg', title: 'VOD C' },
  ],
};

const SAVED: SavedChannel = {
  id: 'ch-srdogg',
  displayName: 'srdogg / srdoglol',
  kickSlug: 'srdoglol',
  twitchSlug: 'srdogg',
  youtubeSlug: '',
  vodVideos: [],
  clipVideos: [],
  updatedAt: '2026-08-01T00:00:00Z',
};

function mockFetch(hits: unknown[] = [], extra: Record<string, unknown> = {}, remote: unknown = undefined) {
  const fn = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes('/api/archive/search/remote')) {
      return new Response(
        JSON.stringify(remote === undefined ? { hits: [], error: null } : remote),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      );
    }
    if (url.includes('/api/archive/search')) {
      return new Response(JSON.stringify({ hits, ...extra }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    if (url.includes('/api/archive/videos')) {
      return new Response(JSON.stringify(ARCHIVE_VIDEOS), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    return new Response(JSON.stringify({}), { status: 404 });
  });
  vi.stubGlobal('fetch', fn);
  return fn;
}

function searchUrls(fetchMock: ReturnType<typeof vi.fn>): string[] {
  return fetchMock.mock.calls
    .map((c) => String(c[0]))
    .filter((u) => u.includes('/api/archive/search'));
}

function searchUrlWith(fetchMock: ReturnType<typeof vi.fn>, needle: string): string | undefined {
  return searchUrls(fetchMock).find((u) => u.includes(needle));
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('ArchiveSearchPopup', () => {
  const HIT = {
    kind: 'transcript' as const,
    platform: 'twitch',
    video_id: 'v1',
    offset_sec: 42,
    text: 'zebra stripes',
    score: 1,
    channel: 'srdogg',
  };

  it('floating mode: fixed + zIndex + 8 resize handles + grab header; embedded: none of that', () => {
    mockFetch();
    const { container, unmount } = render(
      <ArchiveSearchPopup zIndex={7} onClose={() => {}} onOpenHit={() => {}} />,
    );
    const dialog = container.querySelector('[role="dialog"]')!;
    expect(dialog).toHaveStyle({ position: 'fixed', zIndex: '7' });
    expect(container.querySelectorAll('[data-panel-resize]')).toHaveLength(8);
    // Header is the drag handle.
    expect(dialog.querySelector('div')!.className).toContain('cursor-grab');
    unmount();

    const embedded = render(
      <ArchiveSearchPopup zIndex={7} embedded onClose={() => {}} onOpenHit={() => {}} />,
    );
    const embeddedDialog = embedded.container.querySelector('[role="dialog"]')!;
    expect(embeddedDialog.className).toContain('w-full');
    expect(embeddedDialog.className).toContain('h-full');
    expect(embeddedDialog).not.toHaveStyle({ position: 'fixed' });
    expect(embeddedDialog).not.toHaveStyle({ zIndex: '7' });
    expect(embedded.container.querySelectorAll('[data-panel-resize]')).toHaveLength(0);
    expect(embeddedDialog.querySelector('div')!.className).not.toContain('cursor-grab');
  });

  it('floating mode: initialPos seeds the dialog position (default top-right otherwise)', () => {
    mockFetch();
    const anchored = render(
      <ArchiveSearchPopup
        zIndex={7}
        initialPos={{ x: 123, y: 45 }}
        onClose={() => {}}
        onOpenHit={() => {}}
      />,
    );
    const dialog = anchored.container.querySelector('[role="dialog"]')!;
    expect(dialog).toHaveStyle({ left: '123px', top: '45px' });
    anchored.unmount();

    const fallback = render(<ArchiveSearchPopup zIndex={7} onClose={() => {}} onOpenHit={() => {}} />);
    const fbDialog = fallback.container.querySelector('[role="dialog"]')!;
    expect(fbDialog).toHaveStyle({ left: `${window.innerWidth - 24 - 460}px`, top: '80px' });
  });

  it('onSeekHit: row click seeks, per-row open affordance still opens', async () => {
    const fetchMock = mockFetch([HIT]);
    const onSeekHit = vi.fn();
    const onOpenHit = vi.fn();
    render(
      <ArchiveSearchPopup
        zIndex={10}
        onClose={() => {}}
        onOpenHit={onOpenHit}
        onSeekHit={onSeekHit}
      />,
    );
    const input = screen.getByPlaceholderText('SEARCH TRANSCRIPTS + CHAT...');
    fireEvent.change(input, { target: { value: 'zebra' } });
    await waitFor(() => expect(searchUrlWith(fetchMock, 'q=zebra')).toBeTruthy());
    const row = await screen.findByRole('button', { name: /zebra stripes/i });

    fireEvent.click(row);
    expect(onSeekHit).toHaveBeenCalledTimes(1);
    expect(onSeekHit).toHaveBeenCalledWith(expect.objectContaining({ video_id: 'v1', offset_sec: 42 }));
    expect(onOpenHit).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: /open .* in player/i }));
    expect(onOpenHit).toHaveBeenCalledTimes(1);
    expect(onOpenHit.mock.calls[0][0]).toMatchObject({ video_id: 'v1', offset_sec: 42 });
    expect(onOpenHit.mock.calls[0][1]).toMatchObject({ title: 'VOD A' });
    expect(onSeekHit).toHaveBeenCalledTimes(1);
  });

  it('without onSeekHit a row click still opens the hit (unchanged), no open affordance', async () => {
    const fetchMock = mockFetch([HIT]);
    const onOpenHit = vi.fn();
    render(<ArchiveSearchPopup zIndex={10} onClose={() => {}} onOpenHit={onOpenHit} />);
    const input = screen.getByPlaceholderText('SEARCH TRANSCRIPTS + CHAT...');
    fireEvent.change(input, { target: { value: 'zebra' } });
    await waitFor(() => expect(searchUrlWith(fetchMock, 'q=zebra')).toBeTruthy());
    const row = await screen.findByRole('button', { name: /zebra stripes/i });
    fireEvent.click(row);
    expect(onOpenHit).toHaveBeenCalledTimes(1);
    expect(onOpenHit.mock.calls[0][0]).toMatchObject({ video_id: 'v1', offset_sec: 42 });
    expect(onOpenHit.mock.calls[0][1]).toMatchObject({ title: 'VOD A' });
    expect(screen.queryByRole('button', { name: /open .* in player/i })).toBeNull();
  });

  it('result count row renders; ArrowDown/Enter navigate + select hits, Escape closes', async () => {
    const fetchMock = mockFetch([
      HIT,
      { ...HIT, offset_sec: 43, text: 'zebra stripes two' },
    ]);
    const onClose = vi.fn();
    const onOpenHit = vi.fn();
    render(<ArchiveSearchPopup zIndex={10} onClose={onClose} onOpenHit={onOpenHit} />);
    const input = screen.getByPlaceholderText('SEARCH TRANSCRIPTS + CHAT...');
    fireEvent.change(input, { target: { value: 'zebra' } });
    await waitFor(() => expect(searchUrlWith(fetchMock, 'q=zebra')).toBeTruthy());
    expect(await screen.findByText(/2 results/i)).toBeInTheDocument();

    const rows = await screen.findAllByRole('button', { name: /zebra stripes/i });
    expect(rows).toHaveLength(2);
    // Navigation only while typing in the search box.
    fireEvent.keyDown(document.body, { key: 'ArrowDown' });
    expect(rows[0]).not.toHaveAttribute('aria-current');
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    expect(rows[0]).toHaveAttribute('aria-current', 'true');
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    expect(rows[1]).toHaveAttribute('aria-current', 'true');
    fireEvent.keyDown(input, { key: 'ArrowUp' });
    expect(rows[0]).toHaveAttribute('aria-current', 'true');
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(onOpenHit).toHaveBeenCalledTimes(1);
    expect(onOpenHit.mock.calls[0][0]).toMatchObject({ video_id: 'v1', offset_sec: 42 });
    fireEvent.keyDown(input, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('synthetic rows never seek either', async () => {
    const fetchMock = mockFetch([
      {
        kind: 'message',
        platform: 'youtube',
        video_id: 'youtube-live-lubumr-1785714293393',
        offset_sec: 12,
        text: 'zebra synth row',
        score: 1,
        channel: 'lubumr',
      },
    ]);
    const onSeekHit = vi.fn();
    const onOpenHit = vi.fn();
    render(
      <ArchiveSearchPopup
        zIndex={10}
        onClose={() => {}}
        onOpenHit={onOpenHit}
        onSeekHit={onSeekHit}
      />,
    );
    const input = screen.getByPlaceholderText('SEARCH TRANSCRIPTS + CHAT...');
    fireEvent.change(input, { target: { value: 'zebra' } });
    await waitFor(() => expect(searchUrlWith(fetchMock, 'q=zebra')).toBeTruthy());
    const row = await screen.findByRole('button', { name: /zebra synth row/i });
    fireEvent.click(row);
    expect(onSeekHit).not.toHaveBeenCalled();
    expect(onOpenHit).not.toHaveBeenCalled();
    // No open affordance for non-playable rows either.
    expect(screen.queryByRole('button', { name: /open .* in player/i })).toBeNull();
  });

  it('renders platform logos on the filter chips', () => {
    mockFetch();
    render(<ArchiveSearchPopup zIndex={10} onClose={() => {}} onOpenHit={() => {}} />);
    expect(screen.getByLabelText('Twitch')).toBeInTheDocument();
    expect(screen.getByLabelText('Kick')).toBeInTheDocument();
    expect(screen.getByLabelText('YouTube')).toBeInTheDocument();
  });

  it('source filter: BOTH default sends no source, STREAMER → transcript, CHAT → chat', async () => {
    const fetchMock = mockFetch();
    render(<ArchiveSearchPopup zIndex={10} onClose={() => {}} onOpenHit={() => {}} />);
    const input = screen.getByPlaceholderText('SEARCH TRANSCRIPTS + CHAT...');
    fireEvent.change(input, { target: { value: 'zebra' } });
    await waitFor(() => expect(searchUrlWith(fetchMock, 'q=zebra')).toBeTruthy());
    expect(searchUrlWith(fetchMock, 'q=zebra')).not.toContain('source=');

    fireEvent.click(screen.getByRole('button', { name: 'STREAMER' }));
    await waitFor(() =>
      expect(searchUrlWith(fetchMock, 'q=zebra&source=transcript')).toBeTruthy(),
    );

    fireEvent.click(screen.getByRole('button', { name: 'CHAT' }));
    await waitFor(() => expect(searchUrlWith(fetchMock, 'q=zebra&source=chat')).toBeTruthy());

    fireEvent.click(screen.getByRole('button', { name: 'BOTH' }));
    await waitFor(() =>
      expect(searchUrlWith(fetchMock, 'q=zebra&source=chat')).toBeTruthy(),
    );
    // After returning to BOTH, the latest request carries no source param.
    await waitFor(() => {
      const urls = searchUrls(fetchMock).filter((u) => u.includes('q=zebra'));
      expect(urls[urls.length - 1]).not.toContain('source=');
    });
  });

  it('semantic toggle: off by default, on sends semantic=1, disabled for CHAT', async () => {
    const fetchMock = mockFetch();
    render(<ArchiveSearchPopup zIndex={10} onClose={() => {}} onOpenHit={() => {}} />);
    const input = screen.getByPlaceholderText('SEARCH TRANSCRIPTS + CHAT...');
    fireEvent.change(input, { target: { value: 'zebra' } });
    await waitFor(() => expect(searchUrlWith(fetchMock, 'q=zebra')).toBeTruthy());
    expect(searchUrlWith(fetchMock, 'q=zebra')).not.toContain('semantic=');

    const toggle = screen.getByRole('button', { name: 'SEMANTIC' });
    expect(toggle).not.toBeDisabled();
    fireEvent.click(toggle);
    await waitFor(() => expect(searchUrlWith(fetchMock, 'q=zebra&semantic=1')).toBeTruthy());

    // Concept search covers transcripts only — CHAT disables the toggle.
    fireEvent.click(screen.getByRole('button', { name: 'CHAT' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'SEMANTIC' })).toBeDisabled());
    expect(searchUrlWith(fetchMock, 'q=zebra&source=chat')).not.toContain('semantic=');

    // Back to a transcript-capable source re-enables it (state preserved).
    fireEvent.click(screen.getByRole('button', { name: 'STREAMER' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'SEMANTIC' })).not.toBeDisabled());
    await waitFor(() =>
      expect(searchUrlWith(fetchMock, 'q=zebra&source=transcript&semantic=1')).toBeTruthy(),
    );
  });

  it('unions saved channels into the dropdown and sends comma-joined slugs', async () => {
    const fetchMock = mockFetch();
    render(
      <ArchiveSearchPopup
        zIndex={10}
        onClose={() => {}}
        onOpenHit={() => {}}
        savedChannels={[SAVED]}
      />,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const select = screen.getByLabelText('Channel') as HTMLSelectElement;
    const labels = [...select.options].map((o) => o.textContent);
    // Saved channel wins both archived slug groups (srdogg/srdoglol) — one
    // merged option, no bare duplicates (placeholder aside).
    expect(labels.slice(1)).toEqual(['srdogg / srdoglol']);
    const savedOpt = [...select.options].find((o) => o.value === 'srdogg,srdoglol');
    expect(savedOpt).toBeTruthy();

    const input = screen.getByPlaceholderText('SEARCH TRANSCRIPTS + CHAT...');
    fireEvent.change(input, { target: { value: 'zebra' } });
    await waitFor(() => expect(searchUrlWith(fetchMock, 'q=zebra')).toBeTruthy());

    fireEvent.change(select, { target: { value: 'srdogg,srdoglol' } });
    await waitFor(() =>
      expect(searchUrlWith(fetchMock, 'q=zebra&channel=srdogg%2Csrdoglol')).toBeTruthy(),
    );
  });

  it('initialChannel seeds the channel filter; absent keeps the old behavior', async () => {
    const seeded = mockFetch();
    const first = render(
      <ArchiveSearchPopup
        zIndex={10}
        initialChannel="titiltei"
        onClose={() => {}}
        onOpenHit={() => {}}
      />,
    );
    await waitFor(() => expect(seeded).toHaveBeenCalled());
    const input = screen.getByPlaceholderText('SEARCH TRANSCRIPTS + CHAT...');
    fireEvent.change(input, { target: { value: 'zebra' } });
    await waitFor(() => expect(searchUrlWith(seeded, 'q=zebra&channel=titiltei')).toBeTruthy());
    first.unmount();

    // Absent prop → no channel param (old behavior).
    const plain = mockFetch();
    render(
      <ArchiveSearchPopup
        zIndex={10}
        onClose={() => {}}
        onOpenHit={() => {}}
      />,
    );
    await waitFor(() => expect(plain).toHaveBeenCalled());
    const input2 = screen.getByPlaceholderText('SEARCH TRANSCRIPTS + CHAT...');
    fireEvent.change(input2, { target: { value: 'zebra' } });
    await waitFor(() => expect(searchUrlWith(plain, 'q=zebra')).toBeTruthy());
    const urls = searchUrls(plain).filter((u) => u.includes('q=zebra'));
    expect(urls[urls.length - 1]).not.toContain('channel=');
  });

  it('dedups archived channel casing variants into one canonical option', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) => new Response('{}', { status: 404 }));
    vi.stubGlobal('fetch', fetchMock);
    const videos = {
      videos: [
        { platform: 'twitch', video_id: 'v1', channel: 'Titiltei', title: 'A' },
        { platform: 'kick', video_id: 'k1', channel: 'titiltei', title: 'B' },
        { platform: 'youtube', video_id: 'yt1', channel: 'TiTiltei', title: 'C' },
        { platform: 'youtube', video_id: 'yt2', channel: 'lubumr', title: 'D' },
      ],
    };
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/archive/videos')) {
        return new Response(JSON.stringify(videos), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response('{}', { status: 404 });
    });
    render(<ArchiveSearchPopup zIndex={10} onClose={() => {}} onOpenHit={() => {}} />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const select = screen.getByLabelText('Channel') as HTMLSelectElement;
    // First option is the ALL CHANNELS placeholder.
    const labels = [...select.options].map((o) => o.textContent).slice(1);
    expect(labels).toEqual(['lubumr', 'Titiltei']);
    const opt = [...select.options].find((o) => o.value.startsWith('Titiltei'));
    expect(opt?.value).toBe('Titiltei,titiltei,TiTiltei');
  });

  it('every-day toggle: default on; date pick unchecks; re-check ignores dates; uncheck re-applies', async () => {
    const fetchMock = mockFetch();
    render(<ArchiveSearchPopup zIndex={10} onClose={() => {}} onOpenHit={() => {}} />);
    const input = screen.getByPlaceholderText('SEARCH TRANSCRIPTS + CHAT...');
    fireEvent.change(input, { target: { value: 'zebra' } });
    await waitFor(() => expect(searchUrlWith(fetchMock, 'q=zebra')).toBeTruthy());

    // Default: EVERY DAY checked, no date params sent.
    const dayBtn = screen.getByRole('button', { name: 'EVERY DAY' });
    expect(dayBtn).toHaveAttribute('aria-pressed', 'true');
    expect(searchUrlWith(fetchMock, 'q=zebra')).not.toContain('date_from=');

    // Picking a date unchecks it and applies the range.
    fireEvent.change(screen.getByLabelText('From date'), { target: { value: '2026-07-30' } });
    await waitFor(() =>
      expect(searchUrlWith(fetchMock, 'q=zebra&date_from=2026-07-30')).toBeTruthy(),
    );
    expect(screen.getByRole('button', { name: 'EVERY DAY' })).toHaveAttribute('aria-pressed', 'false');

    // Re-checking keeps the stored value but ignores it.
    fireEvent.click(screen.getByRole('button', { name: 'EVERY DAY' }));
    await waitFor(() => {
      const urls = searchUrls(fetchMock).filter((u) => u.includes('q=zebra'));
      expect(urls[urls.length - 1]).not.toContain('date_from=');
    });
    expect((screen.getByLabelText('From date') as HTMLInputElement).value).toBe('2026-07-30');

    // Unchecking re-applies the still-stored date.
    fireEvent.click(screen.getByRole('button', { name: 'EVERY DAY' }));
    await waitFor(() =>
      expect(searchUrlWith(fetchMock, 'q=zebra&date_from=2026-07-30')).toBeTruthy(),
    );
  });

  it('lang chips appear only when both languages exist and send lang param', async () => {
    const hits = [
      { kind: 'transcript' as const, platform: 'youtube', video_id: 'yt1', offset_sec: 1, text: 'ola mundo', score: 1, channel: 'titiltei', lang: 'pt' },
      { kind: 'transcript' as const, platform: 'youtube', video_id: 'yt2', offset_sec: 2, text: 'hello world', score: 1, channel: 'titiltei', lang: 'en' },
    ];
    const fetchMock = mockFetch(hits);
    render(<ArchiveSearchPopup zIndex={10} onClose={() => {}} onOpenHit={() => {}} />);
    const input = screen.getByPlaceholderText('SEARCH TRANSCRIPTS + CHAT...');
    fireEvent.change(input, { target: { value: 'zebra' } });
    await waitFor(() => expect(searchUrlWith(fetchMock, 'q=zebra')).toBeTruthy());

    // Both languages present → chips visible.
    const ptBtn = await screen.findByRole('button', { name: 'PT-BR' });
    expect(screen.getByRole('button', { name: 'EN' })).toBeInTheDocument();

    fireEvent.click(ptBtn);
    await waitFor(() => expect(searchUrlWith(fetchMock, 'q=zebra&lang=pt')).toBeTruthy());
    expect(ptBtn).toHaveAttribute('aria-pressed', 'true');

    // Clicking again clears the filter.
    fireEvent.click(ptBtn);
    await waitFor(() => {
      const urls = searchUrls(fetchMock).filter((u) => u.includes('q=zebra'));
      expect(urls[urls.length - 1]).not.toContain('lang=');
    });
  });

  it('lang chips stay hidden when hits carry a single language', async () => {
    const fetchMock = mockFetch([
      { kind: 'transcript', platform: 'youtube', video_id: 'yt1', offset_sec: 1, text: 'ola mundo', score: 1, channel: 'titiltei', lang: 'pt' },
    ]);
    render(<ArchiveSearchPopup zIndex={10} onClose={() => {}} onOpenHit={() => {}} />);
    const input = screen.getByPlaceholderText('SEARCH TRANSCRIPTS + CHAT...');
    fireEvent.change(input, { target: { value: 'zebra' } });
    await waitFor(() => expect(searchUrlWith(fetchMock, 'q=zebra')).toBeTruthy());
    await screen.findByRole('button', { name: /ola mundo/i });
    expect(screen.queryByRole('button', { name: 'PT-BR' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'EN' })).toBeNull();
  });

  it('scope: renders the chip, hides channel/platform filters, locks video_id', async () => {
    const fetchMock = mockFetch();
    render(
      <ArchiveSearchPopup
        zIndex={10}
        onClose={() => {}}
        onOpenHit={() => {}}
        scope={{ videoId: 'v1', title: 'VOD A' }}
      />,
    );
    expect(screen.getByText(/Searching in this video: VOD A/i)).toBeInTheDocument();
    expect(screen.queryByLabelText('Channel')).toBeNull();
    expect(screen.queryByText('Platform')).toBeNull();
    // Orthogonal filters stay active.
    expect(screen.getByRole('button', { name: 'STREAMER' })).toBeInTheDocument();

    const input = screen.getByPlaceholderText('SEARCH THIS VIDEO...');
    fireEvent.change(input, { target: { value: 'zebra' } });
    await waitFor(() => expect(searchUrlWith(fetchMock, 'q=zebra')).toBeTruthy());
    const url = searchUrlWith(fetchMock, 'q=zebra')!;
    expect(url).toContain('video_id=v1');
    expect(url).not.toContain('channel=');
    expect(url).not.toContain('platform=');
  });

  it('synthetic watchdog hits show nearby chat but never open the preview', async () => {
    const fetchMock = mockFetch([
      {
        kind: 'message',
        platform: 'youtube',
        video_id: 'youtube-live-lubumr-1785714293393',
        offset_sec: 12,
        text: 'zebra synth row',
        score: 1,
        channel: 'lubumr',
      },
    ]);
    const onOpenHit = vi.fn();
    render(<ArchiveSearchPopup zIndex={10} onClose={() => {}} onOpenHit={onOpenHit} />);
    const input = screen.getByPlaceholderText('SEARCH TRANSCRIPTS + CHAT...');
    fireEvent.change(input, { target: { value: 'zebra' } });
    await waitFor(() => expect(searchUrlWith(fetchMock, 'q=zebra')).toBeTruthy());
    await waitFor(() =>
      expect(
        screen.getByRole('button', { name: /zebra synth row/i }),
      ).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole('button', { name: /zebra synth row/i }));
    await waitFor(() => expect(onOpenHit).not.toHaveBeenCalled());
    // Nearby chat still loads for the selected hit.
    expect(
      fetchMock.mock.calls.some((c) =>
        String(c[0]).includes('/api/archive/videos/youtube/youtube-live-lubumr-1785714293393/chat'),
      ),
    ).toBe(true);
  });

  it('enriching status line shows when the backend kicked background work, clears when idle', async () => {
    const enrich = [
      { platform: 'twitch', video_id: 'v2', kind: 'chat_backfill', channel: 'srdogg', title: 'VOD B' },
    ];
    let current = enrich;
    const fetchMock = mockFetch([], {});
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/archive/search')) {
        return new Response(JSON.stringify({ hits: [], enriching: current }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.includes('/api/archive/videos')) {
        return new Response(JSON.stringify(ARCHIVE_VIDEOS), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response(JSON.stringify({}), { status: 404 });
    });
    render(<ArchiveSearchPopup zIndex={10} onClose={() => {}} onOpenHit={() => {}} />);
    const input = screen.getByPlaceholderText('SEARCH TRANSCRIPTS + CHAT...');
    fireEvent.change(input, { target: { value: 'zebra' } });
    await waitFor(() =>
      expect(screen.getByText(/Indexing 1 video.*chat backfill/i)).toBeInTheDocument(),
    );
    // Next response idle → the line clears.
    current = [];
    fireEvent.change(input, { target: { value: 'zebra2' } });
    await waitFor(() => expect(searchUrlWith(fetchMock, 'q=zebra2')).toBeTruthy());
    await waitFor(() =>
      expect(screen.queryByText(/Indexing 1 video/i)).toBeNull(),
    );
  });

  it('channel_hint chip: renders from response, ✕ dismisses via hint=0, no re-fire loop', async () => {
    let hint: string | undefined = 'srdogg';
    const fetchMock = mockFetch([], {});
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/archive/search')) {
        return new Response(JSON.stringify({ hits: [], enriching: [], channel_hint: hint }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.includes('/api/archive/videos')) {
        return new Response(JSON.stringify(ARCHIVE_VIDEOS), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response(JSON.stringify({}), { status: 404 });
    });
    render(<ArchiveSearchPopup zIndex={10} onClose={() => {}} onOpenHit={() => {}} />);
    const input = screen.getByPlaceholderText('SEARCH TRANSCRIPTS + CHAT...');
    fireEvent.change(input, { target: { value: 'zebra' } });
    const chip = await screen.findByLabelText('Channel scope hint');
    expect(chip.textContent).toContain('scoped to srdogg');

    // The hint is an implicit backend scope: the request must NOT echo it
    // as an explicit channel (that suppressed the hint and re-fired the
    // search forever in a request loop). The remote channel-search request
    // legitimately carries channel= — exclude it from this assertion.
    const localUrls = (list: string[]) =>
      list.filter((u) => u.includes('q=zebra') && !u.includes('/api/archive/search/remote'));
    const urls = localUrls(searchUrls(fetchMock));
    expect(urls[urls.length - 1]).not.toContain('channel=');

    // ✕ dismisses: the next request opts out via hint=0 and the chip stays
    // gone — and no further requests fire (response carries no hint again).
    hint = undefined;
    fireEvent.click(screen.getByLabelText('Remove channel scope'));
    await waitFor(() => {
      const u = localUrls(searchUrls(fetchMock));
      expect(u[u.length - 1]).toContain('hint=0');
    });
    expect(screen.queryByLabelText('Channel scope hint')).toBeNull();
    const count = localUrls(searchUrls(fetchMock)).length;
    await new Promise<void>((r) => setTimeout(r, 150));
    expect(localUrls(searchUrls(fetchMock)).length).toBe(count);
  });

  it('spam_count badge: ×N on collapsed rows, absent for single messages', async () => {
    const fetchMock = mockFetch([HIT], {});
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/archive/search')) {
        return new Response(JSON.stringify({ hits: [HIT], enriching: [] }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.includes('/chat')) {
        return new Response(
          JSON.stringify({
            messages: [
              { platform: 'twitch', video_id: 'v1', offset_sec: 40, username: 'bob', text: 'KEKW', spam_count: 5 },
              { platform: 'twitch', video_id: 'v1', offset_sec: 44, username: 'bob', text: 'unique thought', spam_count: 1 },
            ],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        );
      }
      if (url.includes('/api/archive/videos')) {
        return new Response(JSON.stringify(ARCHIVE_VIDEOS), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response(JSON.stringify({}), { status: 404 });
    });
    render(<ArchiveSearchPopup zIndex={10} onClose={() => {}} onOpenHit={() => {}} />);
    const input = screen.getByPlaceholderText('SEARCH TRANSCRIPTS + CHAT...');
    fireEvent.change(input, { target: { value: 'zebra' } });
    await waitFor(() => expect(searchUrlWith(fetchMock, 'q=zebra')).toBeTruthy());
    const row = await screen.findByRole('button', { name: /zebra stripes/i });
    fireEvent.click(row);
    await waitFor(() => expect(screen.getByText(/KEKW/)).toBeInTheDocument());
    expect(screen.getByText('×5')).toBeInTheDocument();
    expect(screen.queryByText('×1')).toBeNull();
  });

  it('remote YouTube search: fires for a saved channel scope, renders hits, opens on click', async () => {
    const REMOTE_HIT = {
      kind: 'youtube',
      platform: 'youtube',
      video_id: 'est1',
      offset_sec: 0,
      text: 'VALE DA ESTRANHEZA #5 — o deserto',
      score: 1,
      channel: 'gaveta',
      title: 'VALE DA ESTRANHEZA #5 — o deserto',
      duration_string: '12:34',
    };
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
    const fetchMock = mockFetch([], {}, { hits: [REMOTE_HIT], error: null });
    const onOpenHit = vi.fn();
    render(
      <ArchiveSearchPopup
        zIndex={7}
        onClose={() => {}}
        onOpenHit={onOpenHit}
        savedChannels={[SAVED_GAVETA]}
        initialChannel="gaveta"
      />,
    );
    const input = screen.getByPlaceholderText('SEARCH TRANSCRIPTS + CHAT...');
    fireEvent.change(input, { target: { value: 'vale da estranheza' } });
    await waitFor(() => expect(searchUrlWith(fetchMock, '/api/archive/search/remote')).toBeTruthy());
    expect(searchUrlWith(fetchMock, '/api/archive/search/remote')).toContain('channel=gaveta');
    await screen.findByText('YouTube results · @gaveta');
    const row = await screen.findByRole('button', { name: /VALE DA ESTRANHEZA #5/i });
    fireEvent.click(row);
    expect(onOpenHit).toHaveBeenCalledTimes(1);
    expect(onOpenHit.mock.calls[0][0]).toMatchObject({
      kind: 'youtube',
      video_id: 'est1',
      channel: 'gaveta',
    });
  });

  it('remote search: fires for an archived-only channel (no savedChannels), backend resolves the slug', async () => {
    const REMOTE_HIT = {
      kind: 'youtube',
      platform: 'youtube',
      video_id: 'r2',
      offset_sec: 0,
      text: 'ARCHIVED ONLY HIT',
      score: 1,
      channel: 'srdogg',
      title: 'ARCHIVED ONLY HIT',
    };
    const fetchMock = mockFetch([], {}, { hits: [REMOTE_HIT], error: null });
    const onOpenHit = vi.fn();
    render(
      <ArchiveSearchPopup
        zIndex={7}
        onClose={() => {}}
        onOpenHit={onOpenHit}
        initialChannel="srdogg"
      />,
    );
    const input = screen.getByPlaceholderText('SEARCH TRANSCRIPTS + CHAT...');
    fireEvent.change(input, { target: { value: 'zebra' } });
    await waitFor(() => expect(searchUrlWith(fetchMock, '/api/archive/search/remote')).toBeTruthy());
    expect(searchUrlWith(fetchMock, '/api/archive/search/remote')).toContain('channel=srdogg');
    await screen.findByText('ARCHIVED ONLY HIT');
  });

  it('remote YouTube search: empty + error states render without breaking the local list', async () => {
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
    const fetchMock = mockFetch([HIT], {}, { hits: [], error: 'YouTube search timed out — try again' });
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
    fireEvent.change(input, { target: { value: 'zebra' } });
    await waitFor(() => expect(searchUrlWith(fetchMock, '/api/archive/search/remote')).toBeTruthy());
    await screen.findByText('YouTube search timed out — try again');
    // Local hit still renders alongside the remote error note.
    expect(await screen.findByRole('button', { name: /zebra stripes/i })).toBeInTheDocument();
  });
});

describe('ArchiveSearchPopup USER filter', () => {
  const HIT = {
    kind: 'transcript' as const,
    platform: 'twitch',
    video_id: 'v1',
    offset_sec: 42,
    text: 'zebra stripes',
    score: 1,
    channel: 'srdogg',
  };

  it('types an author and sends it as the username param', async () => {
    const fetchMock = mockFetch([HIT]);
    render(<ArchiveSearchPopup zIndex={10} onClose={() => {}} onOpenHit={() => {}} />);
    const query = screen.getByPlaceholderText('SEARCH TRANSCRIPTS + CHAT...');
    fireEvent.change(query, { target: { value: 'zebra' } });
    const user = screen.getByLabelText('Chat author');
    fireEvent.change(user, { target: { value: '@Scriptingkata' } });
    await waitFor(() => expect(searchUrlWith(fetchMock, 'username=Scriptingkata')).toBeTruthy());
    // The @ is stripped client-side before it hits the wire.
    expect(searchUrlWith(fetchMock, 'username=@Scriptingkata')).toBeUndefined();
  });

  it('clears the user filter with the ✕ button', async () => {
    render(<ArchiveSearchPopup zIndex={10} onClose={() => {}} onOpenHit={() => {}} />);
    const user = screen.getByLabelText('Chat author');
    fireEvent.change(user, { target: { value: 'scriptingkata' } });
    fireEvent.click(screen.getByTitle('Clear user filter'));
    await waitFor(() => expect(screen.getByLabelText('Chat author')).toHaveValue(''));
  });

  it('renders the author on message hit rows', async () => {
    const fetchMock = mockFetch([{ ...HIT, kind: 'message', author: '@Scriptingkata' }]);
    render(<ArchiveSearchPopup zIndex={10} onClose={() => {}} onOpenHit={() => {}} />);
    const query = screen.getByPlaceholderText('SEARCH TRANSCRIPTS + CHAT...');
    fireEvent.change(query, { target: { value: 'zebra' } });
    await waitFor(() => expect(searchUrlWith(fetchMock, 'q=zebra')).toBeTruthy());
    expect(await screen.findByText('@Scriptingkata:')).toBeInTheDocument();
  });
});

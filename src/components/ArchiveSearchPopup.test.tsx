import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import ArchiveSearchPopup from './ArchiveSearchPopup';
import { todayIso } from '../archiveSearchUtils';
import { setLanguage } from '../i18n';
import type { SavedChannel } from '../types';

const ARCHIVE_VIDEOS = {
  videos: [
    { platform: 'twitch', video_id: 'v1', channel: 'srdogg', title: 'VOD A', canonical_key: 'srdogg-2026-08-03' },
    { platform: 'kick', video_id: 'k1', channel: 'srdoglol', title: 'VOD B' },
    { platform: 'youtube', video_id: 'yt1', channel: 'srdogg', title: 'VOD C', canonical_key: 'srdogg-2026-08-03' },
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
  setLanguage('en'); // the i18n module state is global — never leak a language into the next test
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

  it('close button stacks above the resize handles (corner handle must not eat the click)', () => {
    // jsdom has no layout/hit-testing, so assert the CSS contract: the
    // floating panel's shadow-2xl band grows the corner resize blocks up to
    // ~52px INSIDE the panel (clipsOverflow hug), and the ne block sits
    // exactly on top of the close button — it eats every click there unless
    // the header row (which hosts the button) paints above the z-50 handles.
    mockFetch();
    const { container } = render(
      <ArchiveSearchPopup zIndex={7} onClose={() => {}} onOpenHit={() => {}} />,
    );
    const dialog = container.querySelector('[role="dialog"]')!;
    const header = dialog.firstElementChild as HTMLElement;
    const closeBtn = [...header.querySelectorAll('button')].find(
      (b) => (b.title || '').toLowerCase().includes('close'),
    )!;
    expect(closeBtn).toBeTruthy();
    expect(closeBtn.closest('div')).toBe(header); // the button rides in the raised row
    const zClass = (cls: string): number => {
      const m = cls.match(/z-\[(\d+)\]/) ?? cls.match(/z-(\d+)/);
      return m ? Number(m[1]) : 0;
    };
    const headerZ = zClass(header.className);
    const handles = [...dialog.querySelectorAll('[data-panel-resize]')];
    expect(handles.length).toBeGreaterThan(0);
    for (const h of handles) {
      expect(headerZ).toBeGreaterThan(zClass(h.className));
    }
  });

  it('kind filter chips are VOD/clip/short only — no LIVE chip', () => {
    mockFetch();
    render(<ArchiveSearchPopup zIndex={10} onClose={() => {}} onOpenHit={() => {}} />);
    expect(screen.getByRole('button', { name: 'VOD' })).toHaveAttribute('aria-pressed', 'false');
    expect(screen.getByRole('button', { name: 'CLIP' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'SHORT' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'LIVE' })).toBeNull();
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

  it('kind badges: transcript → speech, message → chat; title stays as-is', async () => {
    const fetchMock = mockFetch([
      { ...HIT, kind: 'transcript' as const },
      { ...HIT, kind: 'message' as const, video_id: 'v2', text: 'zebra message row' },
      { ...HIT, kind: 'title' as const, video_id: 'v3', text: 'zebra title row' },
    ]);
    render(<ArchiveSearchPopup zIndex={10} onClose={() => {}} onOpenHit={() => {}} />);
    const input = screen.getByPlaceholderText('SEARCH TRANSCRIPTS + CHAT...');
    fireEvent.change(input, { target: { value: 'zebra' } });
    await waitFor(() => expect(searchUrlWith(fetchMock, 'q=zebra')).toBeTruthy());
    const speechRow = await screen.findByRole('button', { name: /zebra stripes/i });
    expect(within(speechRow).getByText('speech')).toBeInTheDocument();
    const messageRow = await screen.findByRole('button', { name: /zebra message row/i });
    expect(within(messageRow).getByText('chat')).toBeInTheDocument();
    const titleRow = await screen.findByRole('button', { name: /zebra title row/i });
    expect(within(titleRow).getByText('title')).toBeInTheDocument();
  });

  it('result rows render a logo per platform in hit.platforms, primary first', async () => {
    const fetchMock = mockFetch([
      { ...HIT, platforms: ['twitch', 'youtube'] },
      { ...HIT, video_id: 'k2', platform: 'kick', text: 'zebra kick row', platforms: ['kick'] },
    ]);
    render(<ArchiveSearchPopup zIndex={10} onClose={() => {}} onOpenHit={() => {}} />);
    const input = screen.getByPlaceholderText('SEARCH TRANSCRIPTS + CHAT...');
    fireEvent.change(input, { target: { value: 'zebra' } });
    await waitFor(() => expect(searchUrlWith(fetchMock, 'q=zebra')).toBeTruthy());
    const row = await screen.findByRole('button', { name: /zebra stripes/i });
    const svgs = within(row).getAllByLabelText(/Twitch|YouTube/);
    expect(svgs).toHaveLength(2);
    expect(svgs[0].getAttribute('aria-label')).toBe('Twitch'); // primary first
    expect(svgs[1].getAttribute('aria-label')).toBe('YouTube');
    const kickRow = await screen.findByRole('button', { name: /zebra kick row/i });
    expect(within(kickRow).getAllByLabelText('Kick')).toHaveLength(1);
    expect(within(kickRow).queryByLabelText('Twitch')).toBeNull();
  });

  it('platform filter matches hit.platforms — mirrored hits stay visible', async () => {
    const fetchMock = mockFetch([
      { ...HIT, platforms: ['twitch', 'youtube'] },
      { ...HIT, video_id: 'k2', platform: 'kick', text: 'zebra kick row', platforms: ['kick'] },
    ]);
    render(<ArchiveSearchPopup zIndex={10} onClose={() => {}} onOpenHit={() => {}} />);
    const input = screen.getByPlaceholderText('SEARCH TRANSCRIPTS + CHAT...');
    fireEvent.change(input, { target: { value: 'zebra' } });
    await waitFor(() => expect(searchUrlWith(fetchMock, 'q=zebra')).toBeTruthy());
    expect(await screen.findByRole('button', { name: /zebra stripes/i })).toBeInTheDocument();

    // youtube chip: the twitch-primary hit (mirrored on youtube) still shows;
    // the kick-only hit disappears and the count reflects the visible rows.
    fireEvent.click(screen.getByText('youtube'));
    await waitFor(() => expect(searchUrlWith(fetchMock, 'platform=youtube')).toBeTruthy());
    expect(screen.getByRole('button', { name: /zebra stripes/i })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('button', { name: /zebra kick row/i })).toBeNull());
    expect(screen.getByText('1 result')).toBeInTheDocument();
  });

  it('onOpenHit receives resolvable per-platform targets (primary first)', async () => {
    const fetchMock = mockFetch([{ ...HIT, platforms: ['twitch', 'youtube'] }]);
    const onOpenHit = vi.fn();
    render(<ArchiveSearchPopup zIndex={10} onClose={() => {}} onOpenHit={onOpenHit} />);
    const input = screen.getByPlaceholderText('SEARCH TRANSCRIPTS + CHAT...');
    fireEvent.change(input, { target: { value: 'zebra' } });
    await waitFor(() => expect(searchUrlWith(fetchMock, 'q=zebra')).toBeTruthy());
    fireEvent.click(await screen.findByRole('button', { name: /zebra stripes/i }));
    expect(onOpenHit).toHaveBeenCalledTimes(1);
    const [hitArg, videoArg, targetsArg] = onOpenHit.mock.calls[0] as [unknown, unknown, Array<{ platform: string; video: { video_id: string; title: string } | undefined }>];
    expect(hitArg).toMatchObject({ video_id: 'v1', offset_sec: 42 });
    // arg[1] stays the primary platform's video row (existing App contract).
    expect(videoArg).toMatchObject({ title: 'VOD A' });
    // arg[2] = twitch (primary) then the youtube mirror, resolved via canonical_key.
    expect(targetsArg.map((t) => t.platform)).toEqual(['twitch', 'youtube']);
    expect(targetsArg[1].video).toMatchObject({ video_id: 'yt1', title: 'VOD C' });
  });

  it('open degrades to the primary platform when platforms is absent', async () => {
    const fetchMock = mockFetch([HIT]);
    const onOpenHit = vi.fn();
    render(<ArchiveSearchPopup zIndex={10} onClose={() => {}} onOpenHit={onOpenHit} />);
    const input = screen.getByPlaceholderText('SEARCH TRANSCRIPTS + CHAT...');
    fireEvent.change(input, { target: { value: 'zebra' } });
    await waitFor(() => expect(searchUrlWith(fetchMock, 'q=zebra')).toBeTruthy());
    fireEvent.click(await screen.findByRole('button', { name: /zebra stripes/i }));
    const targetsArg = onOpenHit.mock.calls[0][2] as Array<{ platform: string; video?: { video_id: string } | undefined }>;
    expect(targetsArg.map((t) => t.platform)).toEqual(['twitch']);
    expect(targetsArg[0].video).toMatchObject({ video_id: 'v1' });
  });

  it('source filter + kind badges translate (pt-BR fala/vídeo, es habla/Video)', async () => {
    const fetchMock = mockFetch([HIT]);
    setLanguage('pt-BR');
    render(<ArchiveSearchPopup zIndex={10} onClose={() => {}} onOpenHit={() => {}} />);
    expect(screen.getByRole('button', { name: 'vídeo' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'fala' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'chat' })).toBeInTheDocument();
    // No 'ambos'/'both' chip anymore — every source is a real toggle, all ON.
    expect(screen.queryByRole('button', { name: 'ambos' })).toBeNull();
    const input = screen.getByPlaceholderText(/PESQUISAR/);
    fireEvent.change(input, { target: { value: 'zebra' } });
    await waitFor(() => expect(searchUrlWith(fetchMock, 'q=zebra')).toBeTruthy());
    const row = await screen.findByRole('button', { name: /zebra stripes/i });
    expect(within(row).getByText('fala')).toBeInTheDocument();
    // Live language switch re-renders the same popup.
    setLanguage('es');
    await waitFor(() => expect(screen.getByRole('button', { name: 'habla' })).toBeInTheDocument());
    expect(screen.getByRole('button', { name: 'Video' })).toBeInTheDocument();
    expect(within(row).getByText('habla')).toBeInTheDocument();
  });

  it('source filter: all on sends no source, deselecting one sends the CSV subset', async () => {
    const fetchMock = mockFetch();
    render(<ArchiveSearchPopup zIndex={10} onClose={() => {}} onOpenHit={() => {}} />);
    const input = screen.getByPlaceholderText('SEARCH TRANSCRIPTS + CHAT...');
    fireEvent.change(input, { target: { value: 'zebra' } });
    await waitFor(() => expect(searchUrlWith(fetchMock, 'q=zebra')).toBeTruthy());
    expect(searchUrlWith(fetchMock, 'q=zebra')).not.toContain('source=');

    // Deselect speech → video+chat goes as a comma-joined subset
    // (URLSearchParams percent-encodes the comma).
    fireEvent.click(screen.getByRole('button', { name: 'speech' }));
    await waitFor(() =>
      expect(searchUrlWith(fetchMock, 'q=zebra&source=video%2Cchat')).toBeTruthy(),
    );

    // Deselect chat too → only video remains.
    fireEvent.click(screen.getByRole('button', { name: 'chat' }));
    await waitFor(() =>
      expect(searchUrlWith(fetchMock, 'q=zebra&source=video')).toBeTruthy(),
    );

    // Re-select both — back to all three, the param disappears again.
    fireEvent.click(screen.getByRole('button', { name: 'speech' }));
    fireEvent.click(screen.getByRole('button', { name: 'chat' }));
    await waitFor(() => {
      const urls = searchUrls(fetchMock).filter((u) => u.includes('q=zebra'));
      expect(urls[urls.length - 1]).not.toContain('source=');
    });
  });

  it('semantic toggle: off by default, on sends semantic=1, disabled without speech', async () => {
    const fetchMock = mockFetch();
    render(<ArchiveSearchPopup zIndex={10} onClose={() => {}} onOpenHit={() => {}} />);
    const input = screen.getByPlaceholderText('SEARCH TRANSCRIPTS + CHAT...');
    fireEvent.change(input, { target: { value: 'zebra' } });
    await waitFor(() => expect(searchUrlWith(fetchMock, 'q=zebra')).toBeTruthy());
    expect(searchUrlWith(fetchMock, 'q=zebra')).not.toContain('semantic=');

    const toggle = screen.getByRole('button', { name: 'CONTEXT' });
    expect(toggle).not.toBeDisabled();
    fireEvent.click(toggle);
    // The lang filter now defaults to the UI language (en in tests), so it
    // sits between q and semantic in the query string.
    await waitFor(() => expect(searchUrlWith(fetchMock, 'q=zebra&lang=en&semantic=1')).toBeTruthy());

    // Concept search covers transcripts only — deselecting speech disables it.
    fireEvent.click(screen.getByRole('button', { name: 'speech' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'CONTEXT' })).toBeDisabled());
    expect(searchUrlWith(fetchMock, 'q=zebra&source=video%2Cchat')).not.toContain('semantic=');

    // Re-selecting speech re-enables it (state preserved).
    fireEvent.click(screen.getByRole('button', { name: 'speech' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'CONTEXT' })).not.toBeDisabled());
    await waitFor(() =>
      expect(searchUrlWith(fetchMock, 'q=zebra&lang=en&semantic=1')).toBeTruthy(),
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

  it('every-day uncheck with no dates seeds today and sends a closed today→today range', async () => {
    const fetchMock = mockFetch();
    render(<ArchiveSearchPopup zIndex={10} onClose={() => {}} onOpenHit={() => {}} />);
    const input = screen.getByPlaceholderText('SEARCH TRANSCRIPTS + CHAT...');
    fireEvent.change(input, { target: { value: 'zebra' } });
    await waitFor(() => expect(searchUrlWith(fetchMock, 'q=zebra')).toBeTruthy());

    const today = todayIso();
    fireEvent.click(screen.getByRole('button', { name: 'EVERY DAY' }));
    await waitFor(() =>
      expect(
        searchUrlWith(fetchMock, `q=zebra&date_from=${today}&date_to=${today}`),
      ).toBeTruthy(),
    );
    // The seeded date is visible in the input, and the toggle stays off.
    expect((screen.getByLabelText('From date') as HTMLInputElement).value).toBe(today);
    expect(screen.getByRole('button', { name: 'EVERY DAY' })).toHaveAttribute('aria-pressed', 'false');
  });

  it('start date without end date closes the range at today; end-only stays open at the start', async () => {
    const fetchMock = mockFetch();
    render(<ArchiveSearchPopup zIndex={10} onClose={() => {}} onOpenHit={() => {}} />);
    const input = screen.getByPlaceholderText('SEARCH TRANSCRIPTS + CHAT...');
    fireEvent.change(input, { target: { value: 'zebra' } });
    await waitFor(() => expect(searchUrlWith(fetchMock, 'q=zebra')).toBeTruthy());

    // From only → date_to injected as today (open-ended would reach into
    // future-dated rows).
    fireEvent.change(screen.getByLabelText('From date'), { target: { value: '2026-07-30' } });
    await waitFor(() =>
      expect(
        searchUrlWith(fetchMock, `q=zebra&date_from=2026-07-30&date_to=${todayIso()}`),
      ).toBeTruthy(),
    );

    // Mirror: To only → no date_from injected.
    fireEvent.change(screen.getByLabelText('From date'), { target: { value: '' } });
    fireEvent.change(screen.getByLabelText('To date'), { target: { value: '2026-07-30' } });
    await waitFor(() => {
      const urls = searchUrls(fetchMock).filter((u) => u.includes('q=zebra'));
      expect(urls[urls.length - 1]).toContain('date_to=2026-07-30');
      expect(urls[urls.length - 1]).not.toContain('date_from=');
    });
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
    expect(screen.getByRole('button', { name: 'speech' })).toBeInTheDocument();

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
      { platform: 'twitch', video_id: 'v2', kind: 'chat', channel: 'srdogg', title: 'VOD B' },
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

  it('scope object identity changes (App re-renders) do not re-fire the search', async () => {
    // App passes the preview-search scope as an inline literal — a NEW
    // object on every render (the preview player re-renders App on time
    // sync). The search effect must depend on the scope's CONTENT, not its
    // identity, or every parent render re-issues the request (the observed
    // storm: GET /api/archive/search?q=...&video_id=... re-firing every
    // ~1-2s for 25+ minutes). The response also updates enriching +
    // channel_hint — those must not feed back into the search either.
    const fetchMock = mockFetch([HIT], {});
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/archive/search/remote')) {
        return new Response(
          JSON.stringify({ hits: [], error: null }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        );
      }
      if (url.includes('/api/archive/search')) {
        return new Response(
          JSON.stringify({
            hits: [HIT],
            enriching: [{ platform: 'twitch', video_id: 'v2', kind: 'chat', channel: 'srdogg', title: 'VOD B' }],
            channel_hint: 'srdogg',
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
    // Local search calls only — the remote channel-title request legitimately
    // fires when the hint arrives and would pollute a raw count.
    const localSearchCount = () =>
      searchUrls(fetchMock).filter(
        (u) => u.includes('q=maranguape') && !u.includes('/api/archive/search/remote'),
      ).length;

    const { rerender } = render(
      <ArchiveSearchPopup
        zIndex={10}
        onClose={() => {}}
        onOpenHit={() => {}}
        scope={{ videoId: 'v1', title: 'VOD A' }}
      />,
    );
    const input = screen.getByPlaceholderText('SEARCH THIS VIDEO...');
    fireEvent.change(input, { target: { value: 'maranguape' } });
    await waitFor(() => expect(searchUrlWith(fetchMock, 'q=maranguape')).toBeTruthy());
    // Response state settled: hits + enriching + channel_hint applied.
    await screen.findByLabelText('Channel scope hint');
    const countAfterSettle = localSearchCount();
    expect(countAfterSettle).toBeGreaterThan(0);

    // Simulate an App re-render: a fresh inline scope object with identical
    // content (identity changed, meaning did not).
    rerender(
      <ArchiveSearchPopup
        zIndex={10}
        onClose={() => {}}
        onOpenHit={() => {}}
        scope={{ videoId: 'v1', title: 'VOD A' }}
      />,
    );
    await new Promise<void>((r) => setTimeout(r, 150));
    expect(localSearchCount()).toBe(countAfterSettle);
  });

  it('chat hit opens the whole history from the hit (half=0), not a ±30s slice', async () => {
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
              { platform: 'twitch', video_id: 'v1', offset_sec: 42, username: 'alice', text: 'at the hit' },
              { platform: 'twitch', video_id: 'v1', offset_sec: 500, username: 'bob', text: 'way past the old 60s window' },
              { platform: 'twitch', video_id: 'v1', offset_sec: 1000, username: 'carol', text: 'end of vod' },
            ],
            truncated: false,
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
    // From-offset mode: half=0, anchored at the hit.
    const chatUrl = fetchMock.mock.calls
      .map((c) => String(c[0]))
      .find((u) => u.includes('/chat?'));
    expect(chatUrl).toContain('half=0');
    expect(chatUrl).toContain('offset=42');
    // Rows far beyond ±30s render — the whole remaining history is fed in.
    await screen.findByText(/way past the old 60s window/i);
    expect(screen.getByText(/end of vod/i)).toBeInTheDocument();
    // Marker line preserved at the hit offset.
    expect(screen.getByText(/Hit moment 00:42/)).toBeInTheDocument();
  });

  it('loads a truncated chat history in continuation pages — append, no duplicates, note clears', async () => {
    const fetchMock = mockFetch([HIT], {});
    let chatCalls = 0;
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/archive/search')) {
        return new Response(JSON.stringify({ hits: [HIT], enriching: [] }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.includes('/chat')) {
        chatCalls += 1;
        const page1 = [
          { platform: 'twitch', video_id: 'v1', offset_sec: 42, username: 'alice', text: 'first page' },
          { platform: 'twitch', video_id: 'v1', offset_sec: 42, username: 'bob', text: 'same second row' },
          { platform: 'twitch', video_id: 'v1', offset_sec: 100, username: 'carol', text: 'boundary row' },
        ];
        const page2 = [
          // The backend re-includes the equal-offset boundary row — the
          // append must dedupe it, not render it twice.
          { platform: 'twitch', video_id: 'v1', offset_sec: 100, username: 'carol', text: 'boundary row' },
          { platform: 'twitch', video_id: 'v1', offset_sec: 200, username: 'dave', text: 'second page' },
        ];
        return new Response(
          JSON.stringify({
            messages: chatCalls === 1 ? page1 : page2,
            truncated: chatCalls === 1,
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
    fireEvent.click(await screen.findByRole('button', { name: /zebra stripes/i }));
    // Page 1 lands from the hit offset; the tail note says more is coming.
    await screen.findByText('first page');
    expect(chatCalls).toBe(1);
    expect(screen.queryByText('second page')).toBeNull();
    expect(screen.getByText(/Chat history continues/)).toBeInTheDocument();

    // Scroll near the bottom of the mounted rows → continuation fetch
    // anchored at the last delivered row's offset_sec (100).
    const chatScroll = screen.getByText('first page').closest('.overflow-y-auto') as HTMLElement;
    Object.defineProperty(chatScroll, 'scrollTop', { value: 900, configurable: true });
    Object.defineProperty(chatScroll, 'clientHeight', { value: 100, configurable: true });
    Object.defineProperty(chatScroll, 'scrollHeight', { value: 1000, configurable: true });
    fireEvent.scroll(chatScroll);
    await screen.findByText('second page');
    expect(chatCalls).toBe(2);
    const contUrl = fetchMock.mock.calls
      .map((c) => String(c[0]))
      .find((u) => u.includes('/chat?') && u.includes('offset=100'));
    expect(contUrl).toBeTruthy();
    expect(contUrl).toContain('half=0');
    // Appended without duplicates: the re-fetched boundary row renders once,
    // every page's rows are present.
    expect(screen.getAllByText('boundary row')).toHaveLength(1);
    expect(screen.getByText('first page')).toBeInTheDocument();
    expect(screen.getByText('same second row')).toBeInTheDocument();
    expect(screen.getByText('second page')).toBeInTheDocument();
    // The archive is fully loaded — the continuation note clears.
    await waitFor(() => expect(screen.queryByText(/Chat history continues/)).toBeNull());
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

  it('chat-history message click seeks via onSeekOffset (shared seekToTimestamp contract)', async () => {
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
              { platform: 'twitch', video_id: 'v1', offset_sec: 42, username: 'alice', text: 'at the hit' },
              { platform: 'twitch', video_id: 'v1', offset_sec: 500, username: 'bob', text: 'way past the old 60s window' },
            ],
            truncated: false,
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
    const onSeekOffset = vi.fn();
    render(
      <ArchiveSearchPopup
        zIndex={10}
        onClose={() => {}}
        onOpenHit={() => {}}
        onSeekOffset={onSeekOffset}
      />,
    );
    const input = screen.getByPlaceholderText('SEARCH TRANSCRIPTS + CHAT...');
    fireEvent.change(input, { target: { value: 'zebra' } });
    await waitFor(() => expect(searchUrlWith(fetchMock, 'q=zebra')).toBeTruthy());
    fireEvent.click(await screen.findByRole('button', { name: /zebra stripes/i }));
    await screen.findByText(/way past the old 60s window/i);
    fireEvent.click(screen.getByText(/way past the old 60s window/i));
    expect(onSeekOffset).toHaveBeenCalledTimes(1);
    expect(onSeekOffset).toHaveBeenCalledWith(500);
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

  it('renders a relative date on hit rows with the exact date in the tooltip', async () => {
    // Built from local components so the calendar-day gap is exact in any
    // timezone: the label reads '2 days ago' regardless of the real clock.
    const d = new Date();
    const hitDate = new Date(d.getFullYear(), d.getMonth(), d.getDate() - 2, 12).toISOString();
    const fetchMock = mockFetch([{ ...HIT, date: hitDate }]);
    render(<ArchiveSearchPopup zIndex={10} onClose={() => {}} onOpenHit={() => {}} />);
    const query = screen.getByPlaceholderText('SEARCH TRANSCRIPTS + CHAT...');
    fireEvent.change(query, { target: { value: 'zebra' } });
    await waitFor(() => expect(searchUrlWith(fetchMock, 'q=zebra')).toBeTruthy());
    const row = await screen.findByRole('button', { name: /zebra stripes/i });
    expect(row).toHaveTextContent('2 days ago');
    expect(row.querySelector('[title]')).toHaveAttribute(
      'title',
      new Date(hitDate).toLocaleString(),
    );
  });

  it('renders no date label when the hit carries no date', async () => {
    const fetchMock = mockFetch([HIT]);
    render(<ArchiveSearchPopup zIndex={10} onClose={() => {}} onOpenHit={() => {}} />);
    const query = screen.getByPlaceholderText('SEARCH TRANSCRIPTS + CHAT...');
    fireEvent.change(query, { target: { value: 'zebra' } });
    await waitFor(() => expect(searchUrlWith(fetchMock, 'q=zebra')).toBeTruthy());
    const row = await screen.findByRole('button', { name: /zebra stripes/i });
    expect(row).not.toHaveTextContent(/today|yesterday|\d+ (day|week|month|year)s? ago/);
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

  it('placeholder advertises comma-separated users; comma list passes through', async () => {
    const fetchMock = mockFetch([HIT]);
    render(<ArchiveSearchPopup zIndex={10} onClose={() => {}} onOpenHit={() => {}} />);
    const user = screen.getByPlaceholderText('user1,user2…');
    fireEvent.change(user, { target: { value: 'Scriptingkata,AlguemAe' } });
    await waitFor(() =>
      expect(searchUrlWith(fetchMock, 'username=Scriptingkata%2CAlguemAe')).toBeTruthy(),
    );
  });

  it('empty query + user filter fires the author-history search and renders hits', async () => {
    const fetchMock = mockFetch([
      {
        kind: 'message', platform: 'twitch', video_id: 'v1', offset_sec: 1801,
        text: 'olha a raposa', author: 'Scriptingkata', channel: 'srdogg',
        title: 'VOD A', date: '2026-08-03T17:24:00Z', video_kind: 'live',
        channel_language: null,
      },
    ]);
    render(<ArchiveSearchPopup zIndex={10} onClose={() => {}} onOpenHit={() => {}} />);
    const user = screen.getByLabelText('Chat author');
    fireEvent.change(user, { target: { value: 'Scriptingkata' } });
    await waitFor(() => expect(searchUrlWith(fetchMock, 'username=Scriptingkata')).toBeTruthy());
    expect(searchUrlWith(fetchMock, 'q=')).toBeTruthy();
    expect(await screen.findByText('Scriptingkata:')).toBeInTheDocument();
  });

  it('empty query + user filter shows the author-only empty state', async () => {
    mockFetch([]);
    render(<ArchiveSearchPopup zIndex={10} onClose={() => {}} onOpenHit={() => {}} />);
    const user = screen.getByLabelText('Chat author');
    fireEvent.change(user, { target: { value: 'Scriptingkata,AlguemAe' } });
    expect(
      await screen.findByText('No archived messages from @Scriptingkata, @AlguemAe.'),
    ).toBeInTheDocument();
  });
});

describe('ArchiveSearchPopup batch-3', () => {
  // The first describe's HIT is scoped to that block — batch-3 needs its own.
  const HIT = {
    kind: 'transcript' as const,
    platform: 'twitch',
    video_id: 'v1',
    offset_sec: 42,
    text: 'zebra stripes',
    score: 1,
    channel: 'srdogg',
  };

  it('video source chip: 3 chips, deselecting speech+chat leaves video only, disables CONTEXT', async () => {
    const fetchMock = mockFetch([]);
    render(<ArchiveSearchPopup zIndex={10} onClose={() => {}} onOpenHit={() => {}} />);
    const video = screen.getByRole('button', { name: 'video' });
    expect(video).toBeInTheDocument();
    // All three chips are ON by default (aria-pressed).
    for (const name of ['video', 'speech', 'chat']) {
      expect(screen.getByRole('button', { name })).toHaveAttribute('aria-pressed', 'true');
    }
    // Deselect speech and chat → video-only subset.
    fireEvent.click(screen.getByRole('button', { name: 'speech' }));
    fireEvent.click(screen.getByRole('button', { name: 'chat' }));
    const input = screen.getByPlaceholderText('SEARCH TRANSCRIPTS + CHAT...');
    fireEvent.change(input, { target: { value: 'zebra' } });
    await waitFor(() => expect(searchUrlWith(fetchMock, 'q=zebra&source=video')).toBeTruthy());
    expect(screen.getByRole('button', { name: 'CONTEXT' })).toBeDisabled();
    // Semantic never reaches the wire for video-title filters.
    expect(searchUrls(fetchMock).every((u) => !u.includes('semantic='))).toBe(true);
  });

  it('Indexando line dedupes kinds: "chat backfill (2), transcription" each once', async () => {
    const enrich = [
      { platform: 'twitch', video_id: 'v1', kind: 'chat', channel: 'srdogg', title: 'VOD A' },
      { platform: 'kick', video_id: 'k1', kind: 'chat', channel: 'srdoglol', title: 'VOD B' },
      { platform: 'youtube', video_id: 'yt1', kind: 'transcribe', channel: 'srdogg', title: 'VOD C' },
    ];
    const fetchMock = mockFetch([], {});
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/archive/search')) {
        return new Response(JSON.stringify({ hits: [], enriching: enrich }), {
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
      expect(
        screen.getByText(/Indexing 3 videos \(chat backfill \(2\), transcription\)/),
      ).toBeInTheDocument(),
    );
    // One label per kind — the 2 chat entries collapse into one "(2)".
    expect(screen.getAllByText(/Indexing 3 videos/)).toHaveLength(1);
  });

  it('refresh button re-runs the search with the current filters', async () => {
    let searchCalls = 0;
    const fetchMock = mockFetch([HIT], {});
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/archive/search')) {
        searchCalls += 1;
        return new Response(JSON.stringify({ hits: [HIT], enriching: [] }), {
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
    await screen.findByRole('button', { name: /zebra stripes/i });
    const afterFirst = searchCalls;
    fireEvent.click(screen.getByRole('button', { name: 'Refresh search' }));
    await waitFor(() => expect(searchCalls).toBeGreaterThan(afterFirst));
  });

  it('Enter with no arrow-selected hit re-runs the search', async () => {
    let searchCalls = 0;
    const fetchMock = mockFetch([HIT], {});
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/archive/search')) {
        searchCalls += 1;
        return new Response(JSON.stringify({ hits: [HIT], enriching: [] }), {
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
    await screen.findByRole('button', { name: /zebra stripes/i });
    const afterFirst = searchCalls;
    // No ArrowDown happened → activeIdx is -1 → Enter re-runs instead of selecting.
    fireEvent.keyDown(input, { key: 'Enter' });
    await waitFor(() => expect(searchCalls).toBeGreaterThan(afterFirst));
  });

  it('chat group: platform chips render, hide/show filters rows, last visible chip locks', async () => {
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
              { platform: 'twitch', video_id: 'v1', offset_sec: 42, username: 'alice', text: 'twitch row' },
              { platform: 'youtube', video_id: 'yt1', offset_sec: 100, username: 'bob', text: 'youtube row' },
            ],
            truncated: false,
            platforms: ['twitch', 'youtube'],
            next_offsets: { twitch: 42, youtube: 100 },
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
    fireEvent.click(await screen.findByRole('button', { name: /zebra stripes/i }));
    await screen.findByText('twitch row');
    expect(screen.getByText('youtube row')).toBeInTheDocument();

    // Both chips render, ALL on by default.
    const twChip = screen.getByTitle('Show/hide twitch chat');
    const ytChip = screen.getByTitle('Show/hide youtube chat');
    expect(twChip).toHaveAttribute('aria-pressed', 'true');
    expect(ytChip).toHaveAttribute('aria-pressed', 'true');

    // Hide youtube → its rows disappear, twitch stays, and the last visible
    // chip locks (min one platform).
    fireEvent.click(ytChip);
    expect(screen.queryByText('youtube row')).toBeNull();
    expect(screen.getByText('twitch row')).toBeInTheDocument();
    expect(ytChip).toHaveAttribute('aria-pressed', 'false');
    expect(twChip).toBeDisabled();

    // Re-show it.
    fireEvent.click(ytChip);
    expect(screen.getByText('youtube row')).toBeInTheDocument();
  });

  it('chat group continuation: page 2 echoes per-platform offsets, platform-aware dedupe', async () => {
    const fetchMock = mockFetch([HIT], {});
    let chatCalls = 0;
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/archive/search')) {
        return new Response(JSON.stringify({ hits: [HIT], enriching: [] }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.includes('/chat')) {
        chatCalls += 1;
        const page1 = [
          { platform: 'twitch', video_id: 'v1', offset_sec: 42, username: 'alice', text: 'first page' },
          { platform: 'youtube', video_id: 'yt1', offset_sec: 100, username: 'bob', text: 'boundary row' },
        ];
        const page2 = [
          // twitch@100 mirrors the boundary row from another platform — the
          // dedupe key carries the platform, so it renders separately.
          { platform: 'twitch', video_id: 'v1', offset_sec: 100, username: 'bob', text: 'boundary row' },
          // youtube@100 is the equal-offset re-include of page1's last row → dropped.
          { platform: 'youtube', video_id: 'yt1', offset_sec: 100, username: 'bob', text: 'boundary row' },
          { platform: 'youtube', video_id: 'yt1', offset_sec: 200, username: 'dave', text: 'second page' },
        ];
        return new Response(
          JSON.stringify({
            messages: chatCalls === 1 ? page1 : page2,
            truncated: chatCalls === 1,
            platforms: ['twitch', 'youtube'],
            next_offsets: chatCalls === 1 ? { twitch: 42, youtube: 100 } : { twitch: 100, youtube: 200 },
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
    fireEvent.click(await screen.findByRole('button', { name: /zebra stripes/i }));
    await screen.findByText('first page');
    expect(chatCalls).toBe(1);

    const chatScroll = screen.getByText('first page').closest('.overflow-y-auto') as HTMLElement;
    Object.defineProperty(chatScroll, 'scrollTop', { value: 900, configurable: true });
    Object.defineProperty(chatScroll, 'clientHeight', { value: 100, configurable: true });
    Object.defineProperty(chatScroll, 'scrollHeight', { value: 1000, configurable: true });
    fireEvent.scroll(chatScroll);
    await screen.findByText('second page');
    expect(chatCalls).toBe(2);

    // Continuation keeps the global offset AND echoes the per-member keyset.
    const contUrl = fetchMock.mock.calls
      .map((c) => String(c[0]))
      .find((u) => u.includes('/chat?') && u.includes('offsets='));
    expect(contUrl).toBeTruthy();
    expect(contUrl).toContain('offset=100');
    expect(contUrl).toContain('half=0');
    expect(contUrl).toContain('offsets=twitch:42');
    // Only the first member carries the 'offsets=' prefix — the rest are
    // comma-joined mid-list.
    expect(contUrl).toContain('twitch:42,youtube:100');
    // Platform-aware dedupe: youtube's equal-offset re-include dropped, the
    // twitch mirror kept — exactly 2 boundary rows + page rows.
    expect(screen.getAllByText('boundary row')).toHaveLength(2);
    expect(screen.getByText('first page')).toBeInTheDocument();
    expect(screen.getByText('second page')).toBeInTheDocument();
  });

  it('chat X button closes the chat section (selected cleared), search stays open', async () => {
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
            messages: [{ platform: 'twitch', video_id: 'v1', offset_sec: 42, username: 'alice', text: 'twitch row' }],
            truncated: false,
            platforms: ['twitch'],
            next_offsets: { twitch: 42 },
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
    const onClose = vi.fn();
    render(<ArchiveSearchPopup zIndex={10} onClose={onClose} onOpenHit={() => {}} />);
    const input = screen.getByPlaceholderText('SEARCH TRANSCRIPTS + CHAT...');
    fireEvent.change(input, { target: { value: 'zebra' } });
    await waitFor(() => expect(searchUrlWith(fetchMock, 'q=zebra')).toBeTruthy());
    fireEvent.click(await screen.findByRole('button', { name: /zebra stripes/i }));
    await screen.findByText('twitch row');
    // The chat header X clears only the chat section — the popup survives.
    fireEvent.click(screen.getByTitle('Close'));
    expect(screen.queryByText('Chat from hit')).toBeNull();
    expect(screen.queryByText('twitch row')).toBeNull();
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByLabelText('Archive search')).toBeInTheDocument();
  });
});

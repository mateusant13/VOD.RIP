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

function mockFetch(hits: unknown[] = []) {
  const fn = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes('/api/archive/search')) {
      return new Response(JSON.stringify({ hits }), {
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
    expect(labels).toEqual(
      expect.arrayContaining(['srdogg', 'srdogg / srdoglol', 'srdoglol']),
    );
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
});

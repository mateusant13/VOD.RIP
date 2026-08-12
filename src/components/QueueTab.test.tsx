import { describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import QueueTab, { type ArchiveJobRow } from './QueueTab';
import type { DownloadState } from '../types';

const DL = (over: Partial<DownloadState> = {}): DownloadState => ({
  download_id: 'dl-1',
  url: 'https://www.twitch.tv/videos/1',
  type: 'vod',
  platform: 'twitch',
  status: 'Downloading',
  progress: 50,
  output_file: 'C:\\VODs\\A.mp4',
  error: null,
  started_at: '2026-08-01T00:00:00Z',
  title: 'VOD A',
  thumbnail: 'https://example.com/thumb.jpg',
  ...over,
});

function renderTab(over: { queue?: DownloadState[]; history?: DownloadState[] } = {}) {
  const handlers = {
    onPause: vi.fn(),
    onResume: vi.fn(),
    onCancel: vi.fn(),
    onDelete: vi.fn(),
    onDeleteHistory: vi.fn(),
    onOpenFolder: vi.fn(),
    onRefresh: vi.fn(),
  };
  const { unmount } = render(
    <QueueTab
      queueDownloads={over.queue ?? [DL()]}
      historyDownloads={over.history ?? [DL({ download_id: 'dl-2', title: 'VOD B', status: 'Completed' })]}
      {...handlers}
      basename={(p) => p.split('\\').pop() ?? p}
    />,
  );
  return { ...handlers, unmount };
}

/** One GET /api/archive/jobs row. */
const JOB = (over: Partial<ArchiveJobRow> = {}): ArchiveJobRow => ({
  id: 'tr-1',
  kind: 'transcribe',
  platform: 'twitch',
  video_id: '1001',
  status: 'running',
  progress: 0.42,
  error: null,
  created_at: '2026-08-08T00:00:00Z',
  updated_at: '2026-08-08T00:00:01Z',
  heartbeat: '2026-08-08T00:00:01Z',
  title: 'My VOD',
  ...over,
});

/** Stub fetch: jobs endpoint serves `jobs` (or 500); everything else is the
 *  Twitch clip history (bare array — apiGet shape). */
function mockJobsFetch(jobs: ArchiveJobRow[], status = 200) {
  const fn = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes('/api/archive/jobs')) {
      return new Response(JSON.stringify(status >= 400 ? { detail: 'boom' } : { jobs }), {
        status,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    return new Response(JSON.stringify([]), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  });
  vi.stubGlobal('fetch', fn);
  return fn;
}

function jobsFetchCalls(fn: ReturnType<typeof vi.fn>) {
  return fn.mock.calls.filter((c) => String(c[0]).includes('/api/archive/jobs'));
}


function openNotifications() {
  fireEvent.click(screen.getByRole('button', { name: 'Notifications' }));
}
describe('QueueTab', () => {
  it('renders queue and history rows with enlarged (w-20 h-12) thumbnails', () => {
    renderTab();
    expect(screen.getAllByText('History').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('VOD A')).toBeInTheDocument();
    expect(screen.getByText('VOD B')).toBeInTheDocument();
    const thumbs = document.querySelectorAll('img');
    expect(thumbs).toHaveLength(2);
    for (const t of thumbs) {
      expect(t.className).toContain('w-20 h-12');
    }
  });

  it('history row delete fires onDeleteHistory with the row id', () => {
    const handlers = renderTab();
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    expect(handlers.onDeleteHistory).toHaveBeenCalledWith('dl-2');
  });

  it('history row Folder button opens the completed file', () => {
    const handlers = renderTab();
    const folders = screen.getAllByRole('button', { name: /Folder/i });
    fireEvent.click(folders[folders.length - 1]);
    expect(handlers.onOpenFolder).toHaveBeenCalledWith('C:\\VODs\\A.mp4');
  });

  it('clicking a history row title opens that VOD via onOpenVod', () => {
    const onOpenVod = vi.fn();
    render(
      <QueueTab
        queueDownloads={[]}
        historyDownloads={[DL({ download_id: 'dl-7', title: 'VOD H', url: 'https://www.twitch.tv/videos/7', status: 'Completed' })]}
        onPause={() => {}}
        onResume={() => {}}
        onCancel={() => {}}
        onDelete={() => {}}
        onDeleteHistory={() => {}}
        onOpenFolder={() => {}}
        onRefresh={() => {}}
        basename={(p) => p}
        onOpenVod={onOpenVod}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'VOD H' }));
    expect(onOpenVod).toHaveBeenCalledWith('https://www.twitch.tv/videos/7', {
      title: 'VOD H',
      skipNetwork: true,
    });
  });

  it('playable history row offers a watch button that fires onWatchLocal', () => {
    const onWatchLocal = vi.fn();
    render(
      <QueueTab
        queueDownloads={[]}
        historyDownloads={[DL({ download_id: 'dl-2', title: 'VOD B', status: 'Completed' })]}
        onPause={() => {}}
        onResume={() => {}}
        onCancel={() => {}}
        onDelete={() => {}}
        onDeleteHistory={() => {}}
        onOpenFolder={() => {}}
        onRefresh={() => {}}
        basename={(p) => p}
        onWatchLocal={onWatchLocal}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Play downloaded file' }));
    expect(onWatchLocal).toHaveBeenCalledTimes(1);
  });

  it('has no inner scroll containers — the page scrolls as one', () => {
    renderTab();
    expect(
      document.querySelectorAll('[class*="overflow-y-auto"], [class*="custom-scrollbar"], [class*="max-h-["]'),
    ).toHaveLength(0);
  });

  it('shows empty states for an empty queue and empty history', () => {
    renderTab({ queue: [], history: [] });
    expect(screen.getByText('NO DOWNLOADS IN QUEUE.')).toBeInTheDocument();
    expect(screen.getByText('NO COMPLETED DOWNLOADS YET.')).toBeInTheDocument();
  });

  it('polls /api/archive/jobs and renders running transcribe/chat jobs with progress', async () => {
    vi.useFakeTimers();
    try {
      const fn = mockJobsFetch([
        JOB(),
        JOB({ id: 'ch-1', kind: 'chat', status: 'queued', progress: 0, title: '' }),
      ]);
      renderTab({ queue: [], history: [] });
      openNotifications();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      // First poll lands: kind labels, status, platform icon + title.
      expect(String(jobsFetchCalls(fn)[0][0])).toContain('/api/archive/jobs');
      expect(screen.getAllByText('Transcription').length).toBeGreaterThan(0);
      expect(screen.getByText('My VOD')).toBeTruthy();
      expect(screen.getByText('Running')).toBeTruthy();
      expect(screen.getAllByText('Chat backfill').length).toBeGreaterThan(0);
      expect(screen.getByText('Queued')).toBeTruthy();
      // Transcribe jobs carry a progress bar + %; chat jobs do not.
      const bar = screen.getByRole('progressbar');
      expect(bar.getAttribute('aria-valuenow')).toBe('42');
      expect(screen.getByText('42%')).toBeTruthy();
      expect(screen.queryByText('0%')).toBeNull();
      // Polls again after 3s while the tab is open.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(3000);
      });
      expect(jobsFetchCalls(fn).length).toBeGreaterThanOrEqual(2);
    } finally {
      vi.useRealTimers();
      vi.unstubAllGlobals();
    }
  });

  it('hides done jobs by default; the toggle reveals them', async () => {
    vi.useFakeTimers();
    try {
      mockJobsFetch([
        JOB({ id: 'tr-done', status: 'done', progress: 1 }),
        JOB({ id: 'tr-run', status: 'running', progress: 0.2 }),
      ]);
      renderTab({ queue: [], history: [] });
      openNotifications();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      fireEvent.click(screen.getByRole('button', { name: 'In progress' }));
      expect(screen.queryByText('Done')).toBeNull();
      fireEvent.click(screen.getByRole('button', { name: 'Completed' }));
      expect(screen.getByText('Done')).toBeTruthy();
    } finally {
      vi.useRealTimers();
      vi.unstubAllGlobals();
    }
  });

  it('survives a 500 from the jobs endpoint and retries on the next tick', async () => {
    vi.useFakeTimers();
    try {
      const fn = mockJobsFetch([], 500);
      renderTab({ queue: [], history: [] });
      openNotifications();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(screen.getByText('No background jobs match these filters')).toBeTruthy();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(3000);
      });
      expect(jobsFetchCalls(fn).length).toBeGreaterThanOrEqual(2);
    } finally {
      vi.useRealTimers();
      vi.unstubAllGlobals();
    }
  });

  it('stops polling after unmount', async () => {
    vi.useFakeTimers();
    try {
      const fn = mockJobsFetch([JOB()]);
      const { unmount } = renderTab({ queue: [], history: [] });
      // Polling lives in the Notifications tab — open it first.
      openNotifications();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(jobsFetchCalls(fn).length).toBe(1);
      unmount();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(9000);
      });
      expect(jobsFetchCalls(fn).length).toBe(1);
    } finally {
      vi.useRealTimers();
      vi.unstubAllGlobals();
    }
  });

  it('Clear notifications clears done/failed jobs and refetches', async () => {
    vi.useFakeTimers();
    try {
      let cleared = false;
      const fn = vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/api/archive/jobs/clear')) {
          cleared = true;
          return new Response(JSON.stringify({ ok: true, cleared: 2 }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        if (url.includes('/api/archive/jobs')) {
          const jobs = cleared
            ? [JOB({ id: 'tr-run', status: 'running', progress: 0.2 })]
            : [
                JOB({ id: 'tr-done', status: 'done', progress: 1 }),
                JOB({ id: 'tr-fail', status: 'failed', error: 'archive-file-missing' }),
                JOB({ id: 'tr-run', status: 'running', progress: 0.2 }),
              ];
          return new Response(JSON.stringify({ jobs }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        return new Response(JSON.stringify([]), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      });
      vi.stubGlobal('fetch', fn);
      renderTab({ queue: [], history: [] });
      openNotifications();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      const clearBtn = screen.getByRole('button', { name: 'Clear notifications' });
      expect(clearBtn).not.toBeDisabled();
      fireEvent.click(clearBtn);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      // The clear POST fired, then a refetch re-synced the panel.
      expect(fn.mock.calls.some((c) => String(c[0]).includes('/api/archive/jobs/clear'))).toBe(true);
      expect(screen.queryByText('Done')).toBeNull();
      expect(screen.getByText('Running')).toBeTruthy();
      // Nothing finished left → the button disables.
      expect(screen.getByRole('button', { name: 'Clear notifications' })).toBeDisabled();
    } finally {
      vi.useRealTimers();
      vi.unstubAllGlobals();
    }
  });

  it('history row with a local thumbnail path renders the local-media URL', () => {
    render(
      <QueueTab
        queueDownloads={[]}
        historyDownloads={[DL({
          download_id: 'dl-thumb',
          title: 'VOD Thumb',
          status: 'Completed',
          thumbnail: 'C:\\VODs\\clip.thumb.jpg',
        })]}
        onPause={() => {}}
        onResume={() => {}}
        onCancel={() => {}}
        onDelete={() => {}}
        onDeleteHistory={() => {}}
        onOpenFolder={() => {}}
        onRefresh={() => {}}
        basename={(p) => p}
      />,
    );
    const img = document.querySelector('img') as HTMLImageElement;
    expect(img).not.toBeNull();
    expect(img.src).toContain('/api/local/media?path=');
    expect(img.src).toContain(encodeURIComponent('C:\\VODs\\clip.thumb.jpg'));
  });
});

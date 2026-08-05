import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import QueueTab from './QueueTab';
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
  render(
    <QueueTab
      queueDownloads={over.queue ?? [DL()]}
      historyDownloads={over.history ?? [DL({ download_id: 'dl-2', title: 'VOD B', status: 'Completed' })]}
      {...handlers}
      basename={(p) => p.split('\\').pop() ?? p}
    />,
  );
  return handlers;
}

describe('QueueTab', () => {
  it('renders queue and history rows with enlarged (w-20 h-12) thumbnails', () => {
    renderTab();
    expect(screen.getByText('Queue')).toBeInTheDocument();
    expect(screen.getByText('History')).toBeInTheDocument();
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
});

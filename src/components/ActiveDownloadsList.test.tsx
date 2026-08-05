import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { ActiveDownloadsList, type ActiveDownloadRow } from './ActiveDownloadsList';

const QUEUE: ActiveDownloadRow[] = [
  {
    download_id: 'dl-1',
    url: 'https://www.twitch.tv/videos/1',
    platform: 'twitch',
    status: 'Downloading',
    progress: 42,
    output_file: 'C:\\VODs\\srdogg - 2026-08-01.mp4',
    title: 'VOD A',
    thumbnail: 'https://example.com/thumb1.jpg',
  },
  {
    download_id: 'dl-2',
    url: 'https://kick.com/videos/2',
    platform: 'kick',
    status: 'Failed',
    progress: 10,
    output_file: 'C:\\VODs\\VOD B.mp4',
    title: 'VOD B',
    thumbnail: 'https://example.com/thumb2.jpg',
  },
];

function makeProps() {
  return {
    onPause: vi.fn(),
    onResume: vi.fn(),
    onCancel: vi.fn(),
    onDelete: vi.fn(),
    onOpenFolder: vi.fn(),
    basename: (p: string) => p.split('\\').pop() ?? p,
    platformIcon: (platform: string) => <span>{platform}</span>,
  };
}

describe('ActiveDownloadsList', () => {
  it('renders rows with title, progress badge, and an enlarged (w-20 h-12) thumbnail', () => {
    render(<ActiveDownloadsList downloads={QUEUE} {...makeProps()} />);
    expect(screen.getByText('VOD A')).toBeInTheDocument();
    expect(screen.getByText('VOD B')).toBeInTheDocument();
    expect(screen.getByText('42%')).toBeInTheDocument();
    expect(screen.getByText('Failed · 10%')).toBeInTheDocument();
    const thumbs = document.querySelectorAll('img');
    expect(thumbs).toHaveLength(2);
    for (const t of thumbs) {
      expect(t.className).toContain('w-20 h-12');
    }
  });

  it('shows the empty state when there are no downloads', () => {
    render(<ActiveDownloadsList downloads={[]} {...makeProps()} />);
    expect(screen.getByText('NO DOWNLOADS IN QUEUE.')).toBeInTheDocument();
  });

  it('active row pauses; failed row resumes and deletes', () => {
    const props = makeProps();
    render(<ActiveDownloadsList downloads={QUEUE} {...props} />);
    fireEvent.click(screen.getByRole('button', { name: 'Pause' }));
    expect(props.onPause).toHaveBeenCalledWith('dl-1');
    fireEvent.click(screen.getByRole('button', { name: 'Resume' }));
    expect(props.onResume).toHaveBeenCalledWith('dl-2');
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    expect(props.onDelete).toHaveBeenCalledWith('dl-2');
  });

  it('Folder button opens the output file location', () => {
    const props = makeProps();
    render(<ActiveDownloadsList downloads={QUEUE} {...props} />);
    fireEvent.click(screen.getAllByRole('button', { name: /Folder/i })[0]);
    expect(props.onOpenFolder).toHaveBeenCalledWith('C:\\VODs\\srdogg - 2026-08-01.mp4');
  });
});

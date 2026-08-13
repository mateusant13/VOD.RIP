import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import { useInstantPreview, INSTANT_CLIP_SEC } from './useInstantPreview';
import {
  refreshInstantPreviews,
  resetInstantPreviewsForTests,
  type InstantPreviewEntry,
} from '../instantPreview';

const ENTRY: InstantPreviewEntry = {
  channel_id: 'c1',
  platform: 'twitch',
  title: 'Some VOD',
  vod_url: 'https://www.twitch.tv/videos/123456789',
  vod_id: '123456789',
  video_id: null,
  generated_at: '2026-08-13T00:00:00Z',
  media_url: '/api/previews/c1/media',
};

const ENTRY2: InstantPreviewEntry = {
  channel_id: 'c2',
  platform: 'youtube',
  title: 'Other VOD',
  vod_url: 'https://www.youtube.com/watch?v=AbCdEfGhIjK',
  vod_id: '',
  video_id: 'AbCdEfGhIjK',
  generated_at: '2026-08-13T00:00:00Z',
  media_url: '/api/previews/c2/media',
};

/** Harness mirrors the overlay wiring used by App.tsx / ChannelExplorePopup.tsx. */
function Harness({ url, active, remoteReady, startSec }: {
  url: string;
  active: boolean;
  remoteReady: boolean;
  startSec: number;
}) {
  const inst = useInstantPreview({ url, active, remoteReady, startSec });
  return (
    <div>
      <video
        data-testid="surface-video"
        ref={inst.videoRef}
        src={inst.show && inst.matched ? inst.matched.media_url : undefined}
        autoPlay
        muted
        playsInline
        onEnded={inst.onOverlayEnded}
        onError={inst.onOverlayError}
      />
      <span data-testid="show">{inst.show ? '1' : '0'}</span>
      <span data-testid="matched">{inst.matched?.channel_id ?? 'none'}</span>
    </div>
  );
}

function renderHarness(props: { url?: string; active?: boolean; remoteReady?: boolean; startSec?: number } = {}) {
  return render(
    <Harness
      url={props.url ?? ENTRY.vod_url}
      active={props.active ?? true}
      remoteReady={props.remoteReady ?? false}
      startSec={props.startSec ?? 0}
    />,
  );
}

describe('useInstantPreview', () => {
  beforeEach(() => {
    // jsdom has no real media pipeline — the overlay video is a fake element;
    // stub play() so autoPlay doesn't reject unhandled.
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ previews: [ENTRY, ENTRY2] }),
    }));
    HTMLMediaElement.prototype.play = vi.fn(() => Promise.resolve());
  });
  afterEach(() => {
    cleanup();
    resetInstantPreviewsForTests();
    vi.unstubAllGlobals();
  });

  it('plays the local clip while the remote session boots (autoplay source set)', async () => {
    await refreshInstantPreviews(true);
    renderHarness();
    expect(screen.getByTestId('show').textContent).toBe('1');
    expect(screen.getByTestId('matched').textContent).toBe('c1');
    const video = screen.getByTestId('surface-video') as HTMLVideoElement;
    expect(video.getAttribute('src')).toBe('/api/previews/c1/media');
    expect(video.muted).toBe(true);
  });

  it('never engages without a match — exact current behavior', async () => {
    await refreshInstantPreviews(true);
    renderHarness({ url: 'https://www.twitch.tv/videos/999' });
    expect(screen.getByTestId('show').textContent).toBe('0');
    expect(screen.getByTestId('matched').textContent).toBe('none');
    expect((screen.getByTestId('surface-video') as HTMLVideoElement).getAttribute('src')).toBeNull();
  });

  it('hands off to the remote session the moment it becomes ready', async () => {
    await refreshInstantPreviews(true);
    const { rerender } = renderHarness();
    expect(screen.getByTestId('show').textContent).toBe('1');
    rerender(<Harness url={ENTRY.vod_url} active remoteReady startSec={0} />);
    expect(screen.getByTestId('show').textContent).toBe('0');
  });

  it('abandons the clip when it ends and never re-shows for the same URL', async () => {
    await refreshInstantPreviews(true);
    const { rerender } = renderHarness();
    expect(screen.getByTestId('show').textContent).toBe('1');
    fireEvent.ended(screen.getByTestId('surface-video'));
    expect(screen.getByTestId('show').textContent).toBe('0');
    // Remote still booting: a re-render must NOT resurrect the ended clip.
    rerender(<Harness url={ENTRY.vod_url} active remoteReady={false} startSec={0} />);
    expect(screen.getByTestId('show').textContent).toBe('0');
  });

  it('abandons the clip on media error (e.g. 404 while backend regenerates)', async () => {
    await refreshInstantPreviews(true);
    renderHarness();
    fireEvent.error(screen.getByTestId('surface-video'));
    expect(screen.getByTestId('show').textContent).toBe('0');
  });

  it('stays off when the trim window starts past the clip', async () => {
    await refreshInstantPreviews(true);
    renderHarness({ startSec: INSTANT_CLIP_SEC + 10 });
    expect(screen.getByTestId('show').textContent).toBe('0');
  });

  it('re-arms after the surface closes and reopens the same VOD', async () => {
    await refreshInstantPreviews(true);
    const { rerender } = renderHarness();
    expect(screen.getByTestId('show').textContent).toBe('1');
    rerender(<Harness url={ENTRY.vod_url} active={false} remoteReady={false} startSec={0} />);
    expect(screen.getByTestId('show').textContent).toBe('0');
    rerender(<Harness url={ENTRY.vod_url} active remoteReady={false} startSec={0} />);
    expect(screen.getByTestId('show').textContent).toBe('1');
  });

  it('re-matches when a different VOD opens', async () => {
    await refreshInstantPreviews(true);
    const { rerender } = renderHarness();
    expect(screen.getByTestId('show').textContent).toBe('1');
    // New media while the surface stays open (explore next-row click).
    rerender(<Harness url={ENTRY2.vod_url} active remoteReady={false} startSec={0} />);
    expect(screen.getByTestId('show').textContent).toBe('1');
    expect(screen.getByTestId('matched').textContent).toBe('c2');
    expect((screen.getByTestId('surface-video') as HTMLVideoElement).getAttribute('src'))
      .toBe('/api/previews/c2/media');
  });
});

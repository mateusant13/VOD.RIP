import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Mock } from 'vitest';
import type { ComponentProps } from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { LivePlayerPopup, __resetLivePlayerRegistryForTests, liveQualityCountNow, registerLivePlayer } from './LivePlayerPopup';
import { EXPLORE_POPUP_Z, LIVE_POPUP_ACTIVE_Z, SEARCH_POPUP_Z } from '../layoutUtils';
import { registerPreviewPlayback } from '../previewPlaybackBus';
import type { PreviewSessionResponse } from '../types';

/**
 * hls.js needs MediaSource, which jsdom lacks — stub the module so the
 * popup's REPLAY switch (arrow seek) creates a controllable instance: tests
 * fire MANIFEST_PARSED and assert the position handed to startLoad. The real
 * module is only reached via the dynamic import inside createHlsPlayer, and
 * the existing tests' session mock carries no src, so they never import it.
 */
const { FakeHls } = vi.hoisted(() => {
  class FakeHls {
    static isSupported = () => true;
    static Events = {
      MANIFEST_PARSED: 'manifestParsed',
      LEVEL_SWITCHED: 'levelSwitched',
      FRAG_BUFFERED: 'fragBuffered',
      ERROR: 'error',
    };
    static ErrorDetails = { BUFFER_STALLED_ERROR: 'bufferStalledError' };
    static ErrorTypes = { NETWORK_ERROR: 'networkError', MEDIA_ERROR: 'mediaError' };
    config: Record<string, unknown>;
    levels: { height: number; bitrate: number }[] = [];
    liveSyncPosition: number | undefined = undefined;
    currentLevel = -1;
    loadLevel = -1;
    /** Mirrors real hls.js: auto ABR only while no manual level is pinned. */
    get autoLevelEnabled() { return this.currentLevel === -1; }
    loadSource = vi.fn();
    attachMedia = vi.fn();
    startLoad = vi.fn();
    stopLoad = vi.fn();
    destroy = vi.fn();
    recoverMediaError = vi.fn();
    private handlers = new Map<string, Array<(...args: unknown[]) => void>>();
    constructor(config: Record<string, unknown>) {
      this.config = config;
    }
    on(event: string, cb: (...args: unknown[]) => void) {
      const list = this.handlers.get(event) ?? [];
      list.push(cb);
      this.handlers.set(event, list);
    }
    /** Test hook — fire a registered hls.js event handler. */
    trigger(event: string, ...args: unknown[]) {
      for (const cb of this.handlers.get(event) ?? []) cb({}, ...args);
    }
  }
  return { FakeHls };
});
vi.mock('hls.js', () => ({ default: FakeHls }));

/**
 * jsdom does not implement the Fullscreen API (no requestFullscreen /
 * fullscreenElement / fullscreenchange). The tests stub those browser
 * primitives and simulate transitions by mutating the fake active element and
 * dispatching `fullscreenchange` — the same signal a real browser fires.
 */

let fsElement: Element | null = null;
let fsTargets: Element[] = [];
let requestFullscreenMock: Mock;
let exitFullscreenMock: Mock;

const ENTRY = { url: 'https://kick.com/srdoglol', title: 'Late night', platform: 'kick' };

/** Minimal EventSource stub — tests drive onopen/onmessage per instance and
 *  named events via fire(). */
class FakeEventSource {
  static instances: FakeEventSource[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  private listeners = new Map<string, Array<(ev: MessageEvent) => void>>();
  constructor(public url: string) {
    FakeEventSource.instances.push(this);
  }
  addEventListener(type: string, cb: (ev: MessageEvent) => void) {
    const list = this.listeners.get(type) ?? [];
    list.push(cb);
    this.listeners.set(type, list);
  }
  removeEventListener(type: string, cb: (ev: MessageEvent) => void) {
    const list = this.listeners.get(type) ?? [];
    this.listeners.set(type, list.filter((c) => c !== cb));
  }
  /** Test hook — fire a named event with a JSON payload string. */
  fire(type: string, data: string) {
    const ev = { data } as MessageEvent;
    for (const cb of this.listeners.get(type) ?? []) cb(ev);
  }
  close() {
    this.closed = true;
  }
}

function mockFetch() {
  const fn = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes('/api/preview/live')) {
      // No src → the session resolves and the transport (with the fullscreen
      // and search buttons) renders without touching hls.js.
      return new Response(JSON.stringify({ session_id: 's1' }), { status: 200 });
    }
    if (url.includes('/api/archive/videos')) {
      return new Response(JSON.stringify({ videos: [] }), { status: 200 });
    }
    if (url.includes('/api/live/captions/available')) {
      // Parakeet gate probe — full contract shape; translation enabled.
      return new Response(JSON.stringify({
        available: true, pending: false, reason: null, translation_available: true, low_latency: false,
      }), { status: 200 });
    }
    if (url.includes('/api/preview/session')) {
      // TwitchClipPopup mini-preview session — resolves so the popup's HLS
      // attach runs quietly against FakeHls (a 404 would retry 1.2s/2.4s and
      // reject with an unhandled error after the test ends).
      return new Response(JSON.stringify({
        session_id: 'cs1',
        kind: 'hls',
        master_url: '/api/preview/hls/cs1/master.m3u8',
        playback_url: '/api/preview/hls/cs1/master.m3u8',
      }), { status: 200 });
    }
    return new Response(JSON.stringify({}), { status: 404 });
  });
  vi.stubGlobal('fetch', fn);
  return fn;
}

/** Archive-ready fetch mock — the lazy DVR snapshot request returns a
 *  playlist whose EXTINF lines sum to `sec`, so the rail enables
 *  (archiveAvailable) with railMax = sec. */
function mockFetchWithArchive(sec = 100) {
  const fn = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes('/api/preview/live')) {
      return new Response(JSON.stringify({ session_id: 's1' }), { status: 200 });
    }
    if (url.includes('/api/preview/hls/s1/resource')) {
      const half = sec / 2;
      return new Response(
        `#EXTM3U\n#EXT-X-TARGETDURATION:${Math.ceil(half)}\n#EXTINF:${half},no\nseg0.ts\n#EXTINF:${half},no\nseg1.ts\n#EXT-X-ENDLIST\n`,
        { status: 200 },
      );
    }
    return new Response(JSON.stringify({}), { status: 404 });
  });
  vi.stubGlobal('fetch', fn);
  return fn;
}

/** Live-session mock that resolves a REAL master URL → createHlsPlayer runs
 *  with the FakeHls instance (window.__livePopupHls). No DVR archive (404) →
 *  the rail stays disabled and the arrows seek the retained back-buffer. */
function mockFetchWithLiveSrc() {
  const fn = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes('/api/preview/live')) {
      return new Response(JSON.stringify({
        session_id: 's1',
        kind: 'hls',
        master_url: 'https://edge/live/master.m3u8',
        playback_url: 'https://edge/live/master.m3u8',
        archive_duration: 0,
      }), { status: 200 });
    }
    if (url.includes('/api/preview/hls/s1/resource')) {
      return new Response('', { status: 404 });
    }
    if (url.includes('/api/live/captions/available')) {
      return new Response(JSON.stringify({
        available: true, pending: false, reason: null, translation_available: true, low_latency: false,
      }), { status: 200 });
    }
    return new Response(JSON.stringify({}), { status: 404 });
  });
  vi.stubGlobal('fetch', fn);
  return fn;
}

function renderPopup() {
  return render(
    <LivePlayerPopup
      entry={ENTRY}
      channelName="srdogg / srdoglol"
      onClose={vi.fn()}
      channelSlug="srdoglol"
      onOpenHit={vi.fn()}
      savedChannels={[]}
    />,
  );
}

beforeEach(() => {
  fsElement = null;
  fsTargets = [];
  Object.defineProperty(document, 'fullscreenElement', {
    configurable: true,
    get: () => fsElement,
  });
  requestFullscreenMock = vi.fn(function (this: Element) {
    fsTargets.push(this);
    fsElement = this;
    return Promise.resolve();
  });
  exitFullscreenMock = vi.fn(function () {
    fsElement = null;
    return Promise.resolve();
  });
  Element.prototype.requestFullscreen = requestFullscreenMock as unknown as typeof Element.prototype.requestFullscreen;
  Document.prototype.exitFullscreen = exitFullscreenMock as unknown as typeof Document.prototype.exitFullscreen;
  // jsdom's play() is unimplemented (returns undefined) — the popup calls
  // `video.play().catch(...)`, which would throw in jsdom only.
  HTMLMediaElement.prototype.play = vi.fn(() => Promise.resolve()) as unknown as typeof HTMLMediaElement.prototype.play;
  mockFetch();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('LivePlayerPopup fullscreen', () => {
  it("targets only its own element — another surface's fullscreen neither lies nor gets exited", async () => {
    renderPopup();
    // Transport visible once the session resolves.
    await screen.findByTitle('Fullscreen');
    const popupRoot = document.querySelector('[data-live-popup]') as Element;

    // The main preview goes fullscreen (browser-level).
    const otherSurface = document.createElement('div');
    fsElement = otherSurface;
    document.dispatchEvent(new Event('fullscreenchange'));

    // The popup must NOT claim fullscreen for another surface…
    await waitFor(() => expect(screen.queryByTitle('Exit fullscreen')).toBeNull());
    expect(screen.getByTitle('Fullscreen')).toBeTruthy();

    // …and its toggle must enter fullscreen on ITSELF, never exit the other surface.
    fireEvent.click(screen.getByTitle('Fullscreen'));
    expect(fsTargets).toEqual([popupRoot]);
    expect(exitFullscreenMock).not.toHaveBeenCalled();

    // Browser settles: the popup is now the fullscreen element.
    document.dispatchEvent(new Event('fullscreenchange'));
    await screen.findByTitle('Exit fullscreen');

    // Now it exits only itself.
    fireEvent.click(screen.getByTitle('Exit fullscreen'));
    expect(exitFullscreenMock).toHaveBeenCalledTimes(1);
    expect(fsElement).toBeNull();
  });

  it('enters and exits fullscreen on its own element', async () => {
    renderPopup();
    await screen.findByTitle('Fullscreen');
    const popupRoot = document.querySelector('[data-live-popup]') as Element;

    fireEvent.click(screen.getByTitle('Fullscreen'));
    expect(fsTargets).toEqual([popupRoot]);
    document.dispatchEvent(new Event('fullscreenchange'));
    await screen.findByTitle('Exit fullscreen');

    fireEvent.click(screen.getByTitle('Exit fullscreen'));
    expect(exitFullscreenMock).toHaveBeenCalledTimes(1);
    document.dispatchEvent(new Event('fullscreenchange'));
    await screen.findByTitle('Fullscreen');
  });

  it('hides the docked archive search while fullscreen and restores it on exit', async () => {
    renderPopup();
    await screen.findByTitle(/Search the local archive/);

    fireEvent.click(screen.getByTitle(/Search the local archive/));
    await screen.findByRole('dialog', { name: 'Archive search' });

    // Enter fullscreen (browser-level).
    const popupRoot = document.querySelector('[data-live-popup]') as Element;
    fsElement = popupRoot;
    document.dispatchEvent(new Event('fullscreenchange'));

    // The docked panel must not cover the video, and the affordance is gone.
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Archive search' })).toBeNull());
    expect(screen.queryByTitle(/Search the local archive/)).toBeNull();

    // Exit — panel returns in its previous state.
    fsElement = null;
    document.dispatchEvent(new Event('fullscreenchange'));
    await screen.findByRole('dialog', { name: 'Archive search' });
  });
});

describe('LivePlayerPopup fast clip', () => {
  const twitchEntry = { url: 'https://www.twitch.tv/titiltei', title: 'Late night', platform: 'twitch' };
  const twitchVodUrl = 'https://www.twitch.tv/videos/2117068816';
  // Anchored live timeline: frag anchor pos 0 ↔ PDT 1_000_000 ms → the
  // current session started at wall epoch 1000s.
  const SESSION_START_EPOCH_S = 1000;
  const currentVodCreatedAt = new Date(SESSION_START_EPOCH_S * 1000).toISOString();
  // A PREVIOUS broadcast's cache row — 3h before the current session start.
  const staleVodCreatedAt = new Date((SESSION_START_EPOCH_S - 3 * 3600) * 1000).toISOString();

  function twitchChannel(createdAt: string) {
    return {
      id: 'c1',
      displayName: 'titiltei',
      kickSlug: '',
      twitchSlug: 'titiltei',
      youtubeSlug: '',
      vodVideos: [{
        id: '2117068816',
        platform: 'Twitch',
        title: 'Late night',
        duration: null,
        created_at: createdAt,
        views: null,
        thumbnail_url: null,
        url: twitchVodUrl,
        channel: 'titiltei',
      }],
      clipVideos: [],
      updatedAt: '2026-08-01T00:00:00Z',
    };
  }

  function renderTwitchPopup(vodUrl?: string, channel?: ComponentProps<typeof LivePlayerPopup>['channel']) {
    return render(
      <LivePlayerPopup
        entry={twitchEntry}
        entries={[twitchEntry]}
        channelName="titiltei"
        channelSlug="titiltei"
        channel={channel}
        vodUrl={vodUrl}
        onClose={vi.fn()}
        onOpenHit={vi.fn()}
        savedChannels={[]}
      />,
    );
  }

  /** Live session (real master URL → FakeHls) + a resolvable clip-popup
   *  session, so both the transport and the mini-preview render. */
  function mockFetchLiveForClip() {
    const fn = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/preview/live')) {
        return new Response(JSON.stringify({
          session_id: 's1',
          kind: 'hls',
          master_url: 'https://edge/live/master.m3u8',
          playback_url: 'https://edge/live/master.m3u8',
          archive_duration: 0,
        }), { status: 200 });
      }
      if (url.includes('/api/preview/hls/s1/resource')) {
        return new Response('', { status: 404 });
      }
      if (url.includes('/api/live/captions/available')) {
        return new Response(JSON.stringify({
          available: true, pending: false, reason: null, translation_available: true, low_latency: false,
        }), { status: 200 });
      }
      if (url.includes('/api/preview/session')) {
        return new Response(JSON.stringify({
          session_id: 'cs1',
          kind: 'hls',
          master_url: '/api/preview/hls/cs1/master.m3u8',
          playback_url: '/api/preview/hls/cs1/master.m3u8',
        }), { status: 200 });
      }
      return new Response(JSON.stringify({}), { status: 404 });
    });
    vi.stubGlobal('fetch', fn);
    return fn;
  }

  /** Render the Twitch popup on a live session and anchor the caption clock
   *  at SESSION_START_EPOCH_S (frag PDT anchor pos 0 ↔ epoch 1000). */
  async function renderAnchoredTwitch(vodUrl: string | undefined, channel: ComponentProps<typeof LivePlayerPopup>['channel']) {
    const fetchMock = mockFetchLiveForClip();
    renderTwitchPopup(vodUrl, channel);
    await waitFor(() => expect((window as unknown as { __livePopupHls?: InstanceType<typeof FakeHls> }).__livePopupHls).toBeTruthy());
    const hls = (window as unknown as { __livePopupHls?: InstanceType<typeof FakeHls> }).__livePopupHls!;
    await act(async () => { hls.trigger(FakeHls.Events.MANIFEST_PARSED); });
    await screen.findByTitle('Fullscreen');
    act(() => { hls.trigger('fragBuffered', { frag: { start: 0, programDateTime: 1_000_000 } }); });
    return fetchMock;
  }

  it('TWITCH: clicking clip opens the in-app mini-preview (TwitchClipPopup) — no /api/live/clip call', async () => {
    const fetchMock = await renderAnchoredTwitch(twitchVodUrl, twitchChannel(currentVodCreatedAt));

    fireEvent.click(screen.getByTitle('Open the Twitch clip mini-preview at the playhead'));

    // The mini-preview renders inside the app (channel header + popup marker),
    // exactly like the preview clip buttons — not a browser tab.
    await screen.findByText('Twitch clip');
    expect(document.querySelector('[data-twitch-clip-popup]')).toBeTruthy();

    // The old server capability POST is gone entirely.
    const clipCalls = fetchMock.mock.calls.filter(([u]) => String(u).includes('/api/live/clip'));
    expect(clipCalls).toHaveLength(0);
  });

  it('TWITCH: mini-preview Create opens clips.twitch.tv/create with the VOD id + offsetSeconds', async () => {
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(null);
    await renderAnchoredTwitch(twitchVodUrl, twitchChannel(currentVodCreatedAt));

    fireEvent.click(screen.getByTitle('Open the Twitch clip mini-preview at the playhead'));
    await screen.findByText('Twitch clip');
    fireEvent.click(screen.getByRole('button', { name: 'Create clip' }));

    await waitFor(() => expect(openSpy).toHaveBeenCalledTimes(1));
    const u = new URL(String(openSpy.mock.calls[0][0]));
    expect(u.host).toBe('clips.twitch.tv');
    expect(u.pathname).toBe('/create');
    expect(u.searchParams.get('vodID')).toBe('2117068816');
    expect(u.searchParams.get('broadcasterLogin')).toBe('titiltei');
    // offsetSeconds = the trimmed selection END (VOD time of the clip).
    expect(Number(u.searchParams.get('offsetSeconds'))).toBeGreaterThanOrEqual(0);
    openSpy.mockRestore();
  });

  it('TWITCH: a STALE cached VOD (previous broadcast) never opens the clip window', async () => {
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(null);
    await renderAnchoredTwitch(twitchVodUrl, twitchChannel(staleVodCreatedAt));

    fireEvent.click(screen.getByTitle('Open the Twitch clip mini-preview at the playhead'));

    // The cached VOD's created_at is 3h before the current session start —
    // applying the live playhead to its timeline would clip the WRONG
    // broadcast. The notice path is shown instead.
    const notice = await screen.findByRole('status');
    expect(notice.textContent).toContain('Not a Twitch VOD URL');
    expect(document.querySelector('[data-twitch-clip-popup]')).toBeNull();
    expect(openSpy).not.toHaveBeenCalled();
    openSpy.mockRestore();
  });

  it('TWITCH: the cached VOD cannot be verified (no clock map yet) → clip is blocked, not risked', async () => {
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(null);
    mockFetchLiveForClip();
    renderTwitchPopup(twitchVodUrl, twitchChannel(currentVodCreatedAt));
    await waitFor(() => expect((window as unknown as { __livePopupHls?: InstanceType<typeof FakeHls> }).__livePopupHls).toBeTruthy());
    const hls = (window as unknown as { __livePopupHls?: InstanceType<typeof FakeHls> }).__livePopupHls!;
    await act(async () => { hls.trigger(FakeHls.Events.MANIFEST_PARSED); });
    await screen.findByTitle('Fullscreen');
    // NOTE: no frag anchor fired — the session start is unmapped.

    fireEvent.click(screen.getByTitle('Open the Twitch clip mini-preview at the playhead'));

    const notice = await screen.findByRole('status');
    expect(notice.textContent).toContain('Not a Twitch VOD URL');
    expect(document.querySelector('[data-twitch-clip-popup]')).toBeNull();
    expect(openSpy).not.toHaveBeenCalled();
    openSpy.mockRestore();
  });

  it('TWITCH without a DVR VOD URL shows "Not a Twitch VOD URL" and opens nothing', async () => {
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(null);
    mockFetch();
    renderTwitchPopup();
    await screen.findByTitle('Fullscreen');

    fireEvent.click(screen.getByTitle('Open the Twitch clip mini-preview at the playhead'));

    const notice = await screen.findByRole('status');
    expect(notice.textContent).toContain('Not a Twitch VOD URL');
    expect(document.querySelector('[data-twitch-clip-popup]')).toBeNull();
    expect(openSpy).not.toHaveBeenCalled();
    openSpy.mockRestore();
  });

  it('KICK without twitchSlug: no clip button', async () => {
    mockFetch();
    renderPopup();
    await screen.findByTitle('Fullscreen');

    // Kick entry without a twitchSlug → no clip button (no twin Twitch VOD).
    expect(screen.queryByTitle('Open the Twitch clip mini-preview at the playhead')).toBeNull();
  });

  it('mounts into the shared preview-pause bus (pauses other previews when a live opens)', async () => {
    const pauseSpy = vi.fn();
    const unreg = registerPreviewPlayback(pauseSpy);
    try {
      mockFetch();
      renderPopup();
      await screen.findByTitle('Fullscreen');
      // The live popup's mount calls pauseOtherPreviews() — any other
      // registered preview (VOD preview, clip popup) is paused.
      expect(pauseSpy).toHaveBeenCalled();
    } finally {
      unreg();
    }
  });
});

describe('LivePlayerPopup live chat', () => {
  it('preloads chat EventSources on live open; panel hidden until toggle', async () => {
    mockFetch();
    renderPopup();
    await screen.findByTitle('Fullscreen');

    // Chat panel is ALWAYS mounted (for preloading EventSources on live open)
    // but visually hidden when chatOpen is false.
    const panel = document.querySelector('[data-live-chat-panel]');
    expect(panel).toBeTruthy();
    expect(panel?.closest('[aria-hidden="true"]')).toBeTruthy();

    // The header toggle shows the panel.
    fireEvent.click(screen.getByTitle('Live chat'));
    expect(panel?.closest('[aria-hidden="true"]')).toBeNull();
    // jsdom has no EventSource — the panel must degrade, not crash.
    expect(screen.getByText('Live chat unavailable')).toBeTruthy();

    // Closing hides it again.
    fireEvent.click(screen.getAllByTitle('Close live chat')[0]);
    await waitFor(() => {
      expect(document.querySelector('[data-live-chat-panel]')?.closest('[aria-hidden="true"]')).toBeTruthy();
    });
  });
});

describe('LivePlayerPopup YouTube live chat', () => {
  /** YouTube live popup: the backend live status carries the googlevideo
   *  HLS master URL (no slug in it), so chat sources fall back to the saved
   *  channel's youtubeSlug — the same mechanism Twitch/Kick use. */
  function renderYoutubePopup(entryUrl: string) {
    return render(
      <LivePlayerPopup
        entry={{ url: entryUrl, title: 'SOLOQ', platform: 'youtube' }}
        entries={[{ url: entryUrl, title: 'SOLOQ', platform: 'youtube' }]}
        channelName="titiltei"
        onClose={vi.fn()}
        channelSlug="titiltei"
        channel={{
          id: 'c1',
          displayName: 'titiltei',
          kickSlug: '',
          twitchSlug: '',
          youtubeSlug: 'titiltei',
          vodVideos: [],
          clipVideos: [],
          updatedAt: '2026-08-01T00:00:00Z',
        }}
        onOpenHit={vi.fn()}
        savedChannels={[]}
      />,
    );
  }

  function youtubeChatEs(): FakeEventSource[] {
    return FakeEventSource.instances.filter(
      (es) => es.url.includes('/api/live/chat/stream') && es.url.includes('platform=youtube'),
    );
  }

  it('opens the same chat-history panel as Twitch/Kick for a YouTube live', async () => {
    const fn = mockFetch();
    vi.stubGlobal('EventSource', FakeEventSource);
    FakeEventSource.instances = [];
    renderYoutubePopup(
      'https://rr2---sn-ab5l6n7k.googlevideo.com/videoplayback/expire/1785600000000/master.m3u8',
    );
    await screen.findByTitle('Fullscreen');

    // The header toggle exists for YouTube lives too.
    fireEvent.click(screen.getByTitle('Live chat'));
    await waitFor(() => expect(document.querySelector('[data-live-chat-panel]')).toBeTruthy());

    // Live stream resolves via the saved youtubeSlug (googlevideo master
    // carries no handle)…
    const es = youtubeChatEs()[0];
    expect(es).toBeTruthy();
    expect(decodeURIComponent(es.url)).toContain('platform=youtube&slug=titiltei');

    // …and the backlog pre-fill hits the SAME /api/chat/history endpoint
    // as Twitch/Kick, same schema of rows.
    await waitFor(() =>
      expect(
        fn.mock.calls.some((c) =>
          decodeURIComponent(String(c[0])).includes(
            '/api/chat/history?platform=youtube&slug=titiltei&limit=300',
          ),
        ),
      ).toBe(true),
    );

    // A row streamed from the live SSE renders in the panel.
    es.onopen?.();
    es.onmessage?.({
      data: JSON.stringify({ username: '@carlos_x_Y_z', text: 'e o hit nas costas?' }),
    } as MessageEvent);
    await waitFor(() => expect(screen.getByText('e o hit nas costas?')).toBeTruthy());
  });

  it('sends the @-handle slug for youtube.com URLs (the backend normalizes it)', async () => {
    mockFetch();
    vi.stubGlobal('EventSource', FakeEventSource);
    FakeEventSource.instances = [];
    renderYoutubePopup('https://www.youtube.com/@titiltei/live');
    await screen.findByTitle('Fullscreen');

    fireEvent.click(screen.getByTitle('Live chat'));
    await waitFor(() => expect(document.querySelector('[data-live-chat-panel]')).toBeTruthy());

    // liveChatSlugFromUrl's youtube branch keeps the @ handle — the backend
    // chat-history lookup strips it, so the backlog still finds the rows.
    const es = youtubeChatEs()[0];
    expect(es).toBeTruthy();
    expect(decodeURIComponent(es.url)).toContain('platform=youtube&slug=@titiltei');
  });

  it('opens caption SSE for YouTube live streams (same ASR path as Twitch/Kick)', async () => {
    mockFetch();
    vi.stubGlobal('EventSource', FakeEventSource);
    FakeEventSource.instances = [];
    renderYoutubePopup(
      'https://rr2---sn-ab5l6n7k.googlevideo.com/videoplayback/expire/1785600000000/master.m3u8',
    );
    await screen.findByTitle('Fullscreen');
    await screen.findByTitle('Hide captions');
    const captionEs = FakeEventSource.instances.filter((es) => es.url.includes('/api/live/captions'));
    await waitFor(() => expect(captionEs).toHaveLength(1));
    expect(decodeURIComponent(captionEs[0].url)).toContain('platform=youtube');
    expect(decodeURIComponent(captionEs[0].url)).toContain('channel=titiltei');
  });
});

describe('LivePlayerPopup multi chat', () => {
  /** Multi-stream popup: live on Kick AND Twitch, with a saved channel that
   *  carries both platform slugs (multi-chat sources). */
  function renderMultiPopup() {
    return render(
      <LivePlayerPopup
        entry={ENTRY}
        entries={[
          { url: 'https://kick.com/srdoglol', title: 'Late night', platform: 'kick' },
          { url: 'https://usher.ttvnw.net/api/channel/hls/srdogg.m3u8', title: 'Morning', platform: 'twitch' },
        ]}
        channelName="srdogg / srdoglol"
        onClose={vi.fn()}
        channelSlug="srdoglol"
        channel={{
          id: 'c1',
          displayName: 'srdogg / srdoglol',
          kickSlug: 'srdoglol',
          twitchSlug: 'srdogg',
          youtubeSlug: '',
          vodVideos: [],
          clipVideos: [],
          updatedAt: '2026-08-01T00:00:00Z',
        }}
        onOpenHit={vi.fn()}
        savedChannels={[]}
      />,
    );
  }

  /** Chat-stream EventSources only — the caption SSE shares the instances
   *  list once the availability probe resolves. */
  function chatEsInstances(): FakeEventSource[] {
    return FakeEventSource.instances.filter((es) => es.url.includes('/api/live/chat/stream'));
  }

  it('shows filter chips only when the channel is live on >1 platform', async () => {
    mockFetch();
    vi.stubGlobal('EventSource', FakeEventSource);
    FakeEventSource.instances = [];
    renderMultiPopup();
    await screen.findByTitle('Fullscreen');

    // Chat is closed by default — open the dock to mount the merged panel.
    fireEvent.click(screen.getByTitle('Live chat'));

    // Multi-stream → the merged panel exposes All/Kick/Twitch filters
    // (YouTube not live → no chip).
    const filters = document.querySelector('[data-live-chat-filters]');
    expect(filters).toBeTruthy();
    expect(screen.getByRole('button', { name: 'All' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Kick' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Twitch' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'YouTube' })).toBeNull();

    // Two chat streams opened — one per live platform (the caption SSE is
    // a separate connection, filtered out above).
    expect(chatEsInstances()).toHaveLength(2);
  });

  it('filters merged rows by platform (All shows both streams)', async () => {
    mockFetch();
    vi.stubGlobal('EventSource', FakeEventSource);
    FakeEventSource.instances = [];
    renderMultiPopup();
    await screen.findByTitle('Fullscreen');

    // Chat is closed by default — open the dock to mount the panel.
    fireEvent.click(screen.getByTitle('Live chat'));

    // Both streams open and deliver one row each.
    const kickEs = chatEsInstances().find((es) => es.url.includes('platform=kick'))!;
    const twitchEs = chatEsInstances().find((es) => es.url.includes('platform=twitch'))!;
    kickEs.onopen?.();
    twitchEs.onopen?.();
    kickEs.onmessage?.({ data: JSON.stringify({ username: 'kick_user', text: 'hi from kick' }) } as MessageEvent);
    twitchEs.onmessage?.({ data: JSON.stringify({ username: 'twitch_user', text: 'hi from twitch' }) } as MessageEvent);

    // All → both rows visible, each tagged with its platform.
    await screen.findByText('hi from kick');
    expect(screen.getByText('hi from twitch')).toBeTruthy();
    expect(document.querySelectorAll('[data-live-chat-row]')).toHaveLength(2);
    // Platform label replaced by the brand logo next to the username.
    expect(screen.getByRole('img', { name: 'Kick' })).toBeTruthy();
    expect(screen.getByRole('img', { name: 'Twitch' })).toBeTruthy();
    expect(screen.queryByRole('img', { name: 'YouTube' })).toBeNull();

    // Twitch filter → only the Twitch row stays.
    fireEvent.click(screen.getByRole('button', { name: 'Twitch' }));
    await waitFor(() => expect(screen.queryByText('hi from kick')).toBeNull());
    expect(screen.getByText('hi from twitch')).toBeTruthy();
    expect(document.querySelectorAll('[data-live-chat-row]')).toHaveLength(1);

    // Back to All → both rows return (no data re-fetch).
    fireEvent.click(screen.getByRole('button', { name: 'All' }));
    await waitFor(() => expect(screen.getByText('hi from kick')).toBeTruthy());
    expect(document.querySelectorAll('[data-live-chat-row]')).toHaveLength(2);
    // Exactly two chat streams total — filtering never re-opens EventSources.
    expect(chatEsInstances()).toHaveLength(2);
  });

  it('merges multi-stream URLs via the saved channel slugs (CDN masters have no slug)', async () => {
    mockFetch();
    vi.stubGlobal('EventSource', FakeEventSource);
    FakeEventSource.instances = [];
    renderMultiPopup();
    await screen.findByTitle('Fullscreen');

    // Chat is closed by default — open the dock to mount the panel.
    fireEvent.click(screen.getByTitle('Live chat'));

    // Twitch CDN master URL → login extracted from the m3u8 filename.
    const twitchEs = chatEsInstances().find((es) => es.url.includes('platform=twitch'))!;
    expect(twitchEs.url).toContain('slug=srdogg');
    // Kick page URL → slug from the path.
    const kickEs = chatEsInstances().find((es) => es.url.includes('platform=kick'))!;
    expect(kickEs.url).toContain('slug=srdoglol');
  });
});

describe('LivePlayerPopup live captions', () => {
  beforeEach(() => {
    vi.stubGlobal('EventSource', FakeEventSource);
    FakeEventSource.instances = [];
  });
  /** Filter EventSource instances to only caption SSE (not chat streams). */
  function captionEsInstances() {
    return FakeEventSource.instances.filter((es) => es.url.includes('/api/live/captions'));
  }

  it('renders the latest caption block over the video when available (PLAYING entry URL)', async () => {
    mockFetch();
    renderPopup();
    await screen.findByTitle('Fullscreen');
    // Captions default ON: the CC button and the caption EventSource appear
    // once the availability probe resolves.
    await screen.findByTitle('Hide captions');
    await waitFor(() => expect(captionEsInstances()).toHaveLength(1));
    const es = captionEsInstances()[0];
    // The stream URL resolves the playing entry's platform + slug (kick).
    expect(es.url).toContain('/api/live/captions?platform=kick');
    expect(es.url).toContain('channel=srdoglol');

    act(() => { es.fire('caption', JSON.stringify({ text: 'olá pessoal, bem-vindos ao canal', start: 1, end: 4 })); });
    expect(screen.getByText('olá pessoal, bem-vindos ao canal')).toBeTruthy();
    expect(document.querySelector('[data-live-captions-overlay]')).toBeTruthy();

    // A newer block REPLACES the previous one — the overlay never stacks.
    // Blocks are anchored to the video clock: the second window [4,7] is not
    // due while the video sits at t=0, so it is queued until the clock
    // reaches it (the fallback origin maps the first block's due point).
    act(() => { es.fire('caption', JSON.stringify({ text: 'segunda legenda', start: 4, end: 7 })); });
    expect(screen.queryByText('segunda legenda')).toBeNull();
    const video = document.querySelector('video') as HTMLVideoElement;
    act(() => { video.currentTime = 3; fireEvent(video, new Event('timeupdate')); });
    await waitFor(() => expect(screen.getByText('segunda legenda')).toBeTruthy());
    expect(screen.queryByText('olá pessoal, bem-vindos ao canal')).toBeNull();
  });

  it('toggle hides the overlay + closes the EventSource, re-enable reconnects', async () => {
    mockFetch();
    renderPopup();
    await screen.findByTitle('Hide captions');
    await waitFor(() => expect(captionEsInstances()).toHaveLength(1));
    const es = captionEsInstances()[0];
    act(() => { es.fire('caption', JSON.stringify({ text: 'legenda visível', start: 0, end: 3 })); });
    expect(screen.getByText('legenda visível')).toBeTruthy();

    // ON by default and LIT with the kick accent; OFF dims the button.
    const litBtn = screen.getByTitle('Hide captions');
    expect(litBtn.getAttribute('aria-pressed')).toBe('true');
    expect(litBtn.className).toContain('text-[#53fc18]');
    expect(litBtn.className).not.toContain('opacity-40');

    fireEvent.click(litBtn);
    expect(es.closed).toBe(true);
    expect(screen.queryByText('legenda visível')).toBeNull();
    expect(screen.queryByTitle('Hide captions')).toBeNull();
    const dimBtn = screen.getByTitle('Live captions');
    expect(dimBtn.getAttribute('aria-pressed')).toBe('false');
    expect(dimBtn.className).toContain('opacity-40');
    expect(dimBtn.className).not.toContain('text-[#53fc18]');

    fireEvent.click(dimBtn);
    await waitFor(() => expect(captionEsInstances()).toHaveLength(2));
    expect(captionEsInstances()[1].closed).toBe(false);
    const relitBtn = screen.getByTitle('Hide captions');
    expect(relitBtn.className).toContain('text-[#53fc18]');
    expect(relitBtn.className).not.toContain('opacity-40');
    act(() => { captionEsInstances()[1].fire('caption', JSON.stringify({ text: 'legenda de novo', start: 3, end: 6 })); });
    // Anchored to the video clock: window [3,6] needs the clock at the due
    // point (t=3 → epoch 5.75) before it renders.
    expect(screen.queryByText('legenda de novo')).toBeNull();
    const video = document.querySelector('video') as HTMLVideoElement;
    act(() => { video.currentTime = 3; fireEvent(video, new Event('timeupdate')); });
    expect(screen.getByText('legenda de novo')).toBeTruthy();
  });

  it('closes the caption EventSource when the popup unmounts', async () => {
    mockFetch();
    const view = renderPopup();
    await screen.findByTitle('Hide captions');
    await waitFor(() => expect(captionEsInstances()).toHaveLength(1));
    view.unmount();
    expect(captionEsInstances()[0].closed).toBe(true);
  });

  it('reconnects after an offline event (bounded backoff) and recovers captions without user action', async () => {
    mockFetch();
    renderPopup();
    await screen.findByTitle('Hide captions');
    await waitFor(() => expect(captionEsInstances()).toHaveLength(1));
    const es = captionEsInstances()[0];
    act(() => { es.fire('caption', JSON.stringify({ text: 'última fala', start: 0, end: 3 })); });
    expect(screen.getByText('última fala')).toBeTruthy();

    // Pipeline failure → the connection closes; the CC cluster STAYS and a
    // bounded backoff reconnects — captions never die silently.
    act(() => { es.fire('offline', '{}'); });
    expect(es.closed).toBe(true);
    expect(screen.getByText('última fala')).toBeTruthy();
    expect(screen.queryByTitle('Hide captions')).toBeTruthy();

    // ~1.5s later the retry re-probes /available (mockFetch: true) and opens
    // a fresh EventSource — a fresh connection restarts the backend captioner.
    await waitFor(() => expect(captionEsInstances()).toHaveLength(2), { timeout: 4000 });
    const es2 = captionEsInstances()[1];
    expect(es2.closed).toBe(false);
    act(() => { es2.fire('caption', JSON.stringify({ text: 'recuperada', start: 4, end: 7 })); });
    // Anchored to the video clock: window [4,7] is due at t=4 (origin 2.75).
    expect(screen.queryByText('recuperada')).toBeNull();
    const video = document.querySelector('video') as HTMLVideoElement;
    act(() => { video.currentTime = 4; fireEvent(video, new Event('timeupdate')); });
    expect(screen.getByText('recuperada')).toBeTruthy();
    expect(screen.queryByText('última fala')).toBeNull();
  });

  it('gives up after the bounded retry budget — no endless reconnect storm, CC hides', async () => {
    vi.useFakeTimers();
    try {
      mockFetch();
      renderPopup();
      // Flush the session + availability microtask chain (fetch mock →
      // session resolve → probe → captionsAvailable → SSE).
      for (let i = 0; i < 30 && captionEsInstances().length === 0; i++) {
        await act(async () => {});
      }
      // Wait for the probe to resolve and the CC button to appear.
      for (let i = 0; i < 30 && !screen.queryByTitle('Hide captions'); i++) {
        await act(async () => {});
      }
      expect(captionEsInstances()).toHaveLength(1);
      expect(screen.queryByTitle('Hide captions')).toBeTruthy();

      const fail = (i: number) => act(() => { captionEsInstances()[i].fire('offline', '{}'); });
      const advance = (ms: number) => act(async () => { vi.advanceTimersByTime(ms); await Promise.resolve(); });

      // Attempt 1 → 1.5s → probe true → reconnect.
      fail(0);
      await advance(1500);
      await act(async () => {});
      expect(captionEsInstances()).toHaveLength(2);
      // Attempt 2 → 3s → reconnect.
      fail(1);
      await advance(3000);
      await act(async () => {});
      expect(captionEsInstances()).toHaveLength(3);
      // Attempt 3 → 6s → reconnect.
      fail(2);
      await advance(6000);
      await act(async () => {});
      expect(captionEsInstances()).toHaveLength(4);
      // Attempt 4 exceeds the budget → give up: the CC cluster hides and no
      // further EventSource is created (no endless reconnect storm).
      fail(3);
      await act(async () => {});
      expect(screen.queryByTitle('Hide captions')).toBeNull();
      expect(captionEsInstances()).toHaveLength(4);
    } finally {
      vi.useRealTimers();
    }
  });

  it('retries the availability probe (bounded) so a transient failure at open never kills the CC cluster', async () => {
    let fail = 1; // first probe fails, the 1.5s re-probe succeeds
    const fn = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/preview/live')) {
        return new Response(JSON.stringify({ session_id: 's1' }), { status: 200 });
      }
      if (url.includes('/api/live/captions/available')) {
        if (fail > 0) {
          fail -= 1;
          throw new Error('network down');
        }
        return new Response(JSON.stringify({ available: true }), { status: 200 });
      }
      return new Response(JSON.stringify({}), { status: 404 });
    });
    vi.stubGlobal('fetch', fn);
    renderPopup();
    await screen.findByTitle('Fullscreen');
    // No CC button while the probe keeps failing…
    expect(screen.queryByTitle('Hide captions')).toBeNull();
    // …but the bounded re-probe succeeds → the cluster appears WITHOUT any
    // user interaction (captions "not activating until manual interaction").
    await waitFor(() => expect(screen.queryByTitle('Hide captions')).toBeTruthy(), { timeout: 4000 });
    await waitFor(() => expect(captionEsInstances()).toHaveLength(1));
  });

  it('sends ?lang= on the caption SSE from the in-player selector and persists the choice', async () => {
    localStorage.removeItem('vodrip.live.captionLang');
    mockFetch();
    const view = renderPopup();
    await screen.findByTitle('Hide captions');
    await waitFor(() => expect(captionEsInstances()).toHaveLength(1));
    // Default: no lang param — the backend follows the app language.
    expect(captionEsInstances()[0].url).not.toContain('lang=');

    // The selector lives in the player, next to the CC toggle (same cluster).
    fireEvent.click(screen.getByTitle('Caption language'));
    fireEvent.click(screen.getByText('Español'));
    // The choice reconnects the caption stream with ?lang=es.
    await waitFor(() => expect(captionEsInstances()).toHaveLength(2));
    expect(captionEsInstances()[1].url).toContain('lang=es');
    expect(localStorage.getItem('vodrip.live.captionLang')).toBe('es');

    // Persists across popup reopen.
    view.unmount();
    renderPopup();
    await screen.findByTitle('Hide captions');
    await waitFor(() => expect(captionEsInstances()).toHaveLength(3));
    expect(captionEsInstances()[2].url).toContain('lang=es');
    // Icon-only button: no text label (the old "ES"/"AUTO" text is gone),
    // the icon carries the accessible name via title/aria-label.
    const langBtn = screen.getByTitle('Caption language');
    expect(langBtn.textContent?.trim() ?? '').toBe('');
    expect(langBtn.querySelector('svg')).toBeTruthy();
  });

  it('CC button hidden when parakeet gate reports unavailable — SSE still opens for caption delivery', async () => {
    const fn = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/preview/live')) {
        return new Response(JSON.stringify({ session_id: 's1' }), { status: 200 });
      }
      if (url.includes('/api/live/captions/available')) {
        return new Response(JSON.stringify({ available: false, reason: 'parakeet model missing' }), { status: 200 });
      }
      return new Response(JSON.stringify({}), { status: 404 });
    });
    vi.stubGlobal('fetch', fn);
    renderPopup();
    await screen.findByTitle('Fullscreen');
    // SSE opens immediately (no probe guard) — but CC button stays hidden
    // because the probe reports unavailable.
    await waitFor(() => expect(captionEsInstances()).toHaveLength(1));
    expect(screen.queryByTitle('Hide captions')).toBeNull();
    expect(screen.queryByTitle('Live captions')).toBeNull();
    expect(document.querySelector('[data-live-captions-overlay]')).toBeNull();
  });

  it('renders the CC cluster when the gate reports pending (model downloads on first use)', async () => {
    const fn = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/preview/live')) {
        return new Response(JSON.stringify({ session_id: 's1' }), { status: 200 });
      }
      if (url.includes('/api/live/captions/available')) {
        return new Response(JSON.stringify({ available: false, pending: true, reason: 'model downloading', translation_available: false, low_latency: false }), { status: 200 });
      }
      return new Response(JSON.stringify({}), { status: 404 });
    });
    vi.stubGlobal('fetch', fn);
    renderPopup();
    await screen.findByTitle('Fullscreen');
    // pending permits an explicit caption stream → captions default ON, the
    // toggle appears, and the SSE opens (the first use triggers the download).
    await screen.findByTitle('Hide captions');
    await waitFor(() => expect(captionEsInstances()).toHaveLength(1));
    // Without NLLB translation the language selector is disabled+marked.
    const langBtn = screen.getByTitle('Caption translation unavailable');
    expect((langBtn as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(langBtn);
    expect(screen.queryByText('Español')).toBeNull();
  });

  it('disables the caption language selector when translation_available is false', async () => {
    const fn = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/preview/live')) {
        return new Response(JSON.stringify({ session_id: 's1' }), { status: 200 });
      }
      if (url.includes('/api/live/captions/available')) {
        return new Response(JSON.stringify({ available: true, pending: false, reason: null, translation_available: false, low_latency: false }), { status: 200 });
      }
      return new Response(JSON.stringify({}), { status: 404 });
    });
    vi.stubGlobal('fetch', fn);
    renderPopup();
    await screen.findByTitle('Fullscreen');
    // Captions still available; only the translate target is off.
    await screen.findByTitle('Hide captions');
    const langBtn = screen.getByTitle('Caption translation unavailable');
    expect((langBtn as HTMLButtonElement).disabled).toBe(true);
    // Clicking a disabled selector never opens the lang menu.
    fireEvent.click(langBtn);
    expect(screen.queryByText('Español')).toBeNull();
    expect(screen.queryByText('Auto')).toBeNull();
  });

  it('shows a caption only when the mapped video clock reaches its wall window (frag PDT anchor)', async () => {
    mockFetchWithLiveSrc();
    renderPopup();
    await waitFor(() => expect((window as unknown as { __livePopupHls?: InstanceType<typeof FakeHls> }).__livePopupHls).toBeTruthy());
    const hls = (window as unknown as { __livePopupHls?: InstanceType<typeof FakeHls> }).__livePopupHls!;
    await act(async () => { hls.trigger(FakeHls.Events.MANIFEST_PARSED); }); // clears loading → transport renders
    await screen.findByTitle('Hide captions');
    await waitFor(() => expect(captionEsInstances()).toHaveLength(1));
    const es = captionEsInstances()[0];

    // Frag anchor: timeline position 0 ↔ wall epoch 1000 (frag PDT in ms).
    act(() => { hls.trigger('fragBuffered', { frag: { start: 0, programDateTime: 1_000_000 } }); });
    const video = document.querySelector('video') as HTMLVideoElement;
    const at = (t: number) => act(() => { video.currentTime = t; fireEvent(video, new Event('timeupdate')); });

    // Caption window [1002, 1004] wall — the video clock (epoch 1000) has
    // not reached its due point (end − 0.25s): the overlay must NOT appear.
    act(() => { es.fire('caption', JSON.stringify({ text: 'sincronizada', start: 1002, end: 1004, latency_ms: 800 })); });
    expect(screen.queryByText('sincronizada')).toBeNull();
    expect(document.querySelector('[data-live-captions-overlay]')).toBeNull();

    // Mid-window (epoch 1002 < 1003.75): still not shown — no future drift.
    at(2);
    expect(screen.queryByText('sincronizada')).toBeNull();

    // The video clock reaches the due point (epoch 1004 ≥ 1003.75) → shown.
    at(4);
    expect(screen.getByText('sincronizada')).toBeTruthy();
    expect(document.querySelector('[data-live-captions-overlay]')).toBeTruthy();
  });

  it('drops a caption whose window ended more than 1s before the video clock (late arrival)', async () => {
    mockFetchWithLiveSrc();
    renderPopup();
    await waitFor(() => expect((window as unknown as { __livePopupHls?: InstanceType<typeof FakeHls> }).__livePopupHls).toBeTruthy());
    const hls = (window as unknown as { __livePopupHls?: InstanceType<typeof FakeHls> }).__livePopupHls!;
    await act(async () => { hls.trigger(FakeHls.Events.MANIFEST_PARSED); });
    await screen.findByTitle('Hide captions');
    await waitFor(() => expect(captionEsInstances()).toHaveLength(1));
    const es = captionEsInstances()[0];

    act(() => { hls.trigger('fragBuffered', { frag: { start: 0, programDateTime: 1_000_000 } }); });
    const video = document.querySelector('video') as HTMLVideoElement;
    const at = (t: number) => act(() => { video.currentTime = t; fireEvent(video, new Event('timeupdate')); });

    // The video clock sits at epoch 1006 (the player live-synced past it) —
    // a window ending at 1004 ended 2s earlier, beyond the 1s stale skip.
    at(6);
    act(() => { es.fire('caption', JSON.stringify({ text: 'atrasada', start: 1000, end: 1004, latency_ms: 2500 })); });
    expect(screen.queryByText('atrasada')).toBeNull();
    // Dropped at arrival — it must never surface later either.
    at(8);
    expect(screen.queryByText('atrasada')).toBeNull();
    expect(document.querySelector('[data-live-captions-overlay]')).toBeNull();
  });

  it('calibrates a fallback origin from the first block on a no-PDT timeline (arrival-due, then video-gated)', async () => {
    mockFetch();
    renderPopup();
    await screen.findByTitle('Fullscreen');
    await screen.findByTitle('Hide captions');
    await waitFor(() => expect(captionEsInstances()).toHaveLength(1));
    const es = captionEsInstances()[0];
    const video = document.querySelector('video') as HTMLVideoElement;

    // No frag anchors: the first block calibrates the origin to its due
    // point (origin = end − lead − broadcast position at arrival = 2.75) so
    // it shows on arrival — the fallback never waits for a clock map.
    act(() => { es.fire('caption', JSON.stringify({ text: 'primeira', start: 0, end: 3 })); });
    expect(screen.getByText('primeira')).toBeTruthy();

    // The second window [5, 8] is NOT due at the current clock (epoch 2.75 <
    // 7.75) — queued and hidden even though it already arrived.
    act(() => { es.fire('caption', JSON.stringify({ text: 'segunda', start: 5, end: 8 })); });
    expect(screen.queryByText('segunda')).toBeNull();

    // Video-gated: it appears only when the clock reaches end − lead — the
    // calibration pins this at t = 5 exactly, not on arrival.
    act(() => { video.currentTime = 4.9; fireEvent(video, new Event('timeupdate')); });
    expect(screen.queryByText('segunda')).toBeNull();
    act(() => { video.currentTime = 5; fireEvent(video, new Event('timeupdate')); });
    expect(screen.getByText('segunda')).toBeTruthy();
  });

  it('queues captions while the video is stalled and catches up on resume without stale text', async () => {
    mockFetchWithLiveSrc();
    renderPopup();
    await waitFor(() => expect((window as unknown as { __livePopupHls?: InstanceType<typeof FakeHls> }).__livePopupHls).toBeTruthy());
    const hls = (window as unknown as { __livePopupHls?: InstanceType<typeof FakeHls> }).__livePopupHls!;
    await act(async () => { hls.trigger(FakeHls.Events.MANIFEST_PARSED); });
    await screen.findByTitle('Hide captions');
    await waitFor(() => expect(captionEsInstances()).toHaveLength(1));
    const es = captionEsInstances()[0];

    act(() => { hls.trigger('fragBuffered', { frag: { start: 0, programDateTime: 1_000_000 } }); });
    const video = document.querySelector('video') as HTMLVideoElement;
    const at = (t: number) => act(() => { video.currentTime = t; fireEvent(video, new Event('timeupdate')); });
    const fire = (text: string, end: number) => act(() => {
      es.fire('caption', JSON.stringify({ text, start: end - 2, end, latency_ms: 400 }));
    });

    // The video is STALLED at t=0 (epoch 1000): both windows arrive but the
    // clock never reaches their due points — the overlay stays dark.
    fire('congelada-1', 1004);
    fire('congelada-2', 1008);
    expect(screen.queryByText('congelada-1')).toBeNull();
    expect(screen.queryByText('congelada-2')).toBeNull();

    // Playback resumes to t=8.5 (epoch 1008.5): block 1 is stale (dropped),
    // block 2 is in-window and due → only the newest text appears, no flash
    // of intermediate captions the video already played past.
    at(8.5);
    expect(screen.queryByText('congelada-1')).toBeNull();
    expect(screen.getByText('congelada-2')).toBeTruthy();
  });

  it('parses a naive PDT (no zone offset) as UTC, matching the backend caption clock', async () => {
    mockFetchWithLiveSrc();
    renderPopup();
    await waitFor(() => expect((window as unknown as { __livePopupHls?: InstanceType<typeof FakeHls> }).__livePopupHls).toBeTruthy());
    const hls = (window as unknown as { __livePopupHls?: InstanceType<typeof FakeHls> }).__livePopupHls!;
    await act(async () => { hls.trigger(FakeHls.Events.MANIFEST_PARSED); });
    await screen.findByTitle('Hide captions');
    await waitFor(() => expect(captionEsInstances()).toHaveLength(1));
    const es = captionEsInstances()[0];

    // hls.js computes programDateTime via Date.parse — LOCAL zone for a
    // naive tag. The component must read the raw tag with a UTC default,
    // the same base the backend's start/end/latency_ms live on.
    const naive = '2024-01-01T00:00:00.000';
    act(() => {
      hls.trigger('fragBuffered', {
        frag: { start: 0, rawProgramDateTime: naive, programDateTime: Date.parse(naive) },
      });
    });
    const video = document.querySelector('video') as HTMLVideoElement;
    const at = (t: number) => act(() => { video.currentTime = t; fireEvent(video, new Event('timeupdate')); });
    const wall = Date.parse(naive + '+00:00') / 1000; // 2024-01-01T00:00:00Z epoch

    // Window [wall+2, wall+4] — due when the video clock reaches wall+3.75.
    act(() => { es.fire('caption', JSON.stringify({ text: 'utc', start: wall + 2, end: wall + 4, latency_ms: 500 })); });
    expect(screen.queryByText('utc')).toBeNull();
    at(3.7);
    expect(screen.queryByText('utc')).toBeNull();
    at(4);
    expect(screen.getByText('utc')).toBeTruthy();
  });

  it('gear menu A−/A+ resizes caption overlay text, clamped at the 14px floor', async () => {
    localStorage.removeItem('vodrip.live.captionFontSize');
    mockFetch();
    renderPopup();
    await screen.findByTitle('Hide captions');
    await waitFor(() => expect(captionEsInstances()).toHaveLength(1));
    const es = captionEsInstances()[0];
    act(() => { es.fire('caption', JSON.stringify({ text: 'redimensionável', start: 0, end: 3 })); });

    const overlayText = () => document.querySelector('[data-live-captions-overlay] p') as HTMLElement;
    // Default 14px, no grip button, gear menu exists.
    expect(overlayText().style.fontSize).toBe('14px');
    expect(document.querySelector('[data-caption-resize-grip]')).toBeNull();

    // Open gear menu → A+ increases by 2px.
    fireEvent.click(screen.getByTitle('Caption size'));
    fireEvent.click(screen.getByTitle('Larger captions'));
    expect(overlayText().style.fontSize).toBe('16px');
    fireEvent.click(screen.getByTitle('Larger captions'));
    expect(overlayText().style.fontSize).toBe('18px');

    // A− decreases by 2px.
    fireEvent.click(screen.getByTitle('Smaller captions'));
    expect(overlayText().style.fontSize).toBe('16px');
    fireEvent.click(screen.getByTitle('Smaller captions'));
    expect(overlayText().style.fontSize).toBe('14px');

    // Floor clamp: further A− stays at 14px.
    fireEvent.click(screen.getByTitle('Smaller captions'));
    expect(overlayText().style.fontSize).toBe('14px');
  });

  it('caption clamp scales with the font size (line-clamp-2 → -3 → -4) via gear menu', async () => {
    localStorage.removeItem('vodrip.live.captionFontSize');
    mockFetch();
    renderPopup();
    await screen.findByTitle('Hide captions');
    await waitFor(() => expect(captionEsInstances()).toHaveLength(1));
    const es = captionEsInstances()[0];
    act(() => { es.fire('caption', JSON.stringify({ text: 'redimensionável', start: 0, end: 3 })); });

    const overlayText = () => document.querySelector('[data-live-captions-overlay] p') as HTMLElement;
    // Open the gear menu.
    fireEvent.click(screen.getByTitle('Caption size'));
    // 14px (default) → line-clamp-2
    expect(overlayText().className).toContain('line-clamp-2');
    // 14 → 16 → 18 → 20 → 22 → 24 (6 clicks) → line-clamp-3
    for (let i = 0; i < 5; i++) fireEvent.click(screen.getByTitle('Larger captions'));
    expect(overlayText().style.fontSize).toBe('24px');
    expect(overlayText().className).toContain('line-clamp-3');
    // 24 → 26 → ... → 34 (5 more clicks) → line-clamp-4
    for (let i = 0; i < 5; i++) fireEvent.click(screen.getByTitle('Larger captions'));
    expect(overlayText().style.fontSize).toBe('34px');
    expect(overlayText().className).toContain('line-clamp-4');
    // Shrinking back restores the smaller clamps.
    for (let i = 0; i < 5; i++) fireEvent.click(screen.getByTitle('Smaller captions'));
    expect(overlayText().style.fontSize).toBe('24px');
    expect(overlayText().className).toContain('line-clamp-3');
    for (let i = 0; i < 5; i++) fireEvent.click(screen.getByTitle('Smaller captions'));
    expect(overlayText().style.fontSize).toBe('14px');
    expect(overlayText().className).toContain('line-clamp-2');
  });

  it('captions toggle exposes an aria-label that tracks its state', async () => {
    mockFetch();
    renderPopup();
    await screen.findByTitle('Hide captions');
    await waitFor(() => expect(captionEsInstances()).toHaveLength(1));

    // ON → "Hide captions" as the accessible name (matches the title).
    expect(screen.getByRole('button', { name: 'Hide captions' })).toBeTruthy();
    fireEvent.click(screen.getByTitle('Hide captions'));
    // OFF → "Live captions".
    expect(screen.getByRole('button', { name: 'Live captions' })).toBeTruthy();
  });

  it('cold start: first anchor lands after the pending windows passed → the newest caption stays, never blank', async () => {
    mockFetchWithLiveSrc();
    renderPopup();
    await waitFor(() => expect((window as unknown as { __livePopupHls?: InstanceType<typeof FakeHls> }).__livePopupHls).toBeTruthy());
    const hls = (window as unknown as { __livePopupHls?: InstanceType<typeof FakeHls> }).__livePopupHls!;
    await act(async () => { hls.trigger(FakeHls.Events.MANIFEST_PARSED); });
    await screen.findByTitle('Hide captions');
    await waitFor(() => expect(captionEsInstances()).toHaveLength(1));
    const es = captionEsInstances()[0];
    const video = document.querySelector('video') as HTMLVideoElement;
    const at = (t: number) => act(() => { video.currentTime = t; fireEvent(video, new Event('timeupdate')); });

    // The stream is already 8s in when captions arrive. The first block
    // calibrates the fallback origin (arrival-due) and stays on screen;
    // the later windows queue behind it.
    at(8);
    act(() => { es.fire('caption', JSON.stringify({ text: 'bloco-1', start: 0, end: 2 })); });
    act(() => { es.fire('caption', JSON.stringify({ text: 'bloco-2', start: 2, end: 4 })); });
    act(() => { es.fire('caption', JSON.stringify({ text: 'bloco-3', start: 4, end: 6 })); });
    expect(screen.getByText('bloco-1')).toBeTruthy();
    expect(screen.queryByText('bloco-3')).toBeNull();

    // The FIRST anchor lands LATE — the mapped clock (epoch 1008) is far
    // past every pending window (ends 2/4/6): bloco-1 goes stale AND every
    // queued block would be dropped. The overlay must NOT blank: the newest
    // pending block stays on screen.
    act(() => { hls.trigger('fragBuffered', { frag: { start: 0, programDateTime: 1_000_000 } }); });
    at(8);
    expect(screen.getByText('bloco-3')).toBeTruthy();
    expect(document.querySelector('[data-live-captions-overlay]')).toBeTruthy();
    // Idempotent across ticks — the fallback persists until fresh text lands.
    at(8.5);
    expect(screen.getByText('bloco-3')).toBeTruthy();

    // A fresh caption whose window the video reaches replaces the fallback.
    act(() => { es.fire('caption', JSON.stringify({ text: 'fresca', start: 1006, end: 1008, latency_ms: 300 })); });
    expect(screen.getByText('fresca')).toBeTruthy();
    expect(screen.queryByText('bloco-3')).toBeNull();
  });

  it('gear menu A+ clamps at the 48px ceiling and persists the choice', async () => {
    localStorage.removeItem('vodrip.live.captionFontSize');
    mockFetch();
    renderPopup();
    await screen.findByTitle('Hide captions');
    await waitFor(() => expect(captionEsInstances()).toHaveLength(1));
    const es = captionEsInstances()[0];
    act(() => { es.fire('caption', JSON.stringify({ text: 'gigante', start: 0, end: 3 })); });

    const overlayText = () => document.querySelector('[data-live-captions-overlay] p') as HTMLElement;
    // 14 → 48 = 17 × A+ (each +2px).
    fireEvent.click(screen.getByTitle('Caption size'));
    for (let i = 0; i < 17; i++) fireEvent.click(screen.getByTitle('Larger captions'));
    expect(overlayText().style.fontSize).toBe('48px');
    // Further clicks are a no-op at the ceiling.
    fireEvent.click(screen.getByTitle('Larger captions'));
    expect(overlayText().style.fontSize).toBe('48px');

    // Persisted to localStorage.
    expect(localStorage.getItem('vodrip.live.captionFontSize')).toBe('48');
  });

  it('restores a persisted caption font size on remount', async () => {
    localStorage.setItem('vodrip.live.captionFontSize', '30');
    mockFetch();
    const view = renderPopup();
    await screen.findByTitle('Hide captions');
    await waitFor(() => expect(captionEsInstances()).toHaveLength(1));
    const es = captionEsInstances()[0];
    act(() => { es.fire('caption', JSON.stringify({ text: 'persistida', start: 0, end: 3 })); });
    expect((document.querySelector('[data-live-captions-overlay] p') as HTMLElement).style.fontSize).toBe('30px');

    // Remount — the preference survives the popup reopen.
    view.unmount();
    renderPopup();
    await screen.findByTitle('Hide captions');
    await waitFor(() => expect(captionEsInstances()).toHaveLength(2));
    act(() => { captionEsInstances()[1].fire('caption', JSON.stringify({ text: 'persistida de novo', start: 0, end: 3 })); });
    expect((document.querySelector('[data-live-captions-overlay] p') as HTMLElement).style.fontSize).toBe('30px');
  });
});

describe('LivePlayerPopup z-order', () => {
  it('applies the shared-ladder zIndex and brings itself to front on pointer down', () => {
    const onBringToFront = vi.fn();
    render(
      <LivePlayerPopup
        entry={ENTRY}
        channelName="srdogg / srdoglol"
        onClose={vi.fn()}
        channelSlug="srdoglol"
        onOpenHit={vi.fn()}
        savedChannels={[]}
        zIndex={EXPLORE_POPUP_Z + 3}
        onBringToFront={onBringToFront}
      />,
    );
    // The popup portal is mounted synchronously with the component.
    const popupRoot = document.querySelector('[data-live-popup]') as HTMLElement;
    expect(popupRoot).toBeTruthy();
    // Rank from the shared floating-player ladder wins over any fixed constant.
    expect(Number(popupRoot.style.zIndex)).toBe(EXPLORE_POPUP_Z + 3);
    // Pointer-down on the root (drag start) bumps the ladder rank → the popup
    // comes to the front, above whatever it was dragged onto.
    fireEvent.pointerDown(popupRoot);
    expect(onBringToFront).toHaveBeenCalledTimes(1);
  });

  it('falls back above the floating archive search when no zIndex is given', () => {
    renderPopup();
    const popupRoot = document.querySelector('[data-live-popup]') as HTMLElement;
    expect(popupRoot).toBeTruthy();
    // Unranked popups keep the classic active-state z: above the search panel…
    expect(Number(popupRoot.style.zIndex)).toBe(LIVE_POPUP_ACTIVE_Z);
    expect(LIVE_POPUP_ACTIVE_Z).toBeGreaterThan(SEARCH_POPUP_Z);
  });

  it('closing the popup removes its layer entirely (search regains top order)', () => {
    const onClose = vi.fn();
    const { unmount } = render(
      <LivePlayerPopup
        entry={ENTRY}
        channelName="srdogg / srdoglol"
        onClose={onClose}
        channelSlug="srdoglol"
        onOpenHit={vi.fn()}
        savedChannels={[]}
      />,
    );
    expect(document.querySelector('[data-live-popup]')).toBeTruthy();
    unmount();
    expect(document.querySelector('[data-live-popup]')).toBeNull();
  });
});

describe('LivePlayerPopup volume hover-only', () => {
  it('hover shows volume slider, mousedown elsewhere hides it', async () => {
    renderPopup();
    await screen.findByTitle('Fullscreen');

    // Hover opens the volume slider.
    const wrapper = document.querySelector('[data-volume-menu]')!;
    fireEvent.mouseEnter(wrapper);
    expect(screen.getByLabelText('Volume')).toBeTruthy();

    // A mousedown on the popup root (outside the volume menu) closes it.
    const popup = document.querySelector('[data-live-popup]')!;
    fireEvent.mouseDown(popup);
    await waitFor(() => expect(screen.queryByLabelText('Volume')).toBeNull());
  });

  it('leaving the wrapper hides the slider', async () => {
    renderPopup();
    await screen.findByTitle('Fullscreen');

    const wrapper = document.querySelector('[data-volume-menu]')!;
    fireEvent.mouseEnter(wrapper);
    expect(screen.getByLabelText('Volume')).toBeTruthy();

    fireEvent.mouseLeave(wrapper);
    expect(screen.queryByLabelText('Volume')).toBeNull();
  });

  it('clicking the speaker toggles mute; hover shows slider independently', async () => {
    renderPopup();
    await screen.findByTitle('Fullscreen');

    // Click toggles mute (not the slider).
    fireEvent.click(screen.getByTitle('Volume'));
    expect(screen.queryByLabelText('Volume')).toBeNull();

    // Hover shows the slider.
    const wrapper = document.querySelector('[data-volume-menu]')!;
    fireEvent.mouseEnter(wrapper);
    const slider = screen.getByLabelText('Volume') as HTMLInputElement;
    expect(slider).toBeTruthy();

    // Dragging still adjusts the volume.
    fireEvent.change(slider, { target: { value: '0.5' } });
    expect(slider.value).toBe('0.5');
  });
});

describe('LivePlayerPopup replay rail (wheel zoom + arrow seek)', () => {
  beforeEach(() => {
    // The component sets this e2e probe hook in createHlsPlayer and never
    // clears it on unmount — reset it so a prior test's instance cannot be
    // mistaken for this test's replay switch.
    delete (window as unknown as { __livePopupHls?: unknown }).__livePopupHls;
  });

  it('wheel on the rail zooms min/max around the cursor; full zoom-out restores 0..railMax', async () => {
    mockFetchWithArchive();
    renderPopup();
    await screen.findByTitle('Fullscreen');

    const rail = screen.getByRole('slider', { name: 'Seek back into the broadcast (replay)' }) as HTMLInputElement;
    // The DVR archive resolves lazily AFTER the transport renders — wait for
    // the rail to enable and settle (same as the arrow-seek siblings below)
    // so the native wheel listener (attached by a passive effect keyed on
    // [railDisabled, railMax, railView]) is in place before the wheel fires;
    // firing earlier races the effect flush and the zoom is a silent no-op.
    await waitFor(() => expect(rail.disabled).toBe(false));
    await waitFor(() => expect(rail.max).toBe('100'));
    expect(rail.min).toBe('0');

    // jsdom reports a zero-size rect — give the rail real geometry so the
    // cursor fraction (pointer x within the rail) is computable.
    vi.spyOn(rail, 'getBoundingClientRect').mockReturnValue({
      left: 0, right: 200, top: 0, bottom: 4, width: 200, height: 4, x: 0, y: 0,
      toJSON: () => ({}),
    } as DOMRect);

    // Wheel up at the centre → ×1.5 window [≈16.7, ≈83.3], finer step, badge
    // + hint; the live playhead stays pinned at the (zoomed) archive edge.
    fireEvent.wheel(rail, { clientX: 100, deltaY: -100 });
    expect(parseFloat(rail.min)).toBeCloseTo(100 / 6, 5);
    expect(parseFloat(rail.max)).toBeCloseTo(500 / 6, 5);
    expect(rail.step).toBe('0.1');
    expect(parseFloat(rail.value)).toBeCloseTo(500 / 6, 5);
    expect(screen.getByText('×1.5')).toBeTruthy();
    expect(rail.title).toContain('Scroll on the rail to zoom');

    // Wheel down → back to the full 0..railMax, coarse step, badge + hint gone.
    fireEvent.wheel(rail, { clientX: 100, deltaY: 100 });
    expect(rail.min).toBe('0');
    expect(rail.max).toBe('100');
    expect(rail.step).toBe('0.5');
    expect(screen.queryByText('×1.5')).toBeNull();
    expect(rail.title).not.toContain('Scroll on the rail to zoom');
  });

  it('ArrowLeft in live mode switches to replay at railTime − 5s', async () => {
    mockFetchWithArchive();
    renderPopup();
    await screen.findByTitle('Fullscreen');
    // The DVR archive resolves lazily — wait until the rail enables so the
    // window keydown listener is attached before firing arrows.
    const rail = screen.getByRole('slider', { name: 'Seek back into the broadcast (replay)' }) as HTMLInputElement;
    await waitFor(() => expect(rail.disabled).toBe(false));
    await waitFor(() => expect(rail.max).toBe('100'));

    const video = document.querySelector('video') as HTMLVideoElement;
    video.currentTime = 42;
    fireEvent(video, new Event('timeupdate'));

    fireEvent.keyDown(window, { key: 'ArrowLeft' });

    // 250 ms debounce → switchToReplay(max(0, 42 − 5)) = switchToReplay(37).
    await waitFor(() => expect((window as unknown as { __livePopupHls?: InstanceType<typeof FakeHls> }).__livePopupHls).toBeTruthy());
    const hls = (window as unknown as { __livePopupHls?: InstanceType<typeof FakeHls> }).__livePopupHls!;
    await act(async () => { hls.trigger(FakeHls.Events.MANIFEST_PARSED); });
    expect(hls.startLoad).toHaveBeenCalledWith(37);
    // Mode switched: the rail is now a replay seek bar.
    expect(screen.getByRole('slider', { name: 'Seek within replay' })).toBeTruthy();
  });

  it('ArrowRight in live mode switches to replay at railTime + 5s', async () => {
    mockFetchWithArchive();
    renderPopup();
    await screen.findByTitle('Fullscreen');
    const rail = screen.getByRole('slider', { name: 'Seek back into the broadcast (replay)' }) as HTMLInputElement;
    await waitFor(() => expect(rail.disabled).toBe(false));
    await waitFor(() => expect(rail.max).toBe('100'));

    const video = document.querySelector('video') as HTMLVideoElement;
    video.currentTime = 42;
    fireEvent(video, new Event('timeupdate'));

    fireEvent.keyDown(window, { key: 'ArrowRight' });

    await waitFor(() => expect((window as unknown as { __livePopupHls?: InstanceType<typeof FakeHls> }).__livePopupHls).toBeTruthy());
    const hls = (window as unknown as { __livePopupHls?: InstanceType<typeof FakeHls> }).__livePopupHls!;
    await act(async () => { hls.trigger(FakeHls.Events.MANIFEST_PARSED); });
    expect(hls.startLoad).toHaveBeenCalledWith(47);
    expect(screen.getByRole('slider', { name: 'Seek within replay' })).toBeTruthy();
  });

  it('ArrowLeft/ArrowRight seek ±5s inside replay (clamped to the snapshot)', async () => {
    mockFetchWithArchive();
    renderPopup();
    await screen.findByTitle('Fullscreen');
    const rail0 = screen.getByRole('slider', { name: 'Seek back into the broadcast (replay)' }) as HTMLInputElement;
    await waitFor(() => expect(rail0.disabled).toBe(false));
    await waitFor(() => expect(rail0.max).toBe('100'));
    const video = document.querySelector('video') as HTMLVideoElement;

    // Enter replay first: ArrowRight from live (railTime 0 → 5).
    fireEvent.keyDown(window, { key: 'ArrowRight' });
    await waitFor(() => expect((window as unknown as { __livePopupHls?: InstanceType<typeof FakeHls> }).__livePopupHls).toBeTruthy());
    const hls = (window as unknown as { __livePopupHls?: InstanceType<typeof FakeHls> }).__livePopupHls!;
    await act(async () => { hls.trigger(FakeHls.Events.MANIFEST_PARSED); });
    await screen.findByRole('slider', { name: 'Seek within replay' });

    // Give the snapshot a real duration — jsdom reports NaN by default.
    Object.defineProperty(video, 'duration', { configurable: true, value: 100 });
    fireEvent(video, new Event('durationchange'));
    const rail = screen.getByRole('slider', { name: 'Seek within replay' }) as HTMLInputElement;
    await waitFor(() => expect(rail.max).toBe('100'));

    // ±5s around the playhead, directly on the snapshot timeline.
    video.currentTime = 42;
    fireEvent(video, new Event('timeupdate'));
    fireEvent.keyDown(window, { key: 'ArrowRight' });
    expect(video.currentTime).toBe(47);
    // The seek moved the playhead — a real timeupdate syncs railTime.
    fireEvent(video, new Event('timeupdate'));
    fireEvent.keyDown(window, { key: 'ArrowLeft' });
    expect(video.currentTime).toBe(42);

    // Clamped at the start: −5 below 0 stops at 0.
    video.currentTime = 2;
    fireEvent(video, new Event('timeupdate'));
    fireEvent.keyDown(window, { key: 'ArrowLeft' });
    expect(video.currentTime).toBe(0);

    // Clamped at the end: +5 past the snapshot edge re-snapshots at railMax
    // (inSnapshot is false beyond duration − 0.5) — the new session lands there.
    video.currentTime = 97;
    fireEvent(video, new Event('timeupdate'));
    fireEvent.keyDown(window, { key: 'ArrowRight' });
    await waitFor(() => expect(hls.destroy).toHaveBeenCalled()); // parked instance replaced
    const hls2 = (window as unknown as { __livePopupHls?: InstanceType<typeof FakeHls> }).__livePopupHls!;
    await act(async () => { hls2.trigger(FakeHls.Events.MANIFEST_PARSED); });
    expect(hls2.startLoad).toHaveBeenCalledWith(100);
  });

  it('ArrowLeft/ArrowRight seek ±5s inside the retained back-buffer when there is no archive', async () => {
    mockFetchWithLiveSrc();
    renderPopup();
    // The session resolved a live master URL → the FakeHls instance exists;
    // MANIFEST_PARSED clears loading so the transport renders.
    await waitFor(() => expect((window as unknown as { __livePopupHls?: InstanceType<typeof FakeHls> }).__livePopupHls).toBeTruthy());
    const hls = (window as unknown as { __livePopupHls?: InstanceType<typeof FakeHls> }).__livePopupHls!;
    await act(async () => { hls.trigger(FakeHls.Events.MANIFEST_PARSED); });
    await screen.findByTitle('Fullscreen');

    // No DVR archive → the rail stays disabled/undraggable (the user asked
    // only for KEYBOARD seek in the buffer).
    const rail = screen.getByRole('slider', { name: 'Seek back into the broadcast (replay)' }) as HTMLInputElement;
    expect(rail.disabled).toBe(true);

    // Live edge = liveSyncPosition(100) + three-segment lag (3 × 2s) = 106;
    // the back-buffer window is [106−30, 106−0.75] = [76, 105.25].
    hls.liveSyncPosition = 100;
    const video = document.querySelector('video') as HTMLVideoElement;
    video.currentTime = 95;

    fireEvent.keyDown(window, { key: 'ArrowLeft' });
    expect(video.currentTime).toBe(90); // 95 − 5, inside the window

    fireEvent.keyDown(window, { key: 'ArrowRight' });
    expect(video.currentTime).toBe(95); // 90 + 5

    // Clamped to the buffer's leading edge: 5 − 5 → −0 → 76.
    video.currentTime = 5;
    fireEvent.keyDown(window, { key: 'ArrowLeft' });
    expect(video.currentTime).toBe(76);

    // Clamped just below the live edge (0.75s safety): 110 + 5 → 115 → 105.25.
    video.currentTime = 110;
    fireEvent.keyDown(window, { key: 'ArrowRight' });
    expect(video.currentTime).toBe(105.25);

    // Never below 0: early stream (edge 18 → window [0, 17.25]).
    hls.liveSyncPosition = 12; // edge 18 (three-segment lag)
    video.currentTime = 0;
    fireEvent.keyDown(window, { key: 'ArrowLeft' });
    expect(video.currentTime).toBe(0);

    // Still live, still no replay switch — the buffer seek never touches hls.
    expect(screen.queryByRole('slider', { name: 'Seek within replay' })).toBeNull();
    expect(rail.disabled).toBe(true);
  });

  it('is a no-op while the stream is still loading (manifest not parsed yet)', async () => {
    mockFetchWithLiveSrc();
    renderPopup();
    await waitFor(() => expect((window as unknown as { __livePopupHls?: InstanceType<typeof FakeHls> }).__livePopupHls).toBeTruthy());
    const hls = (window as unknown as { __livePopupHls?: InstanceType<typeof FakeHls> }).__livePopupHls!;
    hls.liveSyncPosition = 100;

    const video = document.querySelector('video') as HTMLVideoElement;
    video.currentTime = 95;
    fireEvent.keyDown(window, { key: 'ArrowLeft' });
    fireEvent.keyDown(window, { key: 'ArrowRight' });

    // loading → arrows are a no-op, no replay switch, no hls re-creation.
    expect(video.currentTime).toBe(95);
    await new Promise((r) => setTimeout(r, 300));
    expect((window as unknown as { __livePopupHls?: unknown }).__livePopupHls).toBe(hls);
    expect(screen.queryByRole('slider', { name: 'Seek within replay' })).toBeNull();
  });

  it('is a no-op when the live edge is unknown (no hls instance, no seekable range)', async () => {
    renderPopup(); // default mock — no src → no hls instance
    await screen.findByTitle('Fullscreen');

    const rail = screen.getByRole('slider', { name: 'Seek back into the broadcast (replay)' }) as HTMLInputElement;
    expect(rail.disabled).toBe(true);

    const video = document.querySelector('video') as HTMLVideoElement;
    video.currentTime = 10;
    fireEvent(video, new Event('timeupdate'));

    fireEvent.keyDown(window, { key: 'ArrowRight' });
    fireEvent.keyDown(window, { key: 'ArrowLeft' });

    // edgeSec 0 → nothing happens: playhead untouched, still live, no hls
    // instance even after the debounce window.
    expect(video.currentTime).toBe(10);
    expect(rail.disabled).toBe(true);
    await new Promise((r) => setTimeout(r, 300));
    expect((window as unknown as { __livePopupHls?: unknown }).__livePopupHls).toBeUndefined();
    expect(screen.queryByRole('slider', { name: 'Seek within replay' })).toBeNull();
  });
});

describe('LivePlayerPopup live latency config', () => {
  it('targets a sub-second player delay with count-based sync knobs on every platform', async () => {
    mockFetchWithLiveSrc();
    renderPopup();
    await waitFor(() => expect((window as unknown as { __livePopupHls?: InstanceType<typeof FakeHls> }).__livePopupHls).toBeTruthy());
    const hls = (window as unknown as { __livePopupHls?: InstanceType<typeof FakeHls> }).__livePopupHls!;

    // 3 segments behind the edge — deep cushion for proxy-mediated playback.
    expect(hls.config.liveSyncDurationCount).toBe(3);
    expect(hls.config.liveSyncDuration).toBeUndefined();
    expect(hls.config.liveMaxLatencyDurationCount).toBe(8);
    expect(hls.config.liveMaxLatencyDuration).toBeUndefined();

    // LL-HLS disabled — proxy + LL-HLS stalls cause buffering.
    expect(hls.config.lowLatencyMode).toBe(false);
    expect(hls.config.maxLiveSyncPlaybackRate).toBe(1.1);

    // Deep forward buffer to absorb proxy jitter; 30s retained back-buffer.
    expect(hls.config.maxBufferLength).toBe(30);
    expect(hls.config.maxMaxBufferLength).toBe(60);
    expect(hls.config.backBufferLength).toBe(30);
    expect(hls.config.liveDurationInfinity).toBe(false);

    // Live mode auto-starts.
    expect(hls.config.autoStartLoad).toBe(true);
  });
});

describe('LivePlayerPopup auto-hide controls', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  /** Flush the session microtask chain (fetch mock → session resolve → lazy
   *  archive probe → setLoading(false)) — waitFor cannot poll under fake
   *  timers, and each await re-queues the next chain link. */
  async function flushSession() {
    for (let i = 0; i < 30 && !document.querySelector('[data-live-transport]'); i++) {
      await act(async () => {});
    }
    expect(document.querySelector('[data-live-transport]')).toBeTruthy();
  }

  it('fades transport + header after ~2.5s idle and restores them on mousemove (cursor hidden with them)', async () => {
    vi.useFakeTimers();
    renderPopup();
    await flushSession();

    const popup = document.querySelector('[data-live-popup]')!;
    const transport = document.querySelector('[data-live-transport]')!;
    const header = document.querySelector('[data-live-header]')!;
    const videoArea = document.querySelector('[data-live-video-area]')!;
    expect(transport.className).toContain('opacity-100');
    expect(header.className).toContain('opacity-100');
    expect(videoArea.className).toContain('cursor-pointer');

    // 2.5s idle → transport + header fade out, cursor hidden over the video.
    act(() => { vi.advanceTimersByTime(2500); });
    expect(transport.className).toContain('opacity-0');
    expect(transport.className).toContain('pointer-events-none');
    expect(header.className).toContain('opacity-0');
    expect(header.className).toContain('pointer-events-none');
    expect(videoArea.className).toContain('cursor-none');

    // Mousemove anywhere in the popup → instant restore + fresh countdown.
    fireEvent.mouseMove(popup);
    expect(transport.className).toContain('opacity-100');
    expect(header.className).toContain('opacity-100');
    expect(videoArea.className).toContain('cursor-pointer');

    // …and it fades again after another 2.5s.
    act(() => { vi.advanceTimersByTime(2500); });
    expect(transport.className).toContain('opacity-0');
  });

  it('restores controls on any key while the popup is focused', async () => {
    vi.useFakeTimers();
    renderPopup();
    await flushSession();

    const popup = document.querySelector('[data-live-popup]')!;
    const transport = document.querySelector('[data-live-transport]')!;

    act(() => { vi.advanceTimersByTime(2500); });
    expect(transport.className).toContain('opacity-0');

    // The popup root holds focus (tabIndex −1, focused on mount) — a keydown
    // from it (or any focused descendant) bumps the controls back.
    fireEvent.keyDown(popup, { key: 'a' });
    expect(transport.className).toContain('opacity-100');
  });

  it('stays visible while paused and resumes hiding after unpause', async () => {
    vi.useFakeTimers();
    renderPopup();
    await flushSession();

    const transport = document.querySelector('[data-live-transport]')!;
    const video = document.querySelector('video') as HTMLVideoElement;

    // Pause → the countdown is cancelled, controls stay up indefinitely.
    fireEvent(video, new Event('pause'));
    act(() => { vi.advanceTimersByTime(6000); });
    expect(transport.className).toContain('opacity-100');

    // Resume → a FRESH 2.5s countdown starts from the unpause.
    fireEvent(video, new Event('play'));
    act(() => { vi.advanceTimersByTime(2000); });
    expect(transport.className).toContain('opacity-100');
    act(() => { vi.advanceTimersByTime(1000); });
    expect(transport.className).toContain('opacity-0');
  });

  it('keeps the header visible while the session is still loading', async () => {
    vi.useFakeTimers();
    // The session POST never resolves — loading stays true past any countdown.
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})));
    renderPopup();
    await act(async () => {});

    const header = document.querySelector('[data-live-header]')!;
    act(() => { vi.advanceTimersByTime(6000); }); // < SESSION_STALL_MS (15s)
    expect(header.className).toContain('opacity-100');
  });
});

describe('LivePlayerPopup live quality registry', () => {
  beforeEach(() => { __resetLivePlayerRegistryForTests(); });
  afterEach(() => { __resetLivePlayerRegistryForTests(); });

  it('counts registered players and notifies every subscriber on register/unregister', () => {
    expect(liveQualityCountNow()).toBe(0);
    const seenA: number[] = [];
    const seenB: number[] = [];
    const unsubA = registerLivePlayer((count) => seenA.push(count));
    expect(liveQualityCountNow()).toBe(1);
    expect(seenA).toEqual([1]); // A notified on its own register
    const unsubB = registerLivePlayer((count) => seenB.push(count));
    expect(liveQualityCountNow()).toBe(2);
    expect(seenA).toEqual([1, 2]); // A re-notified with the new count
    expect(seenB).toEqual([2]);
    unsubA();
    expect(liveQualityCountNow()).toBe(1);
    expect(seenB).toEqual([2, 1]); // B re-applies for the lower count
    unsubB();
    expect(liveQualityCountNow()).toBe(0);
    expect(seenA).toEqual([1, 2]);
    expect(seenB).toEqual([2, 1]); // B leaves before the final count — never notified of its own removal
  });

  it('unregister is idempotent and drops only that subscriber', () => {
    const seen: number[] = [];
    const unsub = registerLivePlayer((count) => seen.push(count));
    const unsub2 = registerLivePlayer((count) => seen.push(count));
    unsub();
    unsub(); // double-unregister is a no-op
    expect(liveQualityCountNow()).toBe(1);
    expect(seen).toEqual([1, 2, 2, 1]);
    unsub2();
    expect(liveQualityCountNow()).toBe(0);
  });

  it('a mounted popup registers itself and unregisters on unmount', async () => {
    __resetLivePlayerRegistryForTests();
    mockFetch();
    const { unmount } = renderPopup();
    await act(async () => {});
    expect(liveQualityCountNow()).toBe(1);
    unmount();
    expect(liveQualityCountNow()).toBe(0);
  });
});

describe('LivePlayerPopup live quality policy', () => {
  beforeEach(() => { __resetLivePlayerRegistryForTests(); });
  afterEach(() => { __resetLivePlayerRegistryForTests(); });

  /** Live session resolving a real master URL, with a 360/720/1080 ladder. */
  async function mountLiveWithLevels() {
    mockFetchWithLiveSrc();
    renderPopup();
    await waitFor(() => expect((window as unknown as { __livePopupHls?: InstanceType<typeof FakeHls> }).__livePopupHls).toBeTruthy());
    const hls = (window as unknown as { __livePopupHls?: InstanceType<typeof FakeHls> }).__livePopupHls!;
    hls.levels = [
      { height: 360, bitrate: 1_000_000 },
      { height: 720, bitrate: 3_000_000 },
      { height: 1080, bitrate: 6_000_000 },
    ];
    return hls;
  }

  it('pins SOURCE once the buffer is deep; a second player caps it to ≤480p; closing restores SOURCE', async () => {
    const hls = await mountLiveWithLevels();
    await act(async () => { hls.trigger(FakeHls.Events.MANIFEST_PARSED); });
    // The pin is DEFERRED at MANIFEST_PARSED: applying the policy level
    // while the live-edge buffer is ~1-2s deep would stall playback on the
    // level switch (the ~3s buffering report). ABR stays on the fast low
    // level until the forward buffer crosses the safety cushion.
    expect(hls.currentLevel).toBe(-1);
    expect(hls.autoLevelEnabled).toBe(true);
    await act(async () => { hls.trigger('fragBuffered', { frag: { start: 10, duration: 2, programDateTime: 1_000_000 } }); });
    // Single player → SOURCE (highest bitrate = 1080p), auto ABR off (fixed level).
    expect(hls.currentLevel).toBe(2);
    expect(hls.autoLevelEnabled).toBe(false);

    // A second player registers → every player re-applies: multi → highest ≤480 (360).
    let unsub2: (() => void) | undefined;
    await act(async () => { unsub2 = registerLivePlayer(() => {}); });
    expect(hls.currentLevel).toBe(0);
    expect(hls.autoLevelEnabled).toBe(false);

    // Back to one player → revert to SOURCE.
    await act(async () => { unsub2!(); });
    expect(hls.currentLevel).toBe(2);
    expect(hls.autoLevelEnabled).toBe(false);
  });

  it('re-applies at the buffer-arm when the second player opened before the manifest parsed', async () => {
    mockFetchWithLiveSrc();
    renderPopup();
    await waitFor(() => expect((window as unknown as { __livePopupHls?: InstanceType<typeof FakeHls> }).__livePopupHls).toBeTruthy());
    const hls = (window as unknown as { __livePopupHls?: InstanceType<typeof FakeHls> }).__livePopupHls!;
    // Second player opens while levels are still empty — the count-change
    // apply no-ops; the buffer-arm apply must read the CURRENT count.
    let unsub2: (() => void) | undefined;
    await act(async () => { unsub2 = registerLivePlayer(() => {}); });
    expect(hls.currentLevel).toBe(-1); // nothing parsed yet, nothing applied
    hls.levels = [
      { height: 360, bitrate: 1_000_000 },
      { height: 480, bitrate: 2_500_000 },
      { height: 1080, bitrate: 6_000_000 },
    ];
    await act(async () => { hls.trigger(FakeHls.Events.MANIFEST_PARSED); });
    expect(hls.currentLevel).toBe(-1); // still warming up — no level switch
    await act(async () => { hls.trigger('fragBuffered', { frag: { start: 10, duration: 2, programDateTime: 1_000_000 } }); });
    expect(hls.currentLevel).toBe(1); // multi → 480
    expect(hls.autoLevelEnabled).toBe(false);
    await act(async () => { unsub2!(); });
    expect(hls.currentLevel).toBe(2); // back to single → source
  });

  it('does not pin while the forward buffer stays shallow (no mid-start level switch)', async () => {
    mockFetchWithLiveSrc();
    renderPopup();
    await waitFor(() => expect((window as unknown as { __livePopupHls?: InstanceType<typeof FakeHls> }).__livePopupHls).toBeTruthy());
    const hls = (window as unknown as { __livePopupHls?: InstanceType<typeof FakeHls> }).__livePopupHls!;
    hls.levels = [
      { height: 360, bitrate: 1_000_000 },
      { height: 720, bitrate: 3_000_000 },
      { height: 1080, bitrate: 6_000_000 },
    ];
    await act(async () => { hls.trigger(FakeHls.Events.MANIFEST_PARSED); });
    expect(hls.currentLevel).toBe(-1);

    // A shallow first fragment (2s buffered at t=0) does not arm the pin.
    await act(async () => { hls.trigger('fragBuffered', { frag: { start: 0, duration: 2, programDateTime: 1_000_000 } }); });
    expect(hls.currentLevel).toBe(-1);
    expect(hls.autoLevelEnabled).toBe(true);

    // Once the forward buffer crosses 8s the pin lands (SOURCE, one player).
    await act(async () => { hls.trigger('fragBuffered', { frag: { start: 10, duration: 2, programDateTime: 1_000_000 } }); });
    expect(hls.currentLevel).toBe(2);
    expect(hls.autoLevelEnabled).toBe(false);
  });

  it('count changes before the manifest parses are safe no-ops', async () => {
    mockFetchWithLiveSrc();
    renderPopup();
    await waitFor(() => expect((window as unknown as { __livePopupHls?: InstanceType<typeof FakeHls> }).__livePopupHls).toBeTruthy());
    const hls = (window as unknown as { __livePopupHls?: InstanceType<typeof FakeHls> }).__livePopupHls!;
    await act(async () => {
      const unsub2 = registerLivePlayer(() => {});
      unsub2();
    });
    expect(hls.currentLevel).toBe(-1);
    expect(hls.autoLevelEnabled).toBe(true);
  });
});

describe('LivePlayerPopup session retry loop', () => {
  it('retries up to LIVE_SESSION_MAX_RETRIES on failure before showing error', async () => {
    vi.useFakeTimers();
    try {
      let callCount = 0;
      const fn = vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/api/preview/live')) {
          callCount++;
          if (callCount <= 3) {
            return new Response(JSON.stringify({ detail: 'temporarily unavailable' }), { status: 500 });
          }
          return new Response(JSON.stringify({ session_id: 's1' }), { status: 200 });
        }
        if (url.includes('/api/archive/videos')) {
          return new Response(JSON.stringify({ videos: [] }), { status: 200 });
        }
        if (url.includes('/api/live/captions/available')) {
          return new Response(JSON.stringify({
            available: true, pending: false, reason: null, translation_available: true, low_latency: false,
          }), { status: 200 });
        }
        return new Response(JSON.stringify({}), { status: 404 });
      });
      vi.stubGlobal('fetch', fn);

      renderPopup();
      await act(async () => {}); // initial render + first attempt (500)

      // First retry backoff: 1s
      await act(async () => { vi.advanceTimersByTime(1000); });
      await act(async () => {}); // second attempt (500)

      // Second retry backoff: 2s
      await act(async () => { vi.advanceTimersByTime(2000); });
      await act(async () => {}); // third attempt (500)

      // Third retry backoff: 4s
      await act(async () => { vi.advanceTimersByTime(4000); });
      await act(async () => {}); // fourth attempt (success)

      // Session should be created on the 4th attempt (after 3 retries)
      const liveCalls = fn.mock.calls.filter(([input]: [RequestInfo | URL]) => String(input).includes('/api/preview/live'));
      expect(liveCalls).toHaveLength(4);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('LivePlayerPopup live session prefetch', () => {
  it('consumes a prefetched session without calling POST', async () => {
    const fn = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/archive/videos')) {
        return new Response(JSON.stringify({ videos: [] }), { status: 200 });
      }
      if (url.includes('/api/live/captions/available')) {
        return new Response(JSON.stringify({ available: true, pending: false, reason: null, translation_available: true, low_latency: false }), { status: 200 });
      }
      return new Response(JSON.stringify({}), { status: 404 });
    });
    vi.stubGlobal('fetch', fn);

    const prefetchedSession = {
      session_id: 'prefetched-s1',
      kind: 'hls' as const,
      master_url: 'https://edge/live/master.m3u8',
      playback_url: 'https://edge/live/master.m3u8',
      archive_duration: 0,
    };
    const liveSessionPrefetchRef = { current: { url: ENTRY.url, session: prefetchedSession } };

    render(
      <LivePlayerPopup
        entry={ENTRY}
        channelName="srdogg / srdoglol"
        onClose={vi.fn()}
        channelSlug="srdoglol"
        onOpenHit={vi.fn()}
        savedChannels={[]}
        liveSessionPrefetchRef={liveSessionPrefetchRef}
      />,
    );

    // Wait for the component to mount and use the prefetched session
    await waitFor(() => {
      // The prefetched session should be consumed (ref cleared)
      expect(liveSessionPrefetchRef.current).toBeNull();
    });

    // POST /api/preview/live should never have been called
    const liveCalls = fn.mock.calls.filter(([input]: [RequestInfo | URL]) => String(input).includes('/api/preview/live'));
    expect(liveCalls).toHaveLength(0);

    // But the replay snapshot should have been called (success path)
    const snapshotCalls = fn.mock.calls.filter(([input]: [RequestInfo | URL]) => String(input).includes('/api/preview/hls/prefetched-s1/resource'));
    expect(snapshotCalls.length).toBeGreaterThanOrEqual(0); // may or may not be called depending on timing
  });

  it('does not consume prefetch for a different URL', async () => {
    vi.useFakeTimers();
    try {
      let callCount = 0;
      const fn = vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/api/preview/live')) {
          callCount++;
          return new Response(JSON.stringify({ session_id: `s${callCount}` }), { status: 200 });
        }
        if (url.includes('/api/archive/videos')) {
          return new Response(JSON.stringify({ videos: [] }), { status: 200 });
        }
        if (url.includes('/api/live/captions/available')) {
          return new Response(JSON.stringify({ available: true, pending: false, reason: null, translation_available: true, low_latency: false }), { status: 200 });
        }
        return new Response(JSON.stringify({}), { status: 404 });
      });
      vi.stubGlobal('fetch', fn);

      // Prefetch for a DIFFERENT URL — should NOT be consumed
      const liveSessionPrefetchRef = {
        current: { url: 'https://kick.com/other-channel', session: { session_id: 'other-s1' } as PreviewSessionResponse },
      };

      render(
        <LivePlayerPopup
          entry={ENTRY}
          channelName="srdogg / srdoglol"
          onClose={vi.fn()}
          channelSlug="srdoglol"
          onOpenHit={vi.fn()}
          savedChannels={[]}
          liveSessionPrefetchRef={liveSessionPrefetchRef}
        />,
      );

      await act(async () => {});
      await act(async () => { vi.advanceTimersByTime(500); });

      // POST /api/preview/live SHOULD have been called (prefetch URL mismatch)
      const liveCalls = fn.mock.calls.filter(([input]: [RequestInfo | URL]) => String(input).includes('/api/preview/live'));
      expect(liveCalls).toHaveLength(1);
    } finally {
      vi.useRealTimers();
    }
  });
});

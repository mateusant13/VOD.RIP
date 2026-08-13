import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Mock } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { LivePlayerPopup, __resetLivePlayerRegistryForTests, liveQualityCountNow, registerLivePlayer } from './LivePlayerPopup';
import { EXPLORE_POPUP_Z, LIVE_POPUP_ACTIVE_Z, SEARCH_POPUP_Z } from '../layoutUtils';
import { registerPreviewPlayback } from '../previewPlaybackBus';

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
    static Events = { MANIFEST_PARSED: 'manifestParsed', LEVEL_SWITCHED: 'levelSwitched', ERROR: 'error' };
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
    if (url.includes('/api/live/clip')) {
      // Honest capability report — never a fake clip.
      return new Response(JSON.stringify({
        available: false,
        reason: 'Kick has no public clip-creation API.',
        needed: [],
      }), { status: 200 });
    }
    if (url.includes('/api/live/captions/available')) {
      // Parakeet gate probe — captions available for the playing entry.
      return new Response(JSON.stringify({ available: true }), { status: 200 });
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
  it('fires ONE clip request on a double-click (5s cooldown ignores the second)', async () => {
    const fetchMock = mockFetch();
    renderPopup();
    await screen.findByTitle('Fullscreen');

    const btn = screen.getByTitle('Create a clip of the live stream');
    fireEvent.click(btn);
    fireEvent.click(btn); // within the 5s window — must be ignored

    const clipCalls = fetchMock.mock.calls.filter(([u]) => String(u).includes('/api/live/clip'));
    expect(clipCalls).toHaveLength(1);

    // The button flips into countdown mode (disabled, showing seconds).
    await waitFor(() => expect(btn).toBeDisabled());

    // Honest notification: reports the capability gap, never "clip saved".
    const notice = await screen.findByRole('status');
    expect(notice.textContent).toContain('Clip unavailable');
  });

  it('clamps the seconds input to 5..60', async () => {
    mockFetch();
    renderPopup();
    await screen.findByTitle('Fullscreen');

    const input = screen.getByLabelText('Clip duration (seconds)');
    fireEvent.change(input, { target: { value: '99' } });
    expect((input as HTMLInputElement).value).toBe('60');
    fireEvent.change(input, { target: { value: '0' } });
    expect((input as HTMLInputElement).value).toBe('5');
  });

  it('shows a fixed "s" suffix next to the seconds value', async () => {
    mockFetch();
    renderPopup();
    await screen.findByTitle('Fullscreen');

    const input = screen.getByLabelText('Clip duration (seconds)');
    expect((input as HTMLInputElement).value).toBe('30');
    // The "s" is a sibling span, never part of the input value — it cannot
    // be deleted, so it always re-appears by construction.
    const suffix = input.parentElement!.querySelector('span');
    expect(suffix?.textContent).toBe('s');
  });

  it('typing over a focused value replaces it (30 → 15)', async () => {
    mockFetch();
    renderPopup();
    await screen.findByTitle('Fullscreen');

    const input = screen.getByLabelText('Clip duration (seconds)') as HTMLInputElement;
    fireEvent.focus(input); // select-all on focus
    fireEvent.change(input, { target: { value: '15' } });
    expect(input.value).toBe('15');
  });

  it('Backspace on a fully-selected value removes ONE digit, then clamps (30 → 5)', async () => {
    mockFetch();
    renderPopup();
    await screen.findByTitle('Fullscreen');

    const input = screen.getByLabelText('Clip duration (seconds)') as HTMLInputElement;
    fireEvent.focus(input); // select-all on focus (selectionStart 0, end = len)
    input.setSelectionRange(0, input.value.length);
    fireEvent.keyDown(input, { key: 'Backspace' });
    expect(input.value).toBe('5');
  });

  it('TWITCH live clip opens the browser editor — NO /api/live/clip call (no Helix path)', async () => {
    const fetchMock = mockFetch();
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(null);
    render(
      <LivePlayerPopup
        entry={{ url: 'https://www.twitch.tv/titiltei', title: 'Late night', platform: 'twitch' }}
        entries={[{ url: 'https://www.twitch.tv/titiltei', title: 'Late night', platform: 'twitch' }]}
        channelName="titiltei"
        channelSlug="titiltei"
        onClose={vi.fn()}
        onOpenHit={vi.fn()}
        savedChannels={[]}
      />,
    );
    await screen.findByTitle('Fullscreen');

    fireEvent.click(screen.getByTitle('Create a clip of the live stream'));

    await waitFor(() => expect(openSpy).toHaveBeenCalledTimes(1));
    const url = String(openSpy.mock.calls[0][0]);
    expect(url).toContain('https://www.twitch.tv/titiltei');
    expect(url).toContain('vodrip_clip=1');
    expect(url).toContain('vodrip_end=30');
    expect(url).toContain('vodrip_title=');
    // The browser editor replaces the old server capability call entirely.
    const clipCalls = fetchMock.mock.calls.filter(([u]) => String(u).includes('/api/live/clip'));
    expect(clipCalls).toHaveLength(0);
    const notice = await screen.findByRole('status');
    expect(notice.textContent).toContain('Opening Twitch clip editor');
    openSpy.mockRestore();
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
  it('keeps the chat panel closed by default and guards a missing EventSource (jsdom)', async () => {
    mockFetch();
    renderPopup();
    await screen.findByTitle('Fullscreen');

    // Live preview chat is CLOSED by default.
    expect(document.querySelector('[data-live-chat-panel]')).toBeNull();

    // The header toggle opens the panel…
    fireEvent.click(screen.getByTitle('Live chat'));
    expect(document.querySelector('[data-live-chat-panel]')).toBeTruthy();
    // jsdom has no EventSource — the panel must degrade, not crash.
    expect(screen.getByText('Live chat unavailable')).toBeTruthy();

    // …and closes it again (first match — the panel's own X shares the title).
    fireEvent.click(screen.getAllByTitle('Close live chat')[0]);
    await waitFor(() => expect(document.querySelector('[data-live-chat-panel]')).toBeNull());
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

  it('renders the latest caption block over the video when available (PLAYING entry URL)', async () => {
    mockFetch();
    renderPopup();
    await screen.findByTitle('Fullscreen');
    // Captions default ON: the CC button and the caption EventSource appear
    // once the availability probe resolves.
    await screen.findByTitle('Hide captions');
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    const es = FakeEventSource.instances[0];
    // The stream URL resolves the playing entry's platform + slug (kick).
    expect(es.url).toContain('/api/live/captions?platform=kick');
    expect(es.url).toContain('channel=srdoglol');

    act(() => { es.fire('caption', JSON.stringify({ text: 'olá pessoal, bem-vindos ao canal', start: 1, end: 4 })); });
    expect(screen.getByText('olá pessoal, bem-vindos ao canal')).toBeTruthy();
    expect(document.querySelector('[data-live-captions-overlay]')).toBeTruthy();

    // A newer block REPLACES the previous one — the overlay never stacks.
    act(() => { es.fire('caption', JSON.stringify({ text: 'segunda legenda', start: 4, end: 7 })); });
    await waitFor(() => expect(screen.getByText('segunda legenda')).toBeTruthy());
    expect(screen.queryByText('olá pessoal, bem-vindos ao canal')).toBeNull();
  });

  it('toggle hides the overlay + closes the EventSource, re-enable reconnects', async () => {
    mockFetch();
    renderPopup();
    await screen.findByTitle('Hide captions');
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    const es = FakeEventSource.instances[0];
    act(() => { es.fire('caption', JSON.stringify({ text: 'legenda visível', start: 0, end: 3 })); });
    expect(screen.getByText('legenda visível')).toBeTruthy();

    fireEvent.click(screen.getByTitle('Hide captions'));
    expect(es.closed).toBe(true);
    expect(screen.queryByText('legenda visível')).toBeNull();
    expect(screen.queryByTitle('Hide captions')).toBeNull();
    expect(screen.getByTitle('Live captions')).toBeTruthy();

    fireEvent.click(screen.getByTitle('Live captions'));
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(2));
    expect(FakeEventSource.instances[1].closed).toBe(false);
    act(() => { FakeEventSource.instances[1].fire('caption', JSON.stringify({ text: 'legenda de novo', start: 3, end: 6 })); });
    expect(screen.getByText('legenda de novo')).toBeTruthy();
  });

  it('closes the caption EventSource when the popup unmounts', async () => {
    mockFetch();
    const view = renderPopup();
    await screen.findByTitle('Hide captions');
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    view.unmount();
    expect(FakeEventSource.instances[0].closed).toBe(true);
  });

  it('hides the overlay and stops after an offline event (no reconnect)', async () => {
    mockFetch();
    renderPopup();
    await screen.findByTitle('Hide captions');
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    const es = FakeEventSource.instances[0];
    act(() => { es.fire('caption', JSON.stringify({ text: 'última fala', start: 0, end: 3 })); });
    expect(screen.getByText('última fala')).toBeTruthy();

    act(() => { es.fire('offline', '{}'); });
    expect(es.closed).toBe(true);
    await waitFor(() => expect(screen.queryByText('última fala')).toBeNull());
    // Availability cleared → no CC button (and no further EventSources).
    expect(screen.queryByTitle('Hide captions')).toBeNull();
    expect(FakeEventSource.instances).toHaveLength(1);
  });

  it('shows no CC button or overlay when the parakeet gate reports unavailable', async () => {
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
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(0));
    expect(screen.queryByTitle('Hide captions')).toBeNull();
    expect(screen.queryByTitle('Live captions')).toBeNull();
    expect(document.querySelector('[data-live-captions-overlay]')).toBeNull();
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

describe('LivePlayerPopup volume menu outside-click', () => {
  it('closes on a mousedown on any other player button', async () => {
    renderPopup();
    await screen.findByTitle('Fullscreen');

    // Open the volume menu.
    fireEvent.click(screen.getByTitle('Volume'));
    expect(screen.getByLabelText('Volume')).toBeTruthy();

    // A mousedown on ANOTHER transport button (fullscreen) closes the menu —
    // the click lands outside the volume menu's own layout.
    fireEvent.mouseDown(screen.getByTitle('Fullscreen'));
    expect(screen.queryByLabelText('Volume')).toBeNull();
  });

  it('keeps the menu open for clicks inside it and the slider drag still works', async () => {
    renderPopup();
    await screen.findByTitle('Fullscreen');

    fireEvent.click(screen.getByTitle('Volume'));
    const slider = screen.getByLabelText('Volume') as HTMLInputElement;
    expect(slider).toBeTruthy();

    // mousedown on the slider (inside the menu layout) must NOT close it…
    fireEvent.mouseDown(slider);
    expect(screen.getByLabelText('Volume')).toBeTruthy();

    // …and dragging still adjusts the volume while the menu stays open.
    fireEvent.change(slider, { target: { value: '0.5' } });
    expect((screen.getByLabelText('Volume') as HTMLInputElement).value).toBe('0.5');
    expect(screen.getByLabelText('Volume')).toBeTruthy();
  });

  it('the toggle still opens and closes the menu', async () => {
    renderPopup();
    await screen.findByTitle('Fullscreen');

    const toggle = screen.getByTitle('Volume');
    fireEvent.click(toggle);
    expect(screen.getByLabelText('Volume')).toBeTruthy();
    fireEvent.click(toggle);
    expect(screen.queryByLabelText('Volume')).toBeNull();
    fireEvent.click(toggle);
    expect(screen.getByLabelText('Volume')).toBeTruthy();
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
    expect(rail.disabled).toBe(false);
    expect(rail.min).toBe('0');
    expect(rail.max).toBe('100');

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

    // Live edge = liveSyncPosition(100) + liveSyncDurationCount(1) × 2s = 102;
    // the back-buffer window is [102−30, 102−0.75] = [72, 101.25].
    hls.liveSyncPosition = 100;
    const video = document.querySelector('video') as HTMLVideoElement;
    video.currentTime = 95;

    fireEvent.keyDown(window, { key: 'ArrowLeft' });
    expect(video.currentTime).toBe(90); // 95 − 5, inside the window

    fireEvent.keyDown(window, { key: 'ArrowRight' });
    expect(video.currentTime).toBe(95); // 90 + 5

    // Clamped to the buffer's leading edge: 5 − 5 → −0 → 72.
    video.currentTime = 5;
    fireEvent.keyDown(window, { key: 'ArrowLeft' });
    expect(video.currentTime).toBe(72);

    // Clamped just below the live edge (0.75s safety): 106 + 5 → 111 → 101.25.
    video.currentTime = 106;
    fireEvent.keyDown(window, { key: 'ArrowRight' });
    expect(video.currentTime).toBe(101.25);

    // Never below 0: early stream (edge 14 → window [0, 13.25]).
    hls.liveSyncPosition = 12; // edge 14
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
  it('targets a ~2-5s player delay with count-based sync knobs on every platform', async () => {
    mockFetchWithLiveSrc();
    renderPopup();
    await waitFor(() => expect((window as unknown as { __livePopupHls?: InstanceType<typeof FakeHls> }).__livePopupHls).toBeTruthy());
    const hls = (window as unknown as { __livePopupHls?: InstanceType<typeof FakeHls> }).__livePopupHls!;

    // Count-based live-sync geometry — 1 segment behind the edge (≈2s target
    // at 2s Twitch/Kick segments); 6 segments (≈12s) is the force-resync
    // ceiling. hls.js 1.7 THROWS when count and duration variants are mixed,
    // so the duration knobs the adblock config injects are nulled out.
    expect(hls.config.liveSyncDurationCount).toBe(1);
    expect(hls.config.liveSyncDuration).toBeUndefined();
    expect(hls.config.liveMaxLatencyDurationCount).toBe(6);
    expect(hls.config.liveMaxLatencyDuration).toBeUndefined();

    // LL-HLS part handling on (backend prefers Twitch LL masters; non-LL
    // playlists play identically) + fast 1.5× catch-up when behind.
    expect(hls.config.lowLatencyMode).toBe(true);
    expect(hls.config.maxLiveSyncPlaybackRate).toBe(1.5);

    // Buffering-fix geometry retained: deep forward buffer, 30s retained
    // back-buffer for the arrow seek, finite live timeline (seekable).
    expect(hls.config.maxBufferLength).toBe(20);
    expect(hls.config.backBufferLength).toBe(30);
    expect(hls.config.liveDurationInfinity).toBe(false);

    // Live mode auto-starts; the replay guard still owns replay mode.
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

  it('stays visible while paused and while the volume menu is open', async () => {
    vi.useFakeTimers();
    renderPopup();
    await flushSession();

    const transport = document.querySelector('[data-live-transport]')!;
    const popup = document.querySelector('[data-live-popup]')!;
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

    // Menu open (bump first so the controls are visible) → never hides.
    fireEvent.mouseMove(popup);
    fireEvent.click(screen.getByTitle('Volume'));
    expect(screen.getByLabelText('Volume')).toBeTruthy();
    act(() => { vi.advanceTimersByTime(6000); });
    expect(transport.className).toContain('opacity-100');
  });

  it('keeps the header visible while the session is still loading', async () => {
    vi.useFakeTimers();
    // The session POST never resolves — loading stays true past any countdown.
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})));
    renderPopup();
    await act(async () => {});

    const header = document.querySelector('[data-live-header]')!;
    act(() => { vi.advanceTimersByTime(6000); }); // < SESSION_STALL_MS (8s)
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

  it('pins SOURCE on MANIFEST_PARSED with one player; a second player caps it to ≤480p; closing restores SOURCE', async () => {
    const hls = await mountLiveWithLevels();
    await act(async () => { hls.trigger(FakeHls.Events.MANIFEST_PARSED); });
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

  it('re-applies at MANIFEST_PARSED when the second player opened before the manifest parsed', async () => {
    mockFetchWithLiveSrc();
    renderPopup();
    await waitFor(() => expect((window as unknown as { __livePopupHls?: InstanceType<typeof FakeHls> }).__livePopupHls).toBeTruthy());
    const hls = (window as unknown as { __livePopupHls?: InstanceType<typeof FakeHls> }).__livePopupHls!;
    // Second player opens while levels are still empty — the count-change
    // apply no-ops; the parse-time apply must read the CURRENT count.
    let unsub2: (() => void) | undefined;
    await act(async () => { unsub2 = registerLivePlayer(() => {}); });
    expect(hls.currentLevel).toBe(-1); // nothing parsed yet, nothing applied
    hls.levels = [
      { height: 360, bitrate: 1_000_000 },
      { height: 480, bitrate: 2_500_000 },
      { height: 1080, bitrate: 6_000_000 },
    ];
    await act(async () => { hls.trigger(FakeHls.Events.MANIFEST_PARSED); });
    expect(hls.currentLevel).toBe(1); // multi → 480
    expect(hls.autoLevelEnabled).toBe(false);
    await act(async () => { unsub2!(); });
    expect(hls.currentLevel).toBe(2); // back to single → source
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

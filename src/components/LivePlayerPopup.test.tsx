import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Mock } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { LivePlayerPopup } from './LivePlayerPopup';
import { EXPLORE_POPUP_Z, LIVE_POPUP_ACTIVE_Z, SEARCH_POPUP_Z } from '../layoutUtils';

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

/** Minimal EventSource stub — tests drive onopen/onmessage per instance. */
class FakeEventSource {
  static instances: FakeEventSource[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public url: string) {
    FakeEventSource.instances.push(this);
  }
  close() {}
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

    // Two chat streams opened — one per live platform.
    expect(FakeEventSource.instances).toHaveLength(2);
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
    const kickEs = FakeEventSource.instances.find((es) => es.url.includes('platform=kick'))!;
    const twitchEs = FakeEventSource.instances.find((es) => es.url.includes('platform=twitch'))!;
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
    // Exactly two streams total — filtering never re-opens EventSources.
    expect(FakeEventSource.instances).toHaveLength(2);
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
    const twitchEs = FakeEventSource.instances.find((es) => es.url.includes('platform=twitch'))!;
    expect(twitchEs.url).toContain('slug=srdogg');
    // Kick page URL → slug from the path.
    const kickEs = FakeEventSource.instances.find((es) => es.url.includes('platform=kick'))!;
    expect(kickEs.url).toContain('slug=srdoglol');
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

  it('ignores arrow seeks while the rail is disabled (no archive)', async () => {
    renderPopup(); // default mock — no DVR archive → rail disabled
    await screen.findByTitle('Fullscreen');

    const rail = screen.getByRole('slider', { name: 'Seek back into the broadcast (replay)' }) as HTMLInputElement;
    expect(rail.disabled).toBe(true);

    const video = document.querySelector('video') as HTMLVideoElement;
    video.currentTime = 10;
    fireEvent(video, new Event('timeupdate'));

    fireEvent.keyDown(window, { key: 'ArrowRight' });
    fireEvent.keyDown(window, { key: 'ArrowLeft' });

    // No seek and no replay switch: still live, playhead untouched, no hls
    // instance even after the debounce window.
    expect(video.currentTime).toBe(10);
    expect(rail.disabled).toBe(true);
    await new Promise((r) => setTimeout(r, 300));
    expect((window as unknown as { __livePopupHls?: unknown }).__livePopupHls).toBeUndefined();
    expect(screen.queryByRole('slider', { name: 'Seek within replay' })).toBeNull();
  });
});

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Mock } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { LivePlayerPopup } from './LivePlayerPopup';
import { EXPLORE_POPUP_Z, LIVE_POPUP_ACTIVE_Z, SEARCH_POPUP_Z } from '../layoutUtils';

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

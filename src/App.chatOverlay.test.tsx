/**
 * Main-preview chat overlay host contract (App.tsx, data-preview-chat-overlay).
 *
 * The chat in the MAIN preview floats over the player container instead of
 * being a row sibling: opening it must not squeeze the player column, closing
 * it unmounts it, and fullscreen keeps it mounted (hidden) so its state
 * survives. Rendering the whole App is not feasible in jsdom (preview only
 * opens through the real URL→session flow), so this host mirrors the exact
 * structure App.tsx renders: preview row → player container → absolute
 * right-anchored overlay → controlled PreviewChatPanel. The one host state
 * observable without a session — the chat default — is pinned directly via
 * App's exported PREVIEW_CHAT_DEFAULT_OPEN (App.tsx imports cleanly in jsdom).
 */
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useState } from 'react';
import { PREVIEW_CHAT_DEFAULT_OPEN } from './App';
import PreviewChatPanel, { type PreviewPanelPayload } from './components/PreviewChatPanel';

const PAYLOAD: PreviewPanelPayload = {
  transcript: [
    { offset_sec: 0, text: 'hello world' },
    { offset_sec: 5, text: 'second line' },
  ],
  chat: [
    { offset_sec: 1, text: 'LETS GO', username: 'bob', spam_count: 1 },
  ],
  events: [],
  has_transcript: true,
  has_chat: true,
};

const origScrollIntoView = Element.prototype.scrollIntoView;

function mockPanelFetch(payload: PreviewPanelPayload) {
  const fn = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes('/api/preview/panel/')) {
      return new Response(JSON.stringify(payload), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    if (url.includes('/api/subtitles')) {
      return new Response(JSON.stringify({ rows: [] }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    return new Response(JSON.stringify({}), { status: 404 });
  });
  vi.stubGlobal('fetch', fn);
  return fn;
}

/** Mirror of the main-preview chat host JSX in App.tsx. */
function ChatOverlayHost({
  open,
  hidden = false,
  maxWidth,
}: {
  open: boolean;
  hidden?: boolean;
  maxWidth?: number;
}) {
  const [isOpen, setIsOpen] = useState(open);
  return (
    <div data-preview-panel className="flex flex-row gap-2">
      <div data-player-container className="relative flex-1">
        <div className="absolute inset-0" />
        {isOpen && (
          <div
            data-preview-chat-overlay
            className="absolute top-0 right-0 bottom-0 z-20 flex max-w-full"
          >
            <PreviewChatPanel
              platform="twitch"
              videoId="v1"
              currentTime={0}
              open={isOpen}
              onOpenChange={setIsOpen}
              hidden={hidden}
              maxWidth={maxWidth}
            />
          </div>
        )}
      </div>
    </div>
  );
}

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn();
});
afterAll(() => {
  Element.prototype.scrollIntoView = origScrollIntoView;
});
beforeEach(() => {
  localStorage.clear();
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe('main preview chat overlay host', () => {
  it('starts with the chat CLOSED — no preview click opens it; only the header toggle does', () => {
    // Host contract: the main preview's chat must NOT auto-open. App's state
    // is initialized from this exported constant (and reset to it on close),
    // so flipping the default back to open breaks this test.
    expect(PREVIEW_CHAT_DEFAULT_OPEN).toBe(false);
  });

  it('renders as an absolute overlay inside the player container, not a row sibling', async () => {
    mockPanelFetch(PAYLOAD);
    render(<ChatOverlayHost open />);
    await waitFor(() => expect(screen.getByText('LETS GO')).toBeTruthy());

    const row = document.querySelector('[data-preview-panel]')!;
    const container = document.querySelector('[data-player-container]')!;
    const overlay = document.querySelector('[data-preview-chat-overlay]')!;

    // The chat is NOT a layout sibling of the player column: the row's only
    // child is the container, so opening the chat cannot reduce its width.
    expect(row.children).toHaveLength(1);
    expect(row.children[0]).toBe(container);
    // The overlay floats over the video: absolutely positioned, right edge of
    // the container, above the video/controls (z-20).
    expect(container.contains(overlay)).toBe(true);
    for (const cls of ['absolute', 'top-0', 'right-0', 'bottom-0', 'z-20', 'flex', 'max-w-full']) {
      expect(overlay.classList.contains(cls)).toBe(true);
    }
    expect(overlay.querySelector('[data-preview-chat-panel]')).toBeTruthy();
  });

  it('unmounts the overlay when the chat closes', async () => {
    mockPanelFetch(PAYLOAD);
    render(<ChatOverlayHost open />);
    await waitFor(() => expect(screen.getByText('LETS GO')).toBeTruthy());

    fireEvent.click(screen.getByTitle('Collapse panel'));

    expect(document.querySelector('[data-preview-chat-overlay]')).toBeNull();
    expect(document.querySelector('[data-preview-chat-panel]')).toBeNull();
    expect(screen.queryByText('LETS GO')).toBeNull();
  });

  it('keeps the panel mounted (hidden) during fullscreen so state survives', async () => {
    mockPanelFetch(PAYLOAD);
    const { rerender } = render(<ChatOverlayHost open hidden />);
    await waitFor(() => expect(screen.getByText('LETS GO')).toBeTruthy());

    // Fullscreen: the overlay wrapper and panel stay in the DOM, display:none.
    const overlay = document.querySelector('[data-preview-chat-overlay]')!;
    const panel = document.querySelector('[data-preview-chat-panel]')!;
    expect(overlay).toBeTruthy();
    expect(panel.className).toContain('hidden');

    // Internal state (tab selection) is preserved across the hidden flip.
    fireEvent.click(screen.getByRole('button', { name: 'Transcript' }));
    rerender(<ChatOverlayHost open hidden={false} />);
    expect(screen.getByRole('button', { name: 'Transcript' }).getAttribute('aria-pressed')).toBe('true');
    await waitFor(() => expect(screen.getByText('hello world')).toBeTruthy());
  });

  it('clamps the overlay width to the player container (maxWidth)', async () => {
    mockPanelFetch(PAYLOAD);
    render(<ChatOverlayHost open maxWidth={250} />);
    const panel = document.querySelector('[data-preview-chat-panel]') as HTMLElement;
    // Stored default 320 clamped to the 250px container: no overflow.
    expect(panel.style.width).toBe('250px');
  });

  it('collapses to zero width when the container is below the panel minimum', async () => {
    mockPanelFetch(PAYLOAD);
    render(<ChatOverlayHost open maxWidth={100} />);
    const panel = document.querySelector('[data-preview-chat-panel]') as HTMLElement;
    expect(panel.style.width).toBe('0px');
  });
});

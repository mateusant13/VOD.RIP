import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useState } from 'react';
import PreviewChatPanel, { type PreviewPanelPayload } from './PreviewChatPanel';

const PAYLOAD: PreviewPanelPayload = {
  transcript: [
    { offset_sec: 0, text: 'hello world' },
    { offset_sec: 5, text: 'second line' },
    { offset_sec: 10, text: 'third line' },
    { offset_sec: 15, text: 'fourth line' },
  ],
  chat: [
    { offset_sec: 1, text: 'gg', username: 'alice', spam_count: 1 },
    { offset_sec: 3, text: 'LETS GO', username: 'bob', spam_count: 12 },
    { offset_sec: 8, text: 'hi', username: 'carol', spam_count: 1 },
    { offset_sec: 20, text: 'pog', username: 'dave', spam_count: 1 },
  ],
  has_transcript: true,
  has_chat: true,
};

const EMPTY_PAYLOAD: PreviewPanelPayload = {
  transcript: [],
  chat: [],
  has_transcript: false,
  has_chat: false,
};

/** jsdom has no scrollIntoView; the panel calls it for active-row sync. */
const origScrollIntoView = Element.prototype.scrollIntoView;

function mockPanelFetch(
  payload: PreviewPanelPayload | null,
  status = 200,
  failFor: (url: string) => boolean = () => false,
) {
  const fn = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes('/api/preview/panel/')) {
      if (failFor(url)) return new Response(JSON.stringify({ detail: 'boom' }), { status: 500 });
      return new Response(JSON.stringify(payload), {
        status,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    return new Response(JSON.stringify({}), { status: 404 });
  });
  vi.stubGlobal('fetch', fn);
  return fn;
}

function activeRowText(): string {
  const el = document.querySelector('[data-panel-row][aria-current="true"]');
  return el ? (el.textContent ?? '') : '';
}

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn();
});
afterAll(() => {
  Element.prototype.scrollIntoView = origScrollIntoView;
});
beforeEach(() => {
  // Flush rAF synchronously so the resize handler's direct style writes land.
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
    cb(0);
    return 0;
  });
  vi.stubGlobal('cancelAnimationFrame', () => {});
  localStorage.clear();
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe('PreviewChatPanel', () => {
  it('fetches the panel payload and renders chat rows with a ×N spam badge', async () => {
    const fetchMock = mockPanelFetch(PAYLOAD);
    render(<PreviewChatPanel platform="twitch" videoId="v1" currentTime={0} />);
    expect(fetchMock).toHaveBeenCalled();
    expect(String(fetchMock.mock.calls[0][0])).toContain('/api/preview/panel/twitch/v1?limit=');

    await waitFor(() => {
      expect(screen.getByText('LETS GO')).toBeTruthy();
    });
    expect(screen.getByText('alice:')).toBeTruthy();
    // spam_count > 1 → ×N badge (reuses the archive-search pattern).
    const badge = screen.getByText('×12');
    expect(badge).toBeTruthy();
    expect(badge.getAttribute('title')).toContain('identical messages collapsed');
    // spam_count === 1 rows carry no badge.
    expect(screen.queryByText('×1')).toBeNull();
  });

  it('switches tabs: transcript rows, subtitles caption, chat empty state', async () => {
    mockPanelFetch(PAYLOAD);
    render(<PreviewChatPanel platform="twitch" videoId="v1" currentTime={0} />);
    await waitFor(() => expect(screen.getByText('LETS GO')).toBeTruthy());

    fireEvent.click(screen.getByRole('button', { name: 'Transcript' }));
    await waitFor(() => expect(screen.getByText('hello world')).toBeTruthy());

    fireEvent.click(screen.getByRole('button', { name: 'Subtitles' }));
    // currentTime=0 → first transcript segment (offset 0) is the caption.
    await waitFor(() => {
      expect(document.querySelector('[data-subtitle-line]')?.textContent).toContain('hello world');
    });
  });

  it('empty payload shows per-tab empty states without breaking siblings', async () => {
    mockPanelFetch(EMPTY_PAYLOAD);
    render(<PreviewChatPanel platform="twitch" videoId="v1" currentTime={0} />);
    await waitFor(() => {
      expect(screen.getByText('No archived chat for this video.')).toBeTruthy();
    });
    fireEvent.click(screen.getByRole('button', { name: 'Transcript' }));
    await waitFor(() => {
      expect(screen.getByText('No transcript for this video.')).toBeTruthy();
    });
    fireEvent.click(screen.getByRole('button', { name: 'Subtitles' }));
    await waitFor(() => {
      expect(screen.getByText('No captions for this video.')).toBeTruthy();
    });
  });

  it('auto-switches from an empty Chat tab to a populated Transcript tab', async () => {
    mockPanelFetch({ ...PAYLOAD, chat: [], has_chat: false });
    render(<PreviewChatPanel platform="twitch" videoId="v1" currentTime={0} />);
    await waitFor(() => expect(screen.getByText('hello world')).toBeTruthy());
    expect(screen.queryByText('No archived chat for this video.')).toBeNull();
  });

  it('highlights the active chat/transcript row and updates the subtitle on currentTime change', async () => {
    mockPanelFetch(PAYLOAD);
    const { rerender } = render(
      <PreviewChatPanel platform="twitch" videoId="v1" currentTime={6} />,
    );
    await waitFor(() => expect(screen.getByText('LETS GO')).toBeTruthy());
    // chat offsets [1,3,8,20]: t=6 → last offset ≤ 6 is index 1 (LETS GO).
    expect(activeRowText()).toContain('LETS GO');

    // transcript offsets [0,5,10,15]: t=6 → index 1 (second line).
    fireEvent.click(screen.getByRole('button', { name: 'Transcript' }));
    await waitFor(() => expect(activeRowText()).toContain('second line'));

    fireEvent.click(screen.getByRole('button', { name: 'Subtitles' }));
    await waitFor(() => {
      expect(document.querySelector('[data-subtitle-line]')?.textContent).toContain('second line');
    });

    // Seek to t=12: transcript active row moves to index 2, caption to third line.
    rerender(<PreviewChatPanel platform="twitch" videoId="v1" currentTime={12} />);
    fireEvent.click(screen.getByRole('button', { name: 'Transcript' }));
    await waitFor(() => expect(activeRowText()).toContain('third line'));
    fireEvent.click(screen.getByRole('button', { name: 'Subtitles' }));
    await waitFor(() => {
      expect(document.querySelector('[data-subtitle-line]')?.textContent).toContain('third line');
    });
  });

  it('collapses and expands without losing data', async () => {
    mockPanelFetch(PAYLOAD);
    render(<PreviewChatPanel platform="twitch" videoId="v1" currentTime={0} />);
    await waitFor(() => expect(screen.getByText('LETS GO')).toBeTruthy());
    fireEvent.click(screen.getByTitle('Collapse panel'));
    expect(document.querySelector('[data-preview-chat-panel-collapsed]')).toBeTruthy();
    expect(screen.queryByText('LETS GO')).toBeNull(); // unmounted from DOM
    fireEvent.click(document.querySelector('[data-preview-chat-panel-collapsed]')!);
    await waitFor(() => expect(screen.getByText('LETS GO')).toBeTruthy()); // cached, no refetch
  });

  it('retries after a fetch error', async () => {
    const fetchMock = mockPanelFetch(PAYLOAD, 200, () => true); // always fail
    render(<PreviewChatPanel platform="twitch" videoId="v1" currentTime={0} />);
    await waitFor(() => {
      expect(screen.getByText("Couldn't load panel data.")).toBeTruthy();
    });
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/preview/panel/')) {
        return new Response(JSON.stringify(PAYLOAD), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response(JSON.stringify({}), { status: 404 });
    });
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(screen.getByText('LETS GO')).toBeTruthy());
  });

  it('render boundary: panel-internal state changes never re-render the player sibling', async () => {
    let playerRenders = 0;
    function PlayerStub() {
      playerRenders += 1;
      return <div data-testid="player-stub" />;
    }
    function Harness() {
      const [t] = useState(5);
      return (
        <div style={{ display: 'flex' }}>
          <PlayerStub />
          <PreviewChatPanel platform="twitch" videoId="v1" currentTime={t} />
        </div>
      );
    }
    mockPanelFetch(PAYLOAD);
    render(<Harness />);
    await waitFor(() => expect(screen.getByText('LETS GO')).toBeTruthy());
    const rendersAfterMount = playerRenders;

    // Tab switch (panel-internal state) → parent/player untouched.
    fireEvent.click(screen.getByRole('button', { name: 'Transcript' }));
    await waitFor(() => expect(screen.getByText('hello world')).toBeTruthy());
    expect(playerRenders).toBe(rendersAfterMount);

    // Collapse + re-expand (panel-internal state) → untouched.
    fireEvent.click(screen.getByTitle('Collapse panel'));
    fireEvent.click(document.querySelector('[data-preview-chat-panel-collapsed]')!);
    await waitFor(() => expect(screen.getByText('hello world')).toBeTruthy());
    expect(playerRenders).toBe(rendersAfterMount);

    // Resize drag (rAF + direct style writes) → untouched during AND after.
    const panelEl = document.querySelector('[data-preview-chat-panel]') as HTMLElement;
    panelEl.style.width = '320px';
    fireEvent.pointerDown(document.querySelector('[data-panel-resize-handle]')!, {
      pointerId: 1,
      clientX: 400,
    });
    fireEvent.pointerMove(panelEl, { pointerId: 1, clientX: 350 });
    expect(panelEl.style.width).toBe('370px'); // 320 + (400-350), direct write
    expect(playerRenders).toBe(rendersAfterMount);
    fireEvent.pointerUp(panelEl, { pointerId: 1, clientX: 350 });
    expect(playerRenders).toBe(rendersAfterMount);
    expect(panelEl.style.width).toBe('370px'); // committed state matches

    // Note: playback-time changes flow through the parent's pre-existing
    // previewTimeUi throttle (~4 Hz, not per frame) and re-render the whole
    // surface by design — that path exists before this panel. The contract
    // here is that PANEL-internal state never touches the parent/player.
  });

  it('hidden mode unmounts nothing and preserves state', async () => {
    mockPanelFetch(PAYLOAD);
    const { rerender } = render(
      <PreviewChatPanel platform="twitch" videoId="v1" currentTime={0} hidden />,
    );
    await waitFor(() => expect(screen.getByText('LETS GO')).toBeTruthy());
    const panel = document.querySelector('[data-preview-chat-panel]') as HTMLElement;
    expect(panel.className).toContain('hidden');
    rerender(<PreviewChatPanel platform="twitch" videoId="v1" currentTime={0} hidden={false} />);
    await waitFor(() => expect(screen.getByText('LETS GO')).toBeTruthy()); // cached, still there
  });
});

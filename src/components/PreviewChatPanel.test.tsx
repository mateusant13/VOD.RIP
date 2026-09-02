import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render as rtlRender, screen, waitFor } from '@testing-library/react';
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
  events: [
    { offset_sec: 3, end_sec: 3.6, event: 'Laughter', score: 0.93 },
    { offset_sec: 12.5, end_sec: 13.2, event: 'Clapping', score: 0.81 },
  ],
  has_transcript: true,
  has_chat: true,
};

const EMPTY_PAYLOAD: PreviewPanelPayload = {
  transcript: [],
  chat: [],
  events: [],
  has_transcript: false,
  has_chat: false,
};

/** jsdom has no scrollIntoView; the panel calls it for active-row sync. */
const origScrollIntoView = Element.prototype.scrollIntoView;

const SUBTITLES_PAYLOAD = {
  url: 'https://www.youtube.com/watch?v=yt1',
  lang: 'pt',
  source: 'manual',
  has_subtitles: true,
  rows: [
    { offset_sec: 1, text: 'primeira legenda' },
    { offset_sec: 6, text: 'segunda legenda' },
  ],
};

function mockPanelFetch(
  payload: PreviewPanelPayload | null,
  status = 200,
  failFor: (url: string) => boolean = () => false,
  subtitlesPayload: object | null = SUBTITLES_PAYLOAD,
  subtitlesDelayMs = 0,
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
    if (url.includes('/api/subtitles')) {
      if (failFor(url)) return new Response(JSON.stringify({ detail: 'boom' }), { status: 500 });
      // ponytail: executor form — tsconfig lib predates ES2024 Promise.withResolvers
      if (subtitlesDelayMs > 0)
        await new Promise<void>((resolve) => { setTimeout(resolve, subtitlesDelayMs); });
      return new Response(JSON.stringify(subtitlesPayload), {
        status,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    return new Response(JSON.stringify({}), { status: 404 });
  });
  vi.stubGlobal('fetch', fn);
  return fn;
}
/** Existing panel tests target chat; explicitly opt those fixtures into it
 * while the product default remains the transcription tab. */
function render(...args: Parameters<typeof rtlRender>) {
  const result = rtlRender(...args);
  const chat = screen.queryByRole('button', { name: 'Chat' });
  if (chat?.getAttribute('aria-pressed') === 'false') fireEvent.click(chat);
  return result;
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

  it('opens on transcription tab by default and preserves an explicit chat selection', async () => {
    mockPanelFetch(PAYLOAD);
    rtlRender(<PreviewChatPanel platform="twitch" videoId="v1" currentTime={0} />);
    await waitFor(() => expect(screen.getByText('hello world')).toBeTruthy());
    expect(screen.getByRole('button', { name: 'Transcript' })).toHaveAttribute('aria-pressed', 'true');
    fireEvent.click(screen.getByRole('button', { name: 'Chat' }));
    expect(screen.getByRole('button', { name: 'Chat' })).toHaveAttribute('aria-pressed', 'true');
  });
  it('fetches the panel payload at session-create, before the video starts', async () => {
    const fetchMock = mockPanelFetch(PAYLOAD);
    render(<PreviewChatPanel platform="twitch" videoId="v1" currentTime={0} />);
    // Session created → the archive payload fetches immediately; nothing the
    // panel fetches waits for canplay (the Twitch chat backfill must kick
    // off before playback starts).
    expect(fetchMock).toHaveBeenCalled();
    expect(String(fetchMock.mock.calls[0][0])).toContain('/api/preview/panel/twitch/v1?limit=');
    await waitFor(() => expect(screen.getByText('LETS GO')).toBeTruthy());
  });

  it('fetches YouTube subtitles at preview open, in parallel with video loading (no play gate)', async () => {
    const fetchMock = mockPanelFetch(EMPTY_PAYLOAD);
    const { rerender } = render(
      <PreviewChatPanel platform="youtube" videoId="yt1" currentTime={1.5} />,
    );
    // The live captions fetch starts as soon as the URL-only state is known
    // (session-create), NOT on canplay — subtitles load alongside the video.
    const urls = () => fetchMock.mock.calls.map((c) => String(c[0]));
    await waitFor(() => expect(urls().some((u) => u.includes('/api/subtitles'))).toBe(true));
    expect(urls().some((u) => u.includes('/api/preview/panel/'))).toBe(true);
    await waitFor(() => expect(screen.getByText('primeira legenda')).toBeTruthy());
    // Playback advancing must not re-trigger the fetch (deduped by videoId).
    rerender(<PreviewChatPanel platform="youtube" videoId="yt1" currentTime={12} />);
    expect(urls().filter((u) => u.includes('/api/subtitles')).length).toBe(1);
  });

  it('bounds the subtitles cache: >8 distinct videos prune the oldest (a pruned video re-fetches)', async () => {
    const fetchMock = mockPanelFetch(EMPTY_PAYLOAD);
    const { rerender } = render(
      <PreviewChatPanel platform="youtube" videoId="yt1" currentTime={0} />,
    );
    const subsCalls = () =>
      fetchMock.mock.calls.filter((c) => String(c[0]).includes('/api/subtitles')).length;
    await waitFor(() => expect(subsCalls()).toBe(1));

    // 8 more distinct videos fill the cache past 8 → the OLDEST (yt1) is pruned.
    for (let i = 2; i <= 9; i++) {
      rerender(<PreviewChatPanel platform="youtube" videoId={`yt${i}`} currentTime={0} />);
      await waitFor(() => expect(subsCalls()).toBe(i));
    }

    // Revisit yt1: pruned → fetched again instead of served from the cache
    // (an unbounded cache would have kept it and skipped the network).
    rerender(<PreviewChatPanel platform="youtube" videoId="yt1" currentTime={0} />);
    await waitFor(() => expect(subsCalls()).toBe(10));
  });

  it('renders subtitles that arrive late, after the panel opened', async () => {
    mockPanelFetch(EMPTY_PAYLOAD, 200, () => false, SUBTITLES_PAYLOAD, 60);
    render(<PreviewChatPanel platform="youtube" videoId="yt1" currentTime={1.5} />);
    // The fetch is already in flight from preview open; the caption appears
    // the moment the response lands — no playback signal involved.
    await waitFor(() => expect(screen.getByText('primeira legenda')).toBeTruthy());
  });

  it('colors usernames: platform color wins, palette fallback otherwise', async () => {
    const colored: PreviewPanelPayload = {
      ...PAYLOAD,
      chat: [
        // Platform-provided color (YouTube authorNameTextColor).
        { offset_sec: 1, text: 'oi', username: 'alice', spam_count: 1, color: '#FF0033' },
        // No stored color → deterministic palette by (username, platform).
        { offset_sec: 3, text: 'olá', username: 'bob', spam_count: 1 },
      ],
    };
    mockPanelFetch(colored);
    render(<PreviewChatPanel platform="youtube" videoId="v1" currentTime={0} />);
    await waitFor(() => expect(screen.getByText('oi')).toBeTruthy());
    const alice = screen.getByText('alice:');
    const bob = screen.getByText('bob:');
    // jsdom normalizes hex to rgb().
    expect(alice.getAttribute('style')).toContain('rgb(255, 0, 51)');
    expect(bob.getAttribute('style')).toMatch(/color:\s*rgb\(/);
    // The same user renders the same fallback color across renders.
    expect(bob.getAttribute('style')).toBe(screen.getByText('bob:').getAttribute('style'));
  });

  it('switches tabs: transcript rows, subtitles caption, chat empty state', async () => {
    mockPanelFetch(PAYLOAD);
    render(<PreviewChatPanel platform="youtube" videoId="v1" currentTime={0} />);
    await waitFor(() => expect(screen.getByText('LETS GO')).toBeTruthy());

    fireEvent.click(screen.getByRole('button', { name: 'Transcript' }));
    await waitFor(() => expect(screen.getByText('hello world')).toBeTruthy());

    fireEvent.click(screen.getByRole('button', { name: 'Subtitles' }));
    // currentTime=0 → first transcript segment (offset 0) is the caption.
    await waitFor(() => {
      expect(document.querySelector('[data-subtitle-line]')?.textContent).toContain('hello world');
    });
  });

  it('hides the Subtitles tab for non-YouTube platforms (Twitch/Kick VODs, clips)', async () => {
    mockPanelFetch(PAYLOAD);
    render(<PreviewChatPanel platform="twitch" videoId="v1" currentTime={0} />);
    await waitFor(() => expect(screen.getByText('LETS GO')).toBeTruthy());
    // The archived transcript stays available — under its Transcript tab.
    expect(screen.getByRole('button', { name: 'Transcript' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Subtitles' })).toBeNull();
  });

  it('interleaves acoustic events into the transcript timeline in offset order', async () => {
    mockPanelFetch(PAYLOAD);
    render(<PreviewChatPanel platform="twitch" videoId="v1" currentTime={0} />);
    await waitFor(() => expect(screen.getByText('LETS GO')).toBeTruthy());

    fireEvent.click(screen.getByRole('button', { name: 'Transcript' }));
    await waitFor(() => {
      // Events must sit between the transcript segments at their offsets:
      // Laughter@3 between hello world@0 and second line@5; Clapping@12.5
      // between third line@10 and fourth line@15 (NOT appended at the end).
      const rows = Array.from(document.querySelectorAll('[data-panel-row]')).map(
        (el) => el.textContent ?? '',
      );
      expect(rows[0]).toContain('hello world');
      expect(rows[1]).toContain('Laughter');
      expect(rows[2]).toContain('second line');
      expect(rows[3]).toContain('third line');
      expect(rows[4]).toContain('Clapping');
      expect(rows[5]).toContain('fourth line');
    });

    // Tooltip carries the exact range + confidence.
    const laugh = document.querySelector('[data-event-row="Laughter"]') as HTMLElement;
    expect(laugh).toBeTruthy();
    expect(laugh.title).toContain('93%');
    expect(laugh.title).toContain('0.6s');
    expect(laugh.textContent).toContain('(0.6s)');
  });

  it('empty payload shows per-tab empty states; no Subtitles tab off-YouTube', async () => {
    mockPanelFetch(EMPTY_PAYLOAD);
    render(<PreviewChatPanel platform="twitch" videoId="v1" currentTime={0} />);
    await waitFor(() => {
      expect(screen.getByText('No archived chat for this video.')).toBeTruthy();
    });
    fireEvent.click(screen.getByRole('button', { name: 'Transcript' }));
    await waitFor(() => {
      expect(screen.getByText('No transcript for this video.')).toBeTruthy();
    });
    // Subtitles are YouTube-only — a Twitch/Kick VOD has no Subtitles tab.
    expect(screen.queryByRole('button', { name: 'Subtitles' })).toBeNull();
  });

  it('null platform/videoId (clip/live/channel previews) shows an explanatory message instead of a blank panel', async () => {
    const fetchMock = mockPanelFetch(PAYLOAD);
    const { rerender } = render(<PreviewChatPanel platform={null} videoId={null} currentTime={0} />);
    const MSG = "Chat and transcript history aren't available for this kind of preview.";
    await waitFor(() => {
      expect(screen.getByText(MSG)).toBeTruthy();
    });
    expect(fetchMock).not.toHaveBeenCalled(); // nothing to fetch without a key
    // The message is per-panel, not per-tab — switching tabs must not blank it.
    fireEvent.click(screen.getByRole('button', { name: 'Transcript' }));
    await waitFor(() => expect(screen.getByText(MSG)).toBeTruthy());
    // Once a key resolves (e.g. an archived VOD URL pasted in), the panel
    // fetches and renders normally instead of showing the message.
    rerender(<PreviewChatPanel platform="twitch" videoId="v1" currentTime={0} />);
    fireEvent.click(screen.getByRole('button', { name: 'Chat' }));
    await waitFor(() => expect(screen.getByText('LETS GO')).toBeTruthy());
    expect(screen.queryByText(MSG)).toBeNull();
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
      <PreviewChatPanel platform="youtube" videoId="v1" currentTime={6} />,
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
    rerender(<PreviewChatPanel platform="youtube" videoId="v1" currentTime={12} />);
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

  it('controlled mode: host owns open state, no collapsed strip, collapse reports out', async () => {
    mockPanelFetch(PAYLOAD);
    const onOpenChange = vi.fn();
    const { rerender } = render(
      <PreviewChatPanel platform="twitch" videoId="v1" currentTime={0} open={false} onOpenChange={onOpenChange} />,
    );
    // Host-owned open: the collapsed side strip never renders (the explore
    // mini preview opens chat from its own button next to 'Search this video').
    expect(document.querySelector('[data-preview-chat-panel-collapsed]')).toBeNull();
    // Host flips the prop → panel renders without internal state.
    rerender(<PreviewChatPanel platform="twitch" videoId="v1" currentTime={0} open onOpenChange={onOpenChange} />);
    await waitFor(() => expect(screen.getByText('LETS GO')).toBeTruthy());
    // Collapse button reports the close intent to the host instead of
    // closing itself.
    fireEvent.click(screen.getByTitle('Collapse panel'));
    expect(onOpenChange).toHaveBeenCalledWith(false);
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

  it('fetches and renders live subtitles for a URL-only YouTube preview', async () => {
    const fetchMock = mockPanelFetch(EMPTY_PAYLOAD);
    render(<PreviewChatPanel platform="youtube" videoId="yt1" currentTime={1.5} />);
    await waitFor(() => expect(screen.getByText('primeira legenda')).toBeTruthy());
    // Subtitles-only: no chat or transcript tabs are offered.
    expect(screen.queryByRole('button', { name: 'Chat' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Transcript' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Subtitles' })).toBeTruthy();
    const subsUrl = String(
      fetchMock.mock.calls.map((c) => String(c[0])).find((u) => u.includes('/api/subtitles')) ?? '',
    );
    expect(subsUrl).toContain(
      '/api/subtitles?url=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3Dyt1&langs=en,pt,es',
    );
  });

  it('shows an explicit empty state when a URL-only YouTube video has no captions', async () => {
    mockPanelFetch(EMPTY_PAYLOAD, 200, () => false, {
      ...SUBTITLES_PAYLOAD,
      has_subtitles: false,
      lang: null,
      source: null,
      rows: [],
    });
    render(<PreviewChatPanel platform="youtube" videoId="yt1" currentTime={0} />);
    await waitFor(() =>
      expect(screen.getByText('No subtitles available for this video.')).toBeTruthy(),
    );
  });

  it('shows an error state when the live subtitles fetch fails', async () => {
    mockPanelFetch(EMPTY_PAYLOAD, 200, (u) => u.includes('/api/subtitles'));
    render(<PreviewChatPanel platform="youtube" videoId="yt1" currentTime={0} />);
    await waitFor(() => expect(screen.getByText("Couldn't load subtitles.")).toBeTruthy());
  });

  it('keeps the DB-driven subtitles path for archived YouTube videos', async () => {
    const fetchMock = mockPanelFetch(PAYLOAD);
    render(<PreviewChatPanel platform="youtube" videoId="yt1" currentTime={11} />);
    await waitFor(() => expect(screen.getByText('LETS GO')).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: 'Subtitles' }));
    await waitFor(() => expect(screen.getByText('third line')).toBeTruthy());
    // Archived videos never call the live-subtitles endpoint.
    const calls = fetchMock.mock.calls.map((c) => String(c[0]));
    expect(calls.some((u) => u.includes('/api/subtitles'))).toBe(false);
  });

  it('searches chat inline: filters rows, counts matches, navigates with Enter', async () => {
    mockPanelFetch(PAYLOAD);
    render(<PreviewChatPanel platform="twitch" videoId="v1" currentTime={0} />);
    await waitFor(() => expect(screen.getByText('LETS GO')).toBeTruthy());

    const input = screen.getByRole('textbox', { name: 'Search chat history' });
    // 'g' matches alice 'gg', bob 'LETS GO', dave 'pog' → 3 matches.
    fireEvent.change(input, { target: { value: 'g' } });
    await waitFor(() => {
      expect(screen.getByText('1/3')).toBeTruthy();
    });
    // Unmatched rows are filtered out of the DOM.
    expect(screen.queryByText('hi')).toBeNull();
    expect(screen.getByText('gg')).toBeTruthy();
    expect(screen.getByText('LETS GO')).toBeTruthy();
    expect(screen.getByText('pog')).toBeTruthy();

    // Enter cycles next; Shift+Enter goes back; wraps at the ends.
    fireEvent.keyDown(input, { key: 'Enter' });
    await waitFor(() => expect(screen.getByText('2/3')).toBeTruthy());
    fireEvent.keyDown(input, { key: 'Enter' });
    fireEvent.keyDown(input, { key: 'Enter' });
    await waitFor(() => expect(screen.getByText('1/3')).toBeTruthy()); // wrapped
    fireEvent.keyDown(input, { key: 'Enter', shiftKey: true });
    await waitFor(() => expect(screen.getByText('3/3')).toBeTruthy());

    // Esc clears the query and restores the full list.
    fireEvent.keyDown(input, { key: 'Escape' });
    await waitFor(() => expect(screen.getByText('hi')).toBeTruthy());
    expect(screen.queryByText('1/3')).toBeNull();
  });

  it('shows an empty state when the chat search matches nothing', async () => {
    mockPanelFetch(PAYLOAD);
    render(<PreviewChatPanel platform="twitch" videoId="v1" currentTime={0} />);
    await waitFor(() => expect(screen.getByText('LETS GO')).toBeTruthy());
    fireEvent.change(screen.getByRole('textbox', { name: 'Search chat history' }), {
      target: { value: 'xyzzy' },
    });
    await waitFor(() =>
      expect(screen.getByText('No chat messages match “xyzzy”.')).toBeTruthy(),
    );
  });

  it('matches usernames too, not just message text', async () => {
    mockPanelFetch(PAYLOAD);
    render(<PreviewChatPanel platform="twitch" videoId="v1" currentTime={0} />);
    await waitFor(() => expect(screen.getByText('LETS GO')).toBeTruthy());
    fireEvent.change(screen.getByRole('textbox', { name: 'Search chat history' }), {
      target: { value: 'carol' },
    });
    await waitFor(() => expect(screen.getByText('hi')).toBeTruthy());
    expect(screen.queryByText('LETS GO')).toBeNull();
    expect(screen.getByText('1/1')).toBeTruthy();
  });

  it('polls while the Twitch backfill runs and refreshes once after done', async () => {
    vi.useFakeTimers();
    try {
      let call = 0;
      const fn = vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/api/preview/panel/')) {
          call += 1;
          const p: PreviewPanelPayload =
            call === 1
              ? { ...PAYLOAD, chat: [PAYLOAD.chat[0]], backfill: 'running', backfill_progress: 0.25, total_rows: 1 }
              : call === 2
                ? { ...PAYLOAD, chat: PAYLOAD.chat.slice(0, 2), backfill: 'running', backfill_progress: 0.5, total_rows: 2 }
                : { ...PAYLOAD, backfill: 'done', backfill_progress: 1, total_rows: 4 };
          return new Response(JSON.stringify(p), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        return new Response(JSON.stringify({}), { status: 404 });
      });
      vi.stubGlobal('fetch', fn);
      render(<PreviewChatPanel platform="twitch" videoId="v1" currentTime={0} />);
      // Initial fetch fires at session-create (no playback gate).
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(fn.mock.calls.length).toBe(1);
      // Poll 1 (still running): chat grows.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2500); // PANEL_POLL_MS
      });
      expect(fn.mock.calls.length).toBe(2);
      expect(screen.getByText('LETS GO')).toBeTruthy();
      // Poll 2 returns done → exactly one transition refresh after it.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2500);
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(fn.mock.calls.length).toBe(4);
      // Loop stopped: no further requests while the archive is complete.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2500 * 3);
      });
      expect(fn.mock.calls.length).toBe(4);
    } finally {
      vi.useRealTimers();
    }
  });

  it('shows a loading indicator + backfill progress while the backfill is running, not a terminal empty state', async () => {
    mockPanelFetch({
      ...EMPTY_PAYLOAD,
      backfill: 'running',
      backfill_progress: 0.05,
      total_rows: 0,
    });
    render(<PreviewChatPanel platform="twitch" videoId="v1" currentTime={0} />);
    await waitFor(() => expect(screen.getByText('Loading chat…')).toBeTruthy());
    expect(screen.queryByText('No archived chat for this video.')).toBeNull();
    // Thin progress bar renders the received backfill_progress (0.05 → 5%).
    const bar = screen.getByRole('progressbar');
    expect(bar.getAttribute('aria-valuenow')).toBe('5');
    expect(bar.getAttribute('aria-label')).toBe('Backfill progress');
    expect(screen.getByText('5%')).toBeTruthy();
  });

  it('seeks the player when a chat row is clicked (onSeek), rows show the pointer affordance', async () => {
    mockPanelFetch(PAYLOAD);
    const onSeek = vi.fn();
    render(<PreviewChatPanel platform="twitch" videoId="v1" currentTime={0} onSeek={onSeek} />);
    await waitFor(() => expect(screen.getByText('LETS GO')).toBeTruthy());
    expect(document.querySelector('[data-panel-row]')?.className).toContain('cursor-pointer');
    fireEvent.click(screen.getByText('LETS GO'));
    expect(onSeek).toHaveBeenCalledTimes(1);
    expect(onSeek).toHaveBeenCalledWith(3);
  });

  it('seeks the player when a transcript row or acoustic-event row is clicked', async () => {
    mockPanelFetch(PAYLOAD);
    const onSeek = vi.fn();
    render(<PreviewChatPanel platform="twitch" videoId="v1" currentTime={0} onSeek={onSeek} />);
    await waitFor(() => expect(screen.getByText('LETS GO')).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: 'Transcript' }));
    await waitFor(() => expect(screen.getByText('second line')).toBeTruthy());
    fireEvent.click(screen.getByText('second line'));
    expect(onSeek).toHaveBeenCalledWith(5);
    fireEvent.click(screen.getByText('Laughter'));
    expect(onSeek).toHaveBeenCalledWith(3);
  });

  it('seeks the player when the subtitle caption is clicked', async () => {
    mockPanelFetch(PAYLOAD);
    const onSeek = vi.fn();
    render(<PreviewChatPanel platform="youtube" videoId="v1" currentTime={6} onSeek={onSeek} />);
    await waitFor(() => expect(screen.getByText('LETS GO')).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: 'Subtitles' }));
    await waitFor(() => {
      expect(document.querySelector('[data-subtitle-line]')?.textContent).toContain('second line');
    });
    fireEvent.click(document.querySelector('[data-subtitle-line]') as HTMLElement);
    expect(onSeek).toHaveBeenCalledTimes(1);
    expect(onSeek).toHaveBeenCalledWith(5);
  });

  it('keeps rows scroll-only (no pointer affordance, no seek) without onSeek', async () => {
    mockPanelFetch(PAYLOAD);
    const onSeek = vi.fn();
    render(<PreviewChatPanel platform="twitch" videoId="v1" currentTime={0} />);
    await waitFor(() => expect(screen.getByText('LETS GO')).toBeTruthy());
    const rows = Array.from(document.querySelectorAll('[data-panel-row]'));
    expect(rows.length).toBeGreaterThan(0);
    expect(rows.every((r) => !r.className.includes('cursor-pointer'))).toBe(true);
    fireEvent.click(screen.getByText('LETS GO'));
    expect(onSeek).not.toHaveBeenCalled();
  });

  it('scrolling moves the render window through the whole list (no blank-spacer regions)', async () => {
    const bigChat = Array.from({ length: 600 }, (_, i) => ({
      offset_sec: i,
      text: `msg ${i}`,
      username: `u${i}`,
      spam_count: 1,
    }));
    mockPanelFetch({ ...PAYLOAD, chat: bigChat });
    render(<PreviewChatPanel platform="twitch" videoId="v1" currentTime={0} />);
    await waitFor(() => expect(screen.getByText('msg 0')).toBeTruthy());
    // Scrolling far past the playhead row (0) must reveal rows far beyond
    // the ±WINDOW render window: the window rides the scroll position once
    // the user stops following (the old code pinned it to the playhead and
    // showed blank spacers out here).
    const listEl = document.querySelector('[data-panel-rows]') as HTMLElement;
    // CHAT_ROW_H = 24 → row 500 sits at scrollTop 12000.
    Object.defineProperty(listEl, 'scrollTop', { value: 12000, configurable: true });
    Object.defineProperty(listEl, 'clientHeight', { value: 300, configurable: true });
    // First event only consumes the stale programmatic-scroll flag (jsdom's
    // scrollIntoView stub fires no scroll event); the second is the user's.
    fireEvent.scroll(listEl);
    fireEvent.scroll(listEl);
    expect(screen.getByText('msg 500')).toBeTruthy();
    // The far head of the list is unmounted — the window truly moved.
    expect(screen.queryByText('msg 0')).toBeNull();
  });

  it('shows a note when the backend returned a bounded chat slice (chat_truncated)', async () => {
    mockPanelFetch({ ...PAYLOAD, chat_truncated: true, total_rows: 1200 });
    render(<PreviewChatPanel platform="twitch" videoId="v1" currentTime={0} />);
    await waitFor(() => expect(screen.getByText('LETS GO')).toBeTruthy());
    expect(
      screen.getByText('Chat history is incomplete — 4 of 1200 messages shown.'),
    ).toBeTruthy();
    expect(document.querySelector('[data-chat-truncated]')).toBeTruthy();
  });

  it('renders no truncated note when the payload is complete', async () => {
    mockPanelFetch(PAYLOAD);
    render(<PreviewChatPanel platform="twitch" videoId="v1" currentTime={0} />);
    await waitFor(() => expect(screen.getByText('LETS GO')).toBeTruthy());
    expect(document.querySelector('[data-chat-truncated]')).toBeNull();
  });

  it('start/end markers: hover-revealed S/E set the boundary, chips show + clear it, lifted to the host', async () => {
    mockPanelFetch(PAYLOAD);
    const onSeek = vi.fn();
    const onMarkersChange = vi.fn();
    render(
      <PreviewChatPanel
        platform="twitch"
        videoId="v1"
        currentTime={0}
        onSeek={onSeek}
        onMarkersChange={onMarkersChange}
      />,
    );
    await waitFor(() => expect(screen.getByText('LETS GO')).toBeTruthy());

    // Every rendered chat row carries the hover-revealed green S / red E
    // affordances (pure CSS reveal — present in the DOM regardless).
    expect(document.querySelectorAll('[data-marker-set="start"]').length).toBeGreaterThan(0);
    expect(document.querySelectorAll('[data-marker-set="end"]').length).toBeGreaterThan(0);

    // Click the green START on the row at offset 3 (LETS GO): boundary set,
    // no seek, chip + row badge show the filled state.
    const row3 = screen.getByText('LETS GO').closest('[data-panel-row]') as HTMLElement;
    fireEvent.click(row3.querySelector('[data-marker-set="start"]') as HTMLElement);
    await waitFor(() => {
      expect(onMarkersChange).toHaveBeenLastCalledWith({ start: 3, end: null });
    });
    expect(onSeek).not.toHaveBeenCalled(); // marker click must not seek
    expect(
      (document.querySelector('[data-marker-chip="start"]') as HTMLElement).textContent,
    ).toContain('0:03');
    expect((row3.querySelector('[data-marker-set="start"]') as HTMLElement).className).toContain(
      'bg-[#53fc18]',
    );

    // Red END on the row at offset 8 (hi): range is now [3, 8].
    const row8 = screen.getByText('hi').closest('[data-panel-row]') as HTMLElement;
    fireEvent.click(row8.querySelector('[data-marker-set="end"]') as HTMLElement);
    await waitFor(() => {
      expect(onMarkersChange).toHaveBeenLastCalledWith({ start: 3, end: 8 });
    });
    expect(
      (document.querySelector('[data-marker-chip="end"]') as HTMLElement).textContent,
    ).toContain('0:08');

    // Clamp: setting END before the START pulls START down (never inverted).
    const row1 = screen.getByText('gg').closest('[data-panel-row]') as HTMLElement;
    fireEvent.click(row1.querySelector('[data-marker-set="end"]') as HTMLElement);
    await waitFor(() => {
      expect(onMarkersChange).toHaveBeenLastCalledWith({ start: 1, end: 1 });
    });

    // Chips clear each boundary.
    fireEvent.click(document.querySelector('[data-marker-chip="end"]') as HTMLElement);
    await waitFor(() => {
      expect(onMarkersChange).toHaveBeenLastCalledWith({ start: 1, end: null });
    });
    fireEvent.click(document.querySelector('[data-marker-chip="start"]') as HTMLElement);
    await waitFor(() => {
      expect(onMarkersChange).toHaveBeenLastCalledWith({ start: null, end: null });
    });
  });

  it('clears markers when the panel switches to another video', async () => {
    mockPanelFetch(PAYLOAD);
    const onMarkersChange = vi.fn();
    const { rerender } = render(
      <PreviewChatPanel platform="twitch" videoId="v1" currentTime={0} onMarkersChange={onMarkersChange} />,
    );
    await waitFor(() => expect(screen.getByText('LETS GO')).toBeTruthy());
    const row3 = screen.getByText('LETS GO').closest('[data-panel-row]') as HTMLElement;
    fireEvent.click(row3.querySelector('[data-marker-set="start"]') as HTMLElement);
    await waitFor(() => {
      expect(onMarkersChange).toHaveBeenLastCalledWith({ start: 3, end: null });
    });

    // Same panel instance, new video: the pair resets and the host learns
    // about the clear (a stale range must not ride onto the next download).
    rerender(
      <PreviewChatPanel platform="twitch" videoId="v2" currentTime={0} onMarkersChange={onMarkersChange} />,
    );
    await waitFor(() => expect(screen.getByText('LETS GO')).toBeTruthy());
    expect(onMarkersChange).toHaveBeenLastCalledWith({ start: null, end: null });
    expect(
      (document.querySelector('[data-marker-chip="start"]') as HTMLElement).textContent,
    ).not.toContain('0:03');
  });
});

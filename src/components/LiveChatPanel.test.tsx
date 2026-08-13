/**
 * LiveChatPanel — Chatterino-style backlog pre-fill from /api/chat/history.
 *
 * On mount the panel must fetch archived chat captured BEFORE the session
 * (per source) and render it as pre-populated rows (oldest→newest, MAX_ROWS
 * truncation), while live SSE rows still append on top. The backlog fetch is
 * best-effort: a failed fetch must render an empty panel, never crash.
 *
 * jsdom has no EventSource, so the SSE effect degrades to 'unsupported' —
 * that is exactly the state these tests rely on for the pure-backlog cases;
 * the live-append case stubs EventSource (FakeEventSource pattern from
 * LivePlayerPopup.test.tsx).
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, waitFor } from '@testing-library/react';
import LiveChatPanel, { type LiveChatSource } from './LiveChatPanel';

const HISTORY = {
  messages: [
    { username: 'alice', text: 'old one', ts: '2026-08-01T10:00:00.000Z', color: '#ff0000' },
    { username: 'bob', text: 'middle one', ts: '2026-08-01T10:00:10.000Z', color: null },
    { username: 'carol', text: 'newest one', ts: '2026-08-01T10:00:20.000Z', color: '#00ff00' },
  ],
};

const KICK_HISTORY = {
  messages: [
    { username: 'kuser', text: 'kick old', ts: '2026-08-01T09:00:00.000Z', color: null },
    { username: 'kuser', text: 'kick new', ts: '2026-08-01T11:00:00.000Z', color: null },
  ],
};

/** Stub EventSource — tests drive onopen/onmessage per instance. */
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

/**
 * Mock fetch: serves /api/chat/history per platform and a benign emotes
 * payload; everything else 404s. Returns the mock so tests can assert calls.
 */
function mockPanelFetch(byPlatform: Record<string, object> = { twitch: HISTORY }, failHistory = false) {
  const fn = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes('/api/chat/history')) {
      if (failHistory) return new Response(JSON.stringify({ detail: 'boom' }), { status: 500 });
      for (const [platform, payload] of Object.entries(byPlatform)) {
        if (url.includes(`platform=${platform}`)) {
          return new Response(JSON.stringify(payload), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
      }
      return new Response(JSON.stringify({ messages: [] }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    if (url.includes('/api/chat/emotes')) {
      return new Response(JSON.stringify({ emotes: [] }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    return new Response(JSON.stringify({}), { status: 404 });
  });
  vi.stubGlobal('fetch', fn);
  return fn;
}

function rowTexts(): string[] {
  return [...document.querySelectorAll('[data-live-chat-row]')].map((el) => el.textContent ?? '');
}

afterEach(() => {
  vi.unstubAllGlobals();
  FakeEventSource.instances = [];
});

describe('LiveChatPanel backlog pre-fill', () => {
  it('pre-populates rows from archived chat history, oldest first', async () => {
    const fn = mockPanelFetch();
    render(
      <LiveChatPanel sources={[{ platform: 'twitch', slug: 'chan' }]} onClose={() => {}} />,
    );
    await waitFor(() => expect(document.querySelectorAll('[data-live-chat-row]')).toHaveLength(3));
    const call = fn.mock.calls.find((c) => String(c[0]).includes('/api/chat/history'))!;
    expect(String(call[0])).toContain('/api/chat/history?platform=twitch&slug=chan&limit=300');
    const texts = rowTexts();
    expect(texts[0]).toContain('old one');
    expect(texts[1]).toContain('middle one');
    expect(texts[2]).toContain('newest one');
  });

  it('merges multiple sources in recency order with per-platform fetches', async () => {
    const fn = mockPanelFetch({ twitch: HISTORY, kick: KICK_HISTORY });
    const sources: LiveChatSource[] = [
      { platform: 'twitch', slug: 'chan' },
      { platform: 'kick', slug: 'chan' },
    ];
    render(<LiveChatPanel sources={sources} onClose={() => {}} />);
    await waitFor(() => expect(document.querySelectorAll('[data-live-chat-row]')).toHaveLength(5));
    const urls = fn.mock.calls.map((c) => String(c[0])).filter((u) => u.includes('/api/chat/history'));
    expect(urls).toHaveLength(2);
    expect(urls.some((u) => u.includes('platform=twitch'))).toBe(true);
    expect(urls.some((u) => u.includes('platform=kick'))).toBe(true);
    // Oldest→newest across both platforms (kick old < alice < carol < kick new).
    const texts = rowTexts();
    expect(texts[0]).toContain('kick old');
    expect(texts[1]).toContain('old one');
    expect(texts[3]).toContain('newest one');
    expect(texts[4]).toContain('kick new');
  });

  it('renders an empty panel when the history fetch fails (silent)', async () => {
    mockPanelFetch({ twitch: HISTORY }, true);
    render(
      <LiveChatPanel sources={[{ platform: 'twitch', slug: 'chan' }]} onClose={() => {}} />,
    );
    // Give the fetch a beat to fail; the panel must stay up, no crash, no rows.
    await new Promise((r) => setTimeout(r, 20));
    expect(document.querySelector('[data-live-chat-panel]')).toBeTruthy();
    expect(document.querySelectorAll('[data-live-chat-row]')).toHaveLength(0);
  });

  it('truncates the backlog to MAX_ROWS (300)', async () => {
    const messages = Array.from({ length: 310 }, (_, i) => ({
      username: 'u',
      text: `msg ${i}`,
      ts: new Date(Date.UTC(2026, 7, 1, 10, 0, i)).toISOString(),
      color: null,
    }));
    mockPanelFetch({ twitch: { messages } });
    render(
      <LiveChatPanel sources={[{ platform: 'twitch', slug: 'chan' }]} onClose={() => {}} />,
    );
    await waitFor(() => expect(document.querySelectorAll('[data-live-chat-row]')).toHaveLength(300));
    // The NEWEST 300 of the 310 stay — the oldest 10 are dropped.
    expect(rowTexts()[0]).toContain('msg 10');
    expect(rowTexts()[299]).toContain('msg 309');
  });

  it('still appends live rows after the backlog pre-fill', async () => {
    mockPanelFetch();
    vi.stubGlobal('EventSource', FakeEventSource);
    render(
      <LiveChatPanel sources={[{ platform: 'twitch', slug: 'chan' }]} onClose={() => {}} />,
    );
    await waitFor(() => expect(document.querySelectorAll('[data-live-chat-row]')).toHaveLength(3));
    const es = FakeEventSource.instances.find((e) => e.url.includes('platform=twitch'))!;
    expect(es).toBeTruthy();
    es.onopen?.();
    es.onmessage?.({
      data: JSON.stringify({ username: 'live_user', text: 'just now' }),
    } as MessageEvent);
    await waitFor(() => expect(document.querySelectorAll('[data-live-chat-row]')).toHaveLength(4));
    const texts = rowTexts();
    expect(texts[0]).toContain('old one');
    expect(texts[3]).toContain('just now');
  });
});

describe('LiveChatPanel initial scroll position', () => {
  /**
   * jsdom has no layout, so the scroll container reads 0/0/0 by default.
   * Model a container whose backlog overflows the viewport (scrollHeight
   * 1000 > clientHeight 100) — the panel must land at the bottom (scrollTop
   * == scrollHeight) when the prefill lands, not at the top (scrollTop 0).
   */
  function mockOverflowingScroll(): HTMLElement {
    const scrollEl = document.querySelector('[data-live-chat-scroll]') as HTMLElement;
    Object.defineProperty(scrollEl, 'clientHeight', { value: 100, configurable: true });
    Object.defineProperty(scrollEl, 'scrollHeight', { value: 1000, configurable: true });
    scrollEl.scrollTop = 0;
    return scrollEl;
  }

  it('opens scrolled to the newest messages (bottom) once the backlog prefill lands', async () => {
    mockPanelFetch();
    render(
      <LiveChatPanel sources={[{ platform: 'twitch', slug: 'chan' }]} onClose={() => {}} />,
    );
    const scrollEl = mockOverflowingScroll();
    await waitFor(() => expect(document.querySelectorAll('[data-live-chat-row]')).toHaveLength(3));
    await waitFor(() => expect(scrollEl.scrollTop).toBe(1000));
  });

  it('does not yank a user scrolled up when the prefill lands late', async () => {
    mockPanelFetch();
    render(
      <LiveChatPanel sources={[{ platform: 'twitch', slug: 'chan' }]} onClose={() => {}} />,
    );
    const scrollEl = mockOverflowingScroll();
    // User reads history (scrolls up) while the backlog fetch is in flight.
    scrollEl.scrollTop = 800;
    fireEvent.scroll(scrollEl);
    await waitFor(() => expect(document.querySelectorAll('[data-live-chat-row]')).toHaveLength(3));
    // The late prefill must not yank them back to the live edge.
    expect(scrollEl.scrollTop).toBe(800);
  });
});

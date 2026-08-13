import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import {
  ChatEmoteText,
  splitChatEmotes,
  useChatEmotes,
  type EmoteMap,
} from './chatEmotes';

function emoteMap(pairs: Array<[string, string]>): EmoteMap {
  return new Map(pairs);
}

function mockEmotesFetch(emotes: Array<{ name: string; url: string }>) {
  const fn = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes('/api/chat/emotes')) {
      return new Response(JSON.stringify({ emotes }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    return new Response(JSON.stringify({}), { status: 404 });
  });
  vi.stubGlobal('fetch', fn);
  return fn;
}

/** Probes useChatEmotes — exposes size + entries for assertions. */
function HookProbe({ platform, slug }: { platform: string | null; slug?: string | null }) {
  const emotes = useChatEmotes(platform, slug);
  return (
    <span data-testid="emotes" data-count={emotes.size}>
      {[...emotes.entries()].map(([name, url]) => `${name}=${url}`).join('|')}
    </span>
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('splitChatEmotes', () => {
  it('splits a known emote token into an emote segment with the right url', () => {
    const segs = splitChatEmotes(
      'pog KEKW gg',
      emoteMap([['KEKW', 'https://cdn.example/KEKW.png']]),
    );
    // Whitespace separators are their own text segments (split on /(\s+)/
    // keeping separators); concatenating reproduces the original text.
    expect(segs).toEqual([
      { text: 'pog' },
      { text: ' ' },
      { emote: 'KEKW', url: 'https://cdn.example/KEKW.png' },
      { text: ' ' },
      { text: 'gg' },
    ]);
    expect(segs.map((s) => ('text' in s ? s.text : s.emote)).join('')).toBe('pog KEKW gg');
  });

  it('is case-sensitive: KEKW matches but kekw does not', () => {
    const m = emoteMap([['KEKW', 'https://cdn.example/KEKW.png']]);
    expect(splitChatEmotes('KEKW', m)).toEqual([
      { emote: 'KEKW', url: 'https://cdn.example/KEKW.png' },
    ]);
    expect(splitChatEmotes('kekw', m)).toEqual([{ text: 'kekw' }]);
    expect(splitChatEmotes('KEKW kekw', m)).toEqual([
      { emote: 'KEKW', url: 'https://cdn.example/KEKW.png' },
      { text: ' ' },
      { text: 'kekw' },
    ]);
  });

  it('keeps non-matching tokens verbatim including whitespace', () => {
    const segs = splitChatEmotes('  a  b   ', emoteMap([['x', 'https://cdn.example/x.png']]));
    expect(segs.every((s) => 'text' in s)).toBe(true);
    // Concatenating the segments reproduces the original text exactly.
    expect(segs.map((s) => ('text' in s ? s.text : s.emote)).join('')).toBe('  a  b   ');
  });

  it('empty map → everything stays text', () => {
    const segs = splitChatEmotes('hello KEKW world', new Map());
    expect(segs).toEqual([
      { text: 'hello' },
      { text: ' ' },
      { text: 'KEKW' },
      { text: ' ' },
      { text: 'world' },
    ]);
  });

  it('is whole-word only: fooKEKW does not match KEKW', () => {
    const segs = splitChatEmotes('fooKEKW', emoteMap([['KEKW', 'https://cdn.example/KEKW.png']]));
    expect(segs).toEqual([{ text: 'fooKEKW' }]);
  });
});

describe('ChatEmoteText', () => {
  it('renders emote tokens as inline imgs and the rest as verbatim text', () => {
    const m = emoteMap([['KEKW', 'https://cdn.example/KEKW.png']]);
    const { container } = render(<ChatEmoteText text="hi KEKW there" emotes={m} />);
    const img = screen.getByAltText('KEKW');
    expect(img).toHaveAttribute('src', 'https://cdn.example/KEKW.png');
    expect(img).toHaveAttribute('title', 'KEKW');
    // Text runs and whitespace separators stay verbatim — the rendered text
    // (imgs excluded) reproduces the message exactly.
    expect(screen.getByText('hi')).toBeTruthy();
    expect(screen.getByText('there')).toBeTruthy();
    expect(container.textContent).toBe('hi  there');
  });
});

describe('useChatEmotes', () => {
  it('fetches channel emotes for twitch + slug and caches (no refetch on re-render)', async () => {
    const fetchMock = mockEmotesFetch([{ name: 'KEKW', url: 'https://cdn.example/KEKW.png' }]);
    const { rerender } = render(<HookProbe platform="twitch" slug="srdogg" />);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toContain(
      '/api/chat/emotes?platform=twitch&slug=srdogg',
    );
    await waitFor(() =>
      expect(screen.getByTestId('emotes').getAttribute('data-count')).toBe('1'),
    );
    expect(screen.getByTestId('emotes').textContent).toBe('KEKW=https://cdn.example/KEKW.png');
    // Cache hit: a re-render (or a second consumer) must not re-fetch.
    rerender(<HookProbe platform="twitch" slug="srdogg" />);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('skips the fetch entirely for non-twitch platforms and missing slug', () => {
    const fetchMock = mockEmotesFetch([]);
    const { rerender } = render(<HookProbe platform="kick" slug="srdoglol" />);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.getByTestId('emotes').getAttribute('data-count')).toBe('0');
    rerender(<HookProbe platform="twitch" slug={null} />);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.getByTestId('emotes').getAttribute('data-count')).toBe('0');
  });

  it('returns an empty map on fetch failure — chat must never break', async () => {
    const fn = vi.fn(async () => new Response(JSON.stringify({ detail: 'boom' }), { status: 500 }));
    vi.stubGlobal('fetch', fn);
    render(<HookProbe platform="twitch" slug="downchan" />);
    await waitFor(() =>
      expect(screen.getByTestId('emotes').getAttribute('data-count')).toBe('0'),
    );
  });
});

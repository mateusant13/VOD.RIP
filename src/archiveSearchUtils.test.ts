import { describe, expect, it } from 'vitest';
import {
  ARCHIVE_KINDS,
  buildArchiveVodUrl,
  buildSearchUrl,
  firstMatchIndex,
  formatArchiveOffset,
  groupChatWindow,
  highlightQuerySpans,
  isValidDateParam,
  kindLabel,
  snippetAroundMatch,
  todayIso,
  type ArchiveChatMessage,
} from './archiveSearchUtils';

describe('formatArchiveOffset', () => {
  it('formats sub-minute offsets as mm:ss', () => {
    expect(formatArchiveOffset(0)).toBe('00:00');
    expect(formatArchiveOffset(5)).toBe('00:05');
    expect(formatArchiveOffset(65)).toBe('01:05');
    expect(formatArchiveOffset(3599)).toBe('59:59');
  });

  it('expands to h:mm:ss past an hour', () => {
    expect(formatArchiveOffset(3600)).toBe('1:00:00');
    expect(formatArchiveOffset(3725)).toBe('1:02:05');
    expect(formatArchiveOffset(7200 + 61)).toBe('2:01:01');
  });

  it('floors floats and clamps negatives/NaN', () => {
    expect(formatArchiveOffset(90.9)).toBe('01:30');
    expect(formatArchiveOffset(-12)).toBe('00:00');
    expect(formatArchiveOffset(Number.NaN)).toBe('00:00');
  });
});

describe('highlightQuerySpans', () => {
  it('finds case-insensitive word matches', () => {
    expect(highlightQuerySpans('Watch the ShacoMATE play', 'shacomate')).toEqual([
      { start: 10, end: 19 },
    ]);
    expect(highlightQuerySpans('SHACOMATE shacomate Shacomate', 'shacomate')).toEqual([
      { start: 0, end: 9 },
      { start: 10, end: 19 },
      { start: 20, end: 29 },
    ]);
  });

  it('respects word boundaries — no partial-word hits', () => {
    expect(highlightQuerySpans('chatter chats chat', 'chat')).toEqual([{ start: 14, end: 18 }]);
    expect(highlightQuerySpans('gadgetzinho gadgets', 'gadget')).toEqual([]);
  });

  it('matches every query word and merges overlapping ranges', () => {
    // Adjacent words keep their gap unhighlighted (two ranges, space between).
    expect(highlightQuerySpans('the red red fox', 'red red')).toEqual([
      { start: 4, end: 7 },
      { start: 8, end: 11 },
    ]);
    // Duplicate matches (case variants of the same word) merge into one range.
    expect(highlightQuerySpans('red red', 'Red red')).toEqual([
      { start: 0, end: 3 },
      { start: 4, end: 7 },
    ]);
    expect(highlightQuerySpans('alpha beta gamma', 'beta gamma')).toEqual([
      { start: 6, end: 10 },
      { start: 11, end: 16 },
    ]);
  });

  it('escapes regex metacharacters in the query', () => {
    expect(highlightQuerySpans('cost is $5.00 today', '$5.00')).toEqual([
      { start: 8, end: 13 },
    ]);
  });

  it('returns [] for empty text, empty query, or no match', () => {
    expect(highlightQuerySpans('', 'x')).toEqual([]);
    expect(highlightQuerySpans('hello world', '')).toEqual([]);
    expect(highlightQuerySpans('hello world', '   ')).toEqual([]);
    expect(highlightQuerySpans('hello world', 'zzz')).toEqual([]);
  });
});

describe('firstMatchIndex', () => {
  it('points at the first match, -1 when absent', () => {
    expect(firstMatchIndex('say shacomate now', 'shacomate')).toBe(4);
    expect(firstMatchIndex('nothing here', 'shacomate')).toBe(-1);
  });
});

describe('snippetAroundMatch', () => {
  it('windows around the first match with ellipses', () => {
    const long = 'aaaa '.repeat(30) + 'shacomate moment ' + 'bbbb '.repeat(30);
    const snip = snippetAroundMatch(long, 'shacomate');
    expect(snip.startsWith('…')).toBe(true);
    expect(snip.endsWith('…')).toBe(true);
    expect(snip).toContain('shacomate moment');
    expect(snip.length).toBeLessThan(long.length);
  });

  it('returns the full short text when it fits', () => {
    expect(snippetAroundMatch('hi shacomate bye', 'shacomate')).toBe('hi shacomate bye');
  });

  it('falls back to a leading window when no match exists', () => {
    const long = 'x'.repeat(200);
    const snip = snippetAroundMatch(long, 'nope');
    expect(snip.endsWith('…')).toBe(true);
    expect(snip.length).toBeLessThan(long.length);
  });

  it('handles empty inputs', () => {
    expect(snippetAroundMatch('', 'x')).toBe('');
    expect(snippetAroundMatch('abc', '')).toBe('abc');
  });
});

describe('groupChatWindow', () => {
  const msg = (offset_sec: number, username = 'u'): ArchiveChatMessage => ({
    platform: 'twitch',
    video_id: 'v1',
    offset_sec,
    username,
    text: `m${offset_sec}`,
  });

  it('splits strictly before vs at/after the hit offset', () => {
    const messages = [msg(1), msg(29.5), msg(30), msg(31), msg(60)];
    expect(groupChatWindow(messages, 30)).toEqual({
      before: [msg(1), msg(29.5)],
      after: [msg(30), msg(31), msg(60)],
    });
  });

  it('keeps input order and handles empty lists', () => {
    expect(groupChatWindow([], 30)).toEqual({ before: [], after: [] });
    const unsorted = [msg(40), msg(10), msg(20)];
    expect(groupChatWindow(unsorted, 30)).toEqual({
      before: [msg(10), msg(20)],
      after: [msg(40)],
    });
  });

  it('treats the exact boundary as after (marker line sits at the hit)', () => {
    expect(groupChatWindow([msg(30)], 30).after).toEqual([msg(30)]);
  });
});

describe('buildSearchUrl', () => {
  it('sends q and the default limit only', () => {
    expect(buildSearchUrl({ query: 'shaco' })).toBe('/api/archive/search?q=shaco&limit=30');
  });

  it('URL-encodes the query and honors a custom limit', () => {
    expect(buildSearchUrl({ query: 'lol classico', limit: 10 })).toBe(
      '/api/archive/search?q=lol+classico&limit=10',
    );
  });

  it('omits unset filters entirely', () => {
    expect(
      buildSearchUrl({ query: 'x', channel: '', platforms: [], kinds: [], dateFrom: '', dateTo: '' }),
    ).toBe('/api/archive/search?q=x&limit=30');
  });

  it('adds channel, comma-joined platforms and kinds, and date bounds', () => {
    const url = buildSearchUrl({
      query: 'bronzinhos',
      channel: 'lubu',
      platforms: ['twitch', 'kick'],
      kinds: ['vod', 'clip'],
      dateFrom: '2026-07-30',
      dateTo: '2026-08-01',
    });
    expect(url).toBe(
      '/api/archive/search?q=bronzinhos&channel=lubu&platform=twitch%2Ckick'
      + '&kind=vod%2Cclip&date_from=2026-07-30&date_to=2026-08-01&limit=30',
    );
  });

  it('drops invalid calendar dates instead of sending them', () => {
    const url = buildSearchUrl({ query: 'x', dateFrom: '2026-02-30', dateTo: 'not-a-date' });
    expect(url).toBe('/api/archive/search?q=x&limit=30');
  });

  it('emits source only when not both, and videoId when set', () => {
    expect(buildSearchUrl({ query: 'x', source: 'transcript' })).toBe(
      '/api/archive/search?q=x&source=transcript&limit=30',
    );
    expect(buildSearchUrl({ query: 'x', source: 'chat' })).toBe(
      '/api/archive/search?q=x&source=chat&limit=30',
    );
    expect(buildSearchUrl({ query: 'x', videoId: 'abc123' })).toBe(
      '/api/archive/search?q=x&video_id=abc123&limit=30',
    );
    expect(buildSearchUrl({ query: 'x', source: 'both', videoId: 'v1' })).toBe(
      '/api/archive/search?q=x&video_id=v1&limit=30',
    );
    expect(buildSearchUrl({ query: 'x', source: 'both', videoId: '' })).toBe(
      '/api/archive/search?q=x&limit=30',
    );
  });

  it('emits lang only when set, after dates', () => {
    expect(buildSearchUrl({ query: 'x', lang: 'pt' })).toBe(
      '/api/archive/search?q=x&lang=pt&limit=30',
    );
    expect(
      buildSearchUrl({ query: 'x', lang: 'en', dateFrom: '2026-07-30' }),
    ).toBe('/api/archive/search?q=x&date_from=2026-07-30&lang=en&limit=30');
    expect(buildSearchUrl({ query: 'x', lang: '' })).toBe(
      '/api/archive/search?q=x&limit=30',
    );
  });

  it('emits semantic only when enabled', () => {
    expect(buildSearchUrl({ query: 'x', semantic: true })).toBe(
      '/api/archive/search?q=x&semantic=1&limit=30',
    );
    expect(buildSearchUrl({ query: 'x', semantic: false })).toBe(
      '/api/archive/search?q=x&limit=30',
    );
    expect(buildSearchUrl({ query: 'x', semantic: true, lang: 'pt' })).toBe(
      '/api/archive/search?q=x&lang=pt&semantic=1&limit=30',
    );
  });
});

describe('isValidDateParam', () => {
  it('accepts real YYYY-MM-DD dates', () => {
    expect(isValidDateParam('2026-07-30')).toBe(true);
    expect(isValidDateParam('2024-02-29')).toBe(true); // leap year
  });

  it('rejects malformed and impossible dates', () => {
    for (const bad of ['2026-02-30', '2026-13-01', '2026-00-10', '2026-1-1', '30/07/2026', '', '2026-07-30T00:00:00']) {
      expect(isValidDateParam(bad)).toBe(false);
    }
  });
});

describe('kindLabel', () => {
  it('maps the four archive kinds to uppercase labels', () => {
    expect(ARCHIVE_KINDS).toEqual(['vod', 'clip', 'short', 'live']);
    expect(kindLabel('vod')).toBe('VOD');
    expect(kindLabel('clip')).toBe('CLIP');
    expect(kindLabel('short')).toBe('SHORT');
    expect(kindLabel('live')).toBe('LIVE');
  });

  it('passes unknown/empty values through harmlessly', () => {
    expect(kindLabel('movie')).toBe('movie');
    expect(kindLabel('')).toBe('');
    expect(kindLabel(null)).toBe('');
    expect(kindLabel(undefined)).toBe('');
  });
});

describe('buildArchiveVodUrl', () => {
  it('builds per-platform preview URLs', () => {
    expect(buildArchiveVodUrl('twitch', 'v123456789', 'somechan')).toBe(
      'https://www.twitch.tv/videos/123456789',
    );
    expect(buildArchiveVodUrl('Twitch', '123456789')).toBe(
      'https://www.twitch.tv/videos/123456789',
    );
    expect(buildArchiveVodUrl('youtube', 'dQw4w9WgXcQ')).toBe(
      'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
    );
    expect(buildArchiveVodUrl('kick', 'abc-123', 'xqc')).toBe(
      'https://kick.com/xqc/videos/abc-123',
    );
  });

  it('falls back to the slugless kick route when channel is unknown', () => {
    expect(buildArchiveVodUrl('kick', 'abc-123', '')).toBe('https://kick.com/videos/abc-123');
    expect(buildArchiveVodUrl('kick', 'abc-123', null)).toBe('https://kick.com/videos/abc-123');
  });

  it('returns empty for watchdog synthetic ids (no watchable URL)', () => {
    expect(buildArchiveVodUrl('youtube', 'youtube-live-lubumr-1785714293393', 'lubumr')).toBe('');
    expect(buildArchiveVodUrl('twitch', 'twitch-live-x-1234567890')).toBe('');
    expect(buildArchiveVodUrl('kick', 'youtube-live-someone_else-1', 'someone')).toBe('');
    // real ids must not be mistaken for synthetic ones
    expect(buildArchiveVodUrl('youtube', 'dQw4w9WgXcQ')).toBe(
      'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
    );
    expect(buildArchiveVodUrl('twitch', 'v123456789')).toBe(
      'https://www.twitch.tv/videos/123456789',
    );
  });
});

describe('buildSearchUrl username filter', () => {
  it('omits the username param when unset or empty', () => {
    expect(buildSearchUrl({ query: 'x' })).toBe('/api/archive/search?q=x&limit=30');
    expect(buildSearchUrl({ query: 'x', username: '' })).toBe('/api/archive/search?q=x&limit=30');
    expect(buildSearchUrl({ query: 'x', username: null })).toBe('/api/archive/search?q=x&limit=30');
  });

  it('sends the username param', () => {
    expect(buildSearchUrl({ query: 'x', username: 'scriptingkata' })).toBe(
      '/api/archive/search?q=x&username=scriptingkata&limit=30',
    );
  });

  it('strips a leading @ (YouTube stores the @handle)', () => {
    expect(buildSearchUrl({ query: 'x', username: '@Scriptingkata' })).toBe(
      '/api/archive/search?q=x&username=Scriptingkata&limit=30',
    );
  });

  it('URL-encodes display names with spaces', () => {
    expect(buildSearchUrl({ query: 'x', username: 'Scripting Kata' })).toBe(
      '/api/archive/search?q=x&username=Scripting+Kata&limit=30',
    );
  });

  it('passes comma-separated user lists through unmodified', () => {
    expect(buildSearchUrl({ query: '', username: 'Scriptingkata,AlguemAe' })).toBe(
      '/api/archive/search?q=&username=Scriptingkata%2CAlguemAe&limit=30',
    );
    // A leading @ on the first token is stripped client-side; the backend
    // strips per token too (YouTube stores @handles).
    expect(buildSearchUrl({ query: 'x', username: '@Scriptingkata,@aranha' })).toBe(
      '/api/archive/search?q=x&username=Scriptingkata%2C%40aranha&limit=30',
    );
  });
});

describe('todayIso', () => {
  it('returns a valid local YYYY-MM-DD for today', () => {
    const t = todayIso();
    expect(t).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(isValidDateParam(t)).toBe(true);
    const d = new Date();
    const expectY = `${d.getFullYear()}`;
    const expectM = `${d.getMonth() + 1}`.padStart(2, '0');
    const expectD = `${d.getDate()}`.padStart(2, '0');
    expect(t).toBe(`${expectY}-${expectM}-${expectD}`);
  });
});

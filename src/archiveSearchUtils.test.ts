import { describe, expect, it } from 'vitest';
import {
  buildArchiveVodUrl,
  firstMatchIndex,
  formatArchiveOffset,
  groupChatWindow,
  highlightQuerySpans,
  snippetAroundMatch,
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
});

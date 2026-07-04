/**
 * Unit tests for formatters.ts — pure formatting functions.
 */
import { describe, it, expect } from 'vitest';
import {
  fmtDuration,
  fmtShort,
  fmtClipDuration,
  formatClipDurationHuman,
  normalizeVideoDateInput,
  fmtDate,
  fmtRelativeAgo,
  fmtDateAndAgo,
  parseVideoTs,
  parseHmsDurationString,
  fmtViews,
  formatBytes,
  basename,
} from './formatters';

describe('fmtDuration', () => {
  it('formats seconds as MM:SS', () => {
    expect(fmtDuration(0)).toBe('0:00');
    expect(fmtDuration(59)).toBe('0:59');
    expect(fmtDuration(61)).toBe('1:01');
    expect(fmtDuration(3661)).toBe('1:01:01');
  });

  it('rounds down and clamps to 0', () => {
    expect(fmtDuration(-5)).toBe('0:00');
    expect(fmtDuration(1.9)).toBe('0:01');
  });

  it('handles hours with padding', () => {
    expect(fmtDuration(3600)).toBe('1:00:00');
    expect(fmtDuration(86399)).toBe('23:59:59');
  });
});

describe('fmtShort', () => {
  it('formats seconds as Xh Ym', () => {
    expect(fmtShort(0)).toBe('0m');
    expect(fmtShort(59)).toBe('0m');
    expect(fmtShort(61)).toBe('1m');
    expect(fmtShort(3661)).toBe('1h 1m');
    expect(fmtShort(7200)).toBe('2h 0m');
  });
});

describe('fmtClipDuration', () => {
  it('formats as seconds', () => {
    expect(fmtClipDuration(5)).toBe('5s');
    expect(fmtClipDuration(0)).toBe('0s');
    expect(fmtClipDuration(120)).toBe('120s');
  });

  it('clamps to 0', () => {
    expect(fmtClipDuration(-1)).toBe('0s');
  });
});

describe('formatClipDurationHuman', () => {
  it('formats short clips as seconds', () => {
    expect(formatClipDurationHuman(5)).toBe('5s');
    expect(formatClipDurationHuman(59)).toBe('59s');
  });

  it('formats longer clips as M:SS', () => {
    expect(formatClipDurationHuman(60)).toBe('1:00');
    expect(formatClipDurationHuman(90)).toBe('1:30');
    expect(formatClipDurationHuman(125)).toBe('2:05');
  });

  it('clamps to minimum 1 second', () => {
    expect(formatClipDurationHuman(0)).toBe('1s');
    expect(formatClipDurationHuman(-5)).toBe('1s');
  });
});

describe('normalizeVideoDateInput', () => {
  it('converts Kick format to ISO', () => {
    expect(normalizeVideoDateInput('2024-01-15 10:30:00')).toBe('2024-01-15T10:30:00Z');
  });

  it('appends Z to ISO without timezone', () => {
    expect(normalizeVideoDateInput('2024-01-15T10:30:00')).toBe('2024-01-15T10:30:00Z');
  });

  it('leaves ISO with timezone unchanged', () => {
    expect(normalizeVideoDateInput('2024-01-15T10:30:00+00:00')).toBe('2024-01-15T10:30:00+00:00');
    expect(normalizeVideoDateInput('2024-01-15T10:30:00Z')).toBe('2024-01-15T10:30:00Z');
  });
});

describe('fmtDate', () => {
  it('formats YYYYMMDD Twitch dates', () => {
    expect(fmtDate('20240115')).toBe('2024-01-15');
  });

  it('formats ISO dates', () => {
    expect(fmtDate('2024-01-15T10:30:00Z')).toBe('2024-01-15');
  });

  it('handles null and empty', () => {
    expect(fmtDate(null)).toBe('');
    expect(fmtDate(undefined)).toBe('');
    expect(fmtDate('')).toBe('');
  });
});

describe('fmtRelativeAgo', () => {
  it('returns "just now" for current time', () => {
    const now = new Date().toISOString();
    expect(fmtRelativeAgo(now)).toBe('just now');
  });

  it('returns minutes ago', () => {
    const fiveMinAgo = new Date(Date.now() - 5 * 60 * 1000).toISOString();
    expect(fmtRelativeAgo(fiveMinAgo)).toBe('5 mins ago');
  });

  it('returns hours ago', () => {
    const twoHoursAgo = new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString();
    expect(fmtRelativeAgo(twoHoursAgo)).toBe('2 hours ago');
  });

  it('returns days ago', () => {
    const threeDaysAgo = new Date(Date.now() - 3 * 24 * 60 * 60 * 1000).toISOString();
    expect(fmtRelativeAgo(threeDaysAgo)).toBe('3 days ago');
  });

  it('returns empty for null', () => {
    expect(fmtRelativeAgo(null)).toBe('');
  });
});

describe('fmtDateAndAgo', () => {
  it('combines date and ago', () => {
    const result = fmtDateAndAgo(new Date().toISOString());
    expect(result).toMatch(/^\d{4}-\d{2}-\d{2} · just now$/);
  });

  it('returns combined date and ago when both are available', () => {
    const result = fmtDateAndAgo('20240115');
    expect(result).toContain('2024-01-15');
    expect(result).toContain('days ago');
  });

  it('returns empty string when value is null', () => {
    expect(fmtDateAndAgo(null)).toBe('');
  });
});

describe('parseVideoTs', () => {
  it('parses YYYYMMDD', () => {
    const ts = parseVideoTs('20240115');
    expect(ts).toBeGreaterThan(0);
  });

  it('parses ISO string', () => {
    const ts = parseVideoTs('2024-01-15T00:00:00Z');
    expect(ts).toBeGreaterThan(0);
  });

  it('returns 0 for null', () => {
    expect(parseVideoTs(null)).toBe(0);
    expect(parseVideoTs(undefined)).toBe(0);
    expect(parseVideoTs('')).toBe(0);
  });
});

describe('parseHmsDurationString', () => {
  it('parses HH:MM:SS', () => {
    expect(parseHmsDurationString('1:01:01')).toBe(3661);
    expect(parseHmsDurationString('0:00:59')).toBe(59);
  });

  it('parses MM:SS', () => {
    expect(parseHmsDurationString('1:30')).toBe(90);
  });

  it('returns null for invalid', () => {
    expect(parseHmsDurationString('')).toBeNull();
    expect(parseHmsDurationString('abc')).toBeNull();
    expect(parseHmsDurationString('1:2:3:4')).toBeNull();
  });
});

describe('fmtViews', () => {
  it('formats thousands', () => {
    expect(fmtViews(1500)).toBe('1.5k');
    expect(fmtViews(1000)).toBe('1k');
  });

  it('formats millions', () => {
    expect(fmtViews(1_500_000)).toBe('1.5M');
    expect(fmtViews(2_000_000)).toBe('2M');
  });

  it('leaves small numbers', () => {
    expect(fmtViews(999)).toBe('999');
    expect(fmtViews(0)).toBe('0');
  });
});

describe('formatBytes', () => {
  it('formats MB', () => {
    expect(formatBytes(1048576)).toBe('1 MB');
    expect(formatBytes(52_428_800)).toBe('50 MB');
  });

  it('formats GB', () => {
    expect(formatBytes(1_073_741_824)).toBe('1.00 GB');
  });

  it('handles invalid input', () => {
    expect(formatBytes(0)).toBe('—');
    expect(formatBytes(-1)).toBe('—');
    expect(formatBytes(NaN)).toBe('—');
  });
});

describe('basename', () => {
  it('extracts filename from path', () => {
    expect(basename('/path/to/file.mp4')).toBe('file.mp4');
    expect(basename('C:\\path\\to\\file.mp4')).toBe('file.mp4');
  });

  it('returns the path if no separator', () => {
    expect(basename('file.mp4')).toBe('file.mp4');
  });
});

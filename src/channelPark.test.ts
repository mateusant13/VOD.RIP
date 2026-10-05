import { describe, expect, it, vi, afterEach } from 'vitest';
import {
  learnedAt,
  hasReasonPhrase,
  parseParkedRow,
  parkedState,
  readParkedSnapshot,
  reasonKeyFor,
  releaseParkedChannel,
  skippedOf,
  UNRECOGNISED_REASON_KEY,
  type ParkedSnapshot,
} from './channelPark';

/**
 * The derivation half of the parked-channels surface.
 *
 * These tests exist for the three properties the panel depends on and cannot
 * be seen by looking at the JSX:
 *
 *  1. THE STORED CODE IS THE CONTRACT. `reasonKeyFor` maps a code to a localised
 *     phrase key; a code with no phrase falls back to a key that EXISTS, never
 *     to the raw identifier (which would render `tab_absent` where the owner
 *     expects a sentence).
 *  2. "NOT MEASURED" IS NOT "MEASURED AND EMPTY". A failed, 404, or
 *     unparseable read is `unavailable`, never `empty` - see the state block.
 *  3. A COUNTER THAT WAS NOT REPORTED IS NOT ZERO. `skippedOf` returns null
 *     for a missing/garbage counter, and `learnedAt` returns the raw string
 *     rather than inventing a date.
 */

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** A minimal, well-formed row as the API serves it. */
function row(over: Record<string, unknown> = {}) {
  return {
    channel: 'seeelbr',
    tab: 'streams',
    outcome_code: 'channel_gone',
    permanent: true,
    known: true,
    first_seen: '2026-10-04T22:25:00+00:00',
    last_seen: '2026-10-04T22:25:00+00:00',
    skipped: 0,
    ...over,
  };
}

function stubFetch(handler: (url: string, init?: RequestInit) => Response) {
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) =>
    handler(String(input), init),
  );
  vi.stubGlobal('fetch', fn);
  return fn;
}

describe('channelPark - the stored code is the contract', () => {
  it('maps every vocabulary code to a phrase key, and nothing else does', () => {
    for (const code of [
      'tab_absent',
      'channel_gone',
      'bot_wall_unauthenticated',
      'live_upcoming',
      'live_offline',
      'video_restricted',
    ]) {
      expect(reasonKeyFor(code)).toMatch(/^channelPark\.reason\./);
      expect(hasReasonPhrase(code)).toBe(true);
    }
    // A code this build does not know must still produce a key that EXISTS, or
    // t() would render the raw identifier in the owner's face.
    expect(hasReasonPhrase('something_new')).toBe(false);
    expect(reasonKeyFor('something_new')).toBe(UNRECOGNISED_REASON_KEY);
    expect(reasonKeyFor(null)).toBe(UNRECOGNISED_REASON_KEY);
    expect(reasonKeyFor(undefined)).toBe(UNRECOGNISED_REASON_KEY);
    expect(reasonKeyFor('')).toBe(UNRECOGNISED_REASON_KEY);
  });

  it('is case- and whitespace-tolerant about the code it is handed', () => {
    expect(reasonKeyFor('  TAB_ABSENT ')).toBe('channelPark.reason.tabAbsent');
  });
});

describe('channelPark - a counter that was not reported is not zero', () => {
  it('separates a real zero from a missing or garbage counter', () => {
    expect(skippedOf({ skipped: 0 })).toBe(0);
    expect(skippedOf({ skipped: 7 })).toBe(7);
    expect(skippedOf({})).toBeNull();
    expect(skippedOf({ skipped: null })).toBeNull();
    expect(skippedOf({ skipped: '3' })).toBeNull();
    expect(skippedOf({ skipped: -1 })).toBeNull();
    expect(skippedOf({ skipped: Number.NaN })).toBeNull();
    expect(skippedOf(null)).toBeNull();
  });

  it('shows the raw timestamp rather than inventing one', () => {
    expect(learnedAt('2026-10-04T22:25:00+00:00')).toBe('2026-10-04 22:25');
    // Unparseable and absent must both be visibly absent, never "now".
    expect(learnedAt('not a date')).toBe('not a date');
    expect(learnedAt('')).toBe('');
    expect(learnedAt(null)).toBe('');
    expect(learnedAt(undefined)).toBe('');
  });

  it('omits the counter from a row that did not carry one', () => {
    const parsed = parseParkedRow(row({ skipped: undefined }));
    expect(parsed).not.toBeNull();
    expect('skipped' in (parsed as object)).toBe(false);
    expect(skippedOf(parsed)).toBeNull();
  });
});

describe('channelPark - row parsing is total', () => {
  it('keeps a well-formed row and the backend honesty flags verbatim', () => {
    const parsed = parseParkedRow(row());
    expect(parsed).toEqual({
      channel: 'seeelbr',
      tab: 'streams',
      outcome_code: 'channel_gone',
      permanent: true,
      known: true,
      first_seen: '2026-10-04T22:25:00+00:00',
      last_seen: '2026-10-04T22:25:00+00:00',
      skipped: 0,
    });
  });

  it('does NOT re-derive permanence/known from the client idea of the codes', () => {
    // A row the backend could not vouch for must stay unvouched. A client that
    // recomputed `known` from its own vocabulary would disagree with the walk
    // about what is skippable and would dress the row up as a real park.
    const parsed = parseParkedRow(row({ outcome_code: 'live_offline', known: false, permanent: false }));
    expect(parsed?.known).toBe(false);
    expect(parsed?.permanent).toBe(false);
    expect(parsed?.outcome_code).toBe('live_offline');
  });

  it('drops a row that names no channel or no code', () => {
    expect(parseParkedRow(row({ channel: '' }))).toBeNull();
    expect(parseParkedRow(row({ channel: '   ' }))).toBeNull();
    expect(parseParkedRow(row({ outcome_code: '' }))).toBeNull();
    expect(parseParkedRow(null)).toBeNull();
    expect(parseParkedRow('nope')).toBeNull();
  });
});

describe('channelPark - not measured is not measured and empty', () => {
  it('reads an empty list as empty, because the read SUCCEEDED', async () => {
    stubFetch(
      () =>
        new Response(JSON.stringify({ platform: 'youtube', count: 0, parked: [] }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    );
    const snap = await readParkedSnapshot();
    expect(snap).not.toBeNull();
    expect(parkedState(snap)).toBe('empty');
  });

  it('reads an unreadable payload as unavailable, NOT as empty', async () => {
    // Every one of these is "I do not know", and each must be unavailable. A
    // panel that answered "nothing is parked" here would be telling the owner
    // their channels are fine at the exact moment it has no idea.
    for (const body of [
      { platform: 'youtube' }, // no `parked` key at all
      { platform: 'youtube', parked: 'lots' }, // not an array
      { platform: 'youtube', parked: null },
      {},
    ]) {
      stubFetch(
        () =>
          new Response(JSON.stringify(body), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }),
      );
      const snap = await readParkedSnapshot();
      expect(snap, JSON.stringify(body)).toBeNull();
      expect(parkedState(snap), JSON.stringify(body)).toBe('unavailable');
      expect(parkedState(snap)).not.toBe('empty');
    }
  });

  it('reads a dead or older backend as unavailable', async () => {
    stubFetch(() => new Response('nope', { status: 404 }));
    expect(parkedState(await readParkedSnapshot())).toBe('unavailable');

    stubFetch(() => {
      throw new Error('network down');
    });
    expect(parkedState(await readParkedSnapshot())).toBe('unavailable');
  });

  it('treats a null snapshot as unavailable, never as an empty table', () => {
    expect(parkedState(null)).toBe('unavailable');
    expect(parkedState(null)).not.toBe('empty');
  });

  it('reports rows, and never trusts a stale count over the list', async () => {
    stubFetch(
      () =>
        new Response(
          JSON.stringify({ platform: 'youtube', count: 99, parked: [row(), row({ channel: 'other' })] }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
    );
    const snap = (await readParkedSnapshot()) as ParkedSnapshot;
    expect(parkedState(snap)).toBe('rows');
    expect(snap.parked).toHaveLength(2);
    // A count that disagrees with the rows we can actually render must not be
    // allowed to make the header lie about the list under it. A NUMERIC count
    // is passed through verbatim, even when it disagrees.
    expect(snap.count).toBe(99);
  });

  it('drops unrenderable rows without dropping the readable ones', async () => {
    stubFetch(
      () =>
        new Response(
          JSON.stringify({ parked: [row(), { channel: '', outcome_code: 'x' }, null, 'junk'] }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
    );
    const snap = await readParkedSnapshot();
    expect(snap?.parked).toHaveLength(1);
    expect(snap?.count).toBe(1);
  });
});

describe('channelPark - release', () => {
  it('omits `tab` entirely when the caller means every tab', async () => {
    const fn = stubFetch(
      () =>
        new Response(JSON.stringify({ released: 2, status: 'released' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    );
    await releaseParkedChannel('seeelbr');
    const body = JSON.parse(String((fn.mock.calls[0][1] as RequestInit).body));
    expect(body).toEqual({ channel: 'seeelbr', platform: 'youtube' });
    expect('tab' in body).toBe(false);
  });

  it('sends the tab when one is named', async () => {
    const fn = stubFetch(
      () =>
        new Response(JSON.stringify({ released: 1, status: 'released' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    );
    await releaseParkedChannel('seeelbr', 'streams');
    const body = JSON.parse(String((fn.mock.calls[0][1] as RequestInit).body));
    expect(body).toEqual({ channel: 'seeelbr', platform: 'youtube', tab: 'streams' });
  });

  it('refuses to send a release with no channel rather than posting nothing', async () => {
    // Posting `{channel: ""}` would release nothing and report a success-shaped
    // response; failing here is the honest answer.
    await expect(releaseParkedChannel('   ')).rejects.toThrow(/channel is required/);
  });
});

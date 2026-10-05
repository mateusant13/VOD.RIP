import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  ageParkedCount,
  captionsState,
  fetchParkedReason,
  parkVariant,
  parkedReasonOf,
  type CaptionsFacts,
} from './captionsPark';

/**
 * The backend's two park wordings, verbatim from
 * backend/routers/archive.py::_age_gate_park_reason. They are free text, not an
 * enum, which is why the UI derives a variant instead of switching on a key.
 */
const NO_SESSION_REASON =
  'Age-restricted video — YouTube serves no captions to an anonymous request, and no ' +
  'signed-in YouTube session is configured. Open Settings > Cookie Bridge, sign in to ' +
  'YouTube, then re-run the caption sweep.';
const REJECTED_REASON =
  'Age-restricted video — the configured YouTube session was rejected (YouTube rotates ' +
  'account cookies while a YouTube tab is open), so no captions could be read. Sign in ' +
  'again from a private window via Settings > Cookie Bridge, then re-run the caption sweep.';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('parkedReasonOf — the field is optional on every reader', () => {
  it('reads the reason off a parked row', () => {
    expect(parkedReasonOf({ video_id: 'abc', captions_parked_reason: NO_SESSION_REASON })).toBe(
      NO_SESSION_REASON,
    );
  });

  it('treats an ABSENT key as "not parked" (older build / cached response)', () => {
    // This is the regression guard: a row without the key must read exactly
    // like a row from a build that never heard of parking.
    expect(parkedReasonOf({ video_id: 'abc' })).toBeNull();
  });

  it('never returns a value for blank, non-string or malformed input', () => {
    expect(parkedReasonOf({ captions_parked_reason: '' })).toBeNull();
    expect(parkedReasonOf({ captions_parked_reason: '   ' })).toBeNull();
    expect(parkedReasonOf({ captions_parked_reason: null })).toBeNull();
    expect(parkedReasonOf({ captions_parked_reason: 42 })).toBeNull();
    expect(parkedReasonOf({ captions_parked_reason: { a: 1 } })).toBeNull();
    expect(parkedReasonOf(null)).toBeNull();
    expect(parkedReasonOf(undefined)).toBeNull();
    expect(parkedReasonOf('a string row')).toBeNull();
  });
});

describe('captionsState — the four facts stay four facts', () => {
  const base: CaptionsFacts = {
    parkedReason: null,
    hasTranscript: false,
    hasSubtitles: null,
    cuesInWindow: 0,
    subtitlesOnly: false,
  };

  it('no transcript for this video', () => {
    expect(captionsState({ ...base, hasTranscript: false })).toBe('no-transcript');
  });

  it('a transcript exists but no cue falls inside the trimmed window', () => {
    expect(captionsState({ ...base, hasTranscript: true, cuesInWindow: 0 })).toBe('no-transcript');
    // The distinct fact: cues EXIST (cuesInWindow > 0) but the playhead is
    // outside every one of them, which the panel renders as
    // "No caption at this moment." — NOT as "no captions for this video."
    expect(captionsState({ ...base, hasTranscript: true, cuesInWindow: 4 })).toBe('no-cue-in-window');
  });

  it('YouTube served no caption track (URL-only preview)', () => {
    expect(
      captionsState({ ...base, subtitlesOnly: true, hasSubtitles: false, cuesInWindow: 0 }),
    ).toBe('no-caption-track');
  });

  it('the video is parked, and the park outranks every "no captions" reading', () => {
    expect(
      captionsState({ ...base, parkedReason: NO_SESSION_REASON, hasTranscript: false }),
    ).toBe('parked');
    // Even with cues present, or with subtitlesOnly, the park is the fact the
    // user must act on — it must not be silently downgraded to a green state.
    expect(
      captionsState({
        ...base,
        parkedReason: REJECTED_REASON,
        hasTranscript: true,
        cuesInWindow: 3,
        subtitlesOnly: true,
      }),
    ).toBe('parked');
  });

  it('the four states are mutually distinct', () => {
    const states = [
      captionsState({ ...base, hasTranscript: false }),
      captionsState({ ...base, hasTranscript: true, cuesInWindow: 4 }),
      captionsState({ ...base, subtitlesOnly: true, hasSubtitles: false }),
      captionsState({ ...base, parkedReason: NO_SESSION_REASON }),
    ];
    expect(new Set(states).size).toBe(4);
  });

  it('an absent parked field cannot change any non-parked verdict', () => {
    // Absent vs explicit null must be indistinguishable.
    for (const f of [
      { ...base, hasTranscript: false },
      { ...base, hasTranscript: true, cuesInWindow: 2 },
      { ...base, subtitlesOnly: true, hasSubtitles: false },
    ]) {
      expect(captionsState(f)).toBe(captionsState({ ...f, parkedReason: undefined }));
    }
  });
});

describe('ageParkedCount — the sweep counter is optional too', () => {
  it('reads a real count', () => {
    expect(ageParkedCount({ age_parked: 3 })).toBe(3);
  });

  it('treats absent / null / zero / garbage as 0 so the summary line is unchanged', () => {
    // 0 is the signal the caller renders NO clause at all, so an older backend
    // produces a byte-identical summary line.
    expect(ageParkedCount({})).toBe(0);
    expect(ageParkedCount({ age_parked: null })).toBe(0);
    expect(ageParkedCount({ age_parked: 0 })).toBe(0);
    expect(ageParkedCount({ age_parked: -2 })).toBe(0);
    expect(ageParkedCount({ age_parked: NaN })).toBe(0);
    expect(ageParkedCount({ age_parked: '2' as unknown as number })).toBe(0);
    expect(ageParkedCount(null)).toBe(0);
    expect(ageParkedCount(undefined)).toBe(0);
  });
});

describe('parkVariant — picks the localized sentence from free text', () => {
  it('recognises both backend wordings', () => {
    expect(parkVariant(NO_SESSION_REASON)).toBe('no-session');
    expect(parkVariant(REJECTED_REASON)).toBe('rejected-session');
  });

  it('falls back to a generic variant for unknown or absent wording', () => {
    expect(parkVariant('something a future backend might say')).toBe('unknown');
    expect(parkVariant(null)).toBe('unknown');
    expect(parkVariant(undefined)).toBe('unknown');
    expect(parkVariant('')).toBe('unknown');
  });
});

describe('fetchParkedReason', () => {
  it('scopes the request to the platform and channel, and finds the row', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) =>
      new Response(
        JSON.stringify({
          videos: [
            { platform: 'youtube', video_id: 'other' },
            { platform: 'youtube', video_id: 'yt1', captions_parked_reason: NO_SESSION_REASON },
          ],
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );
    vi.stubGlobal('fetch', fetchMock);

    const reason = await fetchParkedReason('YouTube', 'yt1', 'chan');
    expect(reason).toBe(NO_SESSION_REASON);
    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toContain('/api/archive/videos?');
    expect(url).toContain('platform=youtube');
    expect(url).toContain('channel=chan');
  });

  it('omits the channel filter when the channel is unknown', async () => {
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL) => new Response(JSON.stringify({ videos: [] }), { status: 200 }),
    );
    vi.stubGlobal('fetch', fetchMock);
    expect(await fetchParkedReason('youtube', 'yt1', null)).toBeNull();
    expect(String(fetchMock.mock.calls[0][0])).not.toContain('channel=');
  });

  it('returns null — never throws — when the row is not parked', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify({ videos: [{ video_id: 'yt1' }] }), { status: 200 })),
    );
    expect(await fetchParkedReason('youtube', 'yt1')).toBeNull();
  });

  it('returns null on an unreachable backend and on a garbage body', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new Error('offline');
      }),
    );
    expect(await fetchParkedReason('youtube', 'yt1')).toBeNull();

    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('not json', { status: 200 })),
    );
    expect(await fetchParkedReason('youtube', 'yt1')).toBeNull();
  });

  it('does not call the backend without both platform and video id', async () => {
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL) => new Response(JSON.stringify({ videos: [] }), { status: 200 }),
    );
    vi.stubGlobal('fetch', fetchMock);
    expect(await fetchParkedReason('', 'yt1')).toBeNull();
    expect(await fetchParkedReason('youtube', '')).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

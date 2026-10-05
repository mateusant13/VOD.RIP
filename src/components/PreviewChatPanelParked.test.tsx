import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import PreviewChatPanel, { type PreviewPanelPayload } from './PreviewChatPanel';

/**
 * Panel-level coverage for the age-gated caption park.
 *
 * The regression this guards: a parked video has no transcript and no chat, so
 * the panel takes the `subtitlesOnly` path and rendered the permanent-sounding
 * "No subtitles available for this video." — identical to a video YouTube
 * genuinely has no captions for, and a lie for a park that clears on sign-in.
 */

/** Backend wording, verbatim from _age_gate_park_reason. */
const PARK_REASON =
  'Age-restricted video — YouTube serves no captions to an anonymous request, and no ' +
  'signed-in YouTube session is configured. Open Settings > Cookie Bridge, sign in to ' +
  'YouTube, then re-run the caption sweep.';

/** A parked video: nothing archived, nothing fetched. */
const PARKED_PAYLOAD: PreviewPanelPayload = {
  transcript: [],
  chat: [],
  events: [],
  has_transcript: false,
  has_chat: false,
};

const NO_SUBTITLES = {
  url: 'https://www.youtube.com/watch?v=yt1',
  lang: null,
  source: null,
  has_subtitles: false,
  rows: [],
};

/** `videos` rows for GET /api/archive/videos. */
const ARCHIVE_VIDEOS = {
  videos: [
    { platform: 'youtube', video_id: 'yt1', captions_parked_reason: PARK_REASON },
    { platform: 'youtube', video_id: 'yt-other' },
  ],
};

/**
 * @param videos  the /api/archive/videos body, or null to make that endpoint
 *                404 — the shape an OLDER backend (which never sent the key)
 *                produces, and the shape that must not change existing rendering.
 */
function mockFetch(videos: object | null) {
  const fn = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes('/api/preview/panel/')) {
      return new Response(JSON.stringify(PARKED_PAYLOAD), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    if (url.includes('/api/subtitles')) {
      return new Response(JSON.stringify(NO_SUBTITLES), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    if (url.includes('/api/archive/videos')) {
      if (!videos) return new Response(JSON.stringify({ detail: 'nope' }), { status: 500 });
      return new Response(JSON.stringify(videos), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    return new Response(JSON.stringify({}), { status: 404 });
  });
  vi.stubGlobal('fetch', fn);
  return fn;
}

const origScrollIntoView = Element.prototype.scrollIntoView;
beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn();
});
afterAll(() => {
  Element.prototype.scrollIntoView = origScrollIntoView;
});
beforeEach(() => {
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

describe('PreviewChatPanel — age-gated caption park', () => {
  it('renders the park reason next to the caption state instead of "no subtitles"', async () => {
    const fetchMock = mockFetch(ARCHIVE_VIDEOS);
    render(<PreviewChatPanel platform="youtube" videoId="yt1" currentTime={0} />);

    await waitFor(() => {
      expect(document.querySelector('[data-captions-parked]')).toBeTruthy();
    });
    // The reason is on screen, in the caption view the user is already looking at.
    expect(document.querySelector('[data-captions-park-reason]')?.textContent).toBe(PARK_REASON);
    // The generic permanent-sounding line is GONE — this is the actual bug.
    expect(screen.queryByText('No subtitles available for this video.')).toBeNull();
    expect(screen.queryByText('No captions for this video.')).toBeNull();
    // The probe really did ask the endpoint that carries the field.
    expect(fetchMock.mock.calls.some((c) => String(c[0]).includes('/api/archive/videos'))).toBe(
      true,
    );
  });

  it('offers the Cookie Bridge remedy and reports the click', async () => {
    mockFetch(ARCHIVE_VIDEOS);
    const onOpenCookieBridge = vi.fn();
    render(
      <PreviewChatPanel
        platform="youtube"
        videoId="yt1"
        currentTime={0}
        onOpenCookieBridge={onOpenCookieBridge}
      />,
    );
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /open cookie bridge/i })).toBeTruthy();
    });
    screen.getByRole('button', { name: /open cookie bridge/i }).click();
    expect(onOpenCookieBridge).toHaveBeenCalledTimes(1);
  });

  it('keeps the pre-existing rendering when the field is ABSENT (older backend)', async () => {
    mockFetch(null);
    render(<PreviewChatPanel platform="youtube" videoId="yt1" currentTime={0} />);
    // Untouched: the same string this build rendered before the park existed.
    await waitFor(() => {
      expect(screen.getByText('No subtitles available for this video.')).toBeTruthy();
    });
    expect(document.querySelector('[data-captions-parked]')).toBeNull();
  });

  it('keeps the pre-existing rendering when the field is present but this video is not parked', async () => {
    mockFetch(ARCHIVE_VIDEOS);
    render(<PreviewChatPanel platform="youtube" videoId="yt-other" currentTime={0} />);
    await waitFor(() => {
      expect(screen.getByText('No subtitles available for this video.')).toBeTruthy();
    });
    expect(document.querySelector('[data-captions-parked]')).toBeNull();
  });

  it('does not render the park on the Transcript tab as a "no transcript" verdict', async () => {
    mockFetch(ARCHIVE_VIDEOS);
    render(<PreviewChatPanel platform="youtube" videoId="yt1" currentTime={0} />);
    await waitFor(() => {
      expect(document.querySelector('[data-captions-parked]')).toBeTruthy();
    });
    // subtitlesOnly forces the Subtitles tab; a parked YouTube video with no
    // chat has no Transcript tab to fall back to, so the same notice covers it.
    expect(screen.queryByText('No transcript for this video.')).toBeNull();
  });
});

/**
 * A transient preview failure must stay retryable, and a dead video must not.
 *
 * The old classifier was prose-only:
 *   /members-only|membership|unavailable|private|removed/i
 * The bare `unavailable` matched the message a 503 gets when its body is not
 * JSON — useApiClient.ts:114 falls back to `res.statusText`, the literal string
 * "Service Unavailable" — so a throttle was read as a permanent verdict and the
 * click was abandoned without a single retry. Same defect as the backend
 * reporting a timed-out extract as "This video is unavailable.": a transient
 * condition rendered permanent.
 *
 * ApiError already carries .status, and its own docstring states the intent:
 * "so callers can tell a terminal 404/410 (resource gone) from a transient
 * blip". The classifier now reads the status first.
 *
 * The other half of the law matters just as much: zIOnnd61w40 really is gone
 * (404 "This video is unavailable", measured 1.8s) and MUST stay permanent —
 * retrying it three times would be three lies and three dead waits.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const apiPost = vi.fn();
vi.mock('./hooks/useApiClient', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./hooks/useApiClient')>();
  return { ...actual, apiPost: (...a: unknown[]) => apiPost(...a) };
});

import { ApiError } from './hooks/useApiClient';
import { createPreviewSessionWithRetry } from './previewPlayerUtils';

/** One distinct URL per case: createPreviewSessionWithRetry dedupes by body. */
const body = (n: number) => ({
  url: `https://www.youtube.com/watch?v=case${n}AAAAAAA`,
  crop_start: 0,
  crop_end: 30,
  prefer_height: 360,
});

let caseNo = 0;
beforeEach(() => {
  caseNo += 1;
  apiPost.mockReset();
  // Retries sleep 1.2s/2.4s before trying again; fire them immediately so the
  // test measures the DECISION, not the backoff.
  vi.spyOn(window, 'setTimeout').mockImplementation(((fn: () => void) => {
    fn();
    return 0;
  }) as unknown as typeof window.setTimeout);
});
afterEach(() => {
  vi.restoreAllMocks();
});

const RETRIES = 3; // 1 attempt + 2 retries

describe('preview failure permanence', () => {
  it('retries a 503 whose non-JSON body became the text "Service Unavailable"', async () => {
    apiPost.mockImplementation(() => Promise.reject(new ApiError('Service Unavailable', 503)));
    await expect(createPreviewSessionWithRetry(body(caseNo))).rejects.toThrow();
    expect(apiPost).toHaveBeenCalledTimes(RETRIES);
  });

  it('retries a 503 that names the preview as temporarily restricted', async () => {
    apiPost.mockImplementation(() => Promise.reject(new ApiError('YouTube preview is temporarily restricted. Try again in a few minutes.', 503),));
    await expect(createPreviewSessionWithRetry(body(caseNo))).rejects.toThrow();
    expect(apiPost).toHaveBeenCalledTimes(RETRIES);
  });

  it('retries a 429 throttle', async () => {
    apiPost.mockImplementation(() => Promise.reject(new ApiError('Too Many Requests', 429)));
    await expect(createPreviewSessionWithRetry(body(caseNo))).rejects.toThrow();
    expect(apiPost).toHaveBeenCalledTimes(RETRIES);
  });

  it('does NOT retry a genuine 404 — the video really is gone', async () => {
    apiPost.mockImplementation(() => Promise.reject(new ApiError('This video is unavailable.', 404)));
    await expect(createPreviewSessionWithRetry(body(caseNo))).rejects.toThrow(
      'This video is unavailable.',
    );
    expect(apiPost).toHaveBeenCalledTimes(1);
  });

  it('does NOT retry a 403 members-only verdict', async () => {
    apiPost.mockImplementation(() => Promise.reject(new ApiError('This video is members-only content.', 403)));
    await expect(createPreviewSessionWithRetry(body(caseNo))).rejects.toThrow();
    expect(apiPost).toHaveBeenCalledTimes(1);
  });

  it('does NOT retry a 410 gone', async () => {
    apiPost.mockImplementation(() => Promise.reject(new ApiError('Video has been removed by the uploader', 410)));
    await expect(createPreviewSessionWithRetry(body(caseNo))).rejects.toThrow();
    expect(apiPost).toHaveBeenCalledTimes(1);
  });

  it('falls back to prose when a throw carries no status', async () => {
    apiPost.mockImplementation(() => Promise.reject(new Error('members-only content')));
    await expect(createPreviewSessionWithRetry(body(caseNo))).rejects.toThrow();
    expect(apiPost).toHaveBeenCalledTimes(1);
  });

  it('does not let the prose fallback match generic service text', async () => {
    apiPost.mockImplementation(() => Promise.reject(new Error('HTTP 503 Service Unavailable')));
    await expect(createPreviewSessionWithRetry(body(caseNo))).rejects.toThrow();
    expect(apiPost).toHaveBeenCalledTimes(RETRIES);
  });
});

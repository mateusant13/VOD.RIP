import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import ExtensionWaitOverlay, { POLL_INTERVAL_MS, STILL_WAITING_MS } from './ExtensionWaitOverlay';

const EMPTY = {
  paired: false,
  enabled: true,
  platforms: {
    youtube: { count: 0, lastGrabAt: null, expiredCount: 0 },
    kick: { count: 0, lastGrabAt: null, expiredCount: 0 },
  },
};

const PAIRED = {
  paired: true,
  enabled: true,
  platforms: {
    youtube: { count: 0, lastGrabAt: null, expiredCount: 0 },
    kick: { count: 3, lastGrabAt: '2026-08-07T10:00:00Z', expiredCount: 0 },
  },
};

const DIR = 'C:/AppData/VOD.RIP/cookie-extension/VOD.RIP-cookies';

function mockStatusFetch(status: unknown) {
  const calls: string[] = [];
  const fn = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    calls.push(url);
    if (url.includes('/api/session/cookies/status')) {
      return new Response(JSON.stringify(status), { status: 200 });
    }
    return new Response(JSON.stringify({}), { status: 404 });
  });
  vi.stubGlobal('fetch', fn);
  return { fn, calls };
}

function renderOverlay(props: Partial<Parameters<typeof ExtensionWaitOverlay>[0]> = {}) {
  return render(
    <ExtensionWaitOverlay open extensionDir={DIR} onClose={() => {}} {...props} />,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('ExtensionWaitOverlay', () => {
  it('renders nothing when closed', () => {
    mockStatusFetch(EMPTY);
    render(<ExtensionWaitOverlay open={false} extensionDir={DIR} onClose={() => {}} />);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('renders the install checklist while waiting', () => {
    mockStatusFetch(EMPTY);
    renderOverlay();
    expect(screen.getByRole('dialog')).toBeTruthy();
    expect(screen.getByText('Waiting for cookies')).toBeTruthy();
    expect(screen.getByText('Waiting for cookies…')).toBeTruthy();
    expect(screen.getByText(DIR)).toBeTruthy();
    expect(screen.getByText(/Drop the VOD.RIP-cookies folder onto the page/)).toBeTruthy();
    expect(screen.getByText('Developer mode')).toBeTruthy();
    expect(screen.getByText(/Open the extension popup on Kick or YouTube once/)).toBeTruthy();
    expect(screen.queryByText(/Cookies detected/)).not.toBeInTheDocument();
  });

  it('Close button calls onClose', () => {
    mockStatusFetch(EMPTY);
    const onClose = vi.fn();
    renderOverlay({ onClose });
    fireEvent.click(screen.getByText('Close'));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('shows success on paired + count>0 and stops polling', async () => {
    vi.useFakeTimers();
    let n = 0;
    const calls: string[] = [];
    const fn = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      calls.push(url);
      if (url.includes('/api/session/cookies/status')) {
        n += 1;
        // first poll: still empty; the next tick delivers cookies
        return new Response(JSON.stringify(n === 1 ? EMPTY : PAIRED), { status: 200 });
      }
      return new Response(JSON.stringify({}), { status: 404 });
    });
    vi.stubGlobal('fetch', fn);

    renderOverlay();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0); // flush the immediate poll
    });
    expect(screen.queryByText(/Cookies detected/)).not.toBeInTheDocument();
    const before = calls.filter((c) => c.includes('/status')).length;

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS); // cookie poll lands
    });
    expect(screen.getByText(/Cookies detected — you can close this/)).toBeTruthy();
    const afterSuccess = calls.filter((c) => c.includes('/status')).length;
    expect(afterSuccess).toBeGreaterThan(before);

    // polling stops — advancing more time produces no further status calls
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 5);
    });
    expect(calls.filter((c) => c.includes('/status')).length).toBe(afterSuccess);
  });

  it('does not auto-close after success', async () => {
    vi.useFakeTimers();
    mockStatusFetch(PAIRED);
    renderOverlay();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0); // immediate poll finds cookies
    });
    expect(screen.getByText(/Cookies detected — you can close this/)).toBeTruthy();
    // still mounted with a working Close button — the user dismisses it
    expect(screen.getByText('Close')).toBeTruthy();
    expect(screen.queryByText(/Drop the VOD.RIP-cookies folder onto the page/)).not.toBeInTheDocument();
  });

  it('keeps waiting while no cookies arrive, then shows the still-waiting hint after 30s', async () => {
    vi.useFakeTimers();
    mockStatusFetch(EMPTY);
    renderOverlay();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 3);
    });
    expect(screen.queryByText(/Cookies detected/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Still waiting/)).not.toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(STILL_WAITING_MS);
    });
    expect(screen.getByText(/Still waiting — check that Developer mode is ON/)).toBeTruthy();
    expect(screen.getByText('Close')).toBeTruthy();
  });

  it('reports each polled status to the parent', async () => {
    vi.useFakeTimers();
    mockStatusFetch(EMPTY);
    const onStatus = vi.fn();
    renderOverlay({ onStatus });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    });
    expect(onStatus).toHaveBeenCalled();
    expect(onStatus.mock.calls[0][0]).toMatchObject({ paired: false });
  });
});

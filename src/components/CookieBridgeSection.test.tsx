import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import CookieBridgeSection from './CookieBridgeSection';

// Not-yet-paired state: the install-flow tests exercise the waiting overlay,
// so the fixture must never satisfy the success condition (paired + count>0).
const STATUS = {
  paired: false,
  enabled: true,
  platforms: {
    youtube: { count: 0, lastGrabAt: null, expiredCount: 0 },
    kick: { count: 0, lastGrabAt: null, expiredCount: 0 },
  },
};

const SOURCE = {
  extension_dir: 'C:/AppData/VOD.RIP/cookie-extension/VOD.RIP-cookies',
  ready: true,
  version: '0.7.2',
};

function mockFetch(overrides: Record<string, unknown> = {}) {
  const calls: string[] = [];
  const fn = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    calls.push(url);
    if (url.includes('/api/session/cookies/status')) {
      return new Response(JSON.stringify(STATUS), { status: 200 });
    }
    if (url.includes('/api/session/cookies/token')) {
      return new Response(JSON.stringify({ token: 'pair-abc' }), { status: 200 });
    }
    if (url.includes('/api/session/cookies/extension/source')) {
      return new Response(JSON.stringify(SOURCE), { status: 200 });
    }
    if (url.includes('/api/session/cookies/extension/open')) {
      return new Response(
        JSON.stringify(overrides.open ?? { launched: true, browser: 'chrome', url: 'chrome://extensions/' }),
        { status: 200 },
      );
    }
    if (url.includes('/api/session/cookies/extension/reveal')) {
      return new Response(JSON.stringify({ ok: true }), { status: (overrides.revealStatus as number | undefined) ?? 200 });
    }
    return new Response(JSON.stringify({}), { status: 404 });
  });
  vi.stubGlobal('fetch', fn);
  return { fn, calls };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('CookieBridgeSection extension install flow', () => {
  it('renders the folder + open/reveal buttons once the source is ready', async () => {
    mockFetch();
    render(<CookieBridgeSection />);
    await screen.findByText('Open extensions');
    expect(screen.getByText(SOURCE.extension_dir)).toBeTruthy();
    expect(screen.getByText('Show folder')).toBeTruthy();
    expect(screen.getByText('v0.7.2')).toBeTruthy();
  });

  it('opening the manager shows the dev-mode drag-drop checklist', async () => {
    const { calls } = mockFetch();
    render(<CookieBridgeSection />);
    await screen.findByText('Open extensions');
    fireEvent.click(screen.getByText('Open extensions'));
    await waitFor(() => expect(calls.some((c) => c.includes('/extension/open'))).toBe(true));
    await screen.findByText(/Drop the VOD.RIP-cookies folder onto the page/);
    // "Developer mode" renders inside a styled span within the list item
    expect(screen.getByText('Developer mode')).toBeTruthy();
    expect(screen.getByText(/Open the extension popup on Kick or YouTube once/)).toBeTruthy();
    // fresh-tab launch: no "reused" hint
    expect(screen.queryByText(/no new tab opened/)).not.toBeInTheDocument();
  });

  it('ignores a stale reused hint from an old backend response', async () => {
    // Backend now ALWAYS opens a new tab — this test guards that the hint
    // never appears even if a stale response carries `reused: true`.
    mockFetch({ open: { launched: true, browser: null, url: null, reused: true } });
    render(<CookieBridgeSection />);
    await screen.findByText('Open extensions');
    fireEvent.click(screen.getByText('Open extensions'));
    await screen.findByText(/Drop the VOD.RIP-cookies folder onto the page/);
    expect(screen.queryByText(/no new tab opened/)).not.toBeInTheDocument();
  });

  it('blocked drive surfaces the could-not-focus manual hint', async () => {
    const { calls } = mockFetch({ open: { launched: false, browser: null, url: null, blocked: true } });
    render(<CookieBridgeSection />);
    await screen.findByText('Open extensions');
    fireEvent.click(screen.getByText('Open extensions'));
    await waitFor(() => expect(calls.some((c) => c.includes('/extension/open'))).toBe(true));
    await screen.findByText(/Browser window could not be focused/);
    expect(screen.queryByText(/No Chromium browser found/)).not.toBeInTheDocument();
  });

  it('one click opens extensions AND reveals the folder', async () => {
    const { calls } = mockFetch();
    render(<CookieBridgeSection />);
    await screen.findByText('Open extensions');
    fireEvent.click(screen.getByText('Open extensions'));
    await waitFor(() => {
      expect(calls.some((c) => c.includes('/extension/open'))).toBe(true);
      expect(calls.some((c) => c.includes('/extension/reveal'))).toBe(true);
    });
    await screen.findByText(/Drop the VOD.RIP-cookies folder onto the page/);
  });

  it('reveal failure still shows the checklist', async () => {
    const { calls } = mockFetch({ revealStatus: 500 });
    render(<CookieBridgeSection />);
    await screen.findByText('Open extensions');
    fireEvent.click(screen.getByText('Open extensions'));
    await waitFor(() => expect(calls.some((c) => c.includes('/extension/reveal'))).toBe(true));
    await screen.findByText(/Drop the VOD.RIP-cookies folder onto the page/);
    expect(screen.queryByText(/No Chromium browser found/)).not.toBeInTheDocument();
  });

  it('open failure surfaces a manual-install hint', async () => {
    const { calls } = mockFetch({ open: { launched: false, browser: null, url: null } });
    render(<CookieBridgeSection />);
    await screen.findByText('Open extensions');
    fireEvent.click(screen.getByText('Open extensions'));
    await waitFor(() => expect(calls.some((c) => c.includes('/extension/open'))).toBe(true));
    await screen.findByText(/No Chromium browser found/);
  });

  it('Show folder posts the reveal request', async () => {
    const { calls } = mockFetch();
    render(<CookieBridgeSection />);
    await screen.findByText('Show folder');
    fireEvent.click(screen.getByText('Show folder'));
    await waitFor(() => expect(calls.some((c) => c.includes('/extension/reveal'))).toBe(true));
  });

  it('open click shows the waiting overlay and Close dismisses it', async () => {
    mockFetch();
    render(<CookieBridgeSection />);
    await screen.findByText('Open extensions');
    fireEvent.click(screen.getByText('Open extensions'));
    await screen.findByText('Waiting for cookies');
    expect(screen.getByText(/Drop the VOD.RIP-cookies folder onto the page/)).toBeTruthy();
    fireEvent.click(screen.getByText('Close'));
    await waitFor(() => expect(screen.queryByText('Waiting for cookies')).not.toBeInTheDocument());
  });
});

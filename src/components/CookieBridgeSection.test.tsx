import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import CookieBridgeSection from './CookieBridgeSection';

const STATUS = {
  paired: true,
  enabled: true,
  platforms: {
    youtube: { count: 0, lastGrabAt: null, expiredCount: 0 },
    kick: { count: 2, lastGrabAt: '2026-08-05T10:00:00Z', expiredCount: 0 },
  },
};

const SOURCE = {
  extension_dir: 'C:/AppData/VOD.RIP/cookie-extension/src',
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
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
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
    await screen.findByText(/Drop the folder above onto the page/);
    // "Developer mode" renders inside a styled span within the list item
    expect(screen.getByText('Developer mode')).toBeTruthy();
    expect(screen.getByText(/Open the extension popup on Kick or YouTube once/)).toBeTruthy();
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
});

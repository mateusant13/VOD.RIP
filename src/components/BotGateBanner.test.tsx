import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import BotGateBanner from './BotGateBanner';

const GATED_UNPAIRED = {
  paired: false,
  youtube_gate_active: true,
  youtube_gate_remaining_sec: 25 * 60,
};

function mockFetch(overrides: Record<string, unknown> = {}) {
  const calls: string[] = [];
  const fn = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    calls.push(url);
    if (url.includes('/api/session/cookies/status')) {
      return new Response(JSON.stringify(overrides.status ?? GATED_UNPAIRED), { status: 200 });
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

const MESSAGE = /YouTube is rate-limiting requests \(bot gate\)/;

afterEach(() => {
  vi.unstubAllGlobals();
  sessionStorage.removeItem('botGate.dismissed');
});

describe('BotGateBanner', () => {
  it('shows the banner with remaining time + both actions when gate active and not paired', async () => {
    mockFetch();
    render(<BotGateBanner onOpenInstructions={() => {}} />);
    await screen.findByText(MESSAGE);
    expect(screen.getByText('waiting ~25 min')).toBeTruthy();
    expect(screen.getByText('Open instructions')).toBeTruthy();
    expect(screen.getByText('Install now')).toBeTruthy();
  });

  it('stays hidden when cookies are paired', async () => {
    const { calls } = mockFetch({ status: { ...GATED_UNPAIRED, paired: true } });
    render(<BotGateBanner onOpenInstructions={() => {}} />);
    await waitFor(() => expect(calls.some((c) => c.includes('/api/session/cookies/status'))).toBe(true));
    expect(screen.queryByText(MESSAGE)).not.toBeInTheDocument();
  });

  it('stays hidden when the gate is inactive', async () => {
    const { calls } = mockFetch({
      status: { paired: false, youtube_gate_active: false, youtube_gate_remaining_sec: 0 },
    });
    render(<BotGateBanner onOpenInstructions={() => {}} />);
    await waitFor(() => expect(calls.some((c) => c.includes('/api/session/cookies/status'))).toBe(true));
    expect(screen.queryByText(MESSAGE)).not.toBeInTheDocument();
  });

  it('dismiss hides the banner and persists the session flag across remounts', async () => {
    mockFetch();
    const first = render(<BotGateBanner onOpenInstructions={() => {}} />);
    await screen.findByText(MESSAGE);
    fireEvent.click(first.getByLabelText('Dismiss'));
    await waitFor(() => expect(screen.queryByText(MESSAGE)).not.toBeInTheDocument());
    expect(sessionStorage.getItem('botGate.dismissed')).toBe('1');
    first.unmount();
    render(<BotGateBanner onOpenInstructions={() => {}} />);
    await waitFor(() => expect(screen.queryByText(MESSAGE)).not.toBeInTheDocument());
  });

  it('Install now posts extension/open and extension/reveal', async () => {
    const { calls } = mockFetch();
    render(<BotGateBanner onOpenInstructions={() => {}} />);
    await screen.findByText('Install now');
    fireEvent.click(screen.getByText('Install now'));
    await waitFor(() => {
      expect(calls.some((c) => c.includes('/api/session/cookies/extension/open'))).toBe(true);
      expect(calls.some((c) => c.includes('/api/session/cookies/extension/reveal'))).toBe(true);
    });
  });

  it('Install now failure surfaces the manual-install hint', async () => {
    mockFetch({ open: { launched: false, browser: null, url: null } });
    render(<BotGateBanner onOpenInstructions={() => {}} />);
    await screen.findByText('Install now');
    fireEvent.click(screen.getByText('Install now'));
    await screen.findByText(/No Chromium browser found/);
  });

  it('Open instructions fires the settings-navigation callback', async () => {
    mockFetch();
    const onOpen = vi.fn();
    render(<BotGateBanner onOpenInstructions={onOpen} />);
    await screen.findByText('Open instructions');
    fireEvent.click(screen.getByText('Open instructions'));
    expect(onOpen).toHaveBeenCalledTimes(1);
  });
});

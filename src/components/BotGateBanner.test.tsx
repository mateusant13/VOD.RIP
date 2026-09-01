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
  let installStarted = false;
  const fn = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    calls.push(url);
    if (url.includes('/api/session/cookies/auto-install')) {
      const body = (overrides.autoInstall ?? { ok: true, started: true }) as Record<string, unknown>;
      if (body.ok) installStarted = true;
      return new Response(JSON.stringify(body), { status: 200 });
    }
    if (url.includes('/api/session/cookies/status')) {
      const base = (overrides.status ?? GATED_UNPAIRED) as Record<string, unknown>;
      const auto = overrides.autoInstallStatus as Record<string, unknown> | undefined;
      const auto_install = auto ?? (installStarted
        ? { state: 'done', installed: true, error: null }
        : undefined);
      return new Response(JSON.stringify({ ...base, auto_install }), { status: 200 });
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

  it('Install now runs silent auto-install', async () => {
    const { calls } = mockFetch();
    render(<BotGateBanner onOpenInstructions={() => {}} />);
    await screen.findByText('Install now');
    fireEvent.click(screen.getByText('Install now'));
    await waitFor(() => {
      expect(calls.some((c) => c.includes('/api/session/cookies/auto-install'))).toBe(true);
      expect(calls.some((c) => c.includes('/api/session/cookies/extension/open'))).toBe(false);
    });
  });

  it('Install now failure surfaces an error message', async () => {
    mockFetch({
      autoInstall: { ok: false, error: 'driver missing' },
      autoInstallStatus: { state: 'error', installed: false, error: 'driver missing' },
    });
    render(<BotGateBanner onOpenInstructions={() => {}} />);
    await screen.findByText('Install now');
    fireEvent.click(screen.getByText('Install now'));
    await screen.findByText(/driver missing/);
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

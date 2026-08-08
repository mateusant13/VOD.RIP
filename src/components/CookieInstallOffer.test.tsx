import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import CookieInstallOffer from './CookieInstallOffer';

/** Minimal Response stand-in for useApiClient's apiGet/apiPost. */
function jsonResponse(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as unknown as Response;
}

const fetchMock = vi.fn();

beforeEach(() => {
  localStorage.clear();
  fetchMock.mockReset();
  fetchMock.mockResolvedValue(jsonResponse({ ok: true }));
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const SEEN_KEY = 'vodrip.firstTime.cookieInstall';

describe('CookieInstallOffer', () => {
  it('renders nothing while closed', () => {
    const { container } = render(<CookieInstallOffer open={false} toggleOn onClose={() => {}} />);
    expect(container.firstChild).toBeNull();
  });

  it('shows the install offer with Install now + Later when the toggle is on', () => {
    render(<CookieInstallOffer open toggleOn onClose={() => {}} />);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Install now' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Later' })).toBeInTheDocument();
  });

  it('shows "Don\'t show again" when the toggle is off', () => {
    render(<CookieInstallOffer open toggleOn={false} onClose={() => {}} />);
    expect(screen.getByRole('button', { name: "Don't show again" })).toBeInTheDocument();
  });

  it('Later marks the tutorial seen and closes', () => {
    const onClose = vi.fn();
    render(<CookieInstallOffer open toggleOn onClose={onClose} />);
    fireEvent.click(screen.getByRole('button', { name: 'Later' }));
    expect(localStorage.getItem(SEEN_KEY)).toBe('1');
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('"Don\'t show again" marks seen, arms the settings toggle and closes', async () => {
    const onClose = vi.fn();
    render(<CookieInstallOffer open toggleOn={false} onClose={onClose} />);
    fireEvent.click(screen.getByRole('button', { name: "Don't show again" }));
    expect(localStorage.getItem(SEEN_KEY)).toBe('1');
    expect(onClose).toHaveBeenCalledTimes(1);
    await waitFor(() => {
      const post = fetchMock.mock.calls.find(([url]) => url === '/api/settings');
      expect(post).toBeTruthy();
      expect(JSON.parse(post![1].body)).toEqual({ auto_install_extension: true });
    });
  });

  it('already-paired short-circuit shows the installed result without polling', async () => {
    fetchMock.mockReset();
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ ok: true, alreadyInstalled: true, installed: true, state: 'done' }),
    );
    render(<CookieInstallOffer open toggleOn onClose={() => {}} />);
    fireEvent.click(screen.getByRole('button', { name: 'Install now' }));
    await waitFor(() => {
      expect(screen.getByText(/Extension installed/i)).toBeInTheDocument();
    });
    expect(localStorage.getItem(SEEN_KEY)).toBe('1');
    // only the POST happened — no status polling
    expect(fetchMock.mock.calls.filter(([url]) => url === '/api/session/cookies/status')).toHaveLength(0);
  });

  it('polls the background install to completion and shows the result', async () => {
    vi.useFakeTimers();
    fetchMock.mockReset();
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ ok: true, started: true, state: 'running' }))
      .mockResolvedValueOnce(
        jsonResponse({ paired: false, auto_install: { state: 'running', installed: false, error: null } }),
      )
      .mockResolvedValueOnce(
        jsonResponse({ paired: false, auto_install: { state: 'done', installed: true, error: null } }),
      );
    render(<CookieInstallOffer open toggleOn onClose={() => {}} />);
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Install now' }));
      await vi.advanceTimersByTimeAsync(2000); // first status poll -> running
      await vi.advanceTimersByTimeAsync(2000); // second status poll -> done
    });
    expect(screen.getByText(/Extension installed/i)).toBeInTheDocument();
    expect(localStorage.getItem(SEEN_KEY)).toBe('1');
    vi.useRealTimers();
  });

  it('reports a failed install with a retry button', async () => {
    fetchMock.mockReset();
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ ok: false, started: false, error: 'dialog timeout' }),
    );
    render(<CookieInstallOffer open toggleOn onClose={() => {}} />);
    fireEvent.click(screen.getByRole('button', { name: 'Install now' }));
    await waitFor(() => {
      expect(screen.getByText(/Install failed: dialog timeout/i)).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
    });
  });
});

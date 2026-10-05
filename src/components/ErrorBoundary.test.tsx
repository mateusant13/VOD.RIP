/**
 * The crash boundary.
 *
 * The acceptance criterion is deliberately two-sided: the boundary must CATCH
 * and DISPLAY, and it must NOT degrade into a blank container. A boundary that
 * renders an empty div would be worse than the crash it replaced, so both
 * halves are asserted - the "no blank container" case is checked by asserting
 * real text content, not merely by the absence of a thrown error.
 */
import type { ReactNode } from 'react';
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import ErrorBoundary from './ErrorBoundary';
import { FRONTEND_CRASH_LOG_KEY, readFrontendCrashLog } from '../crashLog';
import { DICTS, setLanguage, getLanguage } from '../i18n';

/** A child that throws during render, the way a real App crash does. */
function Boom({ message }: { message: string }): ReactNode {
  throw new Error(message);
}

const savedLang = getLanguage();

describe('ErrorBoundary', () => {
  beforeEach(() => {
    localStorage.clear();
    // React logs the caught error; silence it so the suite output stays readable
    // without hiding a genuine failure (the assertions below read the UI).
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    setLanguage(savedLang);
  });

  it('renders the children when nothing throws', () => {
    render(
      <ErrorBoundary>
        <p>all good</p>
      </ErrorBoundary>,
    );
    expect(screen.getByText('all good')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('shows the error message and the component stack when a child throws', () => {
    render(
      <ErrorBoundary>
        <Boom message="snap failed: null rect" />
      </ErrorBoundary>,
    );

    // The actual error - the thing that was invisible before.
    const msg = document.querySelector('[data-crash-message]');
    expect(msg).not.toBeNull();
    expect(msg!.textContent).toContain('snap failed: null rect');
    // The error class name travels with it, so the failure is identifiable.
    expect(msg!.textContent).toContain('Error:');

    // The component stack, in its own labelled region.
    const stack = document.querySelector('[data-crash-component-stack]');
    expect(stack).not.toBeNull();
    expect(stack!.textContent!.trim().length).toBeGreaterThan(0);
    expect(stack!.textContent).not.toContain('no component stack was reported');
  });

  it('does NOT render a blank container', () => {
    const { container } = render(
      <ErrorBoundary>
        <Boom message="boom" />
      </ErrorBoundary>,
    );
    // Reached the fallback at all.
    expect(container.querySelector('[data-crash-boundary]')).not.toBeNull();
    // It has a role so assistive tech announces it, and it is not empty.
    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(container.textContent!.trim().length).toBeGreaterThan(0);
    // It offers a way out.
    expect(screen.getByRole('button', { name: /reload/i })).toBeInTheDocument();
  });

  it('persists the report so it survives the reload it offers', () => {
    render(
      <ErrorBoundary>
        <Boom message="durable please" />
      </ErrorBoundary>,
    );
    const log = readFrontendCrashLog();
    expect(log).toHaveLength(1);
    expect(log[0].message).toBe('durable please');
    expect(log[0].name).toBe('Error');
    expect(log[0].componentStack).toBeTruthy();
    // Written under one clearly-named key, since the app's error sink
    // (GET /api/errors/latest) is read-only from the browser.
    expect(localStorage.getItem(FRONTEND_CRASH_LOG_KEY)).toBeTruthy();
    expect(JSON.parse(localStorage.getItem(FRONTEND_CRASH_LOG_KEY)!)[0].message).toBe(
      'durable please',
    );
  });

  it('surfaces a previous crash after a reload', () => {
    localStorage.setItem(
      FRONTEND_CRASH_LOG_KEY,
      JSON.stringify([
        { at: '2024-05-01T00:00:00.000Z', name: 'Error', message: 'first crash', stack: null, componentStack: null },
        { at: '2024-05-02T00:00:00.000Z', name: 'Error', message: 'older crash', stack: null, componentStack: null },
      ]),
    );
    render(
      <ErrorBoundary>
        <Boom message="current crash" />
      </ErrorBoundary>,
    );
    const previous = document.querySelector('[data-crash-previous]')!;
    expect(previous.textContent).toContain('older crash');
    // The current crash is the message panel, not the "earlier" list.
    expect(previous.textContent).not.toContain('current crash');
  });

  it('localizes its own chrome in all three locales', () => {
    for (const lang of ['en', 'pt-BR', 'es'] as const) {
      setLanguage(lang);
      cleanup();
      const { container } = render(
        <ErrorBoundary>
          <Boom message="localized?" />
        </ErrorBoundary>,
      );
      const text = container.textContent!;
      // Real parity, not a pass-through: each locale renders its own string.
      expect(text, `${lang} left the crash title in English`).toContain(DICTS[lang]['crash.title']);
      expect(text).toContain(DICTS[lang]['crash.reload']);
      expect(text).toContain(DICTS[lang]['crash.componentStackLabel']);
    }
  });

  it('reports the failure instead of swallowing it - console.error still fires', () => {
    render(
      <ErrorBoundary>
        <Boom message="audible" />
      </ErrorBoundary>,
    );
    // React itself reports the error; the boundary does not suppress it.
    expect(console.error).toHaveBeenCalled();
  });
});

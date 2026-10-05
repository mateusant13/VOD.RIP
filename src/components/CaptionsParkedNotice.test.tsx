import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { getLanguage, setLanguage } from '../i18n';
import CaptionsParkedNotice from './CaptionsParkedNotice';

const NO_SESSION_REASON =
  'Age-restricted video — YouTube serves no captions to an anonymous request, and no ' +
  'signed-in YouTube session is configured. Open Settings > Cookie Bridge, sign in to ' +
  'YouTube, then re-run the caption sweep.';
const REJECTED_REASON =
  'Age-restricted video — the configured YouTube session was rejected (YouTube rotates ' +
  'account cookies while a YouTube tab is open), so no captions could be read. Sign in ' +
  'again from a private window via Settings > Cookie Bridge, then re-run the caption sweep.';

const saved = getLanguage();
afterEach(() => {
  setLanguage(saved);
});

/**
 * The three generic empty states the park REPLACES. A parked video must never
 * render any of them: each is a permanent-sounding verdict for a condition the
 * user clears by signing in.
 */
const PERMANENT_SOUNDING = [
  'No subtitles available for this video.',
  'No captions for this video.',
  'No transcript for this video.',
  'No caption at this moment.',
];

describe('CaptionsParkedNotice', () => {
  it("renders the backend's reason verbatim", () => {
    render(<CaptionsParkedNotice reason={REJECTED_REASON} />);
    // Scoped: the verbatim reason and the localized sentence deliberately share
    // phrases, so an unscoped text query would match both.
    expect(document.querySelector('[data-captions-park-reason]')?.textContent).toBe(REJECTED_REASON);
  });

  it('says what happened and what to do, for each backend wording', () => {
    const bodyText = () => document.querySelector('[data-captions-park-body]')?.textContent ?? '';
    const { unmount } = render(<CaptionsParkedNotice reason={NO_SESSION_REASON} />);
    expect(bodyText()).toMatch(/no signed-in YouTube session is configured/i);
    expect(bodyText()).toMatch(/waiting on your sign-in, not broken/i);
    unmount();

    render(<CaptionsParkedNotice reason={REJECTED_REASON} />);
    expect(bodyText()).toMatch(/session this app has was rejected/i);
    expect(bodyText()).toMatch(/Sign in again and this video goes back into the caption sweep/i);
  });

  it('never reads as a permanent failure', () => {
    render(<CaptionsParkedNotice reason={NO_SESSION_REASON} />);
    // It names the condition as reversible, in words.
    expect(
      document.querySelector('[data-captions-park-reversible]')?.textContent,
    ).toMatch(/releases itself the moment YouTube is signed in/i);
    expect(document.body.textContent).toMatch(/Not a failure/i);
    // ...and it never borrows one of the permanent-sounding generic states.
    for (const phrase of PERMANENT_SOUNDING) {
      expect(screen.queryByText(phrase)).toBeNull();
    }
  });

  it('offers the Cookie Bridge remedy when the host can navigate', () => {
    const onOpen = vi.fn();
    render(<CaptionsParkedNotice reason={NO_SESSION_REASON} onOpenCookieBridge={onOpen} />);
    const btn = screen.getByRole('button', { name: /open cookie bridge/i });
    btn.click();
    expect(onOpen).toHaveBeenCalledTimes(1);
  });

  it('renders without the button when the host cannot navigate', () => {
    render(<CaptionsParkedNotice reason={NO_SESSION_REASON} />);
    expect(screen.queryByRole('button')).toBeNull();
    // The notice still carries the remedy in words.
    expect(document.querySelector('[data-captions-park-reason]')?.textContent).toMatch(
      /Cookie Bridge, sign in to YouTube/i,
    );
  });

  it('falls back to the generic sentence for wording it does not recognise', () => {
    render(<CaptionsParkedNotice reason="A brand new reason a future backend might send" />);
    expect(document.querySelector('[data-captions-park-body]')?.textContent).toMatch(
      /age-restricted, so no captions could be read/i,
    );
  });

  it('is translated in pt-BR and es (not an English fallback)', () => {
    setLanguage('pt-BR');
    const { unmount } = render(
      <CaptionsParkedNotice reason={NO_SESSION_REASON} onOpenCookieBridge={() => {}} />,
    );
    expect(screen.getByText('Legendas pausadas')).toBeTruthy();
    expect(document.querySelector('[data-captions-park-body]')?.textContent).toMatch(
      /nenhuma sessão autenticada do YouTube está configurada/i,
    );
    expect(screen.getByRole('button', { name: 'Abrir Cookie Bridge' })).toBeTruthy();
    unmount();

    setLanguage('es');
    render(<CaptionsParkedNotice reason={NO_SESSION_REASON} onOpenCookieBridge={() => {}} />);
    expect(screen.getByText('Subtítulos pausados')).toBeTruthy();
    expect(document.querySelector('[data-captions-park-body]')?.textContent).toMatch(
      /no hay ninguna sesión de YouTube autenticada configurada/i,
    );
    expect(screen.getByRole('button', { name: 'Abrir Cookie Bridge' })).toBeTruthy();
  });

  it('keeps the reversibility statement in every locale', () => {
    for (const [lang, needle] of [
      ['pt-BR', /o bloqueio se solta sozinho/i],
      ['es', /el bloqueo se libera solo/i],
    ] as const) {
      setLanguage(lang);
      const { unmount } = render(<CaptionsParkedNotice reason={NO_SESSION_REASON} />);
      expect(document.querySelector('[data-captions-park-reversible]')?.textContent).toMatch(needle);
      unmount();
    }
  });
});

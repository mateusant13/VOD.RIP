import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { getLanguage, setLanguage } from '../i18n';
import { DICTS } from '../i18n';
import ChannelParkedOutcomes from './ChannelParkedOutcomes';

/**
 * The parked-channels panel, rendered.
 *
 * What these pin, in the order the owner meets it:
 *
 *  1. THE FIVE STATES ARE DISTINCT. loading / unavailable / empty / a known row
 *     / a row whose code this build cannot name. The important one is
 *     `unavailable`: a failed read must NEVER render the empty state, because
 *     "I could not read this" and "nothing is parked" are opposite claims and
 *     only one of them is true.
 *  2. THE REASON IS A LOCALISED PHRASE DERIVED FROM THE CODE. Rendered as a
 *     sentence in pt-BR and es, never as the English literal and never as the
 *     raw `tab_absent` identifier.
 *  3. A RELEASE IS NOT A FIX. The copy next to the button says the next cycle
 *     asks again and that the same condition will be learned again.
 */

const saved = getLanguage();
afterEach(() => {
  setLanguage(saved);
});

function row(over: Record<string, unknown> = {}) {
  return {
    channel: 'seeelbr',
    tab: 'streams',
    outcome_code: 'channel_gone',
    permanent: true,
    known: true,
    first_seen: '2026-10-04T22:25:00+00:00',
    last_seen: '2026-10-04T22:25:00+00:00',
    skipped: 0,
    ...over,
  };
}

type Handler = (url: string, init?: RequestInit) => Response | Promise<Response>;

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function stub(handler: Handler) {
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) =>
    handler(String(input), init),
  );
  vi.stubGlobal('fetch', fn);
  return fn;
}

/** The default: a read that succeeds with the rows given. */
function stubSnapshot(rows: unknown[], over: Record<string, unknown> = {}) {
  return stub((_url, init) => {
    if (init?.method === 'POST') {
      return json({ channel: 'seeelbr', tab: 'streams', platform: 'youtube', released: 1, status: 'released' });
    }
    return json({ platform: 'youtube', count: rows.length, parked: rows, ...over });
  });
}

const q = (sel: string) => document.querySelector(sel);

beforeEach(() => {
  vi.unstubAllGlobals();
});

// --- 1. the five states are distinct ----------------------------------------

describe('ChannelParkedOutcomes - the states are distinct', () => {
  it('says nothing about the channels while the first read is in flight', () => {
    stub(() => new Promise<Response>(() => {}));
    render(<ChannelParkedOutcomes />);
    expect(q('[data-parked-loading]')).toBeTruthy();
    // Crucially NOT the empty state: a read that has not answered has told us
    // nothing at all.
    expect(q('[data-parked-empty]')).toBeNull();
  });

  it('renders an EXPLICIT empty state for a read that found nothing', async () => {
    stubSnapshot([]);
    render(<ChannelParkedOutcomes />);
    await waitFor(() => expect(q('[data-parked-empty]')).toBeTruthy());
    expect(q('[data-parked-empty]')?.textContent).toMatch(/no channel is parked/i);
    expect(q('[data-parked-unavailable]')).toBeNull();
    expect(q('[data-parked-list]')).toBeNull();
  });

  it('never renders the empty state when the read FAILED', async () => {
    // The defect this whole surface exists near: telling the owner "nothing is
    // parked" when the app simply could not ask.
    stub(() => new Response('nope', { status: 404 }));    render(<ChannelParkedOutcomes />);
    await waitFor(() => expect(q('[data-parked-unavailable]')).toBeTruthy());
    expect(q('[data-parked-unavailable]')?.textContent).toMatch(
      /nothing is being claimed about your channels/i,
    );
    expect(q('[data-parked-empty]')).toBeNull();
    expect(q('[data-parked-list]')).toBeNull();
  });

  it('never renders the empty state when the payload is not one we understand', async () => {
    stub(() => json({ platform: 'youtube' })); // no `parked` array
    render(<ChannelParkedOutcomes />);
    await waitFor(() => expect(q('[data-parked-unavailable]')).toBeTruthy());
    expect(q('[data-parked-empty]')).toBeNull();
  });

  it('renders each parked channel with its tab, code, reason and learning time', async () => {
    stubSnapshot([row(), row({ channel: 'srdoglol', outcome_code: 'tab_absent', skipped: 12 })]);
    render(<ChannelParkedOutcomes />);
    await waitFor(() => expect(q('[data-parked-list]')).toBeTruthy());

    const rows = document.querySelectorAll('[data-parked-row]');
    expect(rows).toHaveLength(2);
    expect(q('[data-parked-count]')?.textContent).toBe('2');
    expect(q('[data-parked-channel]')?.textContent).toBe('@seeelbr');
    expect(q('[data-parked-tab]')?.textContent).toBe('streams');
    // The learned time, not the raw blob.
    expect(q('[data-parked-learned]')?.textContent).toMatch(/2026-10-04 22:25/);
    // A counted skip is shown...
    expect(rows[1].textContent).toMatch(/12 requests skipped/);
    // ...and an uncounted one is NOT printed as a confident zero.
    expect(rows[0].textContent).not.toMatch(/requests skipped/);
  });

  it('describes a code it KNOWS but that is not a parkable condition, without vouching for it', async () => {
    // `live_offline` is in the vocabulary, so the panel can describe it - but the
    // backend marked it known:false because it is NOT a permanent per-channel
    // condition, so it must not be rendered as a park it stands behind.
    stubSnapshot([row({ outcome_code: 'live_offline', known: false, permanent: false })]);
    render(<ChannelParkedOutcomes />);
    await waitFor(() => expect(q('[data-parked-row]')).toBeTruthy());
    const el = q('[data-parked-row]')!;
    expect(el.getAttribute('data-parked-known')).toBe('false');
    expect(el.getAttribute('data-parked-code')).toBe('live_offline');
    expect(q('[data-parked-reason]')?.textContent).toMatch(/live event is offline/i);
  });

  it('falls back to the catch-all phrase for a code this build cannot name at all', async () => {
    stubSnapshot([
      row({ outcome_code: 'something_a_future_build_knows', known: false, permanent: false }),
    ]);
    render(<ChannelParkedOutcomes />);
    await waitFor(() => expect(q('[data-parked-row]')).toBeTruthy());
    const el = q('[data-parked-row]')!;
    // Still LISTED, so the owner can release it...
    expect(el.getAttribute('data-parked-known')).toBe('false');
    expect(el.getAttribute('data-parked-code')).toBe('something_a_future_build_knows');
    // ...but described with the catch-all phrase, never the raw identifier.
    expect(q('[data-parked-reason]')?.textContent).toMatch(
      /condition this build does not recognise/i,
    );
    expect(q('[data-parked-reason]')?.textContent).not.toMatch(
      /something_a_future_build_knows/,
    );
  });
});

// --- 2. the reason is a LOCALISED phrase derived from the code ---------------

describe('ChannelParkedOutcomes - the reason is localised, not an English literal', () => {
  it('renders a different sentence per parkable code, derived from the code', async () => {
    stubSnapshot([row(), row({ channel: 'other', outcome_code: 'tab_absent' })]);
    render(<ChannelParkedOutcomes />);
    await waitFor(() => expect(q('[data-parked-list]')).toBeTruthy());
    const reasons = [...document.querySelectorAll('[data-parked-reason]')].map(
      (e) => e.textContent,
    );
    expect(reasons[0]).toMatch(/is gone or renamed/i);
    expect(reasons[1]).toMatch(/no streams tab/i);
    expect(reasons[0]).not.toBe(reasons[1]);
  });

  it('names the tab inside the reason, interpolated', async () => {
    stubSnapshot([row({ outcome_code: 'tab_absent', tab: 'shorts' })]);
    render(<ChannelParkedOutcomes />);
    await waitFor(() => expect(q('[data-parked-reason]')).toBeTruthy());
    expect(q('[data-parked-reason]')?.textContent).toMatch(/no shorts tab/i);
  });

  it('is translated in pt-BR and es, and never shows the raw identifier as the reason', async () => {
    for (const [lang, needle] of [
      ['pt-BR', /sumiu ou foi renomeado/i],
      ['es', /desapareció o fue renombrado/i],
    ] as const) {
      setLanguage(lang);
      stubSnapshot([row()]);
      const { unmount } = render(<ChannelParkedOutcomes />);
      await waitFor(() => expect(q('[data-parked-reason]')).toBeTruthy());
      expect(q('[data-parked-reason]')?.textContent).toMatch(needle);
      // The identifier is shown as a CODE chip, never as the reason sentence.
      expect(q('[data-parked-reason]')?.textContent).not.toMatch(/channel_gone/);
      expect(q('[data-parked-code-chip]')?.textContent).toBe('channel_gone');
      expect(screen.getByRole('button', { name: DICTS[lang]['channelPark.release'] })).toBeTruthy();
      unmount();
    }
  });

  it('has every new key in all three locales (no English fallback for a pt-BR user)', () => {
    const keys = Object.keys(DICTS.en).filter((k) => k.startsWith('channelPark.'));
    // Guard the guard: a scan that stops finding keys would report parity while
    // the surface silently renders English.
    expect(keys.length).toBeGreaterThanOrEqual(20);
    for (const key of keys) {
      for (const lang of ['pt-BR', 'es'] as const) {
        expect(DICTS[lang][key], `${lang} is missing "${key}"`).toBeTruthy();
        expect(DICTS[lang][key], `${lang} left "${key}" in English`).not.toBe(DICTS.en[key]);
      }
    }
  });
});

// --- 3. a release is not a fix ----------------------------------------------

describe('ChannelParkedOutcomes - a release asks again, it does not repair', () => {
  it('says so next to the button, not only in a comment', async () => {
    stubSnapshot([row()]);
    render(<ChannelParkedOutcomes />);
    await waitFor(() => expect(q('[data-parked-reversible]')).toBeTruthy());
    const copy = q('[data-parked-reversible]')?.textContent ?? '';
    expect(copy).toMatch(/does not mean the channel works/i);
    expect(copy).toMatch(/learned again/i);
  });

  it('keeps that statement in every locale', async () => {
    for (const [lang, needle] of [
      ['pt-BR', /não significa que o canal funciona/i],
      ['es', /no significa que el canal funcione/i],
    ] as const) {
      setLanguage(lang);
      stubSnapshot([row()]);
      const { unmount } = render(<ChannelParkedOutcomes />);
      await waitFor(() => expect(q('[data-parked-reversible]')).toBeTruthy());
      expect(q('[data-parked-reversible]')?.textContent).toMatch(needle);
      unmount();
    }
  });

  it('posts the release for the row that was pressed and says it is durable', async () => {
    const fn = stubSnapshot([row(), row({ channel: 'srdoglol' })]);
    render(<ChannelParkedOutcomes />);
    await waitFor(() => expect(document.querySelectorAll('[data-parked-row]')).toHaveLength(2));

    fireEvent.click(document.querySelectorAll('[data-parked-release]')[1]);
    await waitFor(() => expect(q('[data-parked-notice]')).toBeTruthy());

    const post = fn.mock.calls.find(([, init]) => (init as RequestInit)?.method === 'POST');
    expect(post).toBeTruthy();
    expect(String((post![0] as string))).toContain('/api/channel/outcome-park/release');
    expect(JSON.parse(String((post![1] as RequestInit).body))).toEqual({
      channel: 'srdoglol',
      platform: 'youtube',
      tab: 'streams',
    });
    expect(q('[data-parked-notice]')?.textContent).toMatch(/next cycle asks again/i);
    expect(q('[data-parked-notice]')?.textContent).toMatch(/learned again/i);
  });

  it('reports a release of NOTHING as a no-op, never as a success', async () => {
    // The backend answers released: 0 on a second press. Printing "released"
    // then would be a fabricated success for a park that was not there.
    stub((_url, init) =>
      init?.method === 'POST'
        ? json({ channel: 'seeelbr', tab: 'streams', platform: 'youtube', released: 0, status: 'nothing_to_release' })
        : json({ platform: 'youtube', count: 1, parked: [row()] }),
    );
    render(<ChannelParkedOutcomes />);
    await waitFor(() => expect(q('[data-parked-release]')).toBeTruthy());
    fireEvent.click(q('[data-parked-release]')!);
    await waitFor(() => expect(q('[data-parked-notice]')).toBeTruthy());
    expect(q('[data-parked-notice]')?.getAttribute('data-parked-notice-kind')).toBe('none');
    expect(q('[data-parked-notice]')?.textContent).toMatch(/nothing to release/i);
  });

  it('says a FAILED release changed nothing, and keeps the row', async () => {
    stub((_url, init) =>
      init?.method === 'POST'
        ? json({ detail: 'boom' }, 500)
        : json({ platform: 'youtube', count: 1, parked: [row()] }),
    );
    render(<ChannelParkedOutcomes />);
    await waitFor(() => expect(q('[data-parked-release]')).toBeTruthy());
    fireEvent.click(q('[data-parked-release]')!);
    await waitFor(() => expect(q('[data-parked-notice]')).toBeTruthy());
    expect(q('[data-parked-notice]')?.getAttribute('data-parked-notice-kind')).toBe('fail');
    expect(q('[data-parked-notice]')?.textContent).toMatch(/nothing changed/i);
    expect(q('[data-parked-row]')).toBeTruthy();
  });
});

/**
 * First-run plugin picker (FirstRunWizard.tsx).
 *
 * Runs in pt-BR on purpose: the app's UI language is pt-BR, and these
 * assertions double as the proof that the new strings are translated.
 *
 * RED ARM per case — see the marked lines.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import FirstRunWizard from './FirstRunWizard';
import { resetOnboarding } from '../lib/featureFlags';
import { isPluginEnabled } from '../lib/plugins';
import { getLanguage, setLanguage } from '../i18n';

const savedLang = getLanguage();
beforeEach(() => {
  localStorage.clear();
  resetOnboarding();
  vi.unstubAllGlobals();
  // setLanguage notifies i18n subscribers: keep it inside act() or React
  // logs an act warning for the next render.
  act(() => { setLanguage('pt-BR'); });
});
afterEach(() => {
  localStorage.clear();
  vi.unstubAllGlobals();
  act(() => { setLanguage(savedLang); });
});

function stubApi() {
  const fn = vi.fn(async () => new Response(JSON.stringify({}), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  }));
  vi.stubGlobal('fetch', fn);
  return fn;
}

function pluginPosts(fn: ReturnType<typeof vi.fn>) {
  return fn.mock.calls
    .filter(([url]) => String(url) === '/api/plugins')
    .map(([, init]) => JSON.parse(String((init as RequestInit).body)));
}

const HEADING = /Bem-vindo ao VOD\.RIP/i;

/** A click writes through the store, which notifies React subscribers in a
 *  microtask — keep it inside act(). */
async function click(el: HTMLElement): Promise<void> {
  await act(async () => { fireEvent.click(el); });
}

describe('FirstRunWizard — seletor de plugins (pt-BR)', () => {
  it('abre na primeira execução e NÃO na segunda', () => {
    stubApi();
    const first = render(<FirstRunWizard />);
    expect(screen.getByText(HEADING)).toBeInTheDocument();
    // RED ARM: drop the hasCompletedOnboarding() guard in the mount effect and
    // the second render below still shows the modal.
    first.unmount();

    localStorage.setItem('vodrip.onboardingDone', '1');
    render(<FirstRunWizard />);
    expect(screen.queryByText(HEADING)).not.toBeInTheDocument();
  });

  it('lista cada plugin com nome, descrição e o selo "consome mais energia" nos pesados', () => {
    stubApi();
    render(<FirstRunWizard />);
    expect(screen.getByRole('checkbox', { name: 'Canais' })).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: 'Transcrição' })).toBeInTheDocument();
    // 4 pesados no catálogo: transcrição, legendas ao vivo, prévia ao vivo, IA.
    expect(screen.getAllByText('Consome mais energia')).toHaveLength(4);
    expect(screen.getByText('Acompanhe canais salvos, veja o que saiu novo e arquive VODs.')).toBeInTheDocument();
    // RED ARM: render CostBadge without the cost check and the light rows
    // below also show the heavy seal (count above stops being 4).
    expect(screen.getAllByText('Leve').length).toBeGreaterThan(0);
  });

  it('trava os plugins do núcleo ligados', () => {
    stubApi();
    render(<FirstRunWizard />);
    const core = screen.getByRole('checkbox', { name: 'Canais' }) as HTMLInputElement;
    expect(core.checked).toBe(true);
    expect(core.disabled).toBe(true);
    expect(screen.getAllByText('Sempre ligado').length).toBe(2);
  });

  it('cada toggle chama POST /api/plugins', async () => {
    const fn = stubApi();
    render(<FirstRunWizard />);
    const box = screen.getByRole('checkbox', { name: 'Busca no arquivo' }) as HTMLInputElement;
    expect(box.checked).toBe(true);
    await click(box);
    // RED ARM: make the row's onChange only set local state (no setPluginEnabled)
    // and this POST never happens.
    await waitFor(() =>
      expect(pluginPosts(fn)).toEqual([{ id: 'archive-search', enabled: false }]),
    );
  });

  it('"SÓ O ESSENCIAL" liga só o núcleo', async () => {
    const fn = stubApi();
    render(<FirstRunWizard />);
    await click(screen.getByRole('button', { name: /só o essencial/i }));
    await waitFor(() => expect(isPluginEnabled('transcribe')).toBe(false));
    expect(isPluginEnabled('channels')).toBe(true);
    expect(isPluginEnabled('link-download')).toBe(true);
    expect(isPluginEnabled('archive-search')).toBe(false);
    // RED ARM: point the "essencial" button at anything but coreOnlyPatch() and
    // these two assertions disagree with the POSTs below.
    const posted = pluginPosts(fn);
    // 12 plugins - 2 núcleo: o núcleo é sempre ligado, não gera POST.
    expect(posted).toHaveLength(10);
    expect(posted.every((p) => p.enabled === false)).toBe(true);
    expect(posted.map((p) => p.id)).not.toContain('channels');
  });

  it('"TUDO" liga todos, pesados incluídos', async () => {
    const fn = stubApi();
    render(<FirstRunWizard />);
    await click(screen.getByRole('button', { name: /^tudo$/i }));
    await waitFor(() => expect(isPluginEnabled('transcribe')).toBe(true));
    expect(pluginPosts(fn).every((p) => p.enabled === true)).toBe(true);
    // 12 plugins - 2 core = 10 POSTs (core never posts).
    expect(pluginPosts(fn)).toHaveLength(10);
  });

  it('"SALVAR E CONTINUAR" fecha o modal e arma o flag de onboarding', async () => {
    stubApi();
    render(<FirstRunWizard />);
    await click(screen.getByRole('button', { name: /salvar e continuar/i }));
    await waitFor(() => expect(screen.queryByText(HEADING)).not.toBeInTheDocument());
    expect(localStorage.getItem('vodrip.onboardingDone')).toBe('1');
  });
});

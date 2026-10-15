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
import { isPluginEnabled, PLUGINS, ALWAYS_ON } from '../lib/plugins';
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

  it('lista os 4 plugins pesados com nome, descrição e custo legível', () => {
    stubApi();
    render(<FirstRunWizard />);
    // O catálogo deixou de ter 12 plugins: só os 4 que custam algo são plugins.
    expect(PLUGINS).toHaveLength(4);
    for (const name of ['Transcrição', 'Prévia ao vivo', 'Legendas ao vivo', 'Perguntar à IA']) {
      expect(screen.getByRole('checkbox', { name })).toBeInTheDocument();
    }
    expect(screen.getByText('Escreve o que foi dito nos VODs arquivados para você pesquisar depois.')).toBeInTheDocument();
    // Custo por linha: MB < 1024 fica em MB; senão GB com 1 casa.
    // RED ARM: remova o toFixed(1) do formatMb e "2.0 GB" deixa de casar.
    expect(screen.getByTestId('wizard-cost-transcribe')).toHaveTextContent('397 MB em disco · 2.0 GB de VRAM');
    expect(screen.getByTestId('wizard-cost-live-preview')).toHaveTextContent('0 MB em disco · 400 MB de VRAM');
  });

  it("troca o número por 'incluso (mesmo modelo)' quando shares_with já está ligado", async () => {
    stubApi();
    render(<FirstRunWizard />);
    // Só a legenda ligada: ela é a dona do modelo e mostra o número.
    await click(screen.getByRole('checkbox', { name: 'Legendas ao vivo' }));
    expect(screen.getByTestId('wizard-cost-live-subtitles')).toHaveTextContent('397 MB em disco · 2.0 GB de VRAM');
    // Ligar a transcrição (MESMO modelo) troca a repetição por "incluso".
    // RED ARM: ignore shares_with na linha de custo e o 397 MB reaparece aqui.
    await click(screen.getByRole('checkbox', { name: 'Transcrição' }));
    expect(screen.getByTestId('wizard-cost-live-subtitles')).toHaveTextContent('incluso (mesmo modelo)');
  });

  it('cada toggle chama POST /api/plugins', async () => {
    const fn = stubApi();
    render(<FirstRunWizard />);
    const box = screen.getByRole('checkbox', { name: 'Perguntar à IA' }) as HTMLInputElement;
    expect(box.checked).toBe(false);
    await click(box);
    // RED ARM: make the row's onChange only set local state (no setPluginEnabled)
    // and this POST never happens.
    await waitFor(() =>
      expect(pluginPosts(fn)).toEqual([{ id: 'ai-ask', enabled: true }]),
    );
  });

  it('"SÓ O ESSENCIAL" desliga todos os plugins (o app always-on fica)', async () => {
    const fn = stubApi();
    render(<FirstRunWizard />);
    await click(screen.getByRole('button', { name: /só o essencial/i }));
    await waitFor(() => expect(pluginPosts(fn)).toHaveLength(4));
    expect(pluginPosts(fn).every((p) => p.enabled === false)).toBe(true);
    for (const p of PLUGINS) expect(isPluginEnabled(p.id)).toBe(false);
    // O que NÃO é plugin continua ligado — é o app.
    expect(isPluginEnabled('channels')).toBe(true);
    expect(isPluginEnabled('link-download')).toBe(true);
  });

  it('"TUDO" liga todos, pesados incluídos', async () => {
    const fn = stubApi();
    render(<FirstRunWizard />);
    await click(screen.getByRole('button', { name: /^tudo$/i }));
    await waitFor(() => expect(pluginPosts(fn)).toHaveLength(4));
    expect(pluginPosts(fn).every((p) => p.enabled === true)).toBe(true);
    for (const p of PLUGINS) expect(isPluginEnabled(p.id)).toBe(true);
  });

  it('resume o total do selecionado sem contar o mesmo modelo duas vezes', async () => {
    stubApi();
    render(<FirstRunWizard />);
    // Nada ligado -> total zerado.
    expect(screen.getByTestId('wizard-totals')).toHaveTextContent('0 MB em disco · 0 MB de VRAM');
    // Transcrever e legendar usam o MESMO modelo de 397 MB / 2.0 GB de VRAM.
    // Os presets fecham o modal, então o total vivo é observado por toggle.
    await click(screen.getByRole('checkbox', { name: 'Transcrição' }));
    await click(screen.getByRole('checkbox', { name: 'Legendas ao vivo' }));
    // RED ARM: soma ingênua (sem shares_with) dobraria para 794 MB / 4.0 GB.
    const totals = screen.getByTestId('wizard-totals');
    expect(totals).toHaveTextContent('397 MB em disco · 2.0 GB de VRAM');
    expect(totals).not.toHaveTextContent('794 MB');
    expect(totals).not.toHaveTextContent('4.0 GB');
  });

  it('lista os 8 alwaysOn como "Sempre ligado", sem toggle', () => {
    stubApi();
    render(<FirstRunWizard />);
    const box = screen.getByTestId('wizard-always-on');
    for (const a of ALWAYS_ON) expect(box).toHaveTextContent(a.name);
    expect(ALWAYS_ON).toHaveLength(8);
    // Sem toggle: os ids always-on não viram checkbox.
    expect(box.querySelectorAll('input')).toHaveLength(0);
    for (const a of ALWAYS_ON) expect(screen.queryByRole('checkbox', { name: a.name })).not.toBeInTheDocument();
  });

  it('"SALVAR E CONTINUAR" fecha o modal e arma o flag de onboarding', async () => {
    stubApi();
    render(<FirstRunWizard />);
    await click(screen.getByRole('button', { name: /salvar e continuar/i }));
    await waitFor(() => expect(screen.queryByText(HEADING)).not.toBeInTheDocument());
    expect(localStorage.getItem('vodrip.onboardingDone')).toBe('1');
  });
});

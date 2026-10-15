/**
 * EnablePluginPrompt — a plugin-gated surface must ASK to be enabled.
 *
 * RED ARM: change the button's call to `setPluginEnabled(pluginId, true)` so it
 * passes a hard-coded wrong id, and the "enables THAT plugin id" case fails on
 * both the stored key and the POST body. See the comment on that case.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { EnablePluginPrompt, formatPluginSize } from './EnablePluginPrompt';
import { DICTS, setLanguage } from '../i18n';

const STATE_LS = 'vodrip.plugins';
const CATALOG_LS = 'vodrip.plugins.catalog';

/**
 * A catalog MIRROR with the measured costs (`plugin_registry.PLUGINS`):
 * transcribe downloads 397 MB of parakeet weights and asks for 2 GB of VRAM;
 * ai-ask is local-nothing (the model runs behind the user's own API key).
 * Seeded explicitly so the case does not depend on the built-in mirror gaining
 * cost fields, and so it proves the numbers are READ from the mirror.
 */
const CATALOG = [
  {
    id: 'transcribe', name: 'Transcrição', blurb: 'Escreve o que foi dito nos VODs.',
    cost: 'pesado', defaultOn: false, settingsKey: 'plugin_transcribe',
    disk_mb: 397, vram_mb: 2048, ram_mb: 2560, shares_with: '', recommended: true,
  },
  {
    id: 'ai-ask', name: 'Perguntar à IA', blurb: 'Perguntas sobre o arquivo.',
    cost: 'pesado', defaultOn: false, settingsKey: 'plugin_ai_ask',
    disk_mb: 0, vram_mb: 0, ram_mb: 128, shares_with: '', recommended: false,
  },
];

/** Seed the stored plugin map + the catalog mirror BEFORE render. */
function seed(state: Record<string, boolean>) {
  localStorage.setItem(CATALOG_LS, JSON.stringify(CATALOG));
  localStorage.setItem(STATE_LS, JSON.stringify(state));
}

function mockFetch() {
  const fn = vi.fn(async () => new Response(JSON.stringify({}), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  }));
  vi.stubGlobal('fetch', fn);
  return fn;
}

function pluginPosts(fn: ReturnType<typeof vi.fn>) {
  return fn.mock.calls
    .filter(([u]) => String(u) === '/api/plugins')
    .map(([, init]) => JSON.parse(String((init as RequestInit).body)));
}

beforeEach(() => {
  localStorage.clear();
  setLanguage('en');
  mockFetch();
});

afterEach(() => {
  localStorage.clear();
  vi.unstubAllGlobals();
  setLanguage('en');
});

describe('EnablePluginPrompt', () => {
  it('renders the plugin name and its cost from the mirror while the plugin is off', () => {
    seed({ transcribe: false });
    render(<EnablePluginPrompt pluginId="transcribe" />);

    // Name comes from the catalog, not the id.
    expect(screen.getByText('Transcrição is off')).toBeTruthy();
    // Cost comes from the mirror's disk_mb/vram_mb — 2048 MB is "2 GB".
    expect(screen.getByText('Disk: 397 MB · VRAM: 2 GB')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Enable' })).toBeTruthy();
    expect(document.querySelector('[data-enable-plugin="transcribe"]')).toBeTruthy();
  });

  it('enables THAT plugin id — stored key AND the POST body (RED ARM below)', async () => {
    const fetchMock = mockFetch();
    seed({ transcribe: false });
    render(<EnablePluginPrompt pluginId="transcribe" />);

    fireEvent.click(screen.getByRole('button', { name: 'Enable' }));

    // The local cache is the UI's truth…
    await waitFor(() => {
      expect(JSON.parse(localStorage.getItem(STATE_LS) ?? '{}').transcribe).toBe(true);
    });
    // …and the backend learned the SAME id. A hard-coded wrong id fails both
    // assertions here.
    await waitFor(() => expect(pluginPosts(fetchMock)).toEqual([{ id: 'transcribe', enabled: true }]));

    // Plugin now on → the prompt asks for nothing.
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Enable' })).toBeNull());
  });

  it('renders nothing when the plugin is already on', () => {
    seed({ transcribe: true });
    const { container } = render(<EnablePluginPrompt pluginId="transcribe" />);

    expect(container.firstChild).toBeNull();
    expect(document.querySelector('[data-enable-plugin]')).toBeNull();
  });

  it('renders nothing for an id that is not in the catalog (not a plugin, nothing to enable)', () => {
    seed({ transcribe: false });
    const { container } = render(<EnablePluginPrompt pluginId="not-a-plugin" />);

    expect(container.firstChild).toBeNull();
  });

  it('a plugin with no disk and no VRAM says so instead of showing fake zeros', () => {
    seed({ 'ai-ask': false });
    render(<EnablePluginPrompt pluginId="ai-ask" />);

    expect(screen.getByText('Perguntar à IA is off')).toBeTruthy();
    expect(screen.getByText('No download and no GPU use.')).toBeTruthy();
  });

  it('renders the strip variant for an overlay slot', () => {
    seed({ transcribe: false });
    render(<EnablePluginPrompt pluginId="transcribe" variant="strip" />);

    expect(document.querySelector('[data-enable-plugin-variant="strip"]')).toBeTruthy();
    expect(screen.getByText('Disk: 397 MB · VRAM: 2 GB')).toBeTruthy();
  });

  it('renders in pt-BR with the translated button label', () => {
    setLanguage('pt-BR');
    seed({ transcribe: false });
    render(<EnablePluginPrompt pluginId="transcribe" />);

    expect(screen.getByText('Transcrição está desligado')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Habilitar' })).toBeTruthy();
  });

  it('every string it renders exists in pt-BR and es', () => {
    for (const key of [
      'plugins.prompt.title', 'plugins.prompt.body', 'plugins.enable',
      'plugins.cost.disk', 'plugins.cost.vram', 'plugins.cost.none',
    ]) {
      expect(DICTS.en[key], `en is missing "${key}"`).toBeTruthy();
      for (const lang of ['pt-BR', 'es'] as const) {
        expect(DICTS[lang][key], `${lang} is missing "${key}"`).toBeTruthy();
        expect(DICTS[lang][key], `${lang} left "${key}" in English`).not.toBe(key);
      }
    }
  });

  it('formats sizes as the shortest honest label', () => {
    expect(formatPluginSize(397)).toBe('397 MB');
    expect(formatPluginSize(2048)).toBe('2 GB');
    expect(formatPluginSize(1536)).toBe('1.5 GB');
    expect(formatPluginSize(0)).toBe('0 MB');
  });
});

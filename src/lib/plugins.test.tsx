/**
 * Plugin contract tests — src/lib/plugins.tsx (the frontend half).
 *
 * RED ARM for each item: revert the behaviour named in the case and the case
 * fails. Item (c) — an id outside the catalog is always on — is the
 * load-bearing one: without it the eight ALWAYS_ON surfaces vanish behind a
 * gate that answers false for a plugin id that is no longer catalogued.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { act, render, screen } from '@testing-library/react';
import {
  ALWAYS_ON,
  ALWAYS_ON_IDS,
  PLUGINS,
  PLUGIN_IDS,
  PLUGIN_TABS,
  allOnPatch,
  alwaysOnFeatures,
  bundleCost,
  coreOnlyPatch,
  countEnabledPlugins,
  enabledPlugins,
  isPluginEnabled,
  orderPlugins,
  pluginCatalog,
  pluginEnabled,
  pluginTotals,
  setPluginEnabled,
  PluginGate,
  setPluginsBulk,
  syncPluginsFromBackend,
  tabsForPlugins,
  usePlugin,
  usePlugins,
} from './plugins';

beforeEach(() => {
  localStorage.clear();
  vi.unstubAllGlobals();
});
afterEach(() => {
  localStorage.clear();
  vi.unstubAllGlobals();
});

/** Fetch stub that records calls, for /api/plugins only. */
function stubApi(impl?: (url: string, init?: RequestInit) => unknown) {
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const body = impl?.(String(input), init);
    return new Response(JSON.stringify(body ?? {}), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  });
  vi.stubGlobal('fetch', fn);
  return fn as unknown as ReturnType<typeof vi.fn>;
}

function posts(fn: ReturnType<typeof vi.fn>) {
  return fn.mock.calls
    .filter(([url]) => String(url) === '/api/plugins')
    .map(([, init]) => JSON.parse(String((init as RequestInit).body)));
}

/** usePlugins() needs a component; this reads it once. */
function usePluginsList() {
  let out: { id: string; enabled: boolean }[] = [];
  function Probe() {
    out = usePlugins().map((p) => ({ id: p.id, enabled: p.enabled }));
    return null;
  }
  render(<Probe />);
  return out;
}

/** Reads usePlugin(id) the way a real gate does. */
function GateProbe({ id }: { id: string }) {
  const on = usePlugin(id);
  return <span data-testid={'gate-' + id}>{on ? 'on' : 'off'}</span>;
}

describe('catalog — only the four heavy plugins', () => {
  it('carries exactly the four heavy ids, in the backend order', () => {
    expect(PLUGIN_IDS).toEqual(['transcribe', 'live-preview', 'live-subtitles', 'ai-ask']);
  });

  it('mirrors every cost field 1:1 with the backend registry', () => {
    // PINNED, not read back from the catalog: these are the measured numbers
    // in backend/services/plugin_registry.py, and a divergence is a settings
    // dialog that lies about a 397 MB download.
    const actual = Object.fromEntries(PLUGINS.map((p) => [p.id, {
      settingsKey: p.settingsKey,
      disk_mb: p.disk_mb,
      vram_mb: p.vram_mb,
      ram_mb: p.ram_mb,
      shares_with: p.shares_with,
      recommended: p.recommended,
      defaultOn: p.defaultOn,
    }]));
    expect(actual).toEqual({
      transcribe: { settingsKey: 'plugin_transcribe', disk_mb: 397, vram_mb: 2048, ram_mb: 2560, shares_with: '', recommended: true, defaultOn: false },
      'live-preview': { settingsKey: 'plugin_live_preview', disk_mb: 0, vram_mb: 400, ram_mb: 600, shares_with: '', recommended: true, defaultOn: false },
      'live-subtitles': { settingsKey: 'plugin_live_subtitles', disk_mb: 397, vram_mb: 2048, ram_mb: 1536, shares_with: 'transcribe', recommended: false, defaultOn: false },
      'ai-ask': { settingsKey: 'plugin_ai_ask', disk_mb: 0, vram_mb: 0, ram_mb: 128, shares_with: '', recommended: false, defaultOn: false },
    });
  });

  it('every catalog row is heavy and carries the contract fields', () => {
    for (const p of PLUGINS) {
      expect(p.id).toBeTruthy();
      expect(p.name).toBeTruthy();
      expect(p.blurb).toBeTruthy();
      // A plugin IS something heavy — that is why the light ones moved out.
      expect(p.cost).toBe('pesado');
      expect(p.settingsKey).toBe('plugin_' + p.id.replace(/-/g, '_'));
      expect(p.disk_mb + p.vram_mb + p.ram_mb).toBeGreaterThan(0);
      // No catalog entry is core any more: core means "cannot be turned off",
      // and the ones that cannot be turned off are ALWAYS_ON.
      expect(p.core).toBeFalsy();
    }
  });

  it('none of the eight light surfaces is in the catalog', () => {
    for (const id of ALWAYS_ON_IDS) expect(PLUGIN_IDS).not.toContain(id);
  });
});

describe('ALWAYS_ON — the eight surfaces that are not heavy', () => {
  it('lists the eight ids in the backend order', () => {
    expect(ALWAYS_ON_IDS).toEqual([
      'link-download', 'channels', 'channel-explore', 'clip-editor',
      'twitch-clip', 'archive-search', 'local-file', 'live-chat',
    ]);
  });

  it('every always-on row has a name and a blurb (there is no toggle to render)', () => {
    for (const a of ALWAYS_ON) {
      expect(a.id).toBeTruthy();
      expect(a.name).toBeTruthy();
      expect(a.blurb).toBeTruthy();
    }
  });
});

describe('nucleus rule — an id outside the catalog is always on', () => {
  it('answers TRUE for every ALWAYS_ON id', () => {
    // RED ARM (item c): drop the early `if (!def) return true` in
    // pluginEnabled() and all eight of these go false — the nine App.tsx /
    // popup gates wrap surfaces in exactly these ids.
    for (const id of ALWAYS_ON_IDS) {
      expect(isPluginEnabled(id)).toBe(true);
      expect(pluginEnabled(id)).toBe(true);
    }
  });

  it('answers TRUE for an id nobody ever catalogued', () => {
    expect(isPluginEnabled('no-such-plugin')).toBe(true);
    expect(pluginEnabled('no-such-plugin')).toBe(true);
  });

  it('still answers FALSE for a catalog plugin that is off', () => {
    // Guards the other direction: a blanket `return true` would break the
    // toggles that ARE real.
    expect(isPluginEnabled('transcribe')).toBe(false);
    expect(pluginEnabled('ai-ask')).toBe(false);
    expect(pluginEnabled('live-preview')).toBe(false);
  });

  it('a stored false cannot switch an always-on surface off', () => {
    // The exact shape of the upgrade bug: settings.json still carries
    // archive-search:false from when it WAS a plugin. The surface must render.
    localStorage.setItem('vodrip.plugins', JSON.stringify({ 'archive-search': false, transcribe: false }));
    expect(isPluginEnabled('archive-search')).toBe(true);
    expect(isPluginEnabled('local-file')).toBe(true);
    expect(isPluginEnabled('transcribe')).toBe(false);
    // The store's map stays catalog-only — there is no toggle to hold a value.
    expect(enabledPlugins()['archive-search']).toBeUndefined();
  });

  it('usePlugin resolves the same rule inside a component', () => {
    render(<GateProbe id="archive-search" />);
    render(<GateProbe id="live-chat" />);
    render(<GateProbe id="transcribe" />);
    expect(screen.getByTestId('gate-archive-search').textContent).toBe('on');
    expect(screen.getByTestId('gate-live-chat').textContent).toBe('on');
    expect(screen.getByTestId('gate-transcribe').textContent).toBe('off');
  });
});

describe('orderPlugins', () => {
  const rows = [
    { id: 'ai-ask', recommended: false, disk_mb: 0, vram_mb: 0, enabled: false },
    { id: 'live-subtitles', recommended: false, disk_mb: 397, vram_mb: 2048, enabled: false },
    { id: 'live-preview', recommended: true, disk_mb: 0, vram_mb: 400, enabled: false },
    { id: 'transcribe', recommended: true, disk_mb: 397, vram_mb: 2048, enabled: false },
  ];

  it('orders enabled, then recommended, then cheapest, then id', () => {
    // RED ARM (item d): drop any of the three tiers and this order changes.
    expect(orderPlugins(rows).map((r) => r.id))
      .toEqual(['live-preview', 'transcribe', 'ai-ask', 'live-subtitles']);
    // enabled wins even over "recommended" and over a cheaper row.
    const withOn = rows.map((r) => (r.id === 'live-subtitles' ? { ...r, enabled: true } : r));
    expect(orderPlugins(withOn).map((r) => r.id))
      .toEqual(['live-subtitles', 'live-preview', 'transcribe', 'ai-ask']);
  });

  it('breaks a cost tie by id and does not mutate the input', () => {
    const tied = [
      { id: 'zeta', recommended: false, disk_mb: 10, vram_mb: 0, enabled: false },
      { id: 'alpha', recommended: false, disk_mb: 10, vram_mb: 0, enabled: false },
    ];
    const before = tied.map((r) => r.id);
    expect(orderPlugins(tied).map((r) => r.id)).toEqual(['alpha', 'zeta']);
    expect(tied.map((r) => r.id)).toEqual(before);
  });

  it('sorts the live catalog the way the backend get_manifest does', () => {
    const states = PLUGIN_IDS.map((id) => {
      const def = PLUGINS.find((p) => p.id === id)!;
      return { id, recommended: def.recommended, disk_mb: def.disk_mb, vram_mb: def.vram_mb, enabled: false };
    });
    expect(orderPlugins(states).map((s) => s.id))
      .toEqual(['live-preview', 'transcribe', 'ai-ask', 'live-subtitles']);
  });
});

describe('bundleCost', () => {
  it('costs nothing for an empty set or for always-on ids only', () => {
    expect(bundleCost([])).toEqual({ disk_mb: 0, vram_mb: 0, ram_mb: 0 });
    expect(bundleCost(ALWAYS_ON_IDS)).toEqual({ disk_mb: 0, vram_mb: 0, ram_mb: 0 });
  });

  it('charges the shared parakeet weights once when both plugins are on', () => {
    // RED ARM (item e): drop the shares_with branch and "both" reports 794 MB.
    expect(bundleCost(['transcribe'])).toEqual({ disk_mb: 397, vram_mb: 2048, ram_mb: 2560 });
    expect(bundleCost(['live-subtitles'])).toEqual({ disk_mb: 397, vram_mb: 2048, ram_mb: 1536 });
    const both = bundleCost(['transcribe', 'live-subtitles']);
    expect(both).toEqual({ disk_mb: 397, vram_mb: 2048, ram_mb: 4096 });
    expect(both.disk_mb).not.toBe(794);
    // Order must not change the total.
    expect(bundleCost(['live-subtitles', 'transcribe'])).toEqual(both);
  });

  it('adds the whole catalog up', () => {
    expect(bundleCost(PLUGIN_IDS)).toEqual({ disk_mb: 397, vram_mb: 2448, ram_mb: 4824 });
  });

  it('ignores unknown and duplicate ids', () => {
    expect(bundleCost(['transcribe', 'transcribe', 'no-such-plugin']))
      .toEqual({ disk_mb: 397, vram_mb: 2048, ram_mb: 2560 });
  });
});

describe('defaults and presets', () => {
  it('a fresh install follows defaultOn — every plugin ships off', () => {
    expect(Object.fromEntries(PLUGINS.map((p) => [p.id, p.defaultOn]))).toEqual({
      transcribe: false,
      'live-preview': false,
      'live-subtitles': false,
      'ai-ask': false,
    });
    for (const p of PLUGINS) expect(enabledPlugins()[p.id]).toBe(p.defaultOn);
  });

  it('"SO O ESSENCIAL" turns every plugin off', () => {
    const patch = coreOnlyPatch();
    expect(Object.keys(patch).sort()).toEqual(PLUGIN_IDS.slice().sort());
    expect(Object.values(patch).every((v) => v === false)).toBe(true);
  });

  it('"TUDO" turns on every plugin', () => {
    const patch = allOnPatch();
    expect(Object.keys(patch).sort()).toEqual(PLUGIN_IDS.slice().sort());
    expect(Object.values(patch).every(Boolean)).toBe(true);
  });
});

describe('pluginTotals', () => {
  it('falls back to the enabled selection before the backend answers', async () => {
    stubApi();
    expect(pluginTotals()).toEqual({ disk_mb: 0, vram_mb: 0, ram_mb: 0 });
    await act(async () => { await setPluginsBulk({ transcribe: true, 'live-subtitles': true }); });
    expect(pluginTotals()).toEqual({ disk_mb: 397, vram_mb: 2048, ram_mb: 4096 });
  });

  it('prefers the totals the backend sent, once synced', async () => {
    stubApi(() => ({
      plugins: [{
        id: 'transcribe', name: 'Transcrição', blurb: 'x', settingsKey: 'plugin_transcribe',
        disk_mb: 397, vram_mb: 2048, ram_mb: 2560, shares_with: '', recommended: true,
        defaultOn: false, enabled: true,
      }],
      totals: { disk_mb: 397, vram_mb: 2048, ram_mb: 9999 },
    }));
    await act(async () => { await syncPluginsFromBackend(); });
    expect(pluginTotals()).toEqual({ disk_mb: 397, vram_mb: 2048, ram_mb: 9999 });
  });
});

describe('persistence', () => {
  it('setPluginEnabled writes the boolean and calls POST /api/plugins', async () => {
    const fn = stubApi();
    await act(async () => { await setPluginEnabled('transcribe', true); });
    expect(enabledPlugins()['transcribe']).toBe(true);
    expect(posts(fn)).toEqual([{ id: 'transcribe', enabled: true }]);
    expect(JSON.parse(localStorage.getItem('vodrip.plugins') ?? '{}')['transcribe']).toBe(true);
  });

  it('an ALWAYS_ON id is not toggleable — no write, no POST', async () => {
    const fn = stubApi();
    // RED ARM (item c, write side): remove the catalog guard and this POSTs
    // {id:'archive-search',enabled:false}, which the backend rejects with 400.
    await act(async () => { await setPluginEnabled('archive-search', false); });
    expect(isPluginEnabled('archive-search')).toBe(true);
    expect(posts(fn)).toEqual([]);
    expect(localStorage.getItem('vodrip.plugins')).toBeNull();
    // bulk filters them too, and still posts the real plugin.
    await act(async () => {
      await setPluginsBulk({ 'archive-search': false, 'live-chat': false, transcribe: true });
    });
    expect(posts(fn)).toEqual([{ id: 'transcribe', enabled: true }]);
  });

  it('setPluginsBulk posts one request per plugin and lands locally', async () => {
    const fn = stubApi();
    await act(async () => { await setPluginsBulk({ 'ai-ask': true, 'live-preview': false }); });
    expect(enabledPlugins()['ai-ask']).toBe(true);
    expect(enabledPlugins()['live-preview']).toBe(false);
    expect(posts(fn)).toEqual([
      { id: 'ai-ask', enabled: true },
      { id: 'live-preview', enabled: false },
    ]);
  });

  it('keeps the legacy feature flag in sync for aliased plugins', async () => {
    stubApi();
    await act(async () => { await setPluginEnabled('transcribe', true); });
    const feats = JSON.parse(localStorage.getItem('vodrip.features') ?? '{}');
    expect(feats['transcribe-vod']).toBe(true);
  });
});

describe('syncPluginsFromBackend', () => {
  it('pulls the catalog, the alwaysOn list and the totals', async () => {
    stubApi((url) => {
      if (String(url) !== '/api/plugins') return {};
      return {
        plugins: [
          {
            id: 'transcribe', name: 'Transcrição', blurb: 'x', settingsKey: 'plugin_transcribe',
            disk_mb: 397, vram_mb: 2048, ram_mb: 2560, shares_with: '', recommended: true,
            defaultOn: false, enabled: false,
          },
          {
            id: 'live-subtitles', name: 'Legendas ao vivo', blurb: 'y', settingsKey: 'plugin_live_subtitles',
            disk_mb: 397, vram_mb: 2048, ram_mb: 1536, shares_with: 'transcribe', recommended: false,
            defaultOn: false, enabled: true,
          },
        ],
        alwaysOn: [{ id: 'archive-search', name: 'Busca no arquivo', blurb: 'z' }],
        totals: { disk_mb: 397, vram_mb: 2048, ram_mb: 1536 },
        enabled: ['live-subtitles'],
      };
    });
    await act(async () => { await syncPluginsFromBackend(); });
    expect(usePluginsList().map((p) => p.id)).toEqual(['transcribe', 'live-subtitles']);
    expect(isPluginEnabled('live-subtitles')).toBe(true);
    expect(isPluginEnabled('transcribe')).toBe(false);
    // the cost fields survive the round trip
    const sub = pluginCatalog().find((p) => p.id === 'live-subtitles');
    expect(sub?.disk_mb).toBe(397);
    expect(sub?.vram_mb).toBe(2048);
    expect(sub?.ram_mb).toBe(1536);
    expect(sub?.shares_with).toBe('transcribe');
    // RED ARM (item f): drop the alwaysOn branch and this falls back to the
    // 8-item mirror; drop the totals branch and it falls back to 4824/…
    expect(alwaysOnFeatures().map((a) => a.id)).toEqual(['archive-search']);
    expect(pluginTotals()).toEqual({ disk_mb: 397, vram_mb: 2048, ram_mb: 1536 });
  });

  it('keeps the mirrors when the backend omits alwaysOn/totals', async () => {
    stubApi(() => ({
      plugins: [{
        id: 'transcribe', name: 'Transcrição', blurb: 'x', settingsKey: 'plugin_transcribe',
        disk_mb: 397, vram_mb: 2048, ram_mb: 2560, shares_with: '', recommended: true,
        defaultOn: false, enabled: true,
      }],
    }));
    await act(async () => { await syncPluginsFromBackend(); });
    expect(alwaysOnFeatures().map((a) => a.id)).toEqual(ALWAYS_ON_IDS);
    // derived, not from a backend totals payload
    expect(pluginTotals()).toEqual({ disk_mb: 397, vram_mb: 2048, ram_mb: 2560 });
  });
});

describe('nav gating', () => {
  it('drops a nav entry whose plugin is off', () => {
    const all = ['url', 'channels', 'queue', 'settings'] as const;
    expect(tabsForPlugins([...all], () => true)).toEqual(['url', 'channels', 'queue', 'settings']);
    // RED ARM: make tabsForPlugins() return the input unfiltered and this fails.
    expect(tabsForPlugins([...all], (id) => id !== 'channels')).toEqual(['url', 'queue', 'settings']);
  });

  it('only the channels tab is plugin-gated, and its plugin is always on', () => {
    expect(PLUGIN_TABS).toEqual({ channels: 'channels' });
    expect(tabsForPlugins(['channels'] as const)).toEqual(['channels']);
  });
});

describe('<PluginGate>', () => {
  it('renders children only while the plugin is on', async () => {
    stubApi();
    await act(async () => { await setPluginEnabled('transcribe', true); });
    const { unmount } = render(
      <PluginGate id="transcribe"><span>presente</span></PluginGate>,
    );
    // RED ARM: make PluginGate always render children and the second half fails.
    expect(screen.getByText('presente')).toBeInTheDocument();
    unmount();

    await act(async () => { await setPluginEnabled('transcribe', false); });
    render(
      <PluginGate id="transcribe"><span>presente</span></PluginGate>,
    );
    expect(screen.queryByText('presente')).not.toBeInTheDocument();
  });

  it('renders an always-on surface even with nothing stored', () => {
    // RED ARM (item c): the same case without the guard makes the whole app
    // disappear behind the eight gates that now name non-plugins.
    render(<PluginGate id="archive-search"><span>sempre</span></PluginGate>);
    expect(screen.getByText('sempre')).toBeInTheDocument();
  });
});

describe('counter', () => {
  it('counts only what is on', async () => {
    stubApi();
    await act(async () => { await setPluginsBulk(coreOnlyPatch()); });
    expect(countEnabledPlugins()).toBe(0);
    await act(async () => { await setPluginsBulk(allOnPatch()); });
    expect(countEnabledPlugins()).toBe(4);
  });
});

/** App wiring — App.tsx cannot be rendered in jsdom (the modal test above
 *  already says why), so the gate is asserted on the source, the same way
 *  ModalZLaw.test.tsx pins the z-index law. Reverting the App.tsx edit fails
 *  these. */
describe('App wiring', () => {
  const app = readFileSync(join(__dirname, '..', 'App.tsx'), 'utf8');

  it('the main nav filters its tabs through tabsForPlugins', () => {
    expect(app).toMatch(/const visibleTabs[^=]*=\s*useMemo\(\s*\(\)\s*=>\s*tabsForPlugins<Tab>\(/);
  });

  it('the archive-search entry point is gated by its plugin', () => {
    expect(app).toMatch(/archiveSearchPluginOn && \(/);
    expect(app).toContain("usePlugin('archive-search')");
  });

  it('the backend catalog is pulled in on mount', () => {
    expect(app).toContain('void syncPluginsFromBackend()');
  });
});

/**
 * Plugin contract tests — src/lib/plugins.tsx (the frontend half).
 *
 * RED ARM for each: revert the behaviour (delete the gate, drop the preset,
 * stop the POST) and the matching case below fails. See the comments marking
 * each one.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { act, render, screen } from '@testing-library/react';
import {
  PLUGINS,
  PLUGIN_TABS,
  allOnPatch,
  coreOnlyPatch,
  enabledPlugins,
  isPluginEnabled,
  setPluginEnabled,
  PluginGate,
  setPluginsBulk,
  syncPluginsFromBackend,
  tabsForPlugins,
  usePlugins,
  countEnabledPlugins,
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

describe('plugin catalog', () => {
  it('carries the contract fields for every plugin', () => {
    for (const p of PLUGINS) {
      expect(p.id).toBeTruthy();
      expect(p.name).toBeTruthy();
      expect(p.blurb).toBeTruthy();
      expect(['leve', 'pesado']).toContain(p.cost);
      expect(typeof p.defaultOn).toBe('boolean');
      // Contract example: "plugin_channels". Ids keep their dashes;
      // the settings key is the id with dashes as underscores.
      expect(p.settingsKey).toBe(`plugin_${p.id.replace(/-/g, '_')}`);
    }
  });

  it('marks download-by-link and channels as core, always on', () => {
    const core = PLUGINS.filter((p) => p.core).map((p) => p.id).sort();
    expect(core).toEqual(['channels', 'link-download']);
    // RED ARM: drop the `if (def?.core) return true` short-circuit in
    // isPluginEnabled and this fails.
    expect(isPluginEnabled('link-download')).toBe(true);
    expect(isPluginEnabled('channels')).toBe(true);
  });

  it('a heavy plugin is the one the contract calls pesado', () => {
    expect(PLUGINS.filter((p) => p.cost === 'pesado').map((p) => p.id).sort())
      .toEqual(['ai-ask', 'live-preview', 'live-subtitles', 'transcribe']);
  });
});

describe('defaults', () => {
  it('a fresh install follows defaultOn, and nothing else', () => {
    // PINNED, not read back from the catalog: an earlier version of this
    // test compared the catalog against itself, so a catalog written with
    // every defaultOn flipped to false still passed and shipped a fresh
    // install with archive search, clip editor, local file and live chat
    // all switched off. The values below are the decision, not a description.
    expect(Object.fromEntries(PLUGINS.map((p) => [p.id, p.defaultOn]))).toEqual({
      'link-download': true,
      channels: true,
      transcribe: false,
      'live-preview': false,
      'ai-ask': false,
      // 'pesado', like the other heavy ones — measured on the live settings.json
      // (2026-10-14): the owner runs live-captions:false, and the wizard copy
      // itself promises the heavy ones start off.
      'live-subtitles': false,
      'live-chat': true,
      'clip-editor': true,
      'twitch-clip': true,
      'archive-search': true,
      'channel-explore': true,
      'local-file': true,
    });
    for (const p of PLUGINS) expect(enabledPlugins()[p.id]).toBe(p.defaultOn);
  });

  it('"SO O ESSENCIAL" turns on ONLY the core plugins', () => {
    const patch = coreOnlyPatch();
    const on = Object.entries(patch).filter(([, v]) => v).map(([k]) => k).sort();
    expect(on).toEqual(['channels', 'link-download']);
    // RED ARM: make coreOnlyPatch() default everything on and this fails.
  });

  it('"TUDO" turns on every plugin', () => {
    const patch = allOnPatch();
    expect(Object.keys(patch).sort()).toEqual(PLUGINS.map((p) => p.id).sort());
    expect(Object.values(patch).every(Boolean)).toBe(true);
  });
});

describe('persistence', () => {
  it('setPluginEnabled writes the boolean and calls POST /api/plugins', async () => {
    const fn = stubApi();
    await act(async () => { await setPluginEnabled('archive-search', false); });
    expect(enabledPlugins()['archive-search']).toBe(false);
    // RED ARM: remove pushToBackend() from setPluginEnabled and this fails.
    expect(posts(fn)).toEqual([{ id: 'archive-search', enabled: false }]);
    // And it survives a fresh read from localStorage (the settings.json cache).
    expect(JSON.parse(localStorage.getItem('vodrip.plugins') ?? '{}')['archive-search']).toBe(false);
  });

  it('a core plugin cannot be switched off', async () => {
    const fn = stubApi();
    await act(async () => { await setPluginEnabled('channels', false); });
    expect(isPluginEnabled('channels')).toBe(true);
    expect(posts(fn)).toEqual([]);
  });

  it('setPluginsBulk posts one request per plugin and lands locally', async () => {
    const fn = stubApi();
    await act(async () => { await setPluginsBulk({ 'ai-ask': true, 'live-chat': false }); });
    expect(enabledPlugins()['ai-ask']).toBe(true);
    expect(enabledPlugins()['live-chat']).toBe(false);
    expect(posts(fn)).toEqual([
      { id: 'ai-ask', enabled: true },
      { id: 'live-chat', enabled: false },
    ]);
  });

  it('keeps the legacy feature flag in sync for aliased plugins', async () => {
    stubApi();
    await act(async () => { await setPluginEnabled('transcribe', true); });
    const feats = JSON.parse(localStorage.getItem('vodrip.features') ?? '{}');
    expect(feats['transcribe-vod']).toBe(true);
  });

  it('syncs the catalog and the flags from GET /api/plugins', async () => {
    stubApi((url) => {
      if (String(url) !== '/api/plugins') return {};
      return {
        plugins: [
          { id: 'link-download', name: 'Baixar por link', blurb: 'x', cost: 'leve', core: true, defaultOn: true, settingsKey: 'plugin_link_download', enabled: true },
          { id: 'from-backend', name: 'Só do backend', blurb: 'y', cost: 'leve', defaultOn: false, settingsKey: 'plugin_from_backend', enabled: true },
        ],
        enabled: ['link-download', 'from-backend'],
      };
    });
    await act(async () => { await syncPluginsFromBackend(); });
    const list = usePluginsList();
    expect(list.map((p) => p.id)).toEqual(['link-download', 'from-backend']);
    expect(isPluginEnabled('from-backend')).toBe(true);
  });
});

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

describe('nav gating', () => {
  it('drops a nav entry whose plugin is off', () => {
    const all = ['url', 'channels', 'queue', 'settings'] as const;
    expect(tabsForPlugins([...all], () => true)).toEqual(['url', 'channels', 'queue', 'settings']);
    // RED ARM: make tabsForPlugins() return the input unfiltered and this fails.
    expect(tabsForPlugins([...all], (id) => id !== 'channels')).toEqual(['url', 'queue', 'settings']);
  });

  it('only the channels tab is plugin-gated', () => {
    expect(PLUGIN_TABS).toEqual({ channels: 'channels' });
  });
});

describe('<PluginGate>', () => {
  it('renders children only while the plugin is on', async () => {
    stubApi();
    await act(async () => { await setPluginsBulk({ 'archive-search': true }); });
    const { unmount } = render(
      <PluginGate id="archive-search"><span>presente</span></PluginGate>,
    );
    // RED ARM: make PluginGate always render children and the second half fails.
    expect(screen.getByText('presente')).toBeInTheDocument();
    unmount();

    await act(async () => { await setPluginEnabled('archive-search', false); });
    render(
      <PluginGate id="archive-search"><span>presente</span></PluginGate>,
    );
    expect(screen.queryByText('presente')).not.toBeInTheDocument();
  });
});

describe('counter', () => {
  it('counts only what is on', async () => {
    stubApi();
    await act(async () => { await setPluginsBulk(coreOnlyPatch()); });
    expect(countEnabledPlugins()).toBe(2);
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

});

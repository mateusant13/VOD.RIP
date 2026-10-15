/**
 * Plugin system — the FRONTEND half of the plugin contract.
 *
 * Single source of truth for WHAT a plugin is: `backend/services/plugin_registry.py`
 * (PLUGINS / ALWAYS_ON). This module mirrors that list so the UI renders before
 * the API answers (and so tests run without a backend), and GET /api/plugins
 * overlays the backend catalog, the per-plugin enabled flags, the ALWAYS_ON
 * list and the bundle totals on top of it. A 404 (route not deployed yet)
 * leaves the mirror in charge instead of breaking the app — the mirror and the
 * registry carry the same ids.
 *
 * A PLUGIN IS SOMETHING HEAVY. Only the four entries that cost disk, VRAM or
 * real RAM are plugins; the eight light surfaces live in ALWAYS_ON and are part
 * of the app. The rule that keeps the existing gates correct after those eight
 * stopped being plugins is the NUCLEUS RULE below: `pluginEnabled(id)` /
 * `usePlugin(id)` answer TRUE for any id that is NOT in the catalog, so a
 * surface wrapped in a now-meaningless id still renders instead of vanishing.
 *
 * Persistence: one boolean per plugin in settings.json, key `plugin_<id>`
 * (see settingsKey). Writes go through POST /api/plugins {id, enabled}; the
 * localStorage copy is the optimistic cache that keeps the UI instant and lets
 * it render before the first fetch resolves. An ALWAYS_ON id is never written:
 * it has no toggle, and the backend rejects it with 400.
 *
 * The three plugins that already existed as *features* (featureManifest.ts)
 * keep one effective truth through PLUGIN_FEATURE_ALIAS: with nothing stored
 * for them yet, the feature flag decides, and a toggle writes BOTH the plugin
 * key and the legacy feature key (so the backend feature registry stays in sync
 * instead of silently disagreeing with the plugin UI). 'live-chat' used to be
 * aliased here; it is ALWAYS_ON now, so its alias was dropped.
 */
import { useSyncExternalStore, type ReactNode } from 'react';
import { setFeature as setFeatureFlag } from './featureFlags';

export type PluginId = string;

/** Legacy label kept for the surfaces that still render a badge from it.
 *  Every entry that remains a plugin is heavy by definition, so the catalog
 *  only ever carries 'pesado' now. */
export type PluginCost = 'leve' | 'pesado';

/** One plugin row: the backend's PluginDef, 1:1
 *  (backend/services/plugin_registry.py::PluginDef). */
export interface PluginDef {
  id: PluginId;
  /** pt-BR display name. */
  name: string;
  /** One pt-BR line: what it does. */
  blurb: string;
  /** Boolean key in settings.json. */
  settingsKey: string;
  /** MB this plugin downloads on its own (0 = nothing to download). */
  disk_mb: number;
  /** MB of VRAM at peak while it runs. */
  vram_mb: number;
  /** MB of system RAM at peak while it runs. */
  ram_mb: number;
  /** Another plugin whose model this one reuses — never charge it twice. */
  shares_with: string;
  /** Recommended in the picker; ordered before the rest. */
  recommended: boolean;
  /** On by default on a fresh install. */
  defaultOn: boolean;
  /** Always 'pesado' for a catalog entry. Required so the existing badges in
   *  SettingsTab/FirstRunWizard still compile and still tell the truth. */
  cost: PluginCost;
  /** Legacy: no catalog entry is core any more — the always-on surfaces are
   *  ALWAYS_ON below. Kept optional so the first-run row still compiles until
   *  it is migrated to render ALWAYS_ON. */
  core?: boolean;
}

export interface PluginState extends PluginDef {
  enabled: boolean;
}

/** A surface that is part of the app: no toggle, because it is not heavy.
 *  Mirrors backend/services/plugin_registry.py::ALWAYS_ON (same order). */
export interface AlwaysOnDef {
  id: PluginId;
  name: string;
  blurb: string;
}

export interface CostTotals {
  disk_mb: number;
  vram_mb: number;
  ram_mb: number;
}

/** Mirrored catalog — used only while GET /api/plugins cannot answer.
 *
 *  Every cost below is copied from the backend registry, which in turn records
 *  how each number was measured (see the comment on each PluginDef there).
 *  Where each one shows up in the UI (measured on the tree, not invented):
 *   transcribe     SettingsTab.tsx:409 TranscriptionSection (feature 'transcribe-vod')
 *   live-preview   App.tsx:378 LiveBadge (feature 'live-preview')
 *   live-subtitles LivePlayerPopup captions overlay (feature 'live-captions')
 *   ai-ask         SettingsTab.tsx:452 Experimental AI · ChannelExplorePopup.tsx:413
 */
export const PLUGINS: PluginDef[] = [
  { id: 'transcribe', name: 'Transcrição', blurb: 'Escreve o que foi dito nos VODs arquivados para você pesquisar depois.', settingsKey: 'plugin_transcribe', disk_mb: 397, vram_mb: 2048, ram_mb: 2560, shares_with: '', recommended: true, defaultOn: false, cost: 'pesado' },
  { id: 'live-preview', name: 'Prévia ao vivo', blurb: 'Vê de relance quem está ao vivo e abre a prévia na hora.', settingsKey: 'plugin_live_preview', disk_mb: 0, vram_mb: 400, ram_mb: 600, shares_with: '', recommended: true, defaultOn: false, cost: 'pesado' },
  { id: 'live-subtitles', name: 'Legendas ao vivo', blurb: 'Mostra legendas enquanto a transmissão está acontecendo.', settingsKey: 'plugin_live_subtitles', disk_mb: 397, vram_mb: 2048, ram_mb: 1536, shares_with: 'transcribe', recommended: false, defaultOn: false, cost: 'pesado' },
  { id: 'ai-ask', name: 'Perguntar à IA', blurb: 'Perguntas respondidas só com o arquivo local, usando sua chave de API.', settingsKey: 'plugin_ai_ask', disk_mb: 0, vram_mb: 0, ram_mb: 128, shares_with: '', recommended: false, defaultOn: false, cost: 'pesado' },
];

/** Features that are NOT plugins because none of them is heavy: no model
 *  download, no GPU slot, no meaningful RAM. Copied verbatim (ids, names,
 *  blurbs and order) from backend/services/plugin_registry.py::ALWAYS_ON. */
export const ALWAYS_ON: AlwaysOnDef[] = [
  { id: 'link-download', name: 'Baixar por link', blurb: 'Cole um link de VOD ou clipe e baixe em vídeo, áudio ou trecho.' },
  { id: 'channels', name: 'Canais', blurb: 'Acompanhe canais salvos, veja o que saiu novo e arquive VODs.' },
  { id: 'channel-explore', name: 'Explorar canais', blurb: 'Abre o painel de um canal com VODs, clipes e ao-vivo.' },
  { id: 'clip-editor', name: 'Editor de clipes', blurb: 'Corta um trecho do VOD e baixa só ele.' },
  { id: 'twitch-clip', name: 'Clipes do Twitch', blurb: 'Cria um clipe do Twitch sem sair do app.' },
  { id: 'archive-search', name: 'Busca no arquivo', blurb: 'Pesquisa transcrições e chat dos VODs já arquivados.' },
  { id: 'local-file', name: 'Arquivo local', blurb: 'Assiste um arquivo de vídeo que já está no seu disco.' },
  { id: 'live-chat', name: 'Chat ao vivo', blurb: 'Mostra o chat ao lado do vídeo e salva junto com o download.' },
];

export const PLUGIN_IDS: PluginId[] = PLUGINS.map((p) => p.id);
export const ALWAYS_ON_IDS: PluginId[] = ALWAYS_ON.map((a) => a.id);

/** Plugins that were features first: the feature flag stays the truth until a
 *  plugin toggle writes a value of its own. */
export const PLUGIN_FEATURE_ALIAS: Record<PluginId, string> = {
  'transcribe': 'transcribe-vod',
  'live-subtitles': 'live-captions',
  'live-preview': 'live-preview',
};

/** Main-nav entries owned by a plugin. A disabled plugin removes its entry
 *  from the tab bar (the URL/history/settings tabs have no plugin — core).
 *  'channels' is ALWAYS_ON, so this entry now resolves TRUE always: the tab is
 *  pinned, exactly like the app's other core surfaces. */
export const PLUGIN_TABS: Record<string, PluginId> = { channels: 'channels' };

const LS_STATE = 'vodrip.plugins';
const LS_CATALOG = 'vodrip.plugins.catalog';
const LS_ALWAYS_ON = 'vodrip.plugins.alwaysOn';
const LS_TOTALS = 'vodrip.plugins.totals';

// ── store ────────────────────────────────────────────────────────────────────
// Cached snapshots: useSyncExternalStore compares by identity, so every read
// must return the SAME object until something actually changes.

let cachedMap: Record<string, boolean> | null = null;
let cachedMapKey: string | null = null;
let cachedList: PluginState[] | null = null;
let cachedListKey: string | null = null;
const subs = new Set<() => void>();

function subscribe(cb: () => void): () => void {
  subs.add(cb);
  return () => { subs.delete(cb); };
}

/** Snapshot identity: the store is keyed by the STORED state + catalog, so
 *  a write from another source (another tab, a test clearing localStorage) is
 *  picked up on the next read while unchanged reads keep the same object
 *  identity — which useSyncExternalStore requires. */
function cacheKey(): string {
  return JSON.stringify(storedState()) + '|' + JSON.stringify(pluginCatalog());
}

function invalidate(): void {
  cachedMap = null;
  cachedMapKey = null;
  cachedList = null;
  cachedListKey = null;
  for (const cb of subs) cb();
}

function readLS(key: string): unknown {
  try { return JSON.parse(localStorage.getItem(key) ?? 'null'); } catch { return null; }
}

function writeLS(key: string, value: unknown): void {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* quota / private mode */ }
}

function num(v: unknown, fallback = 0): number {
  return typeof v === 'number' && Number.isFinite(v) ? v : fallback;
}

function asTotals(v: unknown): CostTotals | null {
  if (!v || typeof v !== 'object') return null;
  const o = v as Record<string, unknown>;
  if (typeof o.disk_mb !== 'number' || typeof o.vram_mb !== 'number' || typeof o.ram_mb !== 'number') return null;
  return { disk_mb: o.disk_mb, vram_mb: o.vram_mb, ram_mb: o.ram_mb };
}

/** Definitions: the backend's catalog once synced, the mirror before that. */
export function pluginCatalog(): PluginDef[] {
  const stored = readLS(LS_CATALOG);
  if (Array.isArray(stored) && stored.length > 0) return stored as PluginDef[];
  return PLUGINS;
}

/** The always-on list: the backend's once synced, the mirror before that. */
export function alwaysOnFeatures(): AlwaysOnDef[] {
  const stored = readLS(LS_ALWAYS_ON);
  if (Array.isArray(stored) && stored.length > 0) return stored as AlwaysOnDef[];
  return ALWAYS_ON;
}

function catalogDef(id: PluginId): PluginDef | undefined {
  return pluginCatalog().find((p) => p.id === id);
}

function storedState(): Record<string, boolean> {
  const v = readLS(LS_STATE);
  if (v && typeof v === 'object') return v as Record<string, boolean>;
  return {};
}

export function enabledPlugins(): Record<string, boolean> {
  const key = cacheKey();
  if (cachedMap && cachedMapKey === key) return cachedMap;
  const stored = storedState();
  const out: Record<string, boolean> = {};
  for (const p of pluginCatalog()) {
    if (p.id in stored) { out[p.id] = !!stored[p.id]; continue; }
    // READ side resolves defaultOn only — the backend registry is the single
    // source of truth (same ids/defaults, enforced by
    // backend/tests/test_plugin_registry.py::test_registry_matches_frontend_mirror).
    // The legacy feature alias is WRITE-side only (setPluginEnabled keeps the
    // feature flag in step); consulting it here made the mirror answer a
    // different value than the registry for 'live-subtitles'.
    out[p.id] = p.defaultOn;
  }
  cachedMap = out;
  cachedMapKey = key;
  return out;
}

/** NUCLEUS RULE — is this feature usable?
 *
 *  TRUE for anything that is not in the plugin catalog. The eight ALWAYS_ON
 *  features (and every id a caller happens to invent) resolve here, which is
 *  what keeps the existing gates correct after they stopped being plugins: a
 *  surface wrapped in a now-meaningless id still renders instead of vanishing.
 *  Only a real catalog row can be OFF. */
export function pluginEnabled(id: PluginId): boolean {
  if (!catalogDef(id)) return true;
  return !!enabledPlugins()[id];
}

/** Historical name for pluginEnabled; kept for the existing call sites. */
export const isPluginEnabled = pluginEnabled;

export function pluginStates(): PluginState[] {
  const key = cacheKey();
  if (cachedList && cachedListKey === key) return cachedList;
  const enabled = enabledPlugins();
  cachedList = pluginCatalog().map((p) => ({ ...p, enabled: !!enabled[p.id] }));
  cachedListKey = key;
  return cachedList;
}

export function countEnabledPlugins(): number {
  return Object.values(enabledPlugins()).filter(Boolean).length;
}

/** Display order for a list of plugin rows: enabled first, then recommended,
 *  then cheapest (disk + VRAM), then id so the order is stable across runs.
 *  Mirrors backend/services/plugin_registry.py::get_manifest. Returns a new
 *  array; the input is not reordered in place. */
export function orderPlugins<T extends { id: string; enabled?: boolean; recommended: boolean; disk_mb: number; vram_mb: number }>(
  list: readonly T[],
): T[] {
  return [...list].sort((a, b) => {
    if (!!a.enabled !== !!b.enabled) return a.enabled ? -1 : 1;
    if (a.recommended !== b.recommended) return a.recommended ? -1 : 1;
    const ca = a.disk_mb + a.vram_mb;
    const cb = b.disk_mb + b.vram_mb;
    if (ca !== cb) return ca - cb;
    return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
  });
}

/** Total cost of a SET of plugin ids, honouring shares_with.
 *
 *  Transcribe and live-subtitles run the SAME 397 MB of parakeet weights, so a
 *  user with both switched on must see one download, not two. RAM is per
 *  running plugin and is always summed; disk/VRAM are charged once per model —
 *  if the plugin named by shares_with is also selected, the shared weights are
 *  charged to it and this row adds none. Ids outside the catalog cost nothing
 *  (an ALWAYS_ON feature downloads no model). Order-independent, unlike the
 *  backend's cost_bundle, which relies on PLUGINS order. */
export function bundleCost(ids: Iterable<PluginId>): CostTotals {
  const byId = new Map(pluginCatalog().map((p) => [p.id, p]));
  const want: PluginDef[] = [];
  const seen = new Set<PluginId>();
  for (const id of ids) {
    const p = byId.get(id);
    if (!p || seen.has(id)) continue;
    seen.add(id);
    want.push(p);
  }
  const selected = new Set(want.map((p) => p.id));
  const out: CostTotals = { disk_mb: 0, vram_mb: 0, ram_mb: 0 };
  for (const p of want) {
    out.ram_mb += p.ram_mb;
    if (p.shares_with && selected.has(p.shares_with)) continue;
    out.disk_mb += p.disk_mb;
    out.vram_mb += p.vram_mb;
  }
  return out;
}

/** What the selection costs, per the backend's totals when it answered, and
 *  derived from the enabled catalog rows otherwise. */
export function pluginTotals(): CostTotals {
  const stored = asTotals(readLS(LS_TOTALS));
  if (stored) return stored;
  return bundleCost(Object.entries(enabledPlugins()).filter(([, on]) => on).map(([id]) => id));
}

/** Apply locally first (instant UI), then push to the backend. */
function applyLocal(patch: Record<string, boolean>): void {
  const next = { ...storedState() };
  for (const [id, on] of Object.entries(patch)) next[id] = !!on;
  writeLS(LS_STATE, next);
  invalidate();
}

function pushToBackend(id: PluginId, enabled: boolean): Promise<void> {
  // Fire-and-forget, exactly like the feature flags it replaces: the local
  // cache is the UI's truth and a failed POST only desyncs settings.json.
  try {
    return fetch('/api/plugins', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ id, enabled }),
    }).then(() => undefined, () => undefined);
  } catch {
    return Promise.resolve();
  }
}

export async function setPluginEnabled(id: PluginId, enabled: boolean): Promise<void> {
  // Not in the catalog = ALWAYS_ON: there is no toggle, so nothing is written
  // anywhere — not locally, not to settings.json, and not a request the
  // backend would reject with 400.
  if (!catalogDef(id)) return;
  applyLocal({ [id]: enabled });
  const alias = PLUGIN_FEATURE_ALIAS[id];
  if (alias) setFeatureFlag(alias, enabled);
  await pushToBackend(id, enabled);
}

export async function setPluginsBulk(patch: Record<string, boolean>): Promise<void> {
  const wanted = Object.fromEntries(Object.entries(patch).filter(([id]) => !!catalogDef(id)));
  applyLocal(wanted);
  for (const [id, on] of Object.entries(wanted)) {
    const alias = PLUGIN_FEATURE_ALIAS[id];
    if (alias) setFeatureFlag(alias, on);
    await pushToBackend(id, on);
  }
}

/** Every plugin off — the app itself (ALWAYS_ON) and nothing extra. The
 *  "SO O ESSENCIAL" preset; no catalog entry ships on by default. */
export function coreOnlyPatch(): Record<string, boolean> {
  const patch: Record<string, boolean> = {};
  for (const p of PLUGINS) patch[p.id] = false;
  return patch;
}

/** Every plugin on, heavy ones included. The "TUDO" preset. */
export function allOnPatch(): Record<string, boolean> {
  const patch: Record<string, boolean> = {};
  for (const p of PLUGINS) patch[p.id] = true;
  return patch;
}

type PluginPayload = {
  id?: string; name?: string; blurb?: string; settingsKey?: string;
  disk_mb?: number; vram_mb?: number; ram_mb?: number;
  shares_with?: string; recommended?: boolean; defaultOn?: boolean;
  cost?: PluginCost; core?: boolean; enabled?: boolean;
};
interface PluginsResponse {
  plugins?: PluginPayload[];
  alwaysOn?: { id?: string; name?: string; blurb?: string }[];
  totals?: { disk_mb?: number; vram_mb?: number; ram_mb?: number };
  enabled?: string[];
}

/** Pull backend truth into the local cache (call once on App mount).
 *  Reads the catalog, the always-on list and the bundle totals. */
export async function syncPluginsFromBackend(): Promise<void> {
  try {
    const r = await fetch('/api/plugins');
    if (!r.ok) return; // 404 while the route is not deployed: the mirror keeps answering
    const j = (await r.json()) as PluginsResponse;
    const rows = Array.isArray(j.plugins)
      ? j.plugins.filter((p) => typeof p.id === 'string' && p.id.length > 0)
      : [];
    if (rows.length > 0) {
      writeLS(LS_CATALOG, rows.map((p) => ({
        id: p.id,
        name: p.name ?? p.id,
        blurb: p.blurb ?? '',
        settingsKey: p.settingsKey ?? ('plugin_' + String(p.id).replace(/-/g, '_')),
        disk_mb: num(p.disk_mb),
        vram_mb: num(p.vram_mb),
        ram_mb: num(p.ram_mb),
        shares_with: typeof p.shares_with === 'string' ? p.shares_with : '',
        recommended: !!p.recommended,
        defaultOn: !!p.defaultOn,
        cost: p.cost === 'leve' ? 'leve' : 'pesado',
      })));
      const patch: Record<string, boolean> = {};
      for (const p of rows) patch[String(p.id)] = !!p.enabled;
      applyLocal(patch);
    } else if (Array.isArray(j.enabled)) {
      applyLocal(Object.fromEntries(j.enabled.map((id) => [id, true])));
    }
    if (Array.isArray(j.alwaysOn) && j.alwaysOn.length > 0) {
      const always = j.alwaysOn
        .filter((a) => !!a && typeof a.id === 'string' && a.id.length > 0)
        .map((a) => ({ id: a.id as string, name: a.name ?? (a.id as string), blurb: a.blurb ?? '' }));
      if (always.length > 0) writeLS(LS_ALWAYS_ON, always);
    }
    const totals = asTotals(j.totals);
    if (totals) writeLS(LS_TOTALS, totals);
  } catch { /* offline: the mirror keeps answering */ }
}

// ── hooks ────────────────────────────────────────────────────────────────────

export function usePlugins(): PluginState[] {
  return useSyncExternalStore(subscribe, pluginStates, () => [] as PluginState[]);
}

export function usePlugin(id: PluginId): boolean {
  return useSyncExternalStore(
    subscribe,
    () => pluginEnabled(id),
    () => pluginEnabled(id),
  );
}

/** Renders its children only while the plugin is enabled. An ALWAYS_ON id is
 *  always enabled, so its children always render. */
export function PluginGate({ id, children }: { id: PluginId; children: ReactNode }) {
  const on = usePlugin(id);
  return on ? <>{children}</> : null;
}

/** Filter a nav list down to the entries whose plugin (if any) is on. */
export function tabsForPlugins<T extends string>(
  tabs: readonly T[],
  enabled: (id: PluginId) => boolean = pluginEnabled,
): T[] {
  return tabs.filter((tab) => {
    const pluginId = PLUGIN_TABS[tab];
    return pluginId ? enabled(pluginId) : true;
  });
}

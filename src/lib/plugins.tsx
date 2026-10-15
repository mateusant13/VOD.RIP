/**
 * Plugin system — the FRONTEND half of the plugin contract.
 *
 * Single source of truth for WHAT a plugin is: `backend/services/plugin_registry.py`
 * (`PLUGINS: PluginDef[]`). This module mirrors that list so the UI renders before
 * the API answers (and so tests run without a backend), and `GET /api/plugins`
 * overlays the backend catalog + the per-plugin enabled flags on top of it. A
 * 404 (route not deployed yet) leaves the mirror in charge instead of breaking
 * the app — the mirror and the registry carry the same ids.
 *
 * Persistence: one boolean per plugin in settings.json, key `plugin_<id>`
 * (see `settingsKey`). Writes go through `POST /api/plugins {id, enabled}`;
 * the localStorage copy is the optimistic cache that keeps the UI instant and
 * lets it render before the first fetch resolves.
 *
 * The four plugins that already existed as *features* (featureManifest.ts) keep
 * one effective truth through PLUGIN_FEATURE_ALIAS: with nothing stored for
 * them yet, the feature flag decides, and a toggle writes BOTH the plugin key
 * and the legacy feature key (so the backend feature registry stays in sync
 * instead of silently disagreeing with the plugin UI).
 */
import { useSyncExternalStore, type ReactNode } from 'react';
import { setFeature as setFeatureFlag } from './featureFlags';

export type PluginId = string;
export type PluginCost = 'leve' | 'pesado';

export interface PluginDef {
  id: PluginId;
  /** pt-BR display name. */
  name: string;
  /** One pt-BR line: what it does. */
  blurb: string;
  /** 'pesado' = costs more energy (transcribe, live subtitles, live preview). */
  cost: PluginCost;
  /** Always on, cannot be turned off (download by link + channels). */
  core?: boolean;
  /** On by default on a fresh install. */
  defaultOn: boolean;
  /** Boolean key in settings.json. */
  settingsKey: string;
}

export interface PluginState extends PluginDef {
  enabled: boolean;
}

/** Mirrored catalog — used only while GET /api/plugins cannot answer.
 *
 *  Where each one shows up in the UI (measured on the tree, not invented):
 *   link-download  App.tsx:7422 "urlTabContent" (paste link → extract → download)
 *   channels       App.tsx:6335 visibleTabs + 6485/7446 ChannelLinkCard
 *   transcribe     SettingsTab.tsx:409 TranscriptionSection (feature 'transcribe-vod')
 *   live-subtitles LivePlayerPopup captions overlay (feature 'live-captions')
 *   live-preview   App.tsx:378 LiveBadge (feature 'live-preview')
 *   live-chat      PreviewChatPanel / LiveChatPanel (feature 'chat-live')
 *   clip-editor    ChannelExplorePopup.tsx:1795 PreviewMiniEditor
 *   twitch-clip    App.tsx:8152 TwitchClipPopup
 *   archive-search App.tsx:7346 "SEARCH CHAT" → App.tsx:8180 ArchiveSearchPopup
 *   channel-explore App.tsx:8070 ChannelExplorePopup
 *   local-file     App.tsx:8095 LocalFilePopup (open a downloaded file)
 *   ai-ask         SettingsTab.tsx:452 Experimental AI · ChannelExplorePopup.tsx:2119
 */
export const PLUGINS: PluginDef[] = [
  { id: 'link-download', name: 'Baixar por link', blurb: 'Cole um link de VOD ou clipe e baixe em vídeo, áudio ou trecho.', cost: 'leve', core: true, defaultOn: true, settingsKey: 'plugin_link_download' },
  { id: 'channels', name: 'Canais', blurb: 'Acompanhe canais salvos, veja o que saiu novo e arquive VODs.', cost: 'leve', core: true, defaultOn: true, settingsKey: 'plugin_channels' },
  { id: 'transcribe', name: 'Transcrição', blurb: 'Escreve o que foi dito nos VODs arquivados para você pesquisar depois.', cost: 'pesado', defaultOn: false, settingsKey: 'plugin_transcribe' },
  { id: 'live-subtitles', name: 'Legendas ao vivo', blurb: 'Mostra legendas enquanto a transmissão está acontecendo.', cost: 'pesado', defaultOn: false, settingsKey: 'plugin_live_subtitles' },
  { id: 'live-preview', name: 'Prévia ao vivo', blurb: 'Vê de relance quem está ao vivo e abre a prévia na hora.', cost: 'pesado', defaultOn: false, settingsKey: 'plugin_live_preview' },
  { id: 'live-chat', name: 'Chat ao vivo', blurb: 'Mostra o chat ao lado do vídeo e salva junto com o download.', cost: 'leve', defaultOn: true, settingsKey: 'plugin_live_chat' },
  { id: 'clip-editor', name: 'Editor de clipes', blurb: 'Corta um trecho do VOD e baixa só ele.', cost: 'leve', defaultOn: true, settingsKey: 'plugin_clip_editor' },
  { id: 'twitch-clip', name: 'Clipes do Twitch', blurb: 'Cria um clipe do Twitch sem sair do app.', cost: 'leve', defaultOn: true, settingsKey: 'plugin_twitch_clip' },
  { id: 'archive-search', name: 'Busca no arquivo', blurb: 'Pesquisa transcrições e chat dos VODs já arquivados.', cost: 'leve', defaultOn: true, settingsKey: 'plugin_archive_search' },
  { id: 'channel-explore', name: 'Explorar canais', blurb: 'Abre o painel de um canal com VODs, clipes e ao-vivo.', cost: 'leve', defaultOn: true, settingsKey: 'plugin_channel_explore' },
  { id: 'local-file', name: 'Arquivo local', blurb: 'Assiste um arquivo de vídeo que já está no seu disco.', cost: 'leve', defaultOn: true, settingsKey: 'plugin_local_file' },
  { id: 'ai-ask', name: 'Perguntar à IA', blurb: 'Perguntas respondidas só com o arquivo local, usando sua chave de API.', cost: 'pesado', defaultOn: false, settingsKey: 'plugin_ai_ask' },
];

export const PLUGIN_IDS: PluginId[] = PLUGINS.map((p) => p.id);

/** Plugins that were features first: the feature flag stays the truth until a
 *  plugin toggle writes a value of its own. */
export const PLUGIN_FEATURE_ALIAS: Record<PluginId, string> = {
  'transcribe': 'transcribe-vod',
  'live-subtitles': 'live-captions',
  'live-preview': 'live-preview',
  'live-chat': 'chat-live',
};

/** Main-nav entries owned by a plugin. A disabled plugin removes its entry
 *  from the tab bar (the URL/history/settings tabs have no plugin — core). */
export const PLUGIN_TABS: Record<string, PluginId> = { channels: 'channels' };

const LS_STATE = 'vodrip.plugins';
const LS_CATALOG = 'vodrip.plugins.catalog';

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

/** Definitions: the backend's catalog once synced, the mirror before that. */
export function pluginCatalog(): PluginDef[] {
  const stored = readLS(LS_CATALOG);
  if (Array.isArray(stored) && stored.length > 0) return stored as PluginDef[];
  return PLUGINS;
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
    if (p.core) { out[p.id] = true; continue; }
    if (p.id in stored) { out[p.id] = !!stored[p.id]; continue; }
    // READ side resolves defaultOn only — the backend registry is the single
    // source of truth (same ids/defaults, enforced by
    // backend/tests/test_plugin_registry.py::test_registry_matches_frontend_mirror).
    // The legacy feature alias is WRITE-side only (setPluginEnabled keeps the
    // feature flag in step); consulting it here made the mirror answer a
    // different value than the registry for 'live-subtitles'/'live-chat'.
    out[p.id] = p.defaultOn;
  }
  cachedMap = out;
  cachedMapKey = key;
  return out;
}

export function isPluginEnabled(id: PluginId): boolean {
  const def = pluginCatalog().find((p) => p.id === id);
  if (def?.core) return true;
  return !!enabledPlugins()[id];
}

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

/** Apply locally first (instant UI), then push to the backend. */
function applyLocal(patch: Record<string, boolean>): void {
  const next = { ...storedState() };
  for (const [id, on] of Object.entries(patch)) {
    next[id] = !!on;
    // A core plugin can never be switched off — refuse locally too.
    const def = pluginCatalog().find((p) => p.id === id);
    if (def?.core) next[id] = true;
  }
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
  // A core plugin is always on: there is nothing to write anywhere, locally
  // or in settings.json — not even a request that would set it false.
  if (pluginCatalog().find((p) => p.id === id)?.core) return;
  applyLocal({ [id]: enabled });
  const alias = PLUGIN_FEATURE_ALIAS[id];
  if (alias) setFeatureFlag(alias, enabled);
  await pushToBackend(id, enabled);
}

export async function setPluginsBulk(patch: Record<string, boolean>): Promise<void> {
  const wanted = Object.fromEntries(
    Object.entries(patch).filter(([id]) => !pluginCatalog().find((p) => p.id === id)?.core),
  );
  applyLocal(wanted);
  for (const [id, on] of Object.entries(wanted)) {
    const alias = PLUGIN_FEATURE_ALIAS[id];
    if (alias) setFeatureFlag(alias, on);
    await pushToBackend(id, on);
  }
}

/** Only the core plugins on; everything else off. The "SO O ESSENCIAL" preset. */
export function coreOnlyPatch(): Record<string, boolean> {
  const patch: Record<string, boolean> = {};
  for (const p of PLUGINS) patch[p.id] = !!p.core;
  return patch;
}

/** Every plugin on, heavy ones included. The "TUDO" preset. */
export function allOnPatch(): Record<string, boolean> {
  const patch: Record<string, boolean> = {};
  for (const p of PLUGINS) patch[p.id] = true;
  return patch;
}

/** Pull backend truth into the local cache (call once on App mount). */
export async function syncPluginsFromBackend(): Promise<void> {
  try {
    const r = await fetch('/api/plugins');
    if (!r.ok) return; // 404 while the route is not deployed: the mirror keeps answering
    const j = (await r.json()) as { plugins?: PluginState[]; enabled?: PluginId[] };
    if (Array.isArray(j.plugins) && j.plugins.length > 0) {
      writeLS(LS_CATALOG, j.plugins.map((p) => ({
        id: p.id, name: p.name, blurb: p.blurb, cost: p.cost,
        core: p.core, defaultOn: p.defaultOn, settingsKey: p.settingsKey,
      })));
      const patch: Record<string, boolean> = {};
      for (const p of j.plugins) patch[p.id] = !!p.enabled;
      applyLocal(patch);
    } else if (Array.isArray(j.enabled)) {
      applyLocal(Object.fromEntries(j.enabled.map((id) => [id, true])));
    }
  } catch { /* offline: the mirror keeps answering */ }
}

// ── hooks ────────────────────────────────────────────────────────────────────

export function usePlugins(): PluginState[] {
  return useSyncExternalStore(subscribe, pluginStates, () => [] as PluginState[]);
}

export function usePlugin(id: PluginId): boolean {
  return useSyncExternalStore(
    subscribe,
    () => isPluginEnabled(id),
    () => false,
  );
}

/** Renders its children only while the plugin is enabled. */
export function PluginGate({ id, children }: { id: PluginId; children: ReactNode }) {
  const on = usePlugin(id);
  return on ? <>{children}</> : null;
}

/** Filter a nav list down to the entries whose plugin (if any) is on. */
export function tabsForPlugins<T extends string>(
  tabs: readonly T[],
  enabled: (id: PluginId) => boolean = isPluginEnabled,
): T[] {
  return tabs.filter((tab) => {
    const pluginId = PLUGIN_TABS[tab];
    return pluginId ? enabled(pluginId) : true;
  });
}

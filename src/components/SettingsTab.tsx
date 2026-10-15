import { type Dispatch, type ReactNode, type SetStateAction, useEffect, useState } from 'react';
import {
  Activity, AlertTriangle, CheckCircle2, ChevronDown, FolderOpen, HardDrive, Languages, Loader2, Mic, Puzzle, RefreshCw, Settings2, ShieldCheck, Sparkles, StopCircle,
  type LucideIcon,
} from 'lucide-react';
import FieldCaption from './FieldCaption';
import InfoHint from './InfoHint';
import CookieBridgeSection, { type BridgeStatus } from './CookieBridgeSection';
import DiskSection from './DiskSection';
import RateGovernorSection from './RateGovernorSection';
import TranscriptionSection from './TranscriptionSection';
import NumberField from './NumberField';
import Toggle from './Toggle';
import { apiGet, apiPost } from '../hooks/useApiClient';
import { resetAll as resetFirstTimeFlags } from '../lib/firstTime';
import { useI18n, type Lang } from '../i18n';
import type { AppSettings, UpdateInfo } from '../types';
import { previewDownloadTree, type DownloadLayout } from '../downloadLayout';
import { ALWAYS_ON, bundleCost, pluginEnabled, setPluginEnabled, usePlugins, type PluginState } from '../lib/plugins';
import EnablePluginPrompt from './EnablePluginPrompt';
type Props = {
  settings: AppSettings;
  setSettings: Dispatch<SetStateAction<AppSettings>>;
  appVersion: string | null;
  updateInfo: UpdateInfo | null;
  updateChecking: boolean;
  updateApplying: boolean;
  updateMessage: string | null;
  pickingFolder: boolean;
  settingsSaved: boolean;
  onPickFolder: () => Promise<string | null>;
  onSave: () => Promise<void>;
  onCheckUpdate: () => Promise<void>;
  onApplyUpdate: () => Promise<void>;
  onFlushPanelLayout: () => void;
};

/** Fields SettingsTab displays; signature change => "unsaved changes" chip. */
const SETTING_KEYS = [
  'download_folder', 'download_threads', 'max_cache_mb', 'skip_youtube_startup_warm',
  'start_with_windows', 'download_layout', 'download_transcript_sidecar',
  'download_transcript_sidecar_format',
  'archive_vod_keep_count', 'whisper_model_cache', 'yt_subtitles_first',
  'asr_language',
  'cache_dir', 'data_dir',
  'auto_install_extension',
  'experimental_ai_enabled',
] as const;
const settingsSignature = (s: AppSettings) =>
  JSON.stringify(SETTING_KEYS.map((k) => s[k] ?? null));

function SettingsCard({
  icon: Icon,
  title,
  right,
  danger,
  open,
  onToggle,
  children,
}: {
  icon: LucideIcon;
  title: string;
  right?: ReactNode;
  danger?: boolean;
  /** Accordion: card content is hidden while collapsed (all start collapsed). */
  open: boolean;
  onToggle: () => void;
  children: ReactNode;
}) {
  return (
    <section className={`border-2 ${danger ? 'border-red-900' : 'border-zinc-800'} bg-zinc-950/60`}>
      <div
        className={`flex items-center justify-between gap-2 px-3 py-2 border-b-2 ${
          danger ? 'border-red-900' : 'border-zinc-800'
        }`}
      >
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={open}
          className="flex items-center gap-2 min-w-0 text-left flex-1"
        >
          <span
            className={`flex items-center gap-2 min-w-0 text-[11px] font-bold uppercase tracking-wide ${
              danger ? 'text-red-400' : 'text-zinc-300'
            }`}
          >
            <Icon size={14} className="shrink-0" />
            <span className="truncate">{title}</span>
          </span>
          <ChevronDown
            size={14}
            className={`shrink-0 transition-transform ${open ? 'rotate-180' : ''} ${
              danger ? 'text-red-500' : 'text-zinc-500'
            }`}
          />
        </button>
        {right}
      </div>
      {open ? <div className="p-3 flex flex-col gap-3">{children}</div> : null}
    </section>
  );
}

/** Custo legível, na regra do dono: abaixo de 1024 MB mostra MB inteiro; a
 *  partir disso, GB com 1 casa (2048 -> "2.0 GB", 397 -> "397 MB"). */
function formatMb(mb: number): string {
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${Math.round(mb)} MB`;
}

/** O custo de UM plugin. Quando `shares_with` nomeia outro plugin JÁ LIGADO o
 *  modelo é o mesmo: mostrar o número de novo faria o dono somar 397 MB duas
 *  vezes, então a linha vira "incluso (mesmo modelo)". */
function PluginCostLine({ plugin, enabledIds }: { plugin: PluginState; enabledIds: Set<string> }) {
  const { t } = useI18n();
  if (plugin.shares_with && enabledIds.has(plugin.shares_with)) {
    return (
      <span className="text-[10px] font-mono text-emerald-400" data-testid={`plugin-cost-${plugin.id}`}>
        {t('included (same model)')}
      </span>
    );
  }
  return (
    <span className="text-[10px] font-mono text-zinc-500" data-testid={`plugin-cost-${plugin.id}`}>
      {t('{size} on disk', { size: formatMb(plugin.disk_mb ?? 0) })}
      {' · '}
      {t('{size} of VRAM', { size: formatMb(plugin.vram_mb ?? 0) })}
    </span>
  );
}

export default function SettingsTab({
  settings,
  setSettings,
  appVersion,
  updateInfo,
  updateChecking,
  updateApplying,
  updateMessage,
  pickingFolder,
  settingsSaved,
  onPickFolder,
  onSave,
  onCheckUpdate,
  onApplyUpdate,
  onFlushPanelLayout,
}: Props) {
  const [savedSig, setSavedSig] = useState(() => settingsSignature(settings));
  const dirty = settingsSignature(settings) !== savedSig;
  const { lang, setLang, t } = useI18n();
  /** Plugin list + per-plugin enabled flags (localStorage cache, synced from
   *  GET /api/plugins on App mount). Replaces the old feature toggles. */
  const plugins = usePlugins();

  /** Ids ligados — alimenta a linha "incluso (mesmo modelo)" e o resumo.
   *  bundleCost honra shares_with: transcribe + legendas ao vivo custam UM
   *  modelo de 397 MB, não dois. */
  const enabledIds = new Set(plugins.filter((p) => p.enabled).map((p) => p.id));
  const totals = bundleCost(enabledIds);

  /** Accordion: every card starts collapsed; expanding one never closes the
   *  others (independent chevron toggles, state keyed by card id). */
  const [openCards, setOpenCards] = useState<Record<string, boolean>>({});
  const toggleCard = (id: string) =>
    setOpenCards((m) => ({ ...m, [id]: !m[id] }));

  /** Machine-aware resource suggestions (threads + cache MB) served by
   *  GET /api/settings/recommended — filled via the "Recommended" button in
   *  the General card. Null until the fetch resolves (button hidden then). */
  const [recommended, setRecommended] = useState<{
    download_threads: number;
    max_cache_mb: number;
  } | null>(null);
  useEffect(() => {
    let alive = true;
    void apiGet<{ download_threads: number; max_cache_mb: number }>('/api/settings/recommended')
      .then((r) => {
        if (alive) setRecommended(r);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);

  /** Persist the language choice immediately — switching must survive a
   *  reload even if the user never presses Save Settings. */
  const changeLanguage = (l: Lang) => {
    setLang(l);
    setSettings({ ...settings, ui_language: l });
    void apiPost('/api/settings', { ui_language: l }).catch(() => {});
  };

  /** Latest Cookie Bridge status — drives card placement. Null until the
   *  first fetch resolves; the card then sits at its normal (last) spot so
   *  paired users don't see it jump on every Settings open. */
  const [cookieStatus, setCookieStatus] = useState<BridgeStatus | null>(null);
  const cookieCount = cookieStatus
    ? Object.values(cookieStatus.platforms ?? {}).reduce((n, p) => n + p.count, 0)
    : 0;
  /** Not installed yet OR no cookies in the app → surface the install first. */
  const needsCookieSetup =
    cookieStatus !== null && (!cookieStatus.paired || cookieCount === 0);

  /** Tutorial reset — re-arms every first-time message (cookie offer etc.). */
  const [tutorialToast, setTutorialToast] = useState(false);
  const resetTutorials = () => {
    resetFirstTimeFlags();
    setTutorialToast(true);
    window.setTimeout(() => setTutorialToast(false), 3000);
  };

  /** Write-only AI key: kept in local state, never rendered back. dirty =
   *  the user edited the field ('' after a typed-then-cleared field REMOVES
   *  the stored key on save; an untouched field leaves it alone). */
  const [aiKey, setAiKey] = useState('');
  const [aiKeyDirty, setAiKeyDirty] = useState(false);
  const [aiKeyError, setAiKeyError] = useState<string | null>(null);

  const cookieCard = (
    <SettingsCard
      icon={ShieldCheck}
      title={t('Cookie Bridge')}
      open={!!openCards.cookie}
      onToggle={() => toggleCard('cookie')}
    >
      <Toggle
        label={t('cookieAuto.toggle')}
        info={t('cookieAuto.toggleInfo')}
        checked={settings.auto_install_extension !== false}
        onChange={(c) => setSettings({ ...settings, auto_install_extension: c })}
        ariaLabel="auto install cookie extension"
      />
      <CookieBridgeSection onStatusChange={setCookieStatus} />
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={resetTutorials}
          className="bg-zinc-900 text-zinc-200 font-black uppercase px-3 py-1.5 text-[11px] border-2 border-zinc-600 hover:border-white hover:text-white"
        >
          {t('tutorial.button')}
        </button>
        {tutorialToast ? (
          <span className="text-[11px] font-mono text-emerald-400" role="status">
            {t('tutorial.resetToast')}
          </span>
        ) : null}
      </div>
    </SettingsCard>
  );

  const save = async () => {
    setAiKeyError(null);
    try {
      // Key is write-only: POST it alone (before the main save, so the
      // toggle-on validation sees the fresh key), only when edited.
      if (aiKeyDirty) {
        await apiPost('/api/settings', { ai_api_key: aiKey.trim() });
        setSettings((prev) => ({ ...prev, ai_api_key_set: aiKey.trim() !== '' }));
        setAiKey('');
        setAiKeyDirty(false);
      }
      await onSave();
    } catch (err) {
      setAiKeyError(err instanceof Error ? err.message : String(err));
      return;
    }
    setSavedSig(settingsSignature(settings));
  };

  const exit = () => {
    if (!window.confirm(t('Exit VOD.RIP? All downloads will be cancelled and the app will close.'))) return;
    onFlushPanelLayout();
    void apiPost('/api/exit', {}).catch(() => {});
  };

  return (
    <div className="flex flex-col gap-3">
      {/* Cookie Bridge first until the extension is installed and cookies
          are detected — after that it moves to the very bottom. */}
      {needsCookieSetup ? cookieCard : null}

      {/* ── Language ─────────────────────────────────────────── */}
      <SettingsCard
        icon={Languages}
        title={t('Language')}
        open={!!openCards.language}
        onToggle={() => toggleCard('language')}
      >
        <div className="flex flex-col gap-1.5">
          <FieldCaption
            noWrap
            info={t('Used for the app UI. Subtitles and transcriptions follow this language unless you pick a specific one.')}
          >
            {t('App Language')}
          </FieldCaption>
          <select
            value={lang}
            aria-label="app language"
            onChange={(e) => changeLanguage(e.target.value as Lang)}
            className="w-full bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-2 px-2.5 text-sm focus:outline-none focus:border-white"
          >
            <option value="en">English</option>
            <option value="pt-BR">Português (Brasil)</option>
            <option value="es">Español</option>
          </select>
        </div>
      </SettingsCard>

      {/* ── General ─────────────────────────────────────────────── */}
      <SettingsCard
        icon={Settings2}
        title={t('General')}
        open={!!openCards.general}
        onToggle={() => toggleCard('general')}
      >
        <div className="flex flex-col gap-1.5">
          <FieldCaption>{t('Download Folder')}</FieldCaption>
          <div className="flex gap-2">
            <input type="text" value={settings.download_folder}
              onChange={(e) => setSettings({ ...settings, download_folder: e.target.value })}
              placeholder="C:\Users\...\Downloads"
              aria-label="download folder"
              className="flex-1 min-w-0 bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-2 px-2.5 text-xs truncate focus:outline-none focus:border-white" />
            <button type="button" onClick={onPickFolder} disabled={pickingFolder}
              className="bg-zinc-900 text-zinc-200 font-black uppercase px-3 py-2 text-[11px] border-2 border-zinc-600 hover:border-white hover:text-white shrink-0 flex items-center gap-1.5 disabled:opacity-50">
              {pickingFolder ? <Loader2 size={14} className="animate-spin" /> : <FolderOpen size={14} />}
              {pickingFolder ? '...' : t('Browse')}
            </button>
          </div>
        </div>
        <div className="flex flex-col gap-1.5">
          <FieldCaption>{t('Download folder structure')}</FieldCaption>
          <div className="flex gap-1.5">
            <button type="button"
              onClick={() => setSettings({ ...settings, download_layout: 'flat' })}
              className={`px-2 py-1 text-[10px] font-bold uppercase tracking-normal border-2 ${(settings.download_layout || 'typed') === 'flat' ? 'border-white bg-white text-black' : 'border-zinc-800 text-zinc-400'}`}>
              {t('One folder')}
            </button>
            <button type="button"
              onClick={() => setSettings({ ...settings, download_layout: 'typed' })}
              className={`px-2 py-1 text-[10px] font-bold uppercase tracking-normal border-2 ${(settings.download_layout || 'typed') === 'typed' ? 'border-white bg-white text-black' : 'border-zinc-800 text-zinc-400'}`}>
              {t('Subfolders')}
            </button>
          </div>
          <pre className="text-[10px] font-mono text-zinc-500 bg-zinc-950 border-2 border-zinc-800 p-2 overflow-x-auto leading-relaxed">
{previewDownloadTree(settings.download_folder || 'Downloads', (settings.download_layout || 'typed') as DownloadLayout).join(String.fromCharCode(10))}
          </pre>
        </div>
        <Toggle
          label={t('Save subtitles with downloads')}
          info={t('Writes a .srt subtitle next to the video — the format video editors import. YouTube fetches its captions at download time; Twitch/Kick reuse the archived transcript (archive the VOD, or the clip’s source VOD, first). On by default.')}
          checked={settings.download_transcript_sidecar !== false}
          onChange={(c) => setSettings({ ...settings, download_transcript_sidecar: c })}
          ariaLabel="save subtitles with downloads"
        />
        {settings.download_transcript_sidecar !== false && (
          <div className="flex flex-col gap-1">
            <FieldCaption>{t('Subtitle file type')}</FieldCaption>
            <select
              className="border-2 border-zinc-800 bg-zinc-950 px-2 py-1 font-mono text-[10px] text-zinc-300"
              value={settings.download_transcript_sidecar_format || 'srt'}
              onChange={(e) => setSettings({
                ...settings,
                download_transcript_sidecar_format: e.target.value as 'srt' | 'srt+txt' | 'txt',
              })}
              aria-label="subtitle file type"
            >
              <option value="srt">{t('Subtitles (.srt) — recommended')}</option>
              <option value="srt+txt">{t('Subtitles (.srt) + plain text (.txt)')}</option>
              <option value="txt">{t('Plain text (.txt) only')}</option>
            </select>
          </div>
        )}
        <div className="grid grid-cols-2 gap-2">
          <div className="flex flex-col gap-1.5">
            <FieldCaption noWrap>{t('Download Threads')}</FieldCaption>
            <NumberField
              ariaLabel="download threads"
              value={settings.download_threads}
              min={1}
              max={16}
              step={1}
              onChange={(v) => setSettings({ ...settings, download_threads: v })}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <FieldCaption>{t('Max Cache (MB)')}</FieldCaption>
            <NumberField
              ariaLabel="max cache mb"
              value={settings.max_cache_mb}
              min={50}
              max={2000}
              step={50}
              onChange={(v) => setSettings({ ...settings, max_cache_mb: v })}
            />
          </div>
        </div>
        {recommended ? (
          <div className="flex items-center gap-2 flex-wrap">
            <button
              type="button"
              aria-label="recommended resource defaults"
              title={t('Suggested for this machine')}
              onClick={() =>
                setSettings({
                  ...settings,
                  download_threads: recommended.download_threads,
                  max_cache_mb: recommended.max_cache_mb,
                })
              }
              className="bg-zinc-900 text-zinc-200 font-black uppercase px-3 py-1.5 text-[11px] border-2 border-zinc-600 hover:border-white hover:text-white flex items-center gap-1.5"
            >
              {t('Recommended')}
            </button>
            <span className="text-[11px] text-zinc-500 font-mono">
              {t('threads {threads} · cache {cache} MB', { threads: recommended.download_threads, cache: recommended.max_cache_mb })}
            </span>
          </div>
        ) : null}
        <Toggle
          label={t('Warm YouTube at startup')}
          info={t('Pre-loads preview data for faster first play (uses ~500MB download at boot)')}
          checked={!settings.skip_youtube_startup_warm}
          onChange={(c) => setSettings({ ...settings, skip_youtube_startup_warm: !c })}
          ariaLabel="warm youtube at startup"
        />
        <Toggle
          label={t('Start with Windows')}
          info={t('Launches hidden in the tray at boot and keeps transcribing, indexing and chat capture running quietly in the background')}
          checked={!!settings.start_with_windows}
          onChange={(c) => setSettings({ ...settings, start_with_windows: c })}
          ariaLabel="start with windows"
        />
      </SettingsCard>

      {/* ── Transcription ─────────────────────────────────────────
          Plugin 'transcribe' (backend/services/plugin_registry.py). Off by
          default. The card does not sit there as a dead switch, and it does
          not silently vanish either: the owner asked for a button that needs
          a plugin to SAY SO ("botoes que necessitam de plugins pra fazer
          algo, deve aparecer uma mensagem pedindo pra habilitar o plugin").
          So OFF renders EnablePluginPrompt with the cost and an Enable
          button, and ON renders the real section. */}
      {pluginEnabled('transcribe') ? (
        <SettingsCard
          icon={Mic}
          title={t('Transcription')}
          open={!!openCards.transcription}
          onToggle={() => toggleCard('transcription')}
        >
          <TranscriptionSection
            settings={settings}
            setSettings={setSettings}
            onSaved={(s) => setSavedSig(settingsSignature(s))}
          />
        </SettingsCard>
      ) : (
          <EnablePluginPrompt pluginId="transcribe" />
        )}

      {/* ── Disk & Storage ──────────────────────────────────────── */}
      <SettingsCard
        icon={HardDrive}
        title={t('Disk & Storage')}
        open={!!openCards.disk}
        onToggle={() => toggleCard('disk')}
      >
        <DiskSection settings={settings} setSettings={setSettings} />
      </SettingsCard>

      {/* ── Platform Requests ──────────────────────────────────────
          Sits with the other infrastructure cards, directly above the
          Cookie Bridge (also platform behaviour): this is where a user
          looks when a platform is being slow or a download is pacing
          itself. Mounted INSIDE the card, so nothing is fetched until
          the card is actually opened. */}
      <SettingsCard
        icon={Activity}
        title={t('Platform Requests')}
        open={!!openCards.rate}
        onToggle={() => toggleCard('rate')}
      >
        <RateGovernorSection />
      </SettingsCard>


      {/* ── Cookie Bridge (detected → above Save) ── */}
      {needsCookieSetup ? null : cookieCard}

      {/* ── Experimental ────────────────────────────────────────
          Plugin 'ai-ask' (the AI search & answers feature is the whole card).
          Off by default, and OFF renders the enable prompt rather than a
          disappearing card — same rule as Transcription above. */}
      {pluginEnabled('ai-ask') ? (
        <SettingsCard
          icon={Sparkles}
          title={t('Experimental')}
          open={!!openCards.experimental}
          onToggle={() => toggleCard('experimental')}
        >
          <Toggle
            label={t('Experimental: AI search & answers')}
            info={t('Ask questions about archived channels. Answers come only from the local archive (chat + transcripts) using your own OpenAI-compatible API key.')}
            checked={settings.experimental_ai_enabled === true}
            onChange={(c) => setSettings({ ...settings, experimental_ai_enabled: c })}
            ariaLabel="experimental ai enabled"
          />
          <div className="flex flex-col gap-1.5">
            <FieldCaption noWrap>{t('AI API key (OpenAI-compatible)')}</FieldCaption>
            <input
              type="password"
              value={aiKey}
              onChange={(e) => { setAiKey(e.target.value); setAiKeyDirty(true); }}
              placeholder={settings.ai_api_key_set ? '••••••••' : ''}
              autoComplete="off"
              aria-label="ai api key"
              className="w-full bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-2 px-2.5 text-xs focus:outline-none focus:border-white"
            />
            {settings.ai_api_key_set ? (
              <span className="text-[10px] font-mono text-zinc-500">
                {t('A key is set — the stored key is never shown. Type a new key to replace it, or clear the field and save to remove it.')}
              </span>
            ) : null}
          </div>
          {settings.experimental_ai_enabled === true && !settings.ai_api_key_set && !aiKey.trim() ? (
            <span className="text-[11px] font-mono text-red-400" role="alert">
              {t('Add an AI API key first — the feature cannot run without one.')}
            </span>
          ) : null}
          {aiKeyError ? (
            <span className="text-[11px] font-mono text-red-400" role="alert">
              {aiKeyError}
            </span>
          ) : null}
        </SettingsCard>
      ) : (
          <EnablePluginPrompt pluginId="ai-ask" />
        )}

      <SettingsCard
        icon={Puzzle}
        title={t('Plugins')}
        right={
          <span className="text-[11px] font-mono text-zinc-400 tabular-nums shrink-0 pr-1" data-testid="plugins-count">
            {t('plugins.onCount', {
              on: plugins.filter(p => p.enabled).length,
              total: plugins.length,
            })}
          </span>
        }
        open={!!openCards.plugins}
        onToggle={() => toggleCard('plugins')}
      >
        <p className="text-[11px] font-mono text-zinc-500">{t('Plugins are the extra tools. The heavy ones start off — turn them on whenever you need them, here or in Settings → Plugins.')}</p>
        {/* A ordem é a que o backend mandou (ligados -> recomendados -> mais
            barato -> id): NÃO reordenar aqui. */}
        <div className="flex flex-col gap-3 mt-2">
          {plugins.map(p => (
            <div key={p.id} className="flex flex-col gap-0.5">
              <Toggle
                label={p.name}
                checked={p.enabled}
                onChange={c => { void setPluginEnabled(p.id, c); }}
                ariaLabel={p.id}
              />
              <span className="text-[10px] font-mono text-zinc-500">{p.blurb}</span>
              <PluginCostLine plugin={p} enabledIds={enabledIds} />
            </div>
          ))}
        </div>
        {/* Resumo do selecionado: total de disco e VRAM. */}
        <div className="flex items-center justify-between gap-2 mt-1" data-testid="plugins-totals">
          <span className="text-[10px] font-black uppercase tracking-wide text-zinc-500">{t('Selected total')}</span>
          <span className="text-[10px] font-mono text-zinc-300 tabular-nums">
            {t('{size} on disk', { size: formatMb(totals.disk_mb) })}
            {' · '}
            {t('{size} of VRAM', { size: formatMb(totals.vram_mb) })}
          </span>
        </div>
        {/* "Sempre ligado": as 8 superfícies que SÃO o app — sem toggle. */}
        <div className="mt-2 pt-2 border-t-2 border-zinc-800" data-testid="plugins-always-on">
          <div className="text-[10px] font-black uppercase tracking-wide text-zinc-500">{t('Always on')}</div>
          <div className="flex flex-wrap gap-1.5 mt-1.5">
            {ALWAYS_ON.map(a => (
              <span key={a.id} title={a.blurb} className="text-[10px] font-mono px-1.5 py-0.5 border border-zinc-700 text-zinc-400">
                {a.name}
              </span>
            ))}
          </div>
        </div>
      </SettingsCard>

      {/* ── Danger Zone (user request: directly above Save — exit stays */}
      <SettingsCard
        icon={AlertTriangle}
        title={t('Danger Zone')}
        danger
        right={<InfoHint text={t('Exits VOD.RIP — cancels all downloads and closes the app.')} />}
        open={!!openCards.danger}
        onToggle={() => toggleCard('danger')}
      >
        <button
          type="button"
          onClick={exit}
          className="w-full bg-red-950 text-red-300 font-black uppercase py-2.5 flex items-center justify-center gap-2 text-xs border-2 border-red-900 hover:border-red-500 hover:text-red-200 transition-colors"
        >
          <StopCircle size={16} />
          {t('Exit VOD.RIP')}
        </button>
      </SettingsCard>

      {/* ── Save ────────────────────────────────────────────────── */}
      <div className="flex flex-col gap-1.5 pt-1 pb-1">
        <button
          onClick={() => void save()}
          className="w-full bg-zinc-100 text-black font-black uppercase py-3 flex items-center justify-center gap-2 text-xs border-2 border-zinc-100 hover:bg-zinc-300 hover:border-zinc-300 transition-colors"
        >
          {settingsSaved ? <><CheckCircle2 size={16} /> {t('Saved!')}</> : t('Save Settings')}
        </button>
        {dirty ? (
          <span className="text-[11px] font-mono text-amber-400 text-center" role="status">
            {t('● unsaved changes')}
          </span>
        ) : null}
      </div>
      {/* ── Updates (always visible at the bottom) ───────────────── */}
      <section className="border-2 border-zinc-800 bg-zinc-950/60">
        <div className="flex items-center justify-between gap-2 px-3 py-2 border-b-2 border-zinc-800">
          <span className="flex items-center gap-2 min-w-0 text-[11px] font-bold uppercase tracking-wide text-zinc-300">
            <RefreshCw size={14} className="shrink-0" />
            <span className="truncate">{t('Updates')}</span>
          </span>
          <span className="text-[11px] font-mono text-zinc-400 tabular-nums">v{appVersion ?? '…'}</span>
        </div>
        <div className="p-3 flex flex-col gap-3">
          <div className="flex items-center gap-2 flex-wrap">
            {updateInfo ? (
              <>
                <span className="text-[11px] font-mono text-emerald-400">{t('v{version} available', { version: updateInfo.version })}</span>
                {updateInfo.release_url ? (
                  <a
                    href={updateInfo.release_url}
                    target="_blank"
                    rel="noreferrer"
                    className="text-zinc-400 hover:text-zinc-200 underline-offset-2 hover:underline text-[11px] font-mono"
                  >
                    {t('release')}
                  </a>
                ) : null}
                <button
                  type="button"
                  onClick={() => void onApplyUpdate()}
                  disabled={updateApplying}
                  className="text-emerald-500 hover:text-emerald-300 underline-offset-2 hover:underline disabled:opacity-40 p-0 bg-transparent border-0 font-mono text-[11px] inline-flex items-center gap-1"
                >
                  {updateApplying ? <Loader2 size={12} className="animate-spin" /> : null}
                  {updateApplying ? t('installing') : t('install')}
                </button>
              </>
            ) : updateMessage ? (
              <span className="text-[11px] font-mono text-zinc-400">{updateMessage}</span>
            ) : null}
          </div>
          <button
            type="button"
            onClick={() => void onCheckUpdate()}
            disabled={updateChecking}
            className="bg-zinc-900 text-zinc-200 font-black uppercase px-3 py-2 text-[11px] border-2 border-zinc-600 hover:border-white hover:text-white disabled:opacity-50 flex items-center justify-center gap-1.5"
          >
            {updateChecking ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />}
            {updateChecking ? t('Checking…') : t('Check for Updates')}
          </button>
        </div>
      </section>

    </div>
  );
}

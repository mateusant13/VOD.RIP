import { type Dispatch, type ReactNode, type SetStateAction, useEffect, useState } from 'react';
import {
  AlertTriangle, CheckCircle2, ChevronDown, FolderOpen, HardDrive, KeyRound, Languages, Loader2, Mic, RefreshCw, Settings2, ShieldCheck, StopCircle,
  type LucideIcon,
} from 'lucide-react';
import FieldCaption from './FieldCaption';
import InfoHint from './InfoHint';
import CookieBridgeSection, { type BridgeStatus } from './CookieBridgeSection';
import DiskSection from './DiskSection';
import OfficialApisSection from './OfficialApisSection';
import TranscriptionSection from './TranscriptionSection';
import NumberField from './NumberField';
import Toggle from './Toggle';
import { apiGet, apiPost } from '../hooks/useApiClient';
import { useI18n, type Lang } from '../i18n';
import type { AppSettings, UpdateInfo } from '../types';

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
  'archive_vod_keep_count', 'whisper_model', 'whisper_model_cache', 'yt_subtitles_first',
  'asr_language',
  'cache_dir', 'data_dir',
  'twitch_helix_token',
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
            className={`flex items-center gap-2 min-w-0 text-[11px] font-bold uppercase tracking-widest ${
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

  const cookieCard = (
    <SettingsCard
      icon={ShieldCheck}
      title={t('Cookie Bridge')}
      open={!!openCards.cookie}
      onToggle={() => toggleCard('cookie')}
    >
      <CookieBridgeSection onStatusChange={setCookieStatus} />
    </SettingsCard>
  );

  const save = async () => {
    await onSave();
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
      </SettingsCard>

      {/* ── Official API credentials ─────────────────────────── */}
      <SettingsCard
        icon={KeyRound}
        title={t('Official API credentials')}
        open={!!openCards.official}
        onToggle={() => toggleCard('official')}
      >
        <OfficialApisSection settings={settings} setSettings={setSettings} />
      </SettingsCard>

      {/* ── Transcription ───────────────────────────────────────── */}
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

      {/* ── Disk & Storage ──────────────────────────────────────── */}
      <SettingsCard
        icon={HardDrive}
        title={t('Disk & Storage')}
        open={!!openCards.disk}
        onToggle={() => toggleCard('disk')}
      >
        <DiskSection settings={settings} setSettings={setSettings} />
      </SettingsCard>

      {/* ── Updates ─────────────────────────────────────────────── */}
      <SettingsCard
        icon={RefreshCw}
        title={t('Updates')}
        right={<span className="text-[11px] font-mono text-zinc-400 tabular-nums">v{appVersion ?? '…'}</span>}
        open={!!openCards.updates}
        onToggle={() => toggleCard('updates')}
      >
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
      </SettingsCard>

      {/* ── Cookie Bridge (detected → second-to-last, above Save) ── */}
      {needsCookieSetup ? null : cookieCard}

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

      {/* ── Danger Zone (deliberately last — past Save so a destructive
          action is never one accidental click away from the rest) ── */}
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
    </div>
  );
}

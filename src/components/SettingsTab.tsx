import { type Dispatch, type ReactNode, type SetStateAction, useState } from 'react';
import {
  AlertTriangle, CheckCircle2, FolderOpen, HardDrive, Loader2, Mic, RefreshCw, Settings2, ShieldCheck, StopCircle,
  type LucideIcon,
} from 'lucide-react';
import FieldCaption from './FieldCaption';
import CookieBridgeSection from './CookieBridgeSection';
import DiskSection from './DiskSection';
import TranscriptionSection from './TranscriptionSection';
import NumberField from './NumberField';
import Toggle from './Toggle';
import { apiPost } from '../hooks/useApiClient';
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
  'cache_dir',
] as const;
const settingsSignature = (s: AppSettings) =>
  JSON.stringify(SETTING_KEYS.map((k) => s[k] ?? null));

function SettingsCard({
  icon: Icon,
  title,
  right,
  danger,
  children,
}: {
  icon: LucideIcon;
  title: string;
  right?: ReactNode;
  danger?: boolean;
  children: ReactNode;
}) {
  return (
    <section className={`border-2 ${danger ? 'border-red-900' : 'border-zinc-800'} bg-zinc-950/60`}>
      <div
        className={`flex items-center justify-between gap-2 px-2 py-1.5 border-b-2 ${
          danger ? 'border-red-900' : 'border-zinc-800'
        }`}
      >
        <span
          className={`flex items-center gap-1.5 min-w-0 text-[9px] font-bold uppercase tracking-widest ${
            danger ? 'text-red-400' : 'text-zinc-400'
          }`}
        >
          <Icon size={12} className="shrink-0" />
          <span className="truncate">{title}</span>
        </span>
        {right}
      </div>
      <div className="p-2 flex flex-col gap-2">{children}</div>
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

  const save = async () => {
    await onSave();
    setSavedSig(settingsSignature(settings));
  };

  const exit = () => {
    if (!window.confirm('Exit VOD.RIP? All downloads will be cancelled and the app will close.')) return;
    onFlushPanelLayout();
    void apiPost('/api/exit', {}).catch(() => {});
  };

  return (
    <div className="flex flex-col gap-3">
      {/* ── General ─────────────────────────────────────────────── */}
      <SettingsCard icon={Settings2} title="General">
        <div className="flex flex-col gap-1">
          <FieldCaption>Download Folder</FieldCaption>
          <div className="flex gap-1.5">
            <input type="text" value={settings.download_folder}
              onChange={(e) => setSettings({ ...settings, download_folder: e.target.value })}
              placeholder="C:\Users\...\Downloads"
              aria-label="download folder"
              className="flex-1 min-w-0 bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-1.5 px-2 text-xs truncate focus:outline-none focus:border-white" />
            <button type="button" onClick={onPickFolder} disabled={pickingFolder}
              className="bg-zinc-900 text-zinc-200 font-black uppercase px-3 text-[10px] border-2 border-zinc-600 hover:border-white hover:text-white shrink-0 flex items-center gap-1 disabled:opacity-50">
              {pickingFolder ? <Loader2 size={14} className="animate-spin" /> : <FolderOpen size={14} />}
              {pickingFolder ? '...' : 'Browse'}
            </button>
          </div>
        </div>
        <div className="grid grid-cols-2 gap-2">
          <div className="flex flex-col gap-1">
            <FieldCaption noWrap>Download Threads</FieldCaption>
            <NumberField
              ariaLabel="download threads"
              value={settings.download_threads}
              min={1}
              max={16}
              step={1}
              onChange={(v) => setSettings({ ...settings, download_threads: v })}
            />
          </div>
          <div className="flex flex-col gap-1">
            <FieldCaption>Max Cache (MB)</FieldCaption>
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
        <Toggle
          label="Warm YouTube at startup"
          hint="Pre-loads preview data for faster first play (uses ~500MB download at boot)"
          checked={!settings.skip_youtube_startup_warm}
          onChange={(c) => setSettings({ ...settings, skip_youtube_startup_warm: !c })}
          ariaLabel="warm youtube at startup"
        />
      </SettingsCard>

      {/* ── Transcription ───────────────────────────────────────── */}
      <SettingsCard icon={Mic} title="Transcription">
        <TranscriptionSection
          settings={settings}
          setSettings={setSettings}
          onSaved={(s) => setSavedSig(settingsSignature(s))}
        />
      </SettingsCard>

      {/* ── Disk & Storage ──────────────────────────────────────── */}
      <SettingsCard icon={HardDrive} title="Disk & Storage">
        <DiskSection settings={settings} setSettings={setSettings} />
      </SettingsCard>

      {/* ── Cookie Bridge ───────────────────────────────────────── */}
      <SettingsCard icon={ShieldCheck} title="Cookie Bridge">
        <CookieBridgeSection />
      </SettingsCard>

      {/* ── Updates ─────────────────────────────────────────────── */}
      <SettingsCard
        icon={RefreshCw}
        title="Updates"
        right={<span className="text-[9px] font-mono text-zinc-500 tabular-nums">v{appVersion ?? '…'}</span>}
      >
        <div className="flex items-center gap-2 flex-wrap">
          {updateInfo ? (
            <>
              <span className="text-[10px] font-mono text-emerald-400">v{updateInfo.version} available</span>
              {updateInfo.release_url ? (
                <a
                  href={updateInfo.release_url}
                  target="_blank"
                  rel="noreferrer"
                  className="text-zinc-500 hover:text-zinc-300 underline-offset-2 hover:underline text-[9px] font-mono"
                >
                  release
                </a>
              ) : null}
              <button
                type="button"
                onClick={() => void onApplyUpdate()}
                disabled={updateApplying}
                className="text-emerald-700 hover:text-emerald-500 underline-offset-2 hover:underline disabled:opacity-40 p-0 bg-transparent border-0 font-mono text-[9px] inline-flex items-center gap-0.5"
              >
                {updateApplying ? <Loader2 size={9} className="animate-spin" /> : null}
                {updateApplying ? 'installing' : 'install'}
              </button>
            </>
          ) : updateMessage ? (
            <span className="text-[10px] font-mono text-zinc-500">{updateMessage}</span>
          ) : null}
        </div>
        <button
          type="button"
          onClick={() => void onCheckUpdate()}
          disabled={updateChecking}
          className="bg-zinc-900 text-zinc-200 font-black uppercase px-2 py-1.5 text-[10px] border-2 border-zinc-600 hover:border-white hover:text-white disabled:opacity-50 flex items-center justify-center gap-1.5"
        >
          {updateChecking ? <Loader2 size={11} className="animate-spin" /> : <RefreshCw size={11} />}
          {updateChecking ? 'Checking…' : 'Check for Updates'}
        </button>
      </SettingsCard>

      {/* ── Danger Zone ─────────────────────────────────────────── */}
      <SettingsCard icon={AlertTriangle} title="Danger Zone" danger>
        <p className="text-[9px] text-zinc-600 font-mono leading-snug">
          Exits VOD.RIP — cancels all downloads and closes the app.
        </p>
        <button
          type="button"
          onClick={exit}
          className="w-full bg-red-950 text-red-400 font-black uppercase py-2 flex items-center justify-center gap-2 text-xs border-2 border-red-900 hover:border-red-500 hover:text-red-300 transition-colors"
        >
          <StopCircle size={14} />
          Exit VOD.RIP
        </button>
      </SettingsCard>

      {/* ── Save ────────────────────────────────────────────────── */}
      <div className="flex flex-col gap-1 pt-1 sticky bottom-0 bg-zinc-950/95 backdrop-blur-sm pb-1">
        <button
          onClick={() => void save()}
          className="w-full bg-zinc-900 text-zinc-200 font-black uppercase py-2.5 flex items-center justify-center gap-2 text-xs border-2 border-zinc-600 hover:border-white hover:text-white transition-colors"
        >
          {settingsSaved ? <><CheckCircle2 size={14} /> Saved!</> : 'Save Settings'}
        </button>
        {dirty ? (
          <span className="text-[9px] font-mono text-amber-500 text-center" role="status">
            ● unsaved changes
          </span>
        ) : null}
      </div>
    </div>
  );
}

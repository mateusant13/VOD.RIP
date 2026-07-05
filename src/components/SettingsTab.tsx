import { type Dispatch, type SetStateAction, useEffect, useState } from 'react';
import {
  CheckCircle2, FolderOpen, Loader2, StopCircle,
} from 'lucide-react';
import FieldCaption from './FieldCaption';
import { apiGet, apiPost } from '../hooks/useApiClient';
import type { AppSettings, UpdateInfo } from '../types';

type Props = {
  settings: AppSettings;
  setSettings: Dispatch<SetStateAction<AppSettings | null>>;
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
  const [ytAuth, setYtAuth] = useState<{
    auto_auth: boolean;
    browser: string | null;
    pot_providers: string[];
    pot_auto_available: boolean;
  } | null>(null);

  useEffect(() => {
    void apiGet<{
      auto_auth: boolean;
      browser: string | null;
      pot_providers: string[];
      pot_auto_available: boolean;
    }>('/api/settings/youtube-auth')
      .then(setYtAuth)
      .catch(() => setYtAuth(null));
  }, [settings.youtube_auto_auth, settings.youtube_cookies_browser]);

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-1.5">
        <FieldCaption>Download Folder</FieldCaption>
        <div className="flex gap-2">
          <input type="text" value={settings.download_folder}
            onChange={(e) => setSettings({ ...settings, download_folder: e.target.value })}
            placeholder="C:\Users\...\Downloads"
            className="flex-1 bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-2 px-2 text-xs truncate focus:outline-none focus:border-white" />
          <button type="button" onClick={onPickFolder} disabled={pickingFolder}
            className="bg-zinc-900 text-zinc-200 font-black uppercase px-3 text-[10px] border-2 border-zinc-600 hover:border-white hover:text-white shrink-0 flex items-center gap-1 disabled:opacity-50">
            {pickingFolder ? <Loader2 size={14} className="animate-spin" /> : <FolderOpen size={14} />}
            {pickingFolder ? '...' : 'Browse'}
          </button>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div className="flex flex-col gap-1.5">
          <FieldCaption noWrap>Download Threads</FieldCaption>
          <input type="number" min={1} max={16}
            value={settings.download_threads}
            onChange={(e) => setSettings({ ...settings, download_threads: Math.max(1, Math.min(16, parseInt(e.target.value) || 4)) })}
            className="w-full bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-2 px-2 focus:outline-none focus:border-white text-xs" />
        </div>
        <div className="flex flex-col gap-1.5">
          <FieldCaption>Max Cache (MB)</FieldCaption>
          <input type="number" min={50} max={2000}
            value={settings.max_cache_mb}
            onChange={(e) => setSettings({ ...settings, max_cache_mb: parseInt(e.target.value) || 200 })}
            className="w-full bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-2 px-2 focus:outline-none focus:border-white text-xs" />
        </div>
      </div>

      <div className="flex flex-col gap-2 border border-zinc-800 bg-zinc-950/50 p-2">
        <label className="flex items-center gap-2 text-[10px] font-mono text-zinc-300 cursor-pointer">
          <input
            type="checkbox"
            checked={settings.youtube_auto_auth !== false}
            onChange={(e) => setSettings({ ...settings, youtube_auto_auth: e.target.checked })}
            className="vod-cb-sm"
          />
          Auto YouTube auth (recommended)
        </label>
        <p className="text-[9px] text-zinc-600 font-mono leading-snug">
          Preview and downloads use InnerTube over HTTP (no browser spawned).
          {ytAuth?.browser ? (
            <> Detected browser: <span className="text-zinc-400">{ytAuth.browser}</span>.</>
          ) : null}
          {settings.youtube_cookies_browser ? (
            <> Reading cookies from <span className="text-zinc-400">{settings.youtube_cookies_browser}</span>.</>
          ) : null}
        </p>
      </div>

      <div className="flex flex-col gap-1.5">
        <FieldCaption>YouTube Cookies (optional — logged-in export)</FieldCaption>
        <input
          type="text"
          value={settings.youtube_cookies_file ?? ''}
          onChange={(e) => setSettings({ ...settings, youtube_cookies_file: e.target.value })}
          placeholder="Only if bot-blocked — export while signed in to Google"
          className="w-full bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-2 px-2 text-xs truncate focus:outline-none focus:border-white"
        />
        <p className="text-[9px] font-mono text-zinc-600 leading-snug">
          Anonymous session cookies are fetched automatically (no login). Use this field only for hard blocks.
        </p>
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div className="flex flex-col gap-1.5">
          <FieldCaption>Cookies From Browser</FieldCaption>
          <input
            type="text"
            value={settings.youtube_cookies_browser ?? ''}
            onChange={(e) => setSettings({ ...settings, youtube_cookies_browser: e.target.value })}
            placeholder="chrome / edge / firefox — only when set"
            className="w-full bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-2 px-2 text-xs focus:outline-none focus:border-white"
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <FieldCaption>Tokens JSON</FieldCaption>
          <input
            type="text"
            value={settings.youtube_tokens_file ?? ''}
            onChange={(e) => setSettings({ ...settings, youtube_tokens_file: e.target.value })}
            placeholder="visitorData + po_token JSON"
            className="w-full bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-2 px-2 text-xs truncate focus:outline-none focus:border-white"
          />
        </div>
      </div>

      <div className="flex flex-col gap-1.5">
        <FieldCaption>YouTube PO Token (advanced override)</FieldCaption>
        <input
          type="text"
          value={settings.youtube_po_token ?? ''}
          onChange={(e) => setSettings({ ...settings, youtube_po_token: e.target.value })}
          placeholder="Optional — paste po_token from tokens JSON export"
          className="w-full bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-2 px-2 text-xs truncate focus:outline-none focus:border-white"
        />
        <p className="text-[9px] text-zinc-600 font-mono leading-snug">
          Advanced override only. Preview does not use PO tokens; paste a manual token for yt-dlp downloads.
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5 pt-1 text-[9px] text-zinc-600 font-mono">
        <span>v{appVersion ?? '…'}</span>
        <span aria-hidden>·</span>
        <button
          type="button"
          onClick={() => void onCheckUpdate()}
          disabled={updateChecking}
          className="text-zinc-600 hover:text-zinc-400 underline-offset-2 hover:underline disabled:opacity-40 p-0 bg-transparent border-0 font-mono text-[9px] inline-flex items-center gap-0.5"
        >
          {updateChecking ? <Loader2 size={8} className="animate-spin" /> : null}
          {updateChecking ? 'checking' : 'check for updates'}
        </button>
        {updateInfo ? (
          <>
            <span aria-hidden>·</span>
            <span className="text-emerald-700">v{updateInfo.version}</span>
            {updateInfo.release_url ? (
              <a
                href={updateInfo.release_url}
                target="_blank"
                rel="noreferrer"
                className="text-zinc-500 hover:text-zinc-300 underline-offset-2 hover:underline"
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
              {updateApplying ? <Loader2 size={8} className="animate-spin" /> : null}
              {updateApplying ? 'installing' : 'install'}
            </button>
          </>
        ) : updateMessage ? (
          <>
            <span aria-hidden>·</span>
            <span className="text-zinc-600">{updateMessage}</span>
          </>
        ) : null}
      </div>

      <button onClick={onSave}
        className="w-full bg-zinc-900 text-zinc-200 font-black uppercase py-2.5 flex items-center justify-center gap-2 text-xs border-2 border-zinc-600 hover:border-white hover:text-white transition-colors">
        {settingsSaved ? <><CheckCircle2 size={14} /> Saved!</> : 'Save Settings'}
      </button>

      <button onClick={() => {
        if (!window.confirm('Exit VOD.RIP? All downloads will be cancelled and the app will close.')) return;
        onFlushPanelLayout();
        void apiPost('/api/exit', {}).catch(() => {});
      }}
        className="w-full bg-red-950 text-red-400 font-black uppercase py-2.5 flex items-center justify-center gap-2 text-xs border-2 border-red-900 hover:border-red-500 hover:text-red-300 transition-colors">
        <StopCircle size={14} />
        Exit VOD.RIP
      </button>
    </div>
  );
}

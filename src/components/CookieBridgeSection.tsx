import { useCallback, useEffect, useState } from 'react';
import { Check, Copy, Loader2, ShieldCheck, ShieldOff } from 'lucide-react';
import FieldCaption from './FieldCaption';
import { apiGet, apiPost } from '../hooks/useApiClient';

/**
 * Cookie Bridge consent + diagnostics.
 *
 * This section IS the consent point for the local browser extension: it
 * explains what gets sent where, shows pairing/platform state, and the
 * enable/disable toggle is the kill switch (backend returns 403 for ingest
 * while disabled).
 */

interface BridgeStatus {
  paired: boolean;
  enabled: boolean;
  platforms: Record<string, number>;
}

const PLATFORM_LABELS: Record<string, string> = {
  youtube: 'YouTube',
  twitch: 'Twitch',
  kick: 'Kick',
};

export default function CookieBridgeSection() {
  const [status, setStatus] = useState<BridgeStatus | null>(null);
  const [token, setToken] = useState('');
  const [extId, setExtId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const s = await apiGet<BridgeStatus>('/api/session/cookies/status');
      setStatus(s);
      setError(null);
    } catch {
      setError('Cookie Bridge API unreachable.');
    }
    try {
      const t = await apiGet<{ token: string }>('/api/session/cookies/token');
      setToken(t.token);
    } catch {
      /* keep last known */
    }
    try {
      const id = await apiGet<{ extension_id: string }>('/api/session/cookies/extension/id');
      setExtId(id.extension_id);
    } catch {
      setExtId(null);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const toggle = async () => {
    if (!status || busy) return;
    setBusy(true);
    setError(null);
    try {
      const res = await apiPost<{ enabled: boolean }>(
        status.enabled ? '/api/session/cookies/disable' : '/api/session/cookies/enable',
        {},
      );
      setStatus({ ...status, enabled: res.enabled });
    } catch {
      setError('Could not toggle Cookie Bridge — backend unreachable?');
    }
    setBusy(false);
  };

  const copyToken = async () => {
    try {
      await navigator.clipboard.writeText(token);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard unavailable (non-secure context) — user can select manually */
    }
  };

  const enabled = status?.enabled ?? true;
  const platforms = status?.platforms ?? {};

  return (
    <div className="flex flex-col gap-1.5 border-t-2 border-zinc-800 pt-3">
      <div className="flex items-center justify-between gap-2">
        <FieldCaption noWrap>Cookie Bridge</FieldCaption>
        <button
          type="button"
          onClick={() => void toggle()}
          disabled={!status || busy}
          className={`flex items-center gap-1 px-2 py-0.5 text-[10px] font-black uppercase border-2 transition-colors disabled:opacity-50 ${
            enabled
              ? 'bg-emerald-950 text-emerald-400 border-emerald-900 hover:border-emerald-500'
              : 'bg-red-950 text-red-400 border-red-900 hover:border-red-500'
          }`}
        >
          {busy ? <Loader2 size={11} className="animate-spin" /> : enabled ? <ShieldCheck size={11} /> : <ShieldOff size={11} />}
          {enabled ? 'Enabled' : 'Disabled'}
        </button>
      </div>

      <p className="text-[9px] text-zinc-600 font-mono leading-snug">
        Sends keep-listed session cookies (Kick auth_token, YouTube SID family, Twitch
        auth-token) from your browser to the local VOD.RIP app on 127.0.0.1 only.
        Nothing leaves this machine. Disabling blocks all cookie ingestion.
      </p>

      {error ? <p className="text-[9px] text-red-500 font-mono">{error}</p> : null}

      <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[9px] font-mono">
        <span className={status?.paired ? 'text-emerald-600' : 'text-zinc-600'}>
          {status?.paired ? '● paired' : '○ not paired'}
        </span>
        {Object.keys(platforms).length > 0 ? (
          Object.entries(platforms).map(([platform, count]) => (
            <span key={platform} className="text-zinc-500">
              {PLATFORM_LABELS[platform] ?? platform}: {count}
            </span>
          ))
        ) : (
          <span className="text-zinc-700">no cookies stored</span>
        )}
      </div>

      {token ? (
        <div className="flex items-center gap-2">
          <input
            type="text"
            readOnly
            value={token}
            onFocus={(e) => e.target.select()}
            className="flex-1 min-w-0 bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-1 px-2 text-[10px] focus:outline-none focus:border-white"
          />
          <button
            type="button"
            onClick={() => void copyToken()}
            className="bg-zinc-900 text-zinc-200 font-black uppercase px-2 py-1 text-[10px] border-2 border-zinc-600 hover:border-white hover:text-white flex items-center gap-1 shrink-0"
          >
            {copied ? <Check size={11} /> : <Copy size={11} />}
            {copied ? 'Copied' : 'Copy'}
          </button>
        </div>
      ) : null}

      <p className="text-[9px] text-zinc-600 font-mono leading-snug">
        One-time install (no admin):{' '}
        <span className="text-zinc-400">
          powershell -ExecutionPolicy Bypass -File scripts\install-cookie-bridge-policy.ps1
        </span>
        , then restart Chrome/Edge — the extension force-installs itself via policy.
        {extId ? <> Extension id: <span className="text-zinc-400">{extId}</span></> : null}
      </p>
    </div>
  );
}

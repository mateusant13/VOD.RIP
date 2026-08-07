import { useEffect, useState } from 'react';
import { AlertTriangle, KeyRound } from 'lucide-react';
import { apiGet } from '../hooks/useApiClient';
import FieldCaption from './FieldCaption';
import { useI18n } from '../i18n';
import type { AppSettings } from '../types';

/**
 * Official API credentials (issue #4).
 *
 * Twitch helix token: auto-lifted from the Cookie Bridge's `auth-token`
 * (zero manual steps for extension users); the paste field is the fallback
 * for non-extension users. Never clobbered by a stale cookie — the backend
 * only overwrites an empty field or one older than the cookie export.
 *
 * YouTube Data API key: opt-in official-API layer for captions + metadata +
 * search, quota-aware (degrades back to the unofficial path at 80% of the
 * daily 10k-unit quota — warned here).
 */

interface OfficialApisStatus {
  twitch_helix_token_set: boolean;
  youtube_api_key_set: boolean;
  youtube_quota_used: number;
  youtube_quota_limit: number;
  youtube_degraded: boolean;
}

type Props = {
  settings: AppSettings;
  setSettings: (s: AppSettings) => void;
};

export default function OfficialApisSection({ settings, setSettings }: Props) {
  const [status, setStatus] = useState<OfficialApisStatus | null>(null);
  const [showTwitchToken, setShowTwitchToken] = useState(false);
  const { t } = useI18n();

  useEffect(() => {
    let alive = true;
    void apiGet<OfficialApisStatus>('/api/settings/official-apis-status')
      .then((s) => {
        if (alive) setStatus(s);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [settings.twitch_helix_token, settings.youtube_data_api_key]);

  const degraded = status?.youtube_degraded ?? false;
  const usedPct = status && status.youtube_quota_limit > 0
    ? Math.round((status.youtube_quota_used / status.youtube_quota_limit) * 100)
    : 0;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-1.5">
        <FieldCaption
          noWrap
          info={t('Uses your Twitch auth-token (auto-lifted from the Cookie Bridge, or pasted) as the OAuth bearer for official Helix API calls — faster and more reliable metadata. Falls back to the public API automatically on any failure.')}
        >
          {t('Twitch Helix Token')}
        </FieldCaption>
        <div className="flex gap-2">
          <input
            type={showTwitchToken ? 'text' : 'password'}
            value={settings.twitch_helix_token ?? ''}
            onChange={(e) => setSettings({ ...settings, twitch_helix_token: e.target.value })}
            placeholder={t('auto-filled from Cookie Bridge — paste only if not using the extension')}
            aria-label="twitch helix token"
            autoComplete="off"
            className="flex-1 min-w-0 bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-2 px-2.5 text-xs focus:outline-none focus:border-white"
          />
          <button
            type="button"
            onClick={() => setShowTwitchToken((v) => !v)}
            title={showTwitchToken ? t('Hide') : t('Show')}
            className="bg-zinc-900 text-zinc-200 font-black uppercase px-3 py-2 text-[11px] border-2 border-zinc-600 hover:border-white hover:text-white shrink-0"
          >
            {showTwitchToken ? t('Hide') : t('Show')}
          </button>
        </div>
        <p className="text-[11px] text-zinc-400 font-mono leading-relaxed">
          {status?.twitch_helix_token_set
            ? t('● token configured — Helix is primary for Twitch metadata')
            : t('○ no token — using the public GQL path')}
        </p>
      </div>

      <div className="flex flex-col gap-1.5 border-t-2 border-zinc-800 pt-3">
        <FieldCaption
          noWrap
          info={t('When set, captions, video metadata and channel search go through the official YouTube Data API first (quota-aware, silent fallback to the current path). Historical chat always stays on the paced unofficial path. Get a key at console.cloud.google.com.')}
        >
          {t('YouTube Data API Key')}
        </FieldCaption>
        <input
          type="password"
          value={settings.youtube_data_api_key ?? ''}
          onChange={(e) => setSettings({ ...settings, youtube_data_api_key: e.target.value })}
          placeholder={t('AIza… — leave empty to keep the current path')}
          aria-label="youtube data api key"
          autoComplete="off"
          className="w-full bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-2 px-2.5 text-xs focus:outline-none focus:border-white"
        />
        <div className="flex items-center gap-1.5 flex-wrap">
          <KeyRound size={13} className="shrink-0 text-zinc-500" />
          <span className="text-[11px] font-mono text-zinc-400">
            {status?.youtube_api_key_set
              ? t('quota used {used}/{limit} units today ({pct}%)', { used: status.youtube_quota_used, limit: status.youtube_quota_limit, pct: usedPct })
              : t('○ no key — using the current unofficial path')}
          </span>
          {degraded ? (
            <span className="flex items-center gap-1 text-[11px] font-mono text-amber-400">
              <AlertTriangle size={13} className="shrink-0" />
              {t('quota degraded — official path paused until the daily counter resets')}
            </span>
          ) : null}
        </div>
      </div>
    </div>
  );
}

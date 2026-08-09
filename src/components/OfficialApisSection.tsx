import { useEffect, useState } from 'react';
import { apiGet } from '../hooks/useApiClient';
import FieldCaption from './FieldCaption';
import { useI18n } from '../i18n';
import { DEFAULT_TWITCH_CLIENT_ID } from '../twitchClip';
import type { AppSettings } from '../types';

/**
 * Official API credentials (issue #4).
 *
 * Twitch helix token: auto-lifted from the Cookie Bridge's `auth-token`
 * (zero manual steps for extension users); the paste field is the fallback
 * for non-extension users. Never clobbered by a stale cookie — the backend
 * only overwrites an empty field or one older than the cookie export.
 */

interface OfficialApisStatus {
  twitch_helix_token_set: boolean;
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
  }, [settings.twitch_helix_token]);

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
        <div className="flex flex-col gap-1.5">
          <FieldCaption
            noWrap
            info={t('OAuth app Client ID for the "Get Twitch token" button — register an app at dev.twitch.tv/console/apps with OAuth Redirect URL http://localhost:7897/twitch-oauth-callback, then paste its Client ID here.')}
          >
            {t('Twitch Client ID')}
          </FieldCaption>
          <div className="flex gap-2">
            <input
              type="text"
              value={settings.twitch_helix_client_id ?? ''}
              onChange={(e) => setSettings({ ...settings, twitch_helix_client_id: e.target.value })}
              placeholder={t('advanced: your own OAuth app — otherwise the built-in app is used')}
              aria-label="twitch helix client id"
              autoComplete="off"
              className="flex-1 min-w-0 bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-2 px-2.5 text-xs focus:outline-none focus:border-white"
            />
            <button
              type="button"
              onClick={async () => {
                const clientId = (settings.twitch_helix_client_id ?? '').trim()
                  || DEFAULT_TWITCH_CLIENT_ID;
                if (!clientId) {
                  // No app configured yet (dev builds) — send the user where
                  // they create one.
                  window.open('https://dev.twitch.tv/console/apps', '_blank', 'noopener');
                  return;
                }
                try {
                  const r = await fetch('/api/twitch/oauth-flow', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ client_id: clientId }),
                  });
                  if (!r.ok) throw new Error('HTTP ' + r.status);
                } catch (err) {
                  console.error('oauth flow start failed', err);
                }
              }}
              title={t('Runs the Twitch OAuth consent in a hidden window — the token is saved automatically when you approve.')}
              className="bg-zinc-900 text-zinc-200 font-black uppercase px-3 py-2 text-[11px] border-2 border-zinc-600 hover:border-white hover:text-white shrink-0"
            >
              {t('Get Twitch token')}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

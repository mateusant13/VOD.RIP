import { useCallback, useState, type Dispatch, type SetStateAction } from 'react';
import { Loader2 } from 'lucide-react';
import FieldCaption from './FieldCaption';
import Toggle from './Toggle';
import { apiPost } from '../hooks/useApiClient';
import { useI18n } from '../i18n';
import type { AppSettings } from '../types';

/**
 * Transcription (ASR) settings: engine (parakeet default / whisper), model
 * id, subtitles-first, captions language. The model CACHE lives in
 * DiskSection (own disk picker, same disk-choice rule as cache/data drives).
 * The inline Save persists only these fields (backend applies the rest).
 */
type Props = {
  settings: AppSettings;
  setSettings: Dispatch<SetStateAction<AppSettings>>;
  /** Called with the settings returned by the backend after an inline save. */
  onSaved?: (updated: AppSettings) => void;
};

export default function TranscriptionSection({ settings, setSettings, onSaved }: Props) {
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const { t } = useI18n();

  const engine = (settings.asr_engine ?? '').trim() || 'parakeet';
  // whisper_model is the faster-whisper id (large-v3-turbo default) — the
  // engine selector, not this field, decides parakeet vs whisper.
  const whisperModel = (settings.whisper_model ?? '').trim() || 'large-v3-turbo';

  const onSave = useCallback(async () => {
    setSaving(true);
    setMsg(null);
    try {
      const updated = await apiPost<AppSettings>('/api/settings', {
        whisper_model: whisperModel,
        asr_engine: engine,
        yt_subtitles_first: settings.yt_subtitles_first ?? true,
        asr_language: settings.asr_language ?? 'auto',
        channel_asr_languages: settings.channel_asr_languages ?? null,
      });
      setSettings(updated);
      setMsg('saved');
      onSaved?.(updated);
    } catch (err) {
      setMsg(err instanceof Error ? err.message : 'save failed');
    } finally {
      setSaving(false);
    }
  }, [whisperModel, engine, settings.yt_subtitles_first, settings.asr_language, settings.channel_asr_languages, setSettings, onSaved]);

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-1.5">
        <FieldCaption
          noWrap
          info={t('Parakeet is the default ASR. Whisper large-v3-turbo is downloaded and used only when Parakeet itself fails (unsupported language such as ja/ko/zh/ar, or a Parakeet engine error).')}
        >
          {t('ASR engine')}
        </FieldCaption>
        <select
          value={engine}
          onChange={(e) => setSettings({ ...settings, asr_engine: e.target.value })}
          aria-label="ASR engine"
          className="w-full bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-2 px-2.5 text-xs focus:outline-none focus:border-white"
        >
          <option value="parakeet">{t('Parakeet (default)')}</option>
          <option value="whisper">{t('Whisper large-v3-turbo')}</option>
        </select>
        <p className="text-[10px] font-mono text-zinc-500 leading-relaxed">
          {t('Parakeet is default. Whisper large-v3-turbo runs only if Parakeet fails on that job (Parakeet error or language Parakeet cannot do: ja/ko/zh/ar). Other errors do not trigger Whisper.')}
        </p>
        <span
          className="w-full bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-2 px-2.5 text-xs"
          aria-label="whisper model id (read-only)"
        >
          {engine === 'parakeet' ? t('Parakeet (default)') : whisperModel}
        </span>
      </div>
      <Toggle
        label={t('YouTube subtitles first')}
        info={t('Fallback to Whisper when subtitles are unavailable')}
        checked={settings.yt_subtitles_first ?? true}
        onChange={(c) => setSettings({ ...settings, yt_subtitles_first: c })}
        ariaLabel="use youtube subtitles first"
      />
      <div className="flex flex-col gap-1.5">
        <FieldCaption
          noWrap
          info={t('Default ASR language for Whisper jobs. Per-channel languages are auto-learned from transcript evidence (backend services/channel_language.py) — override only if a channel is consistently misdetected.')}
        >
          {t('Captions Language')}
        </FieldCaption>
        <div className="flex items-center gap-1.5">
          <select
            value={settings.asr_language ?? 'auto'}
            onChange={(e) => setSettings({ ...settings, asr_language: e.target.value })}
            aria-label="default captions language"
            className="flex-1 min-w-0 bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-2 px-2.5 text-xs focus:outline-none focus:border-white"
          >
            <option value="auto">{t('Auto-detect')}</option>
            <option value="pt">{t('Portuguese (pt)')}</option>
            <option value="en">{t('English (en)')}</option>
            <option value="es">{t('Spanish (es)')}</option>
          </select>
        </div>
      </div>
      <div className="flex items-center gap-2 flex-wrap">
        <button
          type="button"
          onClick={() => void onSave()}
          disabled={saving}
          className="bg-zinc-900 text-zinc-200 font-black uppercase px-3 py-2 text-[11px] border-2 border-zinc-600 hover:border-white hover:text-white disabled:opacity-50 flex items-center gap-1.5"
        >
          {saving ? <Loader2 size={13} className="animate-spin" /> : null}
          {saving ? '...' : t('Save')}
        </button>
        <span className="text-[11px] text-zinc-400 font-mono">{t('active: {model}', { model: engine === 'parakeet' ? 'parakeet' : whisperModel })}</span>
        {msg ? <span className="text-[11px] text-emerald-500 font-mono">{msg === 'saved' ? t('saved') : t('save failed')}</span> : null}
      </div>
    </div>
  );
}

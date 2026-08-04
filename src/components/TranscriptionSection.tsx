import { useCallback, useState, type Dispatch, type SetStateAction } from 'react';
import { Loader2 } from 'lucide-react';
import FieldCaption from './FieldCaption';
import Toggle from './Toggle';
import { apiPost } from '../hooks/useApiClient';
import type { AppSettings } from '../types';

/**
 * Transcription (Whisper) settings: model id, cache dir, subtitles-first.
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
  const [channelOverridesText, setChannelOverridesText] = useState(() =>
    Object.entries(settings.channel_asr_languages ?? {})
      .map(([k, v]) => `${k} = ${v}`)
      .join('\n'),
  );

  const activeModel = (settings.whisper_model ?? '').trim() || 'large-v3-turbo';

  const onSave = useCallback(async () => {
    setSaving(true);
    setMsg(null);
    try {
      const updated = await apiPost<AppSettings>('/api/settings', {
        whisper_model: (settings.whisper_model ?? '').trim() || undefined,
        whisper_model_cache: (settings.whisper_model_cache ?? '').trim() || null,
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
  }, [settings.whisper_model, settings.whisper_model_cache, settings.yt_subtitles_first, settings.asr_language, settings.channel_asr_languages, setSettings, onSaved]);

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-col gap-1">
        <FieldCaption noWrap>Model</FieldCaption>
        <input
          type="text"
          value={settings.whisper_model ?? ''}
          onChange={(e) => setSettings({ ...settings, whisper_model: e.target.value })}
          placeholder="large-v3-turbo"
          aria-label="whisper model id"
          className="w-full bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-1.5 px-2 focus:outline-none focus:border-white text-xs"
        />
      </div>
      <div className="flex flex-col gap-1">
        <FieldCaption noWrap>Model Cache Directory</FieldCaption>
        <input
          type="text"
          value={settings.whisper_model_cache ?? ''}
          onChange={(e) => setSettings({ ...settings, whisper_model_cache: e.target.value })}
          placeholder="%APPDATA%/VOD.RIP/whisper-models"
          aria-label="whisper model cache directory"
          className="w-full bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-1.5 px-2 focus:outline-none focus:border-white text-xs"
        />
      </div>
      <Toggle
        label="YouTube subtitles first"
        hint="Fallback to Whisper when subtitles are unavailable"
        checked={settings.yt_subtitles_first ?? true}
        onChange={(c) => setSettings({ ...settings, yt_subtitles_first: c })}
        ariaLabel="use youtube subtitles first"
      />
      <div className="flex flex-col gap-1">
        <FieldCaption noWrap>Captions Language</FieldCaption>
        <div className="flex items-center gap-1.5">
          <select
            value={settings.asr_language ?? 'auto'}
            onChange={(e) => setSettings({ ...settings, asr_language: e.target.value })}
            aria-label="default captions language"
            className="flex-1 min-w-0 bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-1.5 px-2 text-xs focus:outline-none focus:border-white"
          >
            <option value="auto">Auto-detect</option>
            <option value="pt">Portuguese (pt)</option>
            <option value="en">English (en)</option>
            <option value="es">Spanish (es)</option>
          </select>
        </div>
        <span className="text-[9px] text-zinc-600 font-mono">
          default ASR language for Whisper jobs; per-channel overrides below win
        </span>
      </div>
      <div className="flex flex-col gap-1">
        <FieldCaption noWrap>Channel Overrides</FieldCaption>
        <textarea
          rows={3}
          value={channelOverridesText}
          onChange={(e) => {
            const parsed: Record<string, string> = {};
            for (const line of e.target.value.split('\n')) {
              const m = /^\s*([^=#]+?)\s*=\s*([a-zA-Z-]+)\s*$/.exec(line);
              if (m) parsed[m[1].trim().toLowerCase()] = m[2].toLowerCase();
            }
            setChannelOverridesText(e.target.value);
            setSettings({ ...settings, channel_asr_languages: Object.keys(parsed).length ? parsed : null });
          }}
          placeholder={'titiltei = pt\nxqc = en\ngaveta = pt'}
          aria-label="per-channel captions language overrides"
          className="w-full bg-zinc-950 border-2 border-zinc-800 text-white font-mono py-1.5 px-2 text-xs focus:outline-none focus:border-white resize-y"
        />
        <span className="text-[9px] text-zinc-600 font-mono">
          one per line: channel = pt|en|es|auto (overrides the default above)
        </span>
      </div>
      <div className="flex items-center gap-1.5">
        <button
          type="button"
          onClick={() => void onSave()}
          disabled={saving}
          className="bg-zinc-900 text-zinc-200 font-black uppercase px-2 py-1 text-[10px] border-2 border-zinc-600 hover:border-white hover:text-white disabled:opacity-50 flex items-center gap-1"
        >
          {saving ? <Loader2 size={10} className="animate-spin" /> : null}
          {saving ? '...' : 'Save'}
        </button>
        <span className="text-[9px] text-zinc-600 font-mono">active: {activeModel}</span>
        {msg ? <span className="text-[9px] text-emerald-700 font-mono">{msg}</span> : null}
      </div>
      <span className="text-[9px] text-zinc-600 font-mono">
        cache may point at a shared HF hub dir — already-downloaded models are reused without re-download
      </span>
    </div>
  );
}

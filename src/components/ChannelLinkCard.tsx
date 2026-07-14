import { useEffect, useId, useRef } from 'react';
import PlatformVodIcon from './PlatformVodIcon';
import {
  type ChannelLinkDraft,
  type ChannelLinkPlatform,
  channelLinkWillAddSummary,
  channelLinkDraftValid,
  normalizeChannelLinkSlug,
} from '../channelUtils';
import { KICK_COLOR, TWITCH_COLOR, YOUTUBE_COLOR, vodCheckboxStyle } from '../platformColors';

type PlatformKey = 'Kick' | 'Twitch' | 'YouTube';

const ROWS: {
  platform: PlatformKey;
  key: ChannelLinkPlatform;
  enabledKey: keyof Pick<ChannelLinkDraft, 'kickEnabled' | 'twitchEnabled' | 'youtubeEnabled'>;
  slugKey: keyof Pick<ChannelLinkDraft, 'kickSlug' | 'twitchSlug' | 'youtubeSlug'>;
  accent: string;
  focusBorder: string;
  placeholder: string;
}[] = [
  {
    platform: 'Kick',
    key: 'kick',
    enabledKey: 'kickEnabled',
    slugKey: 'kickSlug',
    accent: KICK_COLOR,
    focusBorder: 'focus:border-[#53fc18]',
    placeholder: 'channel name',
  },
  {
    platform: 'Twitch',
    key: 'twitch',
    enabledKey: 'twitchEnabled',
    slugKey: 'twitchSlug',
    accent: TWITCH_COLOR,
    focusBorder: 'focus:border-[#9146FF]',
    placeholder: 'channel name',
  },
  {
    platform: 'YouTube',
    key: 'youtube',
    enabledKey: 'youtubeEnabled',
    slugKey: 'youtubeSlug',
    accent: YOUTUBE_COLOR,
    focusBorder: 'focus:border-[#F03030]',
    placeholder: '@handle or channel id',
  },
];

type Props = {
  draft: ChannelLinkDraft;
  onChange: (draft: ChannelLinkDraft) => void;
  onConfirm: () => void;
  onCancel: () => void;
  duplicateMessage?: string | null;
  className?: string;
};

export default function ChannelLinkCard({
  draft,
  onChange,
  onConfirm,
  onCancel,
  duplicateMessage,
  className = '',
}: Props) {
  const titleId = useId();
  const firstInputRef = useRef<HTMLInputElement>(null);
  const summary = channelLinkWillAddSummary(draft);
  const canSubmit = channelLinkDraftValid(draft) && !duplicateMessage;

  const showGuessHint = draft.detectedFrom === 'youtube'
    && draft.kickEnabled
    && draft.twitchEnabled
    && draft.kickSlug.trim()
    && draft.twitchSlug.trim()
    && draft.kickSlug.trim() === draft.twitchSlug.trim();

  useEffect(() => {
    firstInputRef.current?.focus();
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onCancel();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onCancel]);

  const patchRow = (
    _row: typeof ROWS[number],
    patch: Partial<Pick<ChannelLinkDraft, 'kickEnabled' | 'twitchEnabled' | 'youtubeEnabled' | 'kickSlug' | 'twitchSlug' | 'youtubeSlug'>>,
  ) => onChange({ ...draft, ...patch });

  return (
    <div
      role="dialog"
      aria-labelledby={titleId}
      className={`border border-zinc-700 bg-zinc-900/95 p-2.5 flex flex-col gap-2 ${className}`}
    >
      <div>
        <p id={titleId} className="text-[11px] font-black uppercase text-white tracking-wide">
          Link channel
        </p>
        <p className="text-[10px] font-mono text-zinc-500 leading-snug mt-0.5">
          Usernames can be different on each platform. Edit them before adding.
        </p>
        {draft.detectedFrom ? (
          <p className="text-[9px] font-mono text-zinc-600 mt-1">
            Detected from {draft.detectedFrom === 'kick' ? 'Kick' : draft.detectedFrom === 'twitch' ? 'Twitch' : 'YouTube'}
          </p>
        ) : null}
        {showGuessHint ? (
          <p className="text-[9px] font-mono text-zinc-600 mt-0.5">
            Kick and Twitch were prefilled from the YouTube handle — edit or turn off if different.
          </p>
        ) : null}
      </div>

      <div className="flex flex-col gap-1.5">
        {ROWS.map((row, index) => {
          const enabled = draft[row.enabledKey];
          const slug = draft[row.slugKey];
          const isDetected = draft.detectedFrom === row.key;
          return (
            <div
              key={row.platform}
              className={`flex items-center gap-2 min-w-0 transition-opacity ${enabled ? '' : 'opacity-50'}`}
            >
              <label className="flex items-center gap-1.5 shrink-0 cursor-pointer">
                <input
                  type="checkbox"
                  checked={enabled}
                  onChange={(e) => patchRow(row, { [row.enabledKey]: e.target.checked })}
                  className="vod-cb-sm"
                  style={vodCheckboxStyle(row.accent)}
                />
                <PlatformVodIcon platform={row.platform} className="w-3.5 h-3.5" />
                <span className="text-[9px] font-mono uppercase text-zinc-400 w-12">{row.platform}</span>
              </label>
              <input
                ref={index === 0 ? firstInputRef : undefined}
                type="text"
                value={slug}
                disabled={!enabled}
                placeholder={row.placeholder}
                onChange={(e) => patchRow(row, { [row.slugKey]: e.target.value, [row.enabledKey]: true })}
                onFocus={() => {
                  if (!enabled) patchRow(row, { [row.enabledKey]: true });
                }}
                onBlur={(e) => {
                  if (!enabled) return;
                  const normalized = normalizeChannelLinkSlug(row.key, e.target.value);
                  if (normalized !== slug) patchRow(row, { [row.slugKey]: normalized });
                }}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && canSubmit) {
                    e.preventDefault();
                    onConfirm();
                  }
                }}
                className={`flex-1 min-w-0 bg-zinc-950 border border-zinc-800 text-white font-mono px-2 py-1 text-[10px] disabled:text-zinc-600 disabled:cursor-not-allowed focus:outline-none ${row.focusBorder}`}
              />
              {isDetected && enabled ? (
                <span className="text-[8px] font-mono text-zinc-600 shrink-0 hidden sm:inline">from URL</span>
              ) : (
                <span className="text-[8px] font-mono text-zinc-700 shrink-0 hidden sm:inline w-12" aria-hidden />
              )}
            </div>
          );
        })}
      </div>

      {summary ? (
        <p className="text-[9px] font-mono text-zinc-500">
          Will add: <span className="text-zinc-300">{summary}</span>
        </p>
      ) : (
        <p className="text-[9px] font-mono text-zinc-600">Select at least one platform with a username.</p>
      )}

      {duplicateMessage ? (
        <p className="text-[9px] font-mono text-amber-400">{duplicateMessage}</p>
      ) : null}

      <div className="flex gap-2 pt-0.5">
        <button
          type="button"
          onClick={onConfirm}
          disabled={!canSubmit}
          className="flex-1 bg-white text-black font-black uppercase py-1.5 text-[10px] border-2 border-white disabled:opacity-40"
        >
          Add channel
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="flex-1 border border-zinc-600 text-zinc-400 font-mono uppercase py-1.5 text-[10px] hover:text-white hover:border-zinc-500"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

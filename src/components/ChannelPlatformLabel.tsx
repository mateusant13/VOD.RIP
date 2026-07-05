/** Channel row label: per-platform logo + slug, hover to unlink one platform. */

import { useEffect, useRef, useState } from 'react';
import { X } from 'lucide-react';
import PlatformVodIcon from './PlatformVodIcon';

type Platform = 'Kick' | 'Twitch' | 'YouTube';

const PLATFORM_REMOVE_DELAY_MS = 700;

function PlatformChip({
  platform,
  slug,
  onRemove,
}: {
  platform: Platform;
  slug: string;
  onRemove?: () => void;
}) {
  const [removeReady, setRemoveReady] = useState(false);
  const hoverTimerRef = useRef<number | null>(null);

  useEffect(() => () => {
    if (hoverTimerRef.current != null) {
      window.clearTimeout(hoverTimerRef.current);
    }
  }, []);

  if (!slug.trim()) return null;

  const onChipEnter = () => {
    if (!onRemove) return;
    if (hoverTimerRef.current != null) window.clearTimeout(hoverTimerRef.current);
    hoverTimerRef.current = window.setTimeout(() => {
      setRemoveReady(true);
      hoverTimerRef.current = null;
    }, PLATFORM_REMOVE_DELAY_MS);
  };

  const onChipLeave = () => {
    if (hoverTimerRef.current != null) {
      window.clearTimeout(hoverTimerRef.current);
      hoverTimerRef.current = null;
    }
    setRemoveReady(false);
  };

  return (
    <span
      className="group/chip relative inline-flex items-center gap-0 shrink-0"
      onMouseEnter={onChipEnter}
      onMouseLeave={onChipLeave}
    >
      <PlatformVodIcon platform={platform} />
      <span className="whitespace-nowrap relative">
        {slug}
        {onRemove && (
          <button
            type="button"
            title={removeReady ? `Remove ${platform} from channel` : undefined}
            disabled={!removeReady}
            onClick={(e) => {
              e.stopPropagation();
              if (!removeReady) return;
              onRemove();
            }}
            className={`absolute right-0 top-1/2 -translate-y-1/2 p-0.5 bg-zinc-950/90 border border-zinc-600 rounded-sm text-zinc-400 transition-opacity z-10 ${
              removeReady
                ? 'opacity-100 pointer-events-auto hover:text-red-400 hover:border-red-500/60 cursor-pointer'
                : 'opacity-0 group-hover/chip:opacity-100 pointer-events-none cursor-default'
            }`}
          >
            <X size={8} />
          </button>
        )}
      </span>
    </span>
  );
}

export default function ChannelPlatformLabel({
  kickSlug,
  twitchSlug,
  youtubeSlug,
  onRemoveKick,
  onRemoveTwitch,
  onRemoveYoutube,
}: {
  kickSlug: string;
  twitchSlug: string;
  youtubeSlug: string;
  onRemoveKick?: () => void;
  onRemoveTwitch?: () => void;
  onRemoveYoutube?: () => void;
}) {
  const kick = kickSlug.trim();
  const twitch = twitchSlug.trim();
  const youtube = youtubeSlug.trim();
  const chips: { platform: Platform; slug: string; onRemove?: () => void }[] = [];
  if (twitch) chips.push({ platform: 'Twitch', slug: twitch, onRemove: onRemoveTwitch });
  if (kick) chips.push({ platform: 'Kick', slug: kick, onRemove: onRemoveKick });
  if (youtube) chips.push({ platform: 'YouTube', slug: youtube, onRemove: onRemoveYoutube });

  if (!chips.length) {
    return <span className="text-zinc-500 italic">empty</span>;
  }

  return (
    <span className="inline-flex items-center gap-0 overflow-visible">
      {chips.map((chip, i) => (
        <span key={chip.platform} className="inline-flex items-center gap-0">
          {i > 0 && <span className="text-zinc-600 shrink-0 mx-0.5">/</span>}
          <PlatformChip platform={chip.platform} slug={chip.slug} onRemove={chip.onRemove} />
        </span>
      ))}
    </span>
  );
}

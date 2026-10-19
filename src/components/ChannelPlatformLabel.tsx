/** Channel row label: per-platform logo + name, hover a logo to unlink that platform. */

import { useEffect, useRef, useState } from 'react';
import { X } from 'lucide-react';
import PlatformVodIcon from './PlatformVodIcon';

type Platform = 'Kick' | 'Twitch' | 'YouTube';

const PLATFORM_REMOVE_DELAY_MS = 700;

function PlatformLogo({
  platform,
  onRemove,
}: {
  platform: Platform;
  onRemove?: () => void;
}) {
  const [removeReady, setRemoveReady] = useState(false);
  const hoverTimerRef = useRef<number | null>(null);

  useEffect(() => () => {
    if (hoverTimerRef.current != null) {
      window.clearTimeout(hoverTimerRef.current);
    }
  }, []);

  const onEnter = () => {
    if (!onRemove) return;
    if (hoverTimerRef.current != null) window.clearTimeout(hoverTimerRef.current);
    hoverTimerRef.current = window.setTimeout(() => {
      setRemoveReady(true);
      hoverTimerRef.current = null;
    }, PLATFORM_REMOVE_DELAY_MS);
  };

  const onLeave = () => {
    if (hoverTimerRef.current != null) {
      window.clearTimeout(hoverTimerRef.current);
      hoverTimerRef.current = null;
    }
    setRemoveReady(false);
  };

  return (
    <span
      className="group/chip relative inline-flex items-center shrink-0"
      onMouseEnter={onEnter}
      onMouseLeave={onLeave}
    >
      <PlatformVodIcon platform={platform} />
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
          className={`absolute -right-0.5 -top-0.5 p-0.5 bg-zinc-950/90 border border-zinc-600 rounded-sm text-zinc-400 transition-opacity z-10 ${
            removeReady
              ? 'opacity-100 pointer-events-auto hover:text-red-400 hover:border-red-500/60 cursor-pointer'
              : 'opacity-0 group-hover/chip:opacity-100 pointer-events-none cursor-default'
          }`}
        >
          <X size={8} />
        </button>
      )}
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

  // Collapse platforms that share an identical slug into ONE name token.
  // "titiltei / titiltei / titiltei" is a single creator on Kick+Twitch+YouTube,
  // not three duplicates — repeating the name reads as a bug. Distinct slugs
  // (lubu / lubumr) stay separate, each with its own platform logos.
  const groups: { slug: string; chips: typeof chips }[] = [];
  const seen = new Map<string, number>();
  for (const chip of chips) {
    const key = chip.slug.toLowerCase();
    const idx = seen.get(key);
    if (idx == null) {
      seen.set(key, groups.length);
      groups.push({ slug: chip.slug, chips: [chip] });
    } else {
      groups[idx].chips.push(chip);
    }
  }

  return (
    <span className="inline-flex items-center gap-0 overflow-visible">
      {groups.map((g, gi) => (
        <span key={g.slug.toLowerCase()} className="inline-flex items-center gap-0">
          {gi > 0 && <span className="text-zinc-600 shrink-0 mx-0.5">/</span>}
          {g.chips.map((c) => (
            <PlatformLogo key={c.platform} platform={c.platform} onRemove={c.onRemove} />
          ))}
          <span className="whitespace-nowrap">{g.slug}</span>
        </span>
      ))}
    </span>
  );
}

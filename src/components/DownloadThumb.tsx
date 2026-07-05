import { useState } from 'react';
import { Play } from 'lucide-react';
import { resolveVideoThumbnail } from '../channelUtils';

type Props = {
  thumbnail?: string | null;
  url: string;
  platform: string;
  className?: string;
  watchable?: boolean;
  onWatch?: () => void;
};

function youtubeThumbFromUrl(url: string): string | null {
  const m = url.match(/(?:[?&]v=|youtu\.be\/|\/shorts\/|\/live\/)([a-zA-Z0-9_-]{11})/);
  return m ? `https://i.ytimg.com/vi/${m[1]}/mqdefault.jpg` : null;
}

export default function DownloadThumb({
  thumbnail,
  url,
  platform,
  className = 'w-12 h-9',
  watchable = false,
  onWatch,
}: Props) {
  const [failed, setFailed] = useState(false);
  let src = resolveVideoThumbnail(thumbnail, 48, 36);
  if (!src && platform.toLowerCase() === 'youtube') {
    src = youtubeThumbFromUrl(url);
  }

  const thumbBody = !src || failed ? (
    <div
      className={`border border-zinc-800 bg-zinc-900 flex items-center justify-center w-full h-full ${watchable ? '' : ''}`}
      aria-hidden
    >
      <Play size={11} className={watchable ? 'text-white' : 'text-zinc-600'} />
    </div>
  ) : (
    <img
      src={src}
      alt=""
      loading="lazy"
      decoding="async"
      onError={() => setFailed(true)}
      className="w-full h-full object-cover border border-zinc-800 bg-zinc-900"
    />
  );

  if (watchable && onWatch) {
    return (
      <button
        type="button"
        title="Play downloaded file"
        onClick={onWatch}
        className={`relative shrink-0 overflow-hidden group cursor-pointer ${className}`}
      >
        {thumbBody}
        <span className="absolute inset-0 flex items-center justify-center bg-black/45 group-hover:bg-black/55 transition-colors">
          <Play size={14} className="text-white drop-shadow-md" fill="currentColor" />
        </span>
      </button>
    );
  }

  if (!src || failed) {
    return (
      <div className={`shrink-0 ${className}`} aria-hidden>
        {thumbBody}
      </div>
    );
  }
  return (
    <img
      src={src}
      alt=""
      loading="lazy"
      decoding="async"
      onError={() => setFailed(true)}
      className={`shrink-0 object-cover border border-zinc-800 bg-zinc-900 ${className}`}
    />
  );
}

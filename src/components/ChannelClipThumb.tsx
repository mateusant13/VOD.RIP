/** ponytail: extracted from App.tsx inline helper. Clip thumbnail with load-failed fallback. */

import { useState, type ReactNode } from 'react';
import { Play } from 'lucide-react';
import { resolveVideoThumbnail } from '../channelUtils';

import type { ChannelVideo } from '../types';

export default function ChannelClipThumb({ video }: { video: ChannelVideo }): ReactNode {
  const [failed, setFailed] = useState(false);
  let src = resolveVideoThumbnail(video.thumbnail_url);
  if (!src && video.platform === 'YouTube' && video.id) {
    src = `https://i.ytimg.com/vi/${video.id}/mqdefault.jpg`;
  }
  if (!src || failed) {
    return (
      <div
        className="shrink-0 w-16 h-9 border border-zinc-800 bg-zinc-900 flex items-center justify-center"
        aria-hidden
      >
        <Play size={11} className="text-zinc-600" />
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
      className="shrink-0 w-16 h-9 object-cover border border-zinc-800 bg-zinc-900"
    />
  );
}

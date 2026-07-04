/** ponytail: extracted from App.tsx inline helper. Clip thumbnail with load-failed fallback. */

import { useState, type ReactNode } from 'react';
import { Play } from 'lucide-react';

interface ChannelVideo {
  id: string;
  platform: string;
  title: string;
  duration: number | null;
  duration_string?: string | null;
  created_at: string | null;
  views: number | null;
  thumbnail_url: string | null;
  url: string;
  channel: string;
  content_kind?: 'vod' | 'clip';
}

function resolveChannelThumbnail(
  url: string | null | undefined,
  width = 160,
  height = 90,
): string | null {
  if (!url?.trim()) return null;
  const w = String(width);
  const h = String(height);
  return url
    .replace(/%\{width\}/g, w)
    .replace(/%\{height\}/g, h)
    .replace(/\{width\}/g, w)
    .replace(/\{height\}/g, h);
}

export default function ChannelClipThumb({ video }: { video: ChannelVideo }): ReactNode {
  const [failed, setFailed] = useState(false);
  const src = resolveChannelThumbnail(video.thumbnail_url);
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

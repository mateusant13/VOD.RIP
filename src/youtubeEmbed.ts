export function youtubeVideoIdFromUrl(raw: string): string {
  const trimmed = raw.trim();
  if (!trimmed) return '';
  try {
    const u = new URL(trimmed);
    const host = u.hostname.toLowerCase().replace(/^www\./, '');
    if (host === 'youtu.be') return u.pathname.split('/').filter(Boolean)[0] || '';
    if (!host.endsWith('youtube.com')) return '';
    if (u.pathname === '/watch') return u.searchParams.get('v') || '';
    const parts = u.pathname.split('/').filter(Boolean);
    if (['shorts', 'embed', 'live'].includes(parts[0] || '')) return parts[1] || '';
  } catch {
    const m = trimmed.match(/(?:v=|youtu\.be\/|shorts\/|embed\/|live\/)([A-Za-z0-9_-]{6,})/);
    return m?.[1] || '';
  }
  return '';
}

export function youtubeEmbedUrl(raw: string, startSec = 0): string {
  const id = youtubeVideoIdFromUrl(raw);
  if (!id) return '';
  const origin = typeof window !== 'undefined' ? window.location.origin : '';
  const params = new URLSearchParams({
    autoplay: '1',
    mute: '1',
    controls: '0',
    disablekb: '1',
    enablejsapi: '1',
    fs: '0',
    iv_load_policy: '3',
    playsinline: '1',
    rel: '0',
    modestbranding: '1',
    start: String(Math.max(0, Math.floor(startSec))),
  });
  if (origin.startsWith('http://') || origin.startsWith('https://')) params.set('origin', origin);
  return `https://www.youtube.com/embed/${id}?${params.toString()}`;
}

export function youtubeIframeCommand(iframe: HTMLIFrameElement | null, func: string, args: unknown[] = []): void {
  iframe?.contentWindow?.postMessage(JSON.stringify({ event: 'command', func, args }), 'https://www.youtube.com');
}

export function youtubeIframeListen(iframe: HTMLIFrameElement | null): void {
  // Required for reliable infoDelivery currentTime/playerState messages.
  iframe?.contentWindow?.postMessage(JSON.stringify({ event: 'listening', id: 'vodrip-youtube-preview' }), 'https://www.youtube.com');
}

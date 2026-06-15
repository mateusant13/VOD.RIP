/**
 * Formatter utilities extracted from App.tsx — time, date, bytes, and views formatting.
 */

export function fmtDuration(sec: number): string {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const pad = (n: number) => n.toString().padStart(2, '0');
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

export function fmtShort(sec: number): string {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

export function fmtClipDuration(sec: number): string {
  return `${Math.max(0, Math.floor(sec))}s`;
}

export function formatClipDurationHuman(sec: number): string {
  sec = Math.max(1, Math.floor(sec));
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  if (m > 0) return `${m}:${s.toString().padStart(2, '0')}`;
  return `${s}s`;
}

/**
 * Normalize a VOD's `created_at` for display. Backend returns either an ISO
 * string (Kick) or YYYYMMDD (Twitch) — normalize to YYYY-MM-DD and drop
 * anything we can't parse.
 */
export function normalizeVideoDateInput(value: string): string {
  const raw = value.trim();
  // Kick API: "YYYY-MM-DD HH:MM:SS"
  if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(raw)) {
    return `${raw.replace(' ', 'T')}Z`;
  }
  // ISO without timezone
  if (/^\d{4}-\d{2}-\d{2}T/.test(raw) && !/[zZ]|[+-]\d{2}:?\d{2}$/.test(raw)) {
    return `${raw}Z`;
  }
  return raw;
}

export function fmtDate(value: string | null | undefined): string {
  if (!value) return '';
  const raw = String(value).trim();
  if (!raw) return '';
  // Twitch yt-dlp: YYYYMMDD
  if (/^\d{8}$/.test(raw)) {
    return `${raw.slice(0, 4)}-${raw.slice(4, 6)}-${raw.slice(6, 8)}`;
  }
  const m = raw.match(/^(\d{4}-\d{2}-\d{2})/);
  return m ? m[1] : '';
}

export function fmtRelativeAgo(value: string | null | undefined): string {
  const ts = parseVideoTs(value);
  if (!ts) return '';
  const diffMs = Math.max(0, Date.now() - ts);
  const hours = Math.floor(diffMs / (1000 * 60 * 60));
  const days = Math.floor(diffMs / (1000 * 60 * 60 * 24));
  if (days >= 1) return days === 1 ? '1 day ago' : `${days} days ago`;
  if (hours >= 1) return hours === 1 ? '1 hour ago' : `${hours} hours ago`;
  const mins = Math.floor(diffMs / (1000 * 60));
  if (mins >= 1) return mins === 1 ? '1 min ago' : `${mins} mins ago`;
  return 'just now';
}

export function fmtDateAndAgo(value: string | null | undefined): string {
  const date = fmtDate(value);
  const ago = fmtRelativeAgo(value);
  if (date && ago) return `${date} · ${ago}`;
  return date || ago;
}

export function parseVideoTs(value: string | null | undefined): number {
  if (!value) return 0;
  const raw = String(value).trim();
  if (!raw) return 0;
  if (/^\d{8}$/.test(raw)) {
    return new Date(`${raw.slice(0, 4)}-${raw.slice(4, 6)}-${raw.slice(6, 8)}T00:00:00Z`).getTime() || 0;
  }
  const t = Date.parse(normalizeVideoDateInput(raw));
  return Number.isNaN(t) ? 0 : t;
}

export function parseHmsDurationString(s: string): number | null {
  const parts = s.split(':').map(Number);
  if (parts.length === 3 && parts.every((n) => !Number.isNaN(n))) {
    return parts[0] * 3600 + parts[1] * 60 + parts[2];
  }
  if (parts.length === 2 && parts.every((n) => !Number.isNaN(n))) {
    return parts[0] * 60 + parts[1];
  }
  return null;
}

export function fmtViews(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, '')}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1).replace(/\.0$/, '')}k`;
  return String(n);
}

export function formatBytes(nbytes: number): string {
  if (!Number.isFinite(nbytes) || nbytes <= 0) return '—';
  const mb = nbytes / (1024 * 1024);
  return mb >= 1024 ? `${(mb / 1024).toFixed(2)} GB` : `${Math.max(1, Math.round(mb))} MB`;
}

export function basename(path: string): string {
  return path.split(/[/\\]/).pop() || path;
}

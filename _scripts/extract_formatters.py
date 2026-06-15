import re

with open("src/App.tsx", "r", encoding="utf-8") as f:
    content = f.read()

# 1. Add import for formatters after the existing formatHmsFull import
old_import = "import { formatHmsFull } from './utils';"
new_import = "import { formatHmsFull } from './utils';\nimport { fmtDuration, fmtShort, fmtClipDuration, formatClipDurationHuman, fmtDate, fmtRelativeAgo, fmtDateAndAgo, parseVideoTs, parseHmsDurationString, fmtViews, formatBytes, basename } from './formatters';"
content = content.replace(old_import, new_import, 1)

# 2. Remove formatter functions — they're between // ─── HELPERS ─── and the panel layout section
# We remove specific blocks by function name

funcs_to_remove = [
    # fmtDuration — standalone function
    "function fmtDuration(sec: number): string {\n  sec = Math.max(0, Math.floor(sec));\n  const h = Math.floor(sec / 3600);\n  const m = Math.floor((sec % 3600) / 60);\n  const s = sec % 60;\n  const pad = (n: number) => n.toString().padStart(2, '0');\n  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;\n}",
    # fmtShort — standalone
    "function fmtShort(sec: number): string {\n  sec = Math.max(0, Math.floor(sec));\n  const h = Math.floor(sec / 3600);\n  const m = Math.floor((sec % 3600) / 60);\n  return h > 0 ? `${h}h ${m}m` : `${m}m`;\n}",
    # fmtClipDuration — standalone
    "function fmtClipDuration(sec: number): string {\n  return `${Math.max(0, Math.floor(sec))}s`;\n}",
    # normalizeVideoDateInput with its doc comment
    "// Format a VOD's `created_at` for display. Backend returns either an ISO\n// string (Kick) or YYYYMMDD (Twitch) — normalize to YYYY-MM-DD and drop\n// anything we can't parse. Returns empty string when no date is present\n// so the row can hide the date cell.\nfunction normalizeVideoDateInput(value: string): string {\n  const raw = value.trim();\n  // Kick API: \"YYYY-MM-DD HH:MM:SS\"\n  if (/^\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2}$/.test(raw)) {\n    return `${raw.replace(' ', 'T')}Z`;\n  }\n  // ISO without timezone\n  if (/^\\d{4}-\\d{2}-\\d{2}T/.test(raw) && !/[zZ]|[+-]\\d{2}:?\\d{2}$/.test(raw)) {\n    return `${raw}Z`;\n  }\n  return raw;\n}",
    # fmtDate — standalone
    "function fmtDate(value: string | null | undefined): string {\n  if (!value) return '';\n  const raw = String(value).trim();\n  if (!raw) return '';\n  // Twitch yt-dlp: YYYYMMDD\n  if (/^\\d{8}$/.test(raw)) {\n    return `${raw.slice(0, 4)}-${raw.slice(4, 6)}-${raw.slice(6, 8)}`;\n  }\n  const m = raw.match(/^(\\d{4}-\\d{2}-\\d{2})/);\n  return m ? m[1] : '';\n}",
    # fmtRelativeAgo
    "function fmtRelativeAgo(value: string | null | undefined): string {\n  const ts = parseVideoTs(value);\n  if (!ts) return '';\n  const diffMs = Math.max(0, Date.now() - ts);\n  const hours = Math.floor(diffMs / (1000 * 60 * 60));\n  const days = Math.floor(diffMs / (1000 * 60 * 60 * 24));\n  if (days >= 1) return days === 1 ? '1 day ago' : `${days} days ago`;\n  if (hours >= 1) return hours === 1 ? '1 hour ago' : `${hours} hours ago`;\n  const mins = Math.floor(diffMs / (1000 * 60));\n  if (mins >= 1) return mins === 1 ? '1 min ago' : `${mins} mins ago`;\n  return 'just now';\n}",
    # fmtDateAndAgo
    "function fmtDateAndAgo(value: string | null | undefined): string {\n  const date = fmtDate(value);\n  const ago = fmtRelativeAgo(value);\n  if (date && ago) return `${date} · ${ago}`;\n  return date || ago;\n}",
    # blank line + formatClipDurationHuman
    "\n\nfunction formatClipDurationHuman(sec: number): string {\n  sec = Math.max(1, Math.floor(sec));\n  const m = Math.floor(sec / 60);\n  const s = sec % 60;\n  if (m > 0) return `${m}:${s.toString().padStart(2, '0')}`;\n  return `${s}s`;\n}",
    # basename
    "function basename(path: string): string {\n  return path.split(/[/\\\\]/).pop() || path;\n}",
    # parseVideoTs
    "function parseVideoTs(value: string | null | undefined): number {\n  if (!value) return 0;\n  const raw = String(value).trim();\n  if (!raw) return 0;\n  if (/^\\d{8}$/.test(raw)) {\n    return new Date(`${raw.slice(0, 4)}-${raw.slice(4, 6)}-${raw.slice(6, 8)}T00:00:00Z`).getTime() || 0;\n  }\n  const t = Date.parse(normalizeVideoDateInput(raw));\n  return Number.isNaN(t) ? 0 : t;\n}",
    # fmtViews
    "function fmtViews(n: number): string {\n  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1).replace(/\\.0$/, '')}M`;\n  if (n >= 1_000) return `${(n / 1_000).toFixed(1).replace(/\\.0$/, '')}k`;\n  return String(n);\n}",
    # formatBytes
    "function formatBytes(nbytes: number): string {\n  if (!Number.isFinite(nbytes) || nbytes <= 0) return '—';\n  const mb = nbytes / (1024 * 1024);\n  return mb >= 1024 ? `${(mb / 1024).toFixed(2)} GB` : `${Math.max(1, Math.round(mb))} MB`;\n}",
    # parseHmsDurationString
    "function parseHmsDurationString(s: string): number | null {\n  const parts = s.split(':').map(Number);\n  if (parts.length === 3 && parts.every((n) => !Number.isNaN(n))) {\n    return parts[0] * 3600 + parts[1] * 60 + parts[2];\n  }\n  if (parts.length === 2 && parts.every((n) => !Number.isNaN(n))) {\n    return parts[0] * 60 + parts[1];\n  }\n  return null;\n}",
]

removed = 0
for func in funcs_to_remove:
    if func in content:
        content = content.replace(func, "", 1)
        removed += 1
    else:
        # Try to find the first line of the function for debugging
        first_line = func.split('\n')[0][:60]
        print(f"  NOT FOUND: {first_line}...")

with open("src/App.tsx", "w", encoding="utf-8") as f:
    f.write(content)

print(f"\nDone! Removed {removed}/{len(funcs_to_remove)} functions. Added import for ./formatters")

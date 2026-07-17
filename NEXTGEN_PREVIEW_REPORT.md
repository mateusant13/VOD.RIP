# Next-Gen YouTube Preview Pipeline — Implementation Plan

**Target:** Upgrade window HLS preview from MPEG-TS segments → fMP4/CMAF + LL-HLS + MSE direct push

---

## Current Architecture (Baseline)

### Window HLS Mux Path
```
yt-dlp extract → InnerTube API → googlevideo URLs (DASH video + audio)
    → preview_service.py:_ensure_youtube_window_hls_mux()
        → ytdlp_hls.py:_mux_dash_window_to_hls()
            → ffmpeg -f hls -hls_time 4 -hls_segment_type mpegts
            → output: window_hls/seg_000.ts, seg_001.ts, ... + window.m3u8
    → hls.js loads master → proxies segments via /api/preview/hls/:sid/resource?id=window-seg-NNN
    → hls.js transmuxes TS → fMP4 in Web Worker → feeds MediaSource
```

### Key Files
| File | Function | Lines |
|------|----------|-------|
| `backend/services/ytdlp_hls.py` | `_mux_dash_window_to_hls()` | ~2500-2700 |
| `backend/services/preview_service.py` | `_ensure_youtube_window_hls_mux()`, `_build_youtube_window_hls_media_playlist()` | ~2070-2200, ~2500-2600 |
| `src/App.tsx` | `usePreviewPlayer` hook, Hls.js init | ~850-1200 |
| `backend/services/ytdlp_ffmpeg.py` | ffmpeg command builders | throughout |

---

## Improvement 1: fMP4 (CMAF) Segments Instead of MPEG-TS

### Why
- 30% smaller segments (no TS overhead)
- Native hls.js support via `hls.js fmp4` controller
- CMAF = single encode serves both HLS (fMP4) and DASH
- Faster transmux in hls.js (skips TS→fMP4 step)

### Changes Required

#### 1. ytdlp_hls.py — `_mux_dash_window_to_hls()`
```python
# CURRENT (mpegts):
ffmpeg_args = [
    "-f", "hls",
    "-hls_time", str(segment_sec),
    "-hls_segment_type", "mpegts",
    "-hls_flags", "independent_segments+program_date_time",
    ...
]

# NEW (fMP4/CMAF):
ffmpeg_args = [
    "-f", "hls",
    "-hls_time", str(segment_sec),
    "-hls_segment_type", "fmp4",
    "-hls_flags", "independent_segments+program_date_time+omit_endlist",
    "-hls_fmp4_init_filename", "init.mp4",
    "-hls_segment_filename", "seg_%03d.m4s",
    ...
]
```

#### 2. preview_service.py — `_build_youtube_window_hls_media_playlist()`
```python
# Add fMP4 init segment reference:
lines.append(f"#EXT-X-MAP:URI=\"{base}init.mp4\"")
# Segment extensions change from .ts → .m4s
```

#### 3. open_youtube_window_hls_proxy() — serve init.mp4 + .m4s files
```python
# New resource IDs:
WINDOW_HLS_INIT_RESOURCE = "window-init"
WINDOW_HLS_SEGMENT_RESOURCE_PREFIX = "window-seg-"

# Serve init.mp4 with proper MIME:
content_type = "video/iso.segment"  # or video/mp4 for init
```

---

## Improvement 2: LL-HLS (Low-Latency HLS)

### Why
- Sub-second glass-to-glass latency
- `EXT-X-PRELOAD-HINT` + `EXT-X-PART` for partial segments
- `EXT-X-SERVER-CONTROL` for playlist reload hints
- Native Safari/iOS support; hls.js supports since v1.3+

### Playlist Additions
```m3u8
#EXTM3U
#EXT-X-VERSION:9
#EXT-X-TARGETDURATION:4
#EXT-X-PART-INF:PART-TARGET=0.5
#EXT-X-SERVER-CONTROL:CAN-BLOCK-RELOAD=YES,CAN-SKIP-UNTIL=60,HOLD-BACK=2.0
#EXT-X-PRELOAD-HINT:TYPE=PART,URI="seg_000_part_000.m4s"
#EXTINF:4.0,
seg_000.m4s
#EXT-X-PART:DURATION=0.5,URI="seg_000_part_001.m4s",INDEPENDENT=YES
#EXTINF:4.0,
seg_001.m4s
#EXT-X-PART:DURATION=0.5,URI="seg_001_part_000.m4s",INDEPENDENT=YES
#EXT-X-ENDLIST
```

### ffmpeg LL-HLS Args
```python
ffmpeg_args += [
    "-hls_flags", "independent_segments+program_date_time+omit_endlist+append_list",
    "-hls_part_type", "fmp4",
    "-hls_part_size", "500000",  # ~0.5s parts at 1Mbps
    "-hls_target_duration", "4",
]
```

---

## Improvement 3: MSE Direct fMP4 Push (Bypass hls.js)

### Why
- Eliminate hls.js overhead (Web Worker, transmux, buffer management)
- Direct control over segment append timing
- Lower latency (no hls.js internal buffers)
- Simpler code path for window HLS (known segment durations, no ABR)

### Architecture
```
Window HLS mux (fMP4) → /api/preview/hls/:sid/resource?id=window-init  (init segment)
                       → /api/preview/hls/:sid/resource?id=window-seg-NNN (media segments)

Frontend:
  const ms = new MediaSource()
  video.src = URL.createObjectURL(ms)
  ms.addEventListener('sourceopen', () => {
    const sb = ms.addSourceBuffer('video/mp4; codecs="avc1.4d401e,mp4a.40.2"')
    sb.appendBuffer(initSegment)
    // On seek/timeupdate: fetch next segment, sb.appendBuffer(segment)
  })
```

### Implementation: `src/hooks/useDirectMSEPlayer.ts` (new)
```typescript
export function useDirectMSEPlayer(sessionId: string, videoRef: HTMLVideoElement) {
  const [sourceBuffer, setSourceBuffer] = useState<SourceBuffer>()
  const [mediaSource, setMediaSource] = useState<MediaSource>()
  const pendingSegments = useRef<Uint8Array[]>([])
  const appending = useRef(false)

  // 1. Fetch init segment → append to SourceBuffer
  // 2. On timeupdate, fetch next segment → appendBuffer
  // 3. Handle seeking: abort(), remove(), fetch new init+segments
  // 4. Buffer management: evict old segments (ms.duration - 30s)
}
```

### Trade-off
- **Loses ABR** — window HLS is single-quality anyway
- **Gains**: ~100-200ms lower latency, simpler code, no hls.js dep for preview

---

## Improvement 4: QUIC/HTTP3 for googlevideo CDN

### Why
- googlevideo.com supports HTTP/3 (QUIC)
- 0-RTT resumption, better head-of-line blocking
- curl_cffi supports `impersonate="chrome"` with HTTP/3

### Changes
```python
# ytdlp_hls.py / preview_service.py _open_upstream_stream()
# Add http_version="3" to curl_cffi request:
resp = cffi_requests.get(
    url,
    headers=headers,
    impersonate="chrome",
    http_version="3",  # or curl.CURL_HTTP_VERSION_3
    stream=True,
    timeout=...
)
```

**Fallback:** If QUIC fails, curl_cffi falls back to HTTP/2 → HTTP/1.1 automatically.

---

## Improvement 5: Predictive Prefetch

### Why
- Window HLS segments are fixed 4s duration
- Current playback position → next segment is deterministic
- Prefetch N+1, N+2 while playing N

### Backend: Add `next_segment` hint to playlist
```python
# In _build_youtube_window_hls_media_playlist():
# Add custom tag for client prefetch:
lines.append(f"#EXT-X-VODRIP-NEXT-SEG:{next_seg_index}")
```

### Frontend: `usePreviewPlayer` hook enhancement
```typescript
// In onTimeUpdate:
const nextSeg = Math.floor(video.currentTime / SEGMENT_DURATION) + 1
if (nextSeg > lastPrefetched) {
  prefetchSegment(sessionId, nextSeg)  // fire-and-forget fetch
  prefetchSegment(sessionId, nextSeg + 1)
}
```

---

## Implementation Priority & Effort

| # | Improvement | Effort | Impact | Risk |
|---|-------------|--------|--------|------|
| 1 | fMP4 segments | Low (ffmpeg flags) | High (30% smaller, faster) | Low |
| 2 | LL-HLS playlist tags | Medium | High (sub-sec latency) | Medium (player compat) |
| 3 | MSE direct push | High | High (simpler, lower latency) | High (new code path) |
| 4 | QUIC/HTTP3 | Low | Medium (better throughput) | Low |
| 5 | Predictive prefetch | Medium | Medium (smoother seek) | Low |

---

## Test Plan

| Test | File |
|------|------|
| fMP4 segment validation (ftyp+moof+mdat) | `test_youtube_window_hls.py` |
| LL-HLS playlist contains PART/PRELOAD tags | `test_youtube_window_hls.py` |
| MSE direct player loads + seeks | `e2e/preview-mse-direct.spec.ts` |
| QUIC fallback | `test_youtube_preview_speed_real.py` |
| Prefetch reduces seek latency | `test_youtube_concurrent_preview_seek_real.py` |

---

## Rollback Strategy

Each improvement is feature-flagged:
```python
# ytdlp_hls.py
USE_FMP4 = os.getenv("VODRIP_PREVIEW_FMP4", "1") == "1"
USE_LLHLS = os.getenv("VODRIP_PREVIEW_LLHLS", "1") == "1"
```

Frontend:
```typescript
const USE_MSE_DIRECT = import.meta.env.VITE_PREVIEW_MSE_DIRECT === "true"
```

---

## Files to Modify (Summary)

### Backend
1. `backend/services/ytdlp_hls.py` — `_mux_dash_window_to_hls()` ffmpeg args
2. `backend/services/preview_service.py` — playlist builder, resource registry, proxy
3. `backend/services/ytdlp_ffmpeg.py` — any shared ffmpeg helpers

### Frontend
4. `src/hooks/useDirectMSEPlayer.ts` (new) — MSE direct player
5. `src/App.tsx` — integrate MSE player for window HLS
6. `src/hooks/usePreviewPlayer.ts` — add prefetch logic

### Tests
7. `backend/tests/test_youtube_window_hls.py` — validate fMP4, LL-HLS
8. `e2e/preview-mse-direct.spec.ts` — MSE player e2e

---

*Generated: 2026-07-17*
*Branch: feat/nextgen-preview-pipeline*
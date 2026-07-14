"""Download size estimates from format metadata, HLS bandwidth, or HEAD probes."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests

logger = logging.getLogger(__name__)

# Total Mbps (video+audio) when no metadata — tuned for HLS remux (not peak BANDWIDTH).
_FALLBACK_MBPS: Dict[str, float] = {
    "source": 4.5,
    "1080p60": 5.5,
    "1080p": 4.2,
    "720p60": 3.0,
    "720p": 2.4,
    "480p": 1.4,
    "360p": 0.8,
    "240p": 0.4,
}

# Peak HLS BANDWIDTH (no AVERAGE-BANDWIDTH) overstates remux size ~1.5–1.7×.
_PEAK_BANDWIDTH_TO_AVG = 0.62
# AVERAGE-BANDWIDTH on Twitch/Kick manifests overstates remux size ~1.1–1.3×
# after accounting for container overhead; peak BANDWIDTH needs a separate scale.
_MANIFEST_TO_REMUX_SCALE = 0.55

_DEFAULT_AUDIO_KBPS = 160.0


def _clen_bytes_from_url(url: str) -> Optional[int]:
    """Parse ``clen=`` from googlevideo videoplayback URLs (exact file size)."""
    if not url:
        return None
    m = re.search(r"[?&]clen=(\d+)", url, re.I)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _is_googlevideo_url(url: str) -> bool:
    return "googlevideo.com" in (url or "").lower()


def _label_from_height(height: int, fps: Optional[float] = None) -> str:
    fps_suffix = "60" if fps and float(fps) > 30 else ""
    return f"{int(height)}p{fps_suffix}"


def _fallback_mbps(quality: str) -> float:
    q = (quality or "source").strip().lower()
    if q in _FALLBACK_MBPS:
        return _FALLBACK_MBPS[q]
    m = re.search(r"(\d+)", q)
    if not m:
        return _FALLBACK_MBPS["720p"]
    h = int(m.group(1))
    if h >= 1080:
        return _FALLBACK_MBPS["1080p60"] if "60" in q else _FALLBACK_MBPS["1080p"]
    if h >= 720:
        return _FALLBACK_MBPS["720p60"] if "60" in q else _FALLBACK_MBPS["720p"]
    if h >= 480:
        return _FALLBACK_MBPS["480p"]
    if h >= 360:
        return _FALLBACK_MBPS["360p"]
    return _FALLBACK_MBPS["240p"]


def bytes_from_bitrate_kbps(bitrate_kbps: float, duration_sec: float) -> int:
    if bitrate_kbps <= 0 or duration_sec <= 0:
        return 0
    return int(bitrate_kbps * 1000.0 / 8.0 * duration_sec)


def bytes_from_bandwidth_bps(bandwidth_bps: float, duration_sec: float) -> int:
    if bandwidth_bps <= 0 or duration_sec <= 0:
        return 0
    return int(bandwidth_bps * duration_sec / 8.0 * _MANIFEST_TO_REMUX_SCALE)


def _format_bitrate_kbps(fmt: dict) -> Optional[float]:
    tbr = fmt.get("tbr")
    if tbr is not None:
        try:
            val = float(tbr)
            if val > 0:
                return val
        except (TypeError, ValueError):
            pass
    vbr = fmt.get("vbr")
    abr = fmt.get("abr")
    try:
        total = 0.0
        if vbr is not None:
            total += float(vbr)
        if abr is not None:
            total += float(abr)
        elif vbr is not None:
            total += _DEFAULT_AUDIO_KBPS
        if total > 0:
            return total
    except (TypeError, ValueError):
        pass
    filesize = fmt.get("filesize") or fmt.get("filesize_approx")
    duration = fmt.get("duration")
    if filesize and duration:
        try:
            fs = float(filesize)
            dur = float(duration)
            if fs > 0 and dur > 0:
                return (fs * 8.0) / (dur * 1000.0)
        except (TypeError, ValueError):
            pass
    return None


def size_by_quality_from_formats(
    formats: List[dict],
    duration_sec: Optional[float],
    *,
    is_clip: bool = False,
) -> Dict[str, int]:
    """Map quality labels to estimated full-file bytes using yt-dlp format metadata."""
    if not duration_sec or duration_sec <= 0:
        return {}
    dur = float(duration_sec)
    by_label: Dict[str, float] = {}
    # Labels whose value came from a muxed-progressive format with a concrete
    # byte count (clen, filesize, or filesize_approx). These should not be lifted
    # by the fallback bitrate floor, which is tuned for video-only DASH estimates.
    muxed_progressive_labels: set[str] = set()

    for fmt in formats or []:
        if not isinstance(fmt, dict):
            continue
        height = fmt.get("height")
        if not height or int(height) <= 0:
            continue
        fid = (fmt.get("format_id") or "").lower()
        if fid.startswith("portrait") or "audio" in fid:
            continue
        ext = (fmt.get("ext") or "").lower()
        vcodec = fmt.get("vcodec") or "none"
        if is_clip:
            if ext not in ("mp4", "m4v", "mov", "webm"):
                continue
        elif vcodec == "none":
            continue

        label = _label_from_height(int(height), fmt.get("fps"))
        url = (fmt.get("url") or "").strip()
        is_video_only = (
            fmt.get("acodec") in ("none", None)
            and fmt.get("vcodec") not in ("none", None)
        )
        is_muxed_progressive = not is_video_only and not is_clip

        clen = _clen_bytes_from_url(url)
        if clen and clen > 0:
            total = clen
            if is_video_only:
                total += bytes_from_bitrate_kbps(_DEFAULT_AUDIO_KBPS, dur)
            elif is_muxed_progressive:
                muxed_progressive_labels.add(label)
            by_label[label] = max(by_label.get(label, 0), total)
            continue

        filesize = fmt.get("filesize") or fmt.get("filesize_approx")
        if filesize:
            try:
                fs = float(filesize)
                if fs > 0:
                    if is_video_only:
                        fs += bytes_from_bitrate_kbps(_DEFAULT_AUDIO_KBPS, dur)
                    elif is_muxed_progressive:
                        muxed_progressive_labels.add(label)
                    by_label[label] = max(by_label.get(label, 0), fs)
                    continue
            except (TypeError, ValueError):
                pass

        kbps = _format_bitrate_kbps(fmt)
        if kbps:
            total_kbps = float(kbps)
            if is_video_only:
                total_kbps += _DEFAULT_AUDIO_KBPS
            # googlevideo tbr/clen is direct CDN — don't shrink like HLS manifest BANDWIDTH.
            scale = 1.0 if _is_googlevideo_url(url) else _MANIFEST_TO_REMUX_SCALE
            est = bytes_from_bitrate_kbps(total_kbps * scale, dur)
            by_label[label] = max(by_label.get(label, 0), est)

    # Don't let YouTube fast-preview muxed streams understate the real download.
    # Skip the floor for labels where we already have a concrete muxed-progressive
    # byte count (clen/filesize/filesize_approx); the floor protects video-only
    # DASH estimates that rely on bitrate scaling.
    if not is_clip:
        for label, size in list(by_label.items()):
            if label in muxed_progressive_labels:
                continue
            floor = bytes_from_bitrate_kbps(_fallback_mbps(label) * 1000.0, dur)
            if size < floor:
                by_label[label] = floor

    return {k: int(v) for k, v in by_label.items() if v > 0}


def audio_size_bytes_from_formats(
    formats: List[dict],
    duration_sec: float,
) -> Optional[int]:
    """Best audio-only format size (filesize → abr → tbr) in bytes."""
    if not duration_sec or duration_sec <= 0:
        return None
    best = 0
    for fmt in formats or []:
        if not isinstance(fmt, dict):
            continue
        vcodec = (fmt.get("vcodec") or "none").lower()
        acodec = (fmt.get("acodec") or "none").lower()
        if vcodec != "none" or acodec == "none":
            continue
        filesize = fmt.get("filesize") or fmt.get("filesize_approx")
        if filesize:
            try:
                fs = float(filesize)
                if fs > best:
                    best = int(fs)
                continue
            except (TypeError, ValueError):
                pass
        abr = fmt.get("abr")
        if abr is not None:
            try:
                val = float(abr)
                if val > 0:
                    est = bytes_from_bitrate_kbps(val, duration_sec)
                    if est > best:
                        best = est
                continue
            except (TypeError, ValueError):
                pass
        tbr = fmt.get("tbr")
        if tbr is not None:
            try:
                val = float(tbr)
                if val > 0:
                    est = bytes_from_bitrate_kbps(val, duration_sec)
                    if est > best:
                        best = est
            except (TypeError, ValueError):
                pass
    return best if best > 0 else None


def audio_size_bytes_default(duration_sec: float) -> int:
    """Fallback audio-only size using the default audio bitrate."""
    if duration_sec <= 0:
        return 0
    return bytes_from_bitrate_kbps(_DEFAULT_AUDIO_KBPS, duration_sec)


def hls_bandwidth_by_height(
    master_url: str,
    headers: Optional[dict] = None,
    timeout: float = 12.0,
) -> Dict[int, int]:
    """Parse an HLS master playlist → {height: bandwidth_bps}."""
    headers = headers or {}
    try:
        r = requests.get(master_url, headers=headers, timeout=timeout)
        r.raise_for_status()
        text = r.text
    # ponytail: request errors only
    except (requests.RequestException, OSError, ValueError) as exc:
        logger.debug("HLS master fetch failed %s: %s", master_url, exc)
        return {}

    if "#EXTINF:" in text and "#EXT-X-STREAM-INF" not in text:
        return {}

    by_height: Dict[int, int] = {}
    pending_bw = 0
    pending_h = 0
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#EXT-X-STREAM-INF"):
            avg_m = re.search(r"AVERAGE-BANDWIDTH=(\d+)", stripped)
            bw_m = re.search(r"BANDWIDTH=(\d+)", stripped)
            res_m = re.search(r"RESOLUTION=(\d+)x(\d+)", stripped)
            # Peak BANDWIDTH overstates size ~2×; prefer AVERAGE-BANDWIDTH when present.
            if avg_m:
                pending_bw = int(avg_m.group(1))
            elif bw_m:
                pending_bw = int(int(bw_m.group(1)) * _PEAK_BANDWIDTH_TO_AVG)
            else:
                pending_bw = 0
            pending_h = int(res_m.group(2)) if res_m else 0
            continue
        if stripped and not stripped.startswith("#") and pending_bw > 0:
            if pending_h > 0:
                by_height[pending_h] = max(by_height.get(pending_h, 0), pending_bw)
            pending_bw = 0
            pending_h = 0
    return by_height


def _parse_media_playlist_segments(
    playlist_url: str,
    headers: Optional[dict] = None,
    timeout: float = 12.0,
) -> list[dict]:
    """Return [{duration, url, byte_range_len?, byte_range_offset?}, ...] from an HLS media playlist."""
    headers = headers or {}
    try:
        r = requests.get(playlist_url, headers=headers, timeout=timeout)
        r.raise_for_status()
        text = r.text
    # ponytail: request errors only
    except (requests.RequestException, OSError, ValueError) as exc:
        logger.debug("HLS media playlist fetch failed %s: %s", playlist_url, exc)
        return []
    if "#EXT-X-STREAM-INF" in text and "#EXTINF:" not in text:
        return []
    segments: list[dict] = []
    current_duration: Optional[float] = None
    pending_range: Optional[tuple[int, Optional[int]]] = None
    range_re = re.compile(r"^#EXT-X-BYTERANGE:(\d+)(?:@(\d+))?$")
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            try:
                current_duration = float(line.split(":")[1].split(",")[0])
            except (IndexError, ValueError):
                current_duration = None
            continue
        range_m = range_re.match(line)
        if range_m:
            try:
                length = int(range_m.group(1))
                offset = int(range_m.group(2)) if range_m.group(2) is not None else None
                pending_range = (length, offset)
            except (TypeError, ValueError):
                pending_range = None
            continue
        if line and not line.startswith("#") and current_duration is not None:
            seg: dict = {
                "duration": current_duration,
                "url": urljoin(playlist_url, line),
            }
            if pending_range:
                seg["byte_range_len"] = pending_range[0]
                seg["byte_range_offset"] = pending_range[1]
            segments.append(seg)
            current_duration = None
            pending_range = None
    return segments


def _bytes_per_second_from_media_playlist(
    playlist_url: str,
    headers: Optional[dict] = None,
    *,
    max_samples: int = 6,
) -> Optional[float]:
    """Estimate sustained bytes/sec by sampling segment sizes (remux-accurate)."""
    segments = _parse_media_playlist_segments(playlist_url, headers=headers)
    if not segments:
        return None
    n = len(segments)
    if n <= max_samples:
        indices = list(range(n))
    else:
        indices = [int(round(i * (n - 1) / (max_samples - 1))) for i in range(max_samples)]
    total_bytes = 0.0
    total_dur = 0.0
    for i in indices:
        seg = segments[i]
        nbytes = seg.get("byte_range_len")
        if nbytes is None:
            nbytes = _probe_segment_size(seg["url"], headers)
        if not nbytes or nbytes <= 0:
            continue
        total_bytes += nbytes
        total_dur += float(seg["duration"])
    if total_dur <= 0:
        return None
    return total_bytes / total_dur


def _resolve_hls_master_to_media(
    playlist_url: str,
    headers: Optional[dict] = None,
    prefer_height: int = 1080,
) -> tuple[Optional[str], int, int]:
    """Follow a master playlist to the best media playlist URL + height + bandwidth."""
    headers = headers or {}
    try:
        r = requests.get(playlist_url, headers=headers, timeout=12.0)
        r.raise_for_status()
        text = r.text
    # ponytail: request errors only
    except (requests.RequestException, OSError, ValueError) as exc:
        logger.debug("HLS playlist fetch failed %s: %s", playlist_url, exc)
        return None, 0, 0
    if "#EXTINF:" in text:
        return playlist_url, prefer_height, 0
    if "#EXT-X-STREAM-INF" not in text:
        return None, 0, 0
    variants: list[tuple[int, int, str]] = []
    pending_bw = 0
    pending_h = 0
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#EXT-X-STREAM-INF"):
            avg_m = re.search(r"AVERAGE-BANDWIDTH=(\d+)", stripped)
            bw_m = re.search(r"BANDWIDTH=(\d+)", stripped)
            res_m = re.search(r"RESOLUTION=(\d+)x(\d+)", stripped)
            if avg_m:
                pending_bw = int(avg_m.group(1))
            elif bw_m:
                pending_bw = int(int(bw_m.group(1)) * _PEAK_BANDWIDTH_TO_AVG)
            else:
                pending_bw = 0
            pending_h = int(res_m.group(2)) if res_m else 0
            continue
        if stripped and not stripped.startswith("#") and pending_bw > 0:
            variants.append((pending_h, pending_bw, urljoin(playlist_url, stripped)))
            pending_bw = 0
            pending_h = 0
    if not variants:
        return None, 0, 0
    with_h = [v for v in variants if v[0] > 0]
    if with_h:
        exact = [v for v in with_h if v[0] == prefer_height]
        if exact:
            chosen = max(exact, key=lambda item: item[1])
        else:
            at_or_below = [v for v in with_h if v[0] <= prefer_height]
            chosen = max(at_or_below, key=lambda item: item[0]) if at_or_below else min(with_h, key=lambda item: item[0])
    else:
        chosen = max(variants, key=lambda item: item[1])
    child_url, child_h, child_bw = _resolve_hls_master_to_media(chosen[2], headers, prefer_height)
    if child_url:
        return child_url, child_h or chosen[0], child_bw or chosen[1]
    return None, 0, 0


def size_by_quality_from_hls_master(
    master_url: str,
    duration_sec: Optional[float],
    headers: Optional[dict] = None,
) -> Dict[str, int]:
    if not duration_sec or duration_sec <= 0:
        return {}
    dur = float(duration_sec)
    headers = headers or {}
    out: Dict[str, int] = {}

    # Measured segment throughput is the most accurate path for remux/copy.
    media_url, height, chosen_bw = _resolve_hls_master_to_media(master_url, headers)
    if media_url:
        bps = _bytes_per_second_from_media_playlist(media_url, headers)
        if bps and bps > 0:
            sampled = int(bps * dur)
            if sampled > 0:
                label = _label_from_height(height) if height > 0 else "source"
                out[label] = sampled
                out["source"] = max(out.get("source", 0), sampled)
                # Scale the measured variant to other declared bandwidths so
                # 720p/480p selections get realistic estimates too.
                if chosen_bw > 0:
                    bw_map = hls_bandwidth_by_height(master_url, headers=headers)
                    for other_h, other_bw in bw_map.items():
                        if other_h <= 0 or other_bw <= 0:
                            continue
                        other_label = _label_from_height(other_h)
                        scaled = int(sampled * (other_bw / float(chosen_bw)))
                        if scaled > 0:
                            out[other_label] = max(out.get(other_label, 0), scaled)
                if height >= 1080:
                    out.setdefault("1080p60", out.get("1080p", sampled))
                if height >= 720:
                    out.setdefault("720p60", out.get("720p", sampled))
                # Media playlist with a single variant: the stream is the same
                # regardless of which quality label the UI requests.
                if chosen_bw <= 0:
                    for q in ("source", "1080p", "1080p60", "720p", "720p60", "480p", "360p", "240p"):
                        out[q] = max(out.get(q, 0), sampled)
                return out

    bw_map = hls_bandwidth_by_height(master_url, headers=headers)
    for height, bw in sorted(bw_map.items(), key=lambda item: item[0]):
        label = _label_from_height(height)
        out[label] = bytes_from_bandwidth_bps(float(bw), dur)
        if height >= 1080:
            out.setdefault("1080p60", out[label])
        if height >= 720:
            out.setdefault("720p60", out[label])
    if bw_map and "source" not in out:
        max_h = max(bw_map)
        out["source"] = bytes_from_bandwidth_bps(float(bw_map[max_h]), dur)

    return out


def probe_url_content_length(url: str, headers: Optional[dict] = None) -> Optional[int]:
    headers = headers or {}
    try:
        r = requests.head(url, headers=headers, timeout=12, allow_redirects=True)
        cl = r.headers.get("Content-Length") or r.headers.get("content-length")
        if cl and str(cl).isdigit():
            return int(cl)
        r = requests.get(
            url,
            headers={**headers, "Range": "bytes=0-0"},
            timeout=12,
            stream=True,
        )
        cr = r.headers.get("Content-Range") or ""
        m = re.search(r"/(\d+)\s*$", cr)
        if m:
            return int(m.group(1))
        cl = r.headers.get("Content-Length") or r.headers.get("content-length")
        if cl and str(cl).isdigit():
            return int(cl)
    # ponytail: request errors only
    except (requests.RequestException, OSError, ValueError) as exc:
        logger.debug("HEAD probe failed %s: %s", url, exc)
    return None


def _probe_segment_size(url: str, headers: Optional[dict] = None) -> Optional[int]:
    """Segment size via HEAD, Content-Range, or a small ranged GET (Twitch CDN)."""
    nbytes = probe_url_content_length(url, headers)
    if nbytes and nbytes > 0:
        return nbytes
    headers = headers or {}
    try:
        r = requests.get(
            url,
            headers={**headers, "Range": "bytes=0-524287"},
            timeout=15,
        )
        cr = r.headers.get("Content-Range") or ""
        m = re.search(r"/(\d+)\s*$", cr)
        if m:
            return int(m.group(1))
        if r.status_code == 200 and r.content:
            return len(r.content)
    # ponytail: request errors only
    except (requests.RequestException, OSError, ValueError) as exc:
        logger.debug("segment probe failed %s: %s", url, exc)
    return None


def size_by_quality_from_progressive_urls(
    variants: List[dict],
    duration_sec: Optional[float],
) -> Dict[str, int]:
    """variants: [{height, url, frameRate?}] — Twitch clip progressive MP4s."""
    out: Dict[str, int] = {}
    for v in variants or []:
        if not isinstance(v, dict):
            continue
        try:
            height = int(v.get("height") or v.get("quality") or 0)
        except (TypeError, ValueError):
            continue
        url = (v.get("url") or v.get("sourceURL") or "").strip()
        if not height or not url:
            continue
        fps = v.get("frameRate") or v.get("fps")
        label = _label_from_height(height, fps)
        nbytes = probe_url_content_length(url)
        if nbytes and nbytes > 0:
            out[label] = nbytes
    if duration_sec and duration_sec > 0 and out:
        out.setdefault("source", max(out.values()))
    return out


def estimate_bytes_for_selection(
    *,
    duration_sec: float,
    quality: str,
    size_by_quality: Optional[Dict[str, int]] = None,
    full_duration_sec: Optional[float] = None,
) -> int:
    """Bytes for a trim window at *quality*, scaling known full-file estimates."""
    if duration_sec <= 0:
        return 0
    q = (quality or "source").strip().lower()
    full_dur = full_duration_sec or duration_sec
    sizes = size_by_quality or {}

    def _scale(full_bytes: int) -> int:
        if full_dur <= 0:
            return full_bytes
        ratio = min(1.0, duration_sec / full_dur)
        if ratio < 1.0 and (full_dur <= 120 or ratio < 0.25):
            # Short clips (and very short trims of longer clips) carry container
            # + audio lead-in overhead when stream-copied; scale only the media
            # payload and keep a flat overhead floor.
            overhead = min(max(512 * 1024, int(full_bytes * 0.05)), 1 * 1024 * 1024)
            return int(overhead + (full_bytes - overhead) * ratio)
        return int(full_bytes * ratio)

    if q in sizes and sizes[q] > 0:
        return _scale(sizes[q])
    # Match 1080p when user picked 1080p60 etc.
    for key, val in sizes.items():
        if key.lower().startswith(q) or q.startswith(key.lower()):
            return _scale(val)

    if "source" in sizes and sizes["source"] > 0:
        return _scale(sizes["source"])

    if sizes:
        best = max(sizes.values())
        return _scale(best)

    mbps = _fallback_mbps(q)
    return bytes_from_bitrate_kbps(mbps * 1000.0, duration_sec)


def enrich_info_dict(
    info: Dict[str, Any],
    *,
    formats: Optional[List[dict]] = None,
    m3u8_url: Optional[str] = None,
    m3u8_headers: Optional[dict] = None,
    progressive_variants: Optional[List[dict]] = None,
    is_clip: bool = False,
) -> Dict[str, Any]:
    """Add ``size_by_quality``, ``estimated_bytes``, and ``bitrate_kbps`` to info dicts."""
    duration = info.get("duration")
    try:
        dur = float(duration) if duration is not None else 0.0
    except (TypeError, ValueError):
        dur = 0.0

    size_map: Dict[str, int] = {}
    if formats:
        for k, v in size_by_quality_from_formats(formats, dur, is_clip=is_clip).items():
            size_map[k] = v
    if m3u8_url and dur > 0:
        for k, v in size_by_quality_from_hls_master(m3u8_url, dur, m3u8_headers).items():
            if k in size_map and size_map[k] > 0 and v > 0:
                size_map[k] = min(size_map[k], v)
            elif v > 0:
                size_map[k] = v
    if progressive_variants:
        for k, v in size_by_quality_from_progressive_urls(progressive_variants, dur).items():
            size_map[k] = max(size_map.get(k, 0), v)

    if not size_map and dur > 0:
        labels = list(info.get("qualities") or [])
        if not labels:
            labels = ["source", "1080p", "720p", "480p"]
        elif "source" not in labels:
            labels = [*labels, "source"]
        for label in labels:
            mbps = _fallback_mbps(str(label))
            size_map[str(label)] = bytes_from_bitrate_kbps(
                mbps * 1000.0 * _MANIFEST_TO_REMUX_SCALE, dur,
            )

    # Audio-only estimate so /api/download/video audio_only=True can size the queue.
    audio_bytes = audio_size_bytes_from_formats(formats, dur)
    if not audio_bytes and dur > 0:
        audio_bytes = audio_size_bytes_default(dur)
    if audio_bytes and audio_bytes > 0:
        size_map["audio"] = audio_bytes

    if size_map:
        info["size_by_quality"] = size_map
        qualities = info.get("qualities") or []
        default_q = qualities[0] if qualities else "source"
        info["estimated_bytes"] = estimate_bytes_for_selection(
            duration_sec=dur,
            quality=str(default_q),
            size_by_quality=size_map,
            full_duration_sec=dur,
        )
        if dur > 0 and info.get("estimated_bytes"):
            info["bitrate_kbps"] = round(
                (info["estimated_bytes"] * 8.0) / (dur * 1000.0),
                1,
            )
    return info

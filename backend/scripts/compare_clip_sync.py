"""Compare a published Twitch clip against the VOD window we asked for.

Downloads (or reuses) the official clip MP4 and a padded VOD crop into
%TEMP%/VOD.RIP-clip-sync, extracts grayscale frames, and reports the
measured start offset. No hardcoded 2-4s correction.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

os.chdir(BACKEND)

FPS = 4
FRAME_W = 160
FRAME_H = 90
FRAME_BYTES = FRAME_W * FRAME_H
MATCH_SEC = 2.0
PAD_SEC = 8.0


def _work_dir() -> Path:
    root = Path(tempfile.gettempdir()) / "VOD.RIP-clip-sync"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _ffmpeg() -> str:
    from services.ytdlp_ffmpeg import _resolve_ffmpeg_exe

    return _resolve_ffmpeg_exe()


def _ffprobe(ffmpeg_exe: str) -> str:
    from services.ytdlp_ffmpeg import _resolve_ffprobe_exe

    probe = _resolve_ffprobe_exe(ffmpeg_exe)
    if not probe:
        raise RuntimeError("ffprobe not found next to ffmpeg")
    return probe


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[-1500:]
        raise RuntimeError(
            "command failed (%s): %s...\n%s"
            % (proc.returncode, " ".join(cmd[:6]), err)
        )


def probe_duration(path: Path, ffprobe: str) -> float:
    proc = subprocess.run(
        [
            ffprobe, "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-800:])
    return float(proc.stdout.strip())


def extract_gray(src: Path, dest: Path, ffmpeg_exe: str) -> bytes:
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run([
        ffmpeg_exe, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-vf", "fps=%s,scale=%s:%s,format=gray" % (FPS, FRAME_W, FRAME_H),
        "-f", "rawvideo",
        str(dest),
    ])
    return dest.read_bytes()


def extract_jpg(src: Path, dest: Path, at_sec: float, ffmpeg_exe: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run([
        ffmpeg_exe, "-y", "-hide_banner", "-loglevel", "error",
        "-ss", "%.3f" % at_sec,
        "-i", str(src),
        "-frames:v", "1",
        "-q:v", "3",
        str(dest),
    ])


def frames_from_raw(blob: bytes) -> list:
    n = len(blob) // FRAME_BYTES
    return [memoryview(blob)[i * FRAME_BYTES:(i + 1) * FRAME_BYTES] for i in range(n)]


def mae(a, b) -> float:
    total = 0
    for x, y in zip(a, b):
        total += abs(x - y)
    return total / len(a)


def best_start_offset(clip_frames, vod_frames, *, fps, vod_start, match_sec=MATCH_SEC) -> dict:
    if not clip_frames or not vod_frames:
        raise RuntimeError("no frames extracted")
    n_match = max(1, min(len(clip_frames), int(round(match_sec * fps))))
    last = len(vod_frames) - n_match
    if last < 0:
        raise RuntimeError(
            "VOD crop too short for match window (%s frames, need %s)"
            % (len(vod_frames), n_match)
        )
    best_i = 0
    best_err = float("inf")
    scores = []
    for i in range(last + 1):
        err = 0.0
        for k in range(n_match):
            err += mae(clip_frames[k], vod_frames[i + k])
        err /= n_match
        t = vod_start + i / fps
        scores.append((t, err))
        if err < best_err:
            best_err = err
            best_i = i
    measured = vod_start + best_i / fps
    return {
        "measured_start": measured,
        "best_mae": round(best_err, 3),
        "match_frames": n_match,
        "candidates": len(scores),
        "score_curve": [
            {"t": round(t, 3), "mae": round(err, 3)}
            for t, err in scores
            if abs(t - measured) <= 4 or err == best_err
        ],
    }


def gql_clip_meta(url: str) -> dict:
    from services.twitch_gql_service import (
        CLIP_INFO_QUERY,
        _extract_clip_slug,
        _gql_request,
        get_clip_info_sync,
    )

    info = get_clip_info_sync(url)
    slug = _extract_clip_slug(url)
    node = _gql_request(CLIP_INFO_QUERY, {"slug": slug}).get("clip") or {}
    video = node.get("video") or {}
    return {
        "slug": slug,
        "title": info.get("title"),
        "duration": info.get("duration"),
        "gql_offset": node.get("videoOffsetSeconds"),
        "gql_vod_id": video.get("id") if isinstance(video, dict) else None,
        "channel": info.get("channel"),
    }


def download_clip(url: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 100000:
        return dest
    from services.twitch_gql_service import get_clip_info_sync
    from services.ytdlp_download import _download_twitch_clip_sync

    duration = float(get_clip_info_sync(url).get("duration") or 0)
    if duration <= 0:
        raise RuntimeError("clip has no duration / media")
    dest.parent.mkdir(parents=True, exist_ok=True)
    _download_twitch_clip_sync(
        url, str(dest), "source", 0.0, duration,
        progress_hook=None, cancel_event=None, pause_event=None, register_abort=None,
    )
    return dest


def download_vod_window(vod_id: str, start: float, end: float, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 100000:
        return dest
    from deps import settings_mgr
    from services.ytdlp_download import download_video_sync

    dest.parent.mkdir(parents=True, exist_ok=True)
    download_video_sync(
        "https://www.twitch.tv/videos/%s" % vod_id,
        str(dest),
        quality="720p",
        crop_start=start,
        crop_end=end,
        settings_mgr=settings_mgr,
    )
    return dest


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure clip vs VOD start offset from frames")
    parser.add_argument("--clip", required=True, help="clips.twitch.tv URL or slug")
    parser.add_argument("--expect-start", type=float, default=None)
    parser.add_argument("--expect-end", type=float, default=None)
    parser.add_argument("--vod-id", default=None)
    parser.add_argument("--clip-file", default=None)
    parser.add_argument("--pad", type=float, default=PAD_SEC)
    args = parser.parse_args()

    work = _work_dir()
    ffmpeg_exe = _ffmpeg()
    ffprobe = _ffprobe(ffmpeg_exe)
    meta = gql_clip_meta(args.clip)
    vod_id = args.vod_id or meta.get("gql_vod_id")
    gql_offset = meta.get("gql_offset")
    duration = float(meta.get("duration") or 0)
    expect_start = args.expect_start
    expect_end = args.expect_end
    if expect_start is None and gql_offset is not None:
        expect_start = float(gql_offset)
    if expect_end is None and expect_start is not None and duration:
        expect_end = expect_start + duration
    if expect_start is None or not vod_id:
        print(json.dumps({
            "error": "need --expect-start/--vod-id or GQL videoOffsetSeconds",
            "meta": meta,
        }, indent=2))
        return 2

    slug = meta["slug"]
    clip_mp4 = Path(args.clip_file) if args.clip_file else work / ("%s.mp4" % slug)
    print("work=%s" % work)
    print("gql offset=%s duration=%s vod=%s" % (gql_offset, duration, vod_id))
    print("expected %s..%s" % (expect_start, expect_end))
    print("downloading official clip...")
    clip_mp4 = download_clip(args.clip, clip_mp4)
    clip_dur = probe_duration(clip_mp4, ffprobe)
    print("clip file %s duration=%.3fs" % (clip_mp4, clip_dur))

    vod_start = max(0.0, float(expect_start) - float(args.pad))
    vod_end = float(expect_end) + float(args.pad)
    vod_mp4 = work / ("%s_%s-%s.mp4" % (vod_id, int(vod_start), int(vod_end)))
    print("downloading VOD window %.1f..%.1f..." % (vod_start, vod_end))
    vod_mp4 = download_vod_window(str(vod_id), vod_start, vod_end, vod_mp4)
    vod_dur = probe_duration(vod_mp4, ffprobe)
    print("vod file %s duration=%.3fs" % (vod_mp4, vod_dur))

    clip_raw = work / ("%s.gray" % slug)
    vod_raw = work / ("%s_%s-%s.gray" % (vod_id, int(vod_start), int(vod_end)))
    print("extracting frames...")
    clip_frames = frames_from_raw(extract_gray(clip_mp4, clip_raw, ffmpeg_exe))
    vod_frames = frames_from_raw(extract_gray(vod_mp4, vod_raw, ffmpeg_exe))
    print("frames clip=%s vod=%s" % (len(clip_frames), len(vod_frames)))

    align = best_start_offset(clip_frames, vod_frames, fps=FPS, vod_start=vod_start)
    measured = align["measured_start"]
    stills = work / slug
    stills.mkdir(exist_ok=True)
    extract_jpg(clip_mp4, stills / "clip_t0.jpg", 0.0, ffmpeg_exe)
    extract_jpg(
        vod_mp4, stills / "vod_at_expected.jpg",
        max(0.0, expect_start - vod_start), ffmpeg_exe,
    )
    extract_jpg(
        vod_mp4, stills / "vod_at_measured.jpg",
        max(0.0, measured - vod_start), ffmpeg_exe,
    )

    report = {
        "clip": args.clip,
        "slug": slug,
        "title": meta.get("title"),
        "gql": {"vod_id": vod_id, "offset_sec": gql_offset, "duration_sec": duration},
        "expected": {
            "start": expect_start,
            "end": expect_end,
            "duration": (expect_end - expect_start),
        },
        "files": {"clip": str(clip_mp4), "vod": str(vod_mp4), "stills": str(stills)},
        "probe": {
            "clip_duration": round(clip_dur, 3),
            "vod_window_duration": round(vod_dur, 3),
        },
        "measured_start": round(measured, 3),
        "measured_minus_expected": round(measured - expect_start, 3),
        "gql_minus_expected": (
            None if gql_offset is None else round(float(gql_offset) - expect_start, 3)
        ),
        "best_mae": align["best_mae"],
        "match_frames": align["match_frames"],
        "score_curve": align["score_curve"],
    }
    out = work / ("%s.report.json" % slug)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "score_curve"}, indent=2))
    print("report=%s" % out)
    print("stills=%s" % stills)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

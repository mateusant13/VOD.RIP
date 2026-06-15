"""
yt-dlp service - backward-compatibility shim.

All public symbols are now in ytdlp_ffmpeg, ytdlp_hls, ytdlp_cache,
and ytdlp_download. This module re-exports everything so existing import
paths (from services.ytdlp_service import ...) continue to work.
"""

# flake8: noqa: F401, F403

from services.ytdlp_ffmpeg import *   # noqa: F401, F403
from services.ytdlp_hls import *      # noqa: F401, F403
from services.ytdlp_cache import *    # noqa: F401, F403
from services.ytdlp_download import * # noqa: F401, F403

# Explicit re-exports for private symbols (import * does not export names with _ prefix).
# These are used directly by other modules via from services.ytdlp_service import ...
from services.ytdlp_ffmpeg import (   # noqa: F401, F811
    _check_pause_cancel, _check_cancelled,
    _track_ffmpeg_proc, _untrack_ffmpeg_proc,
    _resolve_ffmpeg_exe, _resolve_ffprobe_exe,
    _run_ffmpeg, _verify_output_file,
    ffmpeg_h264_encode_args,
    _parse_speed_multiplier,
    _phase_id,
    _normalize_crop_range, _require_crop_range,
    _format_ts,
    _find_ffmpeg,
    _apply_mp4_faststart, _atomic_replace, _chunked_copy,
    _codecs_from_stream_inf, _parse_hls_codecs,
    _ffmpeg_cmd_with_progress,
    _ffmpeg_exe_name, _ffmpeg_bin_from_dir,
    _bundled_ffmpeg_dirs,
)
from services.ytdlp_hls import (      # noqa: F401, F811
    _download_hls_clip,
    _extract_hls_info, _find_hls_format,
    _parse_prefer_height,
    _resolve_media_playlist, _parse_m3u8,
    _select_segments,
    _download_one_segment, _download_segments,
    _progressive_hls_copy_to_mp4,
    _concat_and_trim,
    download_hls_media_clip,
)
from services.ytdlp_cache import (    # noqa: F401, F811
    _get_cache_dir, _prune_cache_dir, _dir_size,
)
from services.ytdlp_download import ( # noqa: F401, F811
    _build_ydl_opts, _wrap_progress_hook,
    _ydl_download, _InstrumentedFFmpegPP,
    _qualities_from_formats,
    _hostname_from_url,
    _set_pp_progress_state,
    download_video_sync, get_video_info,
    is_clip_url, detect_platform, build_url,
)
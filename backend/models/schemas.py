from pydantic import BaseModel, Field
from typing import Any, Dict, Optional, List
from pathlib import Path

class VideoInfo(BaseModel):
    id: str
    title: Optional[str] = None
    duration: Optional[float] = None
    include_transcript: Optional[bool] = None
    duration_string: Optional[str] = None
    uploader: Optional[str] = None
    thumbnail: Optional[str] = None
    webpage_url: Optional[str] = None
    extractor: Optional[str] = None
    is_live: Optional[bool] = None
    qualities: List[str] = []
    platform: Optional[str] = None
    created_at: Optional[str] = None
    views: Optional[int] = None
    size_by_quality: Optional[Dict[str, int]] = None
    estimated_bytes: Optional[int] = None
    bitrate_kbps: Optional[float] = None


class DownloadRequest(BaseModel):
    url: str
    output_file: Optional[str] = None
    quality: Optional[str] = None
    crop_start: Optional[float] = None
    crop_end: Optional[float] = None
    audio_only: bool = False
    # ponytail: client already fetched info — skip slow re-extract before queue insert
    title: Optional[str] = None
    channel: Optional[str] = None
    thumbnail: Optional[str] = None
    duration: Optional[float] = None
    include_transcript: Optional[bool] = None
    # Chat .txt export: the user toggles "Download chat history (.txt)" on
    # the download confirm form; chat_start_sec/chat_end_sec carry the
    # START/END markers from the chat history when set, else null — the
    # sidecar writer then falls back to the download's trim window (whole
    # chat when there is no trim).
    include_chat: bool = False
    chat_start_sec: Optional[float] = None
    chat_end_sec: Optional[float] = None


class DownloadState(BaseModel):
    download_id: str
    url: str
    type: str = "video"
    platform: str = "Unknown"
    status: str = "Queued"
    progress: int = 0
    output_file: str = ""
    error: Optional[str] = None
    started_at: str = ""
    # Enriched metadata for the queue UI. Populated by the download manager
    # when the download is enqueued (or fetched lazily) so the queue tab
    # can show the title, thumbnail, channel, and chosen trim range without
    # a second round-trip.
    title: Optional[str] = None
    channel: Optional[str] = None
    thumbnail: Optional[str] = None
    duration: Optional[float] = None
    duration_string: Optional[str] = None
    quality: Optional[str] = None
    crop_start: Optional[float] = None
    crop_end: Optional[float] = None
    estimated_size: Optional[str] = None
    extra: Optional[Dict[str, Any]] = None

def _default_download_folder() -> str:
    return str(Path.home() / "Downloads")


class AppSettings(BaseModel):
    download_folder: str = Field(default_factory=_default_download_folder)
    download_folder_confirmed: bool = False
    download_threads: int = 8
    max_cache_mb: int = 512
    video_encoder: str = "auto"
    throttle_kib: int = -1
    ffmpeg_path: str = ""
    temp_folder: str = ""
    # Cache root for large ephemeral on-disk caches (yt-dlp cache,
    # transcript-fix cache, temp files — no AI models). '' = auto -> the
    # fixed drive with the most free space; an explicit path wins.
    # VODRIP_CACHE_DIR env overrides both.
    cache_dir: str = ""
    # Data root for transcripts/chat (archive DB + WAL/SHM). '' = auto ->
    # the fastest usable drive (<fastest>\VOD.RIP-data); an explicit path
    # wins. VODRIP_DATA_DIR env overrides both. Takes effect after restart —
    # the DB is moved by the relocation plumbing.
    data_dir: str = ""
    youtube_cookies_file: str = ""
    youtube_cookies_browser: str = ""
    youtube_visitor_data: str = ""
    youtube_po_token: str = ""
    youtube_tokens_file: str = ""
    youtube_auto_auth: bool = True
    youtube_pot_headless: bool = True
    youtube_wpc_pot: bool = False
    mp4_faststart: bool = False
    quality: str = "1080p"
    panel_layout: Optional[Dict[str, Any]] = None
    window_geometry: Optional[Dict[str, Any]] = None
    saved_channels: Optional[List[Dict[str, Any]]] = None
    channel_kick_enabled: bool = True
    channel_twitch_enabled: bool = True
    channel_youtube_enabled: bool = True
    channel_content_filter: str = "vods"
    skip_youtube_startup_warm: bool = False
    # Run the app at Windows boot (HKCU Run key -> VOD-RIP.exe --autostart).
    # Autostart launches hidden-to-tray with VODRIP_BACKGROUND=1 so the
    # background machinery (transcribe, index, chat capture) keeps working
    # at a quieter pace; the tray icon reopens the window.
    start_with_windows: bool = True
    cookie_bridge_token: str = ""
    cookie_bridge_enabled: bool = True
    # One-click cookie-extension install (Settings toggle): when ON, the
    # first-run offer auto-installs the unpacked extension via silent UIA
    # (cookie_extension_auto_install.ps1); OFF = manual drag-and-drop only. Absent on older backends -> ON (getattr).
    auto_install_extension: bool = True
    entity_watch_enabled: bool = True
    # Archived VOD retention: keep only the newest N video FILES per platform;
    # older files are deleted but DB rows/transcripts/chat stay forever.
    archive_vod_keep_count: int = Field(default=5, ge=1, le=50)
    # AI-models folder: home of every model weight — sherpa-onnx parakeet,
    # ONNX embedders, PANNs + tokenizers, the SLID NLLB translator
    # (parakeet/embed/PANNs/translate resolve as siblings under it).
    # None/'' = auto -> best-value drive (free space AND speed, see
    # best_model_cache_drive); a custom path pointing at a shared HF hub dir
    # reuses existing weights. (Name is legacy: whisper_model_cache now IS
    # the AI-models root — the faster-whisper engine is gone.)
    whisper_model_cache: Optional[str] = None
    # Captions-first: when a YouTube video already has auto-caption rows at
    # ingest, ASR transcription skips it (default on; toggle in Disk UI).
    yt_subtitles_first: bool = True
    # Targeted search enrichment: lazily backfill chat / enqueue transcribe
    # jobs for videos matching the search scope (default on). Off disables
    # the whole enrichment pass — hits-only responses.
    archive_smart_enrich: bool = True
    # Default ASR language for parakeet jobs: 'auto' (parakeet has no
    # detection — the job language stays None and the channel-language
    # aggregation stamps the family) or a family code ('pt', 'en', 'es').
    # Per-channel overrides win over this (channel_asr_languages: slug ->
    # code or 'auto'). A known language outside parakeet's 26 European
    # coverage fails the job cleanly — there is no whisper fallback.
    asr_language: str = "auto"
    channel_asr_languages: Optional[Dict[str, str]] = None
    # App UI language: 'en' | 'pt-BR' | 'es'. '' = not set yet — the
    # frontend seeds it from the system language on first run (the seed
    # also picks asr_language from the same family, once, never overriding
    # an explicit user choice).
    ui_language: str = ""
    # Download folder layout: 'flat' (everything in download_folder) or
    # 'typed' (VODs / Cuts / Clips / Twitch clips / Live / Audio / Chat).
    download_layout: str = "typed"
    # Write a .txt transcript sidecar next to finished downloads when the
    # archive already has a transcript (on by default).
    download_transcript_sidecar: bool = True
    # Experimental AI ask-about-channel: single-turn RAG over the local
    # archive (chat + transcripts). The API key is WRITE-ONLY — GET never
    # returns it (ai_api_key_set reports presence instead); only the update
    # path accepts it (empty string clears).
    experimental_ai_enabled: bool = False
    ai_api_key: str = ""
    ai_api_key_set: bool = False
    # Live-caption low-latency mode: 1s windows instead of 2s, raising the
    # flush-fail tolerance to compensate for shorter/emptier frames. Drops
    # total caption lag from ~1-2s to ~0.5-1s behind the live edge.
    caption_low_latency: bool = True
    # Opt-in feature registry — persisted to settings.json (see services/feature_registry.py).
    # Keys are feature ids from FEATURE_MANIFEST; absent keys fall back to manifest defaults.
    features: Optional[Dict[str, bool]] = None
    # Window-active runtime policy: controls behavior when window is
    # minimized or closed (hidden to tray). Keys: 'when_minimized',
    # 'when_closed'. Values: 'normal' (default), 'reduced', 'off'.
    window_policy: Dict[str, str] = Field(default_factory=lambda: {
        "when_minimized": "normal",
        "when_closed": "normal",
    })

class SettingsUpdate(BaseModel):
    download_folder: Optional[str] = None
    download_folder_confirmed: Optional[bool] = None
    download_threads: Optional[int] = None
    max_cache_mb: Optional[int] = None
    video_encoder: Optional[str] = None
    throttle_kib: Optional[int] = None
    ffmpeg_path: Optional[str] = None
    temp_folder: Optional[str] = None
    cache_dir: Optional[str] = None
    data_dir: Optional[str] = None
    youtube_cookies_file: Optional[str] = None
    youtube_cookies_browser: Optional[str] = None
    youtube_visitor_data: Optional[str] = None
    youtube_po_token: Optional[str] = None
    youtube_tokens_file: Optional[str] = None
    youtube_auto_auth: Optional[bool] = None
    youtube_pot_headless: Optional[bool] = None
    youtube_wpc_pot: Optional[bool] = None
    quality: Optional[str] = None
    panel_layout: Optional[Dict[str, Any]] = None
    window_geometry: Optional[Dict[str, Any]] = None
    saved_channels: Optional[List[Dict[str, Any]]] = None
    channel_kick_enabled: Optional[bool] = None
    channel_twitch_enabled: Optional[bool] = None
    channel_youtube_enabled: Optional[bool] = None
    channel_content_filter: Optional[str] = None
    mp4_faststart: Optional[bool] = None
    skip_youtube_startup_warm: Optional[bool] = None
    start_with_windows: Optional[bool] = None
    cookie_bridge_token: Optional[str] = None
    cookie_bridge_enabled: Optional[bool] = None
    auto_install_extension: Optional[bool] = None
    entity_watch_enabled: Optional[bool] = None
    archive_vod_keep_count: Optional[int] = None
    whisper_model_cache: Optional[str] = None
    yt_subtitles_first: Optional[bool] = None
    archive_smart_enrich: Optional[bool] = None
    asr_language: Optional[str] = None
    channel_asr_languages: Optional[Dict[str, str]] = None
    ui_language: Optional[str] = None
    download_layout: Optional[str] = None
    download_transcript_sidecar: Optional[bool] = None
    experimental_ai_enabled: Optional[bool] = None
    ai_api_key: Optional[str] = None
    caption_low_latency: Optional[bool] = None
    features: Optional[Dict[str, bool]] = None
    window_policy: Optional[Dict[str, str]] = None
class OpenFolderRequest(BaseModel):
    path: str


class AiAskRequest(BaseModel):
    """POST /api/ai/ask body — single-turn RAG over one channel's archive."""
    channel: str
    platform: str
    question: str
    # 'chat' | 'transcript' | 'all' — which indexed content to search.
    scope: str = "all"
    # Optional window: only videos from the last N days (None = entire history).
    days: Optional[int] = None


class AiSource(BaseModel):
    video_title: str
    created_at: Optional[str] = None
    matched_text: str


class AiAskResponse(BaseModel):
    answer: str
    sources: List[AiSource] = []


class PreviewWarmRequest(BaseModel):
    url: str
    # ponytail: hint that the user is likely to open preview soon (hover/pre-mux).
    # Triggers a background full-VOD mux so first open is instant from cache.
    full_mux: bool = False
    prefer_height: int = 720


class PreviewBatchWarmRequest(BaseModel):
    urls: list[str]
    prefer_height: int = 360


class PreviewSessionCreateRequest(BaseModel):
    url: str
    crop_start: float = 0.0
    crop_end: float = 0.0
    prefer_height: int = 720


class PreviewSessionResponse(BaseModel):
    session_id: str
    master_url: str
    playback_url: str  # alias for master_url (frontend compat)
    kind: str = "hls"  # "hls" (default) or "progressive" for direct MP4 sources
    variant_heights: List[int] = []
    quality_labels: List[str] = []
    active_height: int = 0
    extract_source: str = ""
    mux_ready: bool = True
    playlist_ready: bool = True  # full VOD playlist exists — player can attach immediately
    segment_buffer_ready: bool = True  # first segment(s) on disk (optional warm)
    trim_timeline: bool = False  # window-HLS: playlist is 0-based from window_hls_mux_start
    duration_sec: float = 0.0  # real VOD length from extract (crop_end clamped to this)
    window_hls_mux_start: float = 0.0
    window_hls_mux_end: float = 0.0
    # ponytail: when the backend is serving a local cached MP4, the browser can
    # do native byte-range seeks without a refresh/mux round-trip.
    cached_progressive: bool = False
    # Live/DVR session fields: is_live marks a create_live_session session,
    # growing_vod means the VOD media playlist has no ENDLIST (in-progress
    # broadcast), archive_url/archive_duration expose the REPLAY snapshot.
    is_live: bool = False
    growing_vod: bool = False
    # Quality policy: True for YouTube sessions resolved without user auth —
    # the frontend must keep such previews at 360p (VOD and live alike).
    anonymous: bool = False
    archive_url: str = ""
    archive_duration: float = 0.0
    # WS-2 preview chat panel: true when the archived video for this session
    # has transcript / chat rows in the local archive DB (empty states).
    has_transcript: bool = False
    has_chat: bool = False
    # WS-3: detected channel language of the previewed archived video
    # ('' when the video is not archived / language unknown) — the preview
    # header badge renders this.
    channel_language: str = ""
    # Gap 1: server-side create_session wall time (ms) — surfaces cold vs warm
    # resolve cost to the client (console diagnostic; 0 for non-create paths).
    resolve_ms: float = 0.0


class PreviewSeekRequest(BaseModel):
    position_sec: float = 0.0


class PreviewTimingRequest(BaseModel):
    platform: str = ""
    surface: str = "main"
    event: str = ""
    session_id: str = ""
    open_ms: float = 0.0
    seek_ms: float = 0.0
    detail: str = ""



class LivePreviewRequest(BaseModel):
    url: str
    platform: str
    headers: Dict[str, str] = Field(default_factory=dict)
    title: str = ""
    # Channel's current (in-progress) VOD URL — REPLAY/DVR archive source.
    vod_url: str = ""


class LiveRotateRequest(BaseModel):
    # Optional explicit next player type (default: advance in vaft order
    # embed -> popout -> autoplay). Unknown values are rejected by the route.
    player_type: Optional[str] = None


class PreviewSessionStatusResponse(BaseModel):
    mux_ready: bool
    playlist_ready: bool = True
    segment_buffer_ready: bool = True
    mux_status: str = "unnecessary"
    mux_error: str = ""
    window_hls_mux_start: float = 0.0
    window_hls_mux_end: float = 0.0
    # Live sessions only: last background master/media prewarm failure
    # ('' = healthy). Lets the poller surface a dead upstream instead of
    # spinning forever on an all-green status.
    live_upstream_error: str = ""
    # Seconds until the session is wiped by the idle TTL (or LRU eviction).
    # Sessions die silently — the poll is the only lifecycle surface the
    # client has; expires_in lets it see the end coming. 0 = already
    # eligible for the next cleanup sweep.
    expires_in: int = 0


class PreviewQualityUpdateRequest(BaseModel):
    prefer_height: int = 720

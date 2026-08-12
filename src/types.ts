/**
 * Shared type definitions extracted from App.tsx.
 */

export interface VideoInfo {
  id: string;
  title: string | null;
  duration: number | null;
  duration_string: string | null;
  uploader: string | null;
  /** Broadcaster login/slug when returned by the API (e.g. Twitch VOD owner login). */
  channel?: string | null;
  thumbnail: string | null;
  webpage_url: string | null;
  extractor: string | null;
  is_live: boolean | null;
  qualities: string[];
  platform: string | null;
  created_at?: string | null;
  views?: number | null;
  size_by_quality?: Record<string, number>;
  estimated_bytes?: number;
  bitrate_kbps?: number;
}

export interface DownloadState {
  download_id: string;
  url: string;
  type: string;
  platform: string;
  status: string;
  progress: number;
  output_file: string;
  error: string | null;
  started_at: string;
  title?: string | null;
  channel?: string | null;
  thumbnail?: string | null;
}

export interface DownloadsResponse {
  queue: DownloadState[];
  /** Failed / Cancelled / Interrupted entries — resumable, but not "active". */
  recent?: DownloadState[];
  history: DownloadState[];
}

export interface ChannelVideo {
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
  content_kind?: 'vod' | 'clip' | 'stream';
  /** Detected channel language ('pt'/'en'/'es'/raw code; absent = unknown). */
  channel_language?: string | null;
  /** yt-dlp availability — 'subscriber_only' marks members-only rows (no preview possible). */
  availability?: string | null;
  /** WS-4: original (non-auto-translated) YouTube title + its language. */
  original_title?: string | null;
  original_language?: string | null;
}

export interface ListedChannelVideo extends ChannelVideo {
  /** 1-based index within the currently visible list for this platform. */
  platformListIndex: number;
}

/** Channel list row badge shown on main preview when opened from Channels. */
export interface ChannelPreviewBadge {
  platform: string;
  platformListIndex: number;
  isClip: boolean;
}

export interface AppSettings {
  download_folder: string;
  download_folder_confirmed?: boolean;
  download_threads: number;
  max_cache_mb: number;
  video_encoder?: string;
  throttle_kib: number;
  ffmpeg_path: string;
  temp_folder: string;
  /** Cache root for large on-disk caches ('' = auto -> biggest fixed drive). */
  cache_dir?: string;
  /** Transcripts/chat data root — archive DB lives here ('' = auto ->
   * fastest usable drive; an explicit path wins). Takes effect after
   * restart (DB relocation). */
  data_dir?: string;
  youtube_cookies_file?: string;
  youtube_cookies_browser?: string;
  youtube_visitor_data?: string;
  youtube_po_token?: string;
  youtube_tokens_file?: string;
  youtube_auto_auth?: boolean;
  youtube_pot_headless?: boolean;
  youtube_wpc_pot?: boolean;
  quality: string;
  panel_layout?: PersistedPanelLayout | null;
  window_geometry?: Record<string, number | boolean> | null;
  saved_channels?: SavedChannel[] | null;
  channel_kick_enabled?: boolean;
  channel_twitch_enabled?: boolean;
  channel_youtube_enabled?: boolean;
  channel_content_filter?: 'vods' | 'clips' | 'streams';
  skip_youtube_startup_warm?: boolean;
  /** Run at Windows boot (HKCU Run key -> --autostart): launches
   * hidden-to-tray with quiet pacing for background work. */
  start_with_windows?: boolean;
  download_layout?: 'flat' | 'typed';
  download_transcript_sidecar?: boolean;
  cookie_bridge_enabled?: boolean;
  /** One-click extension auto-install: ON offers the install on first run;
   * OFF = manual drag-and-drop only. Absent on older backends -> ON. */
  auto_install_extension?: boolean;
  twitch_monitor_enabled?: boolean;
  /** Post-merge field (VOD retention slice); absent on older backends -> default 5. */
  archive_vod_keep_count?: number;
  /** Local transcription model: faster-whisper id + HF cache dir (absent on
   * older backends -> default large-v3-turbo + %APPDATA%/VOD.RIP/whisper-models). */
  whisper_model?: string;
  /** ASR engine: 'parakeet' (default) | 'whisper'. Whisper stays the
   * automatic fallback for ja/ko/zh/ar and parakeet-engine failures. */
  asr_engine?: string;
  whisper_model_cache?: string | null;
  /** Captions-first: skip whisper for YouTube videos that already have
   * auto-caption rows at ingest (absent on older backends -> default true). */
  yt_subtitles_first?: boolean;
  /** Default ASR language for whisper jobs: 'auto' or a family code ('pt','en','es'). */
  asr_language?: string;
  /** Per-channel ASR override: channel slug -> 'auto' or family code. */
  channel_asr_languages?: Record<string, string> | null;
  /** App UI language: 'en' | 'pt-BR' | 'es'. Absent/'' = not set yet —
   * the FE seeds it from the system language on first run. */
  ui_language?: string;
}

export interface DiskUsage {
  archive_vods: number;
  whisper_models: number;
  db: number;
  logs: number;
  preview_cache: number;
  update_temps: number;
  total: number;
}

export interface DiskStatus {
  free_bytes: number;
  threshold_bytes: number;
  low: boolean;
  keep_count: number;
  /** Effective cache root ('' when none) + free space on its volume. */
  cache_dir?: string;
  cache_free_bytes?: number;
  /** Auto pick: drive with the most free space (informational). */
  biggest_drive?: string;
}

/** One drive letter from /api/disks (Settings > Storage pickers). */
export interface DiskInfo {
  drive: string;
  label?: string;
  total_bytes: number;
  free_bytes: number;
  media_type: 'NVMe' | 'SSD' | 'HDD' | 'Unknown';
  bus_type: string;
  /** Lower = faster: 1 NVMe, 2 SSD, 3 HDD, 4 Unknown (bus-classified). */
  speed_rank: number;
}

export interface DisksResponse {
  drives: DiskInfo[];
  /** Auto pick for transcripts/chat: fastest usable drive ('' when none). */
  fastest: string;
  /** Auto pick for heavy caches: drive with the most free space. */
  biggest?: string;
  /** Auto pick for the whisper model cache: best-ROI drive (free space AND
   * speed; '' when none). */
  model_cache?: string;
}

export interface UpdateInfo {
  version: string;
  release_notes?: string;
  release_url?: string;
  asset_name?: string;
}

export interface SavedChannel {
  id: string;
  displayName: string;
  kickSlug: string;
  twitchSlug: string;
  youtubeSlug: string;
  vodVideos: ChannelVideo[];
  clipVideos: ChannelVideo[];
  vodErrors?: Record<string, string>;
  clipErrors?: Record<string, string>;
  updatedAt: string;
  loading?: boolean;
  /** True after at least one clips fetch completed (success or failure). */
  clipsFetched?: boolean;
  /** True after at least one YouTube /streams fetch completed. */
  streamsFetched?: boolean;
  /** Per-platform VOD list fetch completed (empty list counts). */
  vodPlatformsFetched?: Partial<Record<'Kick' | 'Twitch' | 'YouTube', boolean>>;
  /** Per-platform clips/shorts fetch completed (empty list counts). */
  clipPlatformsFetched?: Partial<Record<'Kick' | 'Twitch' | 'YouTube', boolean>>;
  /** Legacy — migrated to vodVideos / clipVideos on load */
  videos?: ChannelVideo[];
}

export interface PreviewSessionResponse {
  session_id: string;
  master_url: string;
  playback_url?: string;
  kind?: string;
  variant_heights?: number[];
  quality_labels?: string[];
  active_height?: number;
  extract_source?: string;
  /** False while async YouTube DASH mux is still running on the backend. */
  mux_ready?: boolean;
  /** True when master/media playlist exists — attach player without waiting for segment mux. */
  playlist_ready?: boolean;
  /** True when first segment(s) are cached on disk (warm path). */
  segment_buffer_ready?: boolean;
  /** HLS playlist is 0-based from window_hls_mux_start (YouTube window-HLS). */
  trim_timeline?: boolean;
  /** Real VOD length from backend extract (crop_end clamped to this). */
  duration_sec?: number;
  window_hls_mux_start?: number;
  window_hls_mux_end?: number;
  /** True when the backend is serving a local cached MP4 (instant byte-range seeks). */
  cached_progressive?: boolean;
  /** True for create_live_session sessions (live popup). */
  is_live?: boolean;
  /** VOD media playlist has no ENDLIST — in-progress broadcast (grows). */
  growing_vod?: boolean;
  /** Quality policy: YouTube session resolved without user auth — 360p only. */
  anonymous?: boolean;
  /** DVR archive media URL — REPLAY mode snapshots this (empty = no archive). */
  archive_url?: string;
  /** Archive duration at session creation (grows while the stream runs). */
  archive_duration?: number;
  /** WS-3: detected channel language of the previewed archived video ('' = unknown). */
  channel_language?: string;
}

export interface PreviewSessionStatusResponse {
  mux_ready: boolean;
  playlist_ready?: boolean;
  segment_buffer_ready?: boolean;
  mux_status?: string;
  mux_error?: string;
  window_hls_mux_start?: number;
  window_hls_mux_end?: number;
}

export type Tab = 'url' | 'channels' | 'queue' | 'settings';

export interface PersistedPanelLayout {
  previewPanelWidth: number;
  urlAside: { w: number; h: number };
  main: { w: number; h: number };
  /** Width of the live player panel (live replaces the preview slot while open). */
  livePanelWidth?: number;
  /**
   * User-owned widths — the widths each panel last had when the user dragged IT.
   * Sibling squeezes never write here, so a reverse drag restores the row to the
   * user's shapes instead of latching the squeezed (thin) widths forever.
   * Optional for back-compat with layouts persisted before this field existed.
   */
  owned?: { preview: number; urlAside: number; main: number };
}

export type PanelSize = { w: number; h: number };
export type PanelPos = { x: number; y: number };
export type LayoutPanelKey = 'preview' | 'urlAside' | 'main' | 'live';

export interface LayoutPanelBoundsInput {
  previewOpen: boolean;
  urlPanelAside: boolean;
  preview: PanelSize;
  urlAside: PanelSize;
  main: PanelSize;
  /** Live player panel; replaces the preview slot while open (never coexists with preview). */
  liveOpen?: boolean;
  live?: PanelSize;
}

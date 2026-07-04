/**
 * Shared type definitions extracted from App.tsx.
 */

export interface VideoInfo {
  id: string;
  title: string | null;
  duration: number | null;
  duration_string: string | null;
  uploader: string | null;
  thumbnail: string | null;
  webpage_url: string | null;
  extractor: string | null;
  is_live: boolean | null;
  qualities: string[];
  platform: string | null;
  created_at?: string | null;
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
  content_kind?: 'vod' | 'clip';
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
  oauth: string;
  quality: string;
  panel_layout?: PersistedPanelLayout | null;
  window_geometry?: Record<string, number | boolean> | null;
  saved_channels?: SavedChannel[] | null;
  channel_kick_enabled?: boolean;
  channel_twitch_enabled?: boolean;
  channel_content_filter?: 'vods' | 'clips';
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
  vodVideos: ChannelVideo[];
  clipVideos: ChannelVideo[];
  vodErrors?: Record<string, string>;
  clipErrors?: Record<string, string>;
  updatedAt: string;
  loading?: boolean;
  /** True after at least one clips fetch completed (success or failure). */
  clipsFetched?: boolean;
  /** Legacy — migrated to vodVideos / clipVideos on load */
  videos?: ChannelVideo[];
}

export type Tab = 'url' | 'channels' | 'queue' | 'settings';

export interface PersistedPanelLayout {
  previewPanelWidth: number;
  urlAside: { w: number; h: number };
  main: { w: number; h: number };
}

export type PanelSize = { w: number; h: number };
export type PanelPos = { x: number; y: number };
export type LayoutPanelKey = 'preview' | 'urlAside' | 'main';

export interface LayoutPanelBoundsInput {
  previewOpen: boolean;
  urlPanelAside: boolean;
  preview: PanelSize;
  urlAside: PanelSize;
  main: PanelSize;
}

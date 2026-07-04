from pydantic import BaseModel, Field
from typing import Any, Dict, Optional, List
from pathlib import Path

class VideoInfo(BaseModel):
    id: str
    title: Optional[str] = None
    duration: Optional[float] = None
    duration_string: Optional[str] = None
    uploader: Optional[str] = None
    thumbnail: Optional[str] = None
    webpage_url: Optional[str] = None
    extractor: Optional[str] = None
    is_live: Optional[bool] = None
    qualities: List[str] = []
    platform: Optional[str] = None
    created_at: Optional[str] = None
    size_by_quality: Optional[Dict[str, int]] = None
    estimated_bytes: Optional[int] = None
    bitrate_kbps: Optional[float] = None


class DownloadRequest(BaseModel):
    url: str
    output_file: Optional[str] = None
    quality: Optional[str] = None
    oauth: Optional[str] = None
    crop_start: Optional[float] = None
    crop_end: Optional[float] = None
    audio_only: bool = False


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
    oauth: str = ""
    mp4_faststart: bool = False
    quality: str = "1080p"
    panel_layout: Optional[Dict[str, Any]] = None
    window_geometry: Optional[Dict[str, Any]] = None
    saved_channels: Optional[List[Dict[str, Any]]] = None
    channel_kick_enabled: bool = True
    channel_twitch_enabled: bool = True
    channel_youtube_enabled: bool = True
    channel_content_filter: str = "vods"


class SettingsUpdate(BaseModel):
    download_folder: Optional[str] = None
    download_folder_confirmed: Optional[bool] = None
    download_threads: Optional[int] = None
    max_cache_mb: Optional[int] = None
    video_encoder: Optional[str] = None
    throttle_kib: Optional[int] = None
    ffmpeg_path: Optional[str] = None
    temp_folder: Optional[str] = None
    oauth: Optional[str] = None
    quality: Optional[str] = None
    panel_layout: Optional[Dict[str, Any]] = None
    window_geometry: Optional[Dict[str, Any]] = None
    saved_channels: Optional[List[Dict[str, Any]]] = None
    channel_kick_enabled: Optional[bool] = None
    channel_twitch_enabled: Optional[bool] = None
    channel_youtube_enabled: Optional[bool] = None
    channel_content_filter: Optional[str] = None
    mp4_faststart: Optional[bool] = None


class OpenFolderRequest(BaseModel):
    path: str


class PreviewSessionCreateRequest(BaseModel):
    url: str
    crop_start: float = 0.0
    crop_end: float = 0.0
    prefer_height: int = 480


class PreviewSessionResponse(BaseModel):
    session_id: str
    master_url: str
    playback_url: str  # alias for master_url (frontend compat)
    kind: str = "hls"  # "hls" (default) or "progressive" for direct MP4 sources
    variant_heights: List[int] = []
    quality_labels: List[str] = []
    active_height: int = 0


class PreviewQualityUpdateRequest(BaseModel):
    prefer_height: int = 480

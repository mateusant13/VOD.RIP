from pydantic import BaseModel
from typing import Optional, List


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


class DownloadRequest(BaseModel):
    url: str
    output_file: Optional[str] = None
    quality: Optional[str] = None
    oauth: Optional[str] = None
    crop_start: Optional[float] = None
    crop_end: Optional[float] = None


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


class AppSettings(BaseModel):
    download_threads: int = 4
    max_cache_mb: int = 200
    throttle_kib: int = -1
    ffmpeg_path: str = ""
    temp_folder: str = ""
    oauth: str = ""
    quality: str = ""


class SettingsUpdate(BaseModel):
    download_threads: Optional[int] = None
    max_cache_mb: Optional[int] = None
    throttle_kib: Optional[int] = None
    ffmpeg_path: Optional[str] = None
    temp_folder: Optional[str] = None
    oauth: Optional[str] = None
    quality: Optional[str] = None

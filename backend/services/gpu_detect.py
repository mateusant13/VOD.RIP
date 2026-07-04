"""Detect GPU vendor and pick the best H.264 hardware encoder ffmpeg supports."""

from __future__ import annotations

import logging
import os
import platform as _platform
import re
import shutil
import subprocess
import sys
import tempfile
from functools import lru_cache
from typing import Dict, List, Optional

from services.os_services import _NO_WINDOW, list_gpu_names

logger = logging.getLogger(__name__)

VENDOR_LABELS = {
    "nvidia": "NVIDIA",
    "amd": "AMD",
    "intel": "Intel",
    "apple": "Apple (VideoToolbox)",
    "none": "None (software)",
}

ENCODER_BY_VENDOR = {
    "nvidia": "h264_nvenc",
    "amd": "h264_amf",
    "intel": "h264_qsv",
    "apple": "h264_videotoolbox",
    "vaapi": "h264_vaapi",
    "none": "libx264",
}


def _run_text(cmd: List[str], timeout: float = 8.0) -> str:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
        if proc.returncode != 0:
            return ""
        return (proc.stdout or "") + (proc.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.debug("gpu_detect command failed %s: %s", cmd[:2], e)
        return ""


def _is_apple_silicon() -> bool:
    """Detect Apple Silicon (M1/M2/M3/M4) without importing platform module."""
    if sys.platform != "darwin":
        return False
    try:
        return _platform.processor() == "arm"
    except Exception:
    # ponytail: subprocess errors only — best-effort GPU encoder detection
        return False


def detect_gpu_vendor(names: Optional[List[str]] = None) -> str:
    """Return ``nvidia``, ``amd``, ``intel``, ``apple``, or ``none``."""
    # Apple Silicon check first (doesn't appear in lspci)
    if _is_apple_silicon():
        return "apple"

    combined = " ".join(names if names is not None else list_gpu_names()).lower()
    if not combined.strip():
        return "none"
    if "nvidia" in combined or "geforce" in combined or "quadro" in combined or "tesla" in combined:
        return "nvidia"
    if "amd" in combined or "radeon" in combined or "ati " in combined:
        return "amd"
    if "intel" in combined or "iris" in combined or "uhd graphics" in combined:
        return "intel"
    # Fallback: macOS Intel with no lspci available
    if sys.platform == "darwin":
        return "apple"
    return "none"


def ffmpeg_encoder_names(ffmpeg_bin: Optional[str] = None) -> set[str]:
    """Return encoder ids reported by ``ffmpeg -encoders``."""
    ffmpeg = (ffmpeg_bin or "").strip() or shutil.which("ffmpeg") or "ffmpeg"
    out = _run_text([ffmpeg, "-hide_banner", "-encoders"])
    found: set[str] = set()
    for line in out.splitlines():
        m = re.match(r"\s*([AVS]\.{3})\s+(\S+)", line)
        if m:
            found.add(m.group(2))
    return found


def _encoder_usable(encoder: str, ffmpeg_bin: str) -> bool:
    """Test whether an encoder actually works via a quick trial encode."""
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp_path = tmp.name
        cmd = [
            ffmpeg_bin, "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.1",
            "-c:v", encoder,
            "-frames:v", "1",
            "-y", tmp_path,
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=_NO_WINDOW,
        )
        return result.returncode == 0
    except Exception:
    # ponytail: best-effort — return result.returncode == 0
        return False
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
        # ponytail: best-effort — I/O errors only
            pass


def recommend_encoder(
    vendor: Optional[str] = None,
    ffmpeg_encoders: Optional[set[str]] = None,
    ffmpeg_bin: Optional[str] = None,
    validate: bool = False,
) -> str:
    """Map GPU vendor to the best supported H.264 encoder.

    On Linux, AMD and Intel map to ``h264_vaapi`` (VAAPI) instead of
    AMF/QSV (which are Windows/Mac-oriented). On macOS, Apple Silicon
    and Intel Macs use ``h264_videotoolbox``.
    """
    vendor = vendor or detect_gpu_vendor()
    ffmpeg = (ffmpeg_bin or "").strip() or shutil.which("ffmpeg") or "ffmpeg"
    encoders = ffmpeg_encoders if ffmpeg_encoders is not None else ffmpeg_encoder_names(ffmpeg)

    # Platform-specific encoder overrides
    _vendor = vendor
    if sys.platform.startswith("linux"):
        if vendor in ("amd", "intel"):
            _vendor = "vaapi"
    elif sys.platform == "darwin":
        if vendor in ("amd", "intel", "apple"):
            _vendor = "apple"

    preferred = ENCODER_BY_VENDOR.get(_vendor, "libx264")

    if not encoders:
        return preferred

    # On Linux VAAPI path, also try QSV for Intel
    if sys.platform.startswith("linux") and vendor == "intel" and "h264_qsv" in encoders:
        preferred = "h264_qsv"

    if preferred in encoders:
        if not validate or _encoder_usable(preferred, ffmpeg):
            return preferred
        logger.debug("Encoder %s reported but unusable (trial encode failed)", preferred)

    if "libx264" in encoders:
        return "libx264"

    # Fallback: try any h264 encoder that works
    for enc in ("h264_nvenc", "h264_videotoolbox", "h264_amf", "h264_qsv", "h264_vaapi"):
        if enc in encoders:
            if not validate or _encoder_usable(enc, ffmpeg):
                return enc

    return "libx264"


def _infer_vendor_from_working_encoders(ffmpeg_bin: str, encoders: set[str]) -> Optional[str]:
    """When WMI/lspci fail, pick vendor from the first HW encoder that actually encodes."""
    for vendor, encoder in (
        ("nvidia", "h264_nvenc"),
        ("amd", "h264_amf"),
        ("intel", "h264_qsv"),
    ):
        if encoder in encoders and _encoder_usable(encoder, ffmpeg_bin):
            return vendor
    if sys.platform == "darwin" and "h264_videotoolbox" in encoders:
        if _encoder_usable("h264_videotoolbox", ffmpeg_bin):
            return "apple"
    return None


def _probe_encoder_detection(ffmpeg_bin: str) -> Dict[str, object]:
    names = list_gpu_names()
    vendor = detect_gpu_vendor(names)
    encoders = ffmpeg_encoder_names(ffmpeg_bin)
    if vendor == "none":
        inferred = _infer_vendor_from_working_encoders(ffmpeg_bin, encoders)
        if inferred:
            vendor = inferred
            if not names:
                names = [VENDOR_LABELS.get(inferred, inferred)]
    detected = recommend_encoder(vendor, encoders, ffmpeg_bin, validate=True)
    return {
        "gpus": names,
        "vendor": vendor,
        "vendor_label": VENDOR_LABELS.get(vendor, vendor),
        "detected_encoder": detected,
        "ffmpeg_encoders": {
            "h264_nvenc": "h264_nvenc" in encoders,
            "h264_amf": "h264_amf" in encoders,
            "h264_qsv": "h264_qsv" in encoders,
            "h264_videotoolbox": "h264_videotoolbox" in encoders,
            "h264_vaapi": "h264_vaapi" in encoders,
            "libx264": "libx264" in encoders,
        },
    }


@lru_cache(maxsize=4)
def _cached_encoder_detection(ffmpeg_bin: str) -> Dict[str, object]:
    return _probe_encoder_detection(ffmpeg_bin)


def clear_encoder_detection_cache() -> None:
    _cached_encoder_detection.cache_clear()


def get_encoder_detection(ffmpeg_bin: Optional[str] = None, *, fresh: bool = False) -> Dict[str, object]:
    """GPU + encoder probe for settings UI and auto mode."""
    if ffmpeg_bin is None:
        try:
            from services.ytdlp_service import _resolve_ffmpeg_exe

            ffmpeg_bin = _resolve_ffmpeg_exe()
        except Exception:
        # ponytail: best-effort — ffmpeg_bin = _resolve_ffmpeg_exe()
            ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
    if fresh:
        clear_encoder_detection_cache()
    return _cached_encoder_detection(ffmpeg_bin)

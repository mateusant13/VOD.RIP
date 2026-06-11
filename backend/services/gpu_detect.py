"""Detect GPU vendor and pick the best H.264 hardware encoder ffmpeg supports."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from functools import lru_cache
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

VENDOR_LABELS = {
    "nvidia": "NVIDIA",
    "amd": "AMD",
    "intel": "Intel",
    "none": "None (software)",
}

ENCODER_BY_VENDOR = {
    "nvidia": "h264_nvenc",
    "amd": "h264_amf",
    "intel": "h264_qsv",
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


def _gpu_names_windows() -> List[str]:
    names: List[str] = []
    ps = (
        "Get-CimInstance Win32_VideoController | "
        "Select-Object -ExpandProperty Name"
    )
    out = _run_text(["powershell", "-NoProfile", "-Command", ps])
    for line in out.splitlines():
        line = line.strip()
        if line:
            names.append(line)
    if names:
        return names
    out = _run_text(["wmic", "path", "win32_VideoController", "get", "name"])
    for line in out.splitlines():
        line = line.strip()
        if line and line.lower() != "name":
            names.append(line)
    return names


def _gpu_names_unix() -> List[str]:
    if shutil.which("lspci"):
        out = _run_text(["lspci"])
        for line in out.splitlines():
            if re.search(r"vga|3d|display", line, re.I):
                names.append(line.split(":", 2)[-1].strip())
    return names


def list_gpu_names() -> List[str]:
    if os.name == "nt":
        return _gpu_names_windows()
    return _gpu_names_unix()


def detect_gpu_vendor(names: Optional[List[str]] = None) -> str:
    """Return ``nvidia``, ``amd``, ``intel``, or ``none``."""
    combined = " ".join(names if names is not None else list_gpu_names()).lower()
    if not combined.strip():
        return "none"
    if "nvidia" in combined or "geforce" in combined or "quadro" in combined or "tesla" in combined:
        return "nvidia"
    if "amd" in combined or "radeon" in combined or "ati " in combined:
        return "amd"
    if "intel" in combined or "iris" in combined or "uhd graphics" in combined:
        return "intel"
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


def recommend_encoder(
    vendor: Optional[str] = None,
    ffmpeg_encoders: Optional[set[str]] = None,
) -> str:
    """Map GPU vendor to the best supported H.264 encoder."""
    vendor = vendor or detect_gpu_vendor()
    encoders = ffmpeg_encoders if ffmpeg_encoders is not None else ffmpeg_encoder_names()
    preferred = ENCODER_BY_VENDOR.get(vendor, "libx264")
    if not encoders:
        return preferred
    if preferred in encoders:
        return preferred
    if "libx264" in encoders:
        return "libx264"
    return "libx264"


def _probe_encoder_detection(ffmpeg_bin: str) -> Dict[str, object]:
    names = list_gpu_names()
    vendor = detect_gpu_vendor(names)
    encoders = ffmpeg_encoder_names(ffmpeg_bin)
    detected = recommend_encoder(vendor, encoders)
    return {
        "gpus": names,
        "vendor": vendor,
        "vendor_label": VENDOR_LABELS.get(vendor, vendor),
        "detected_encoder": detected,
        "ffmpeg_encoders": {
            "h264_nvenc": "h264_nvenc" in encoders,
            "h264_amf": "h264_amf" in encoders,
            "h264_qsv": "h264_qsv" in encoders,
            "libx264": "libx264" in encoders,
        },
    }


@lru_cache(maxsize=4)
def _cached_encoder_detection(ffmpeg_bin: str) -> Dict[str, object]:
    return _probe_encoder_detection(ffmpeg_bin)


def get_encoder_detection(ffmpeg_bin: Optional[str] = None) -> Dict[str, object]:
    """GPU + encoder probe for settings UI and auto mode."""
    if ffmpeg_bin is None:
        try:
            from services.ytdlp_service import _resolve_ffmpeg_exe

            ffmpeg_bin = _resolve_ffmpeg_exe()
        except Exception:
            ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
    return _cached_encoder_detection(ffmpeg_bin)

"""Acoustic-event detection for the archive — PANNs Cnn14 (laugh/clap/scream/music…).

Pipeline stage (the one piece the VOD.RIP transcription stack was missing):
  ffmpeg decode (16k mono) -> Silero VAD speech regions -> resample to 32k
  -> Cnn14_DecisionLevelMax framewise scores (10 ms frames over 527 AudioSet
  classes) -> thresholded runs -> ``audio_events`` rows (start, end, event,
  score). Runs standalone via kind='events' jobs or automatically after each
  transcribe job when VODRIP_EVENTS_ENABLED=1.

Design notes:
  * Only VAD speech regions are scored (the same dead-air skip the whisper
    stage uses): a 13.5 h VOD becomes ~7.5 h of windows, ~28 s of GPU work.
  * SoundEventDetection (frame-level) is used instead of AudioTagging
    (clip-level) so events carry real boundaries — the paste's "acoustic
    event detection" needs timestamps, not flags.
  * Model + weights are the package defaults (Cnn14_DecisionLevelMax_mAP=
    0.385.pth under ~/panns_data); panns_inference is imported lazily on
    first use, so a worker with events disabled never pays its import cost.

Env knobs (all optional, VODRIP_WHISPER_* style):
  VODRIP_EVENTS_ENABLED     0|1   auto-run after each transcribe job (default 0)
  VODRIP_EVENTS_THRESHOLD        frame-probability threshold (default 0.5)
  VODRIP_EVENTS_MIN_SEC          minimum event duration (default 0.4)
  VODRIP_EVENTS_WINDOW_SEC       SED window length (default 30)
  VODRIP_EVENTS_DEVICE           cuda|cpu (default: cuda when available)
  VODRIP_EVENTS_CLASSES          comma-separated AudioSet labels (default: interest set)
  VODRIP_EVENTS_CHECKPOINT       Cnn14_DecisionLevelMax .pth path
                                 (default ~/panns_data/Cnn14_DecisionLevelMax_mAP=0.385.pth)
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from services import archive_db

logger = logging.getLogger(__name__)

SR_16K = 16000
SR_32K = 32000
FRAME_SEC = 0.01  # SED hop 320 @ 32 kHz -> one score vector per 10 ms

# Env knobs (all optional).
ENABLED_ENV = "VODRIP_EVENTS_ENABLED"
THRESHOLD_ENV = "VODRIP_EVENTS_THRESHOLD"
MIN_SEC_ENV = "VODRIP_EVENTS_MIN_SEC"
WINDOW_ENV = "VODRIP_EVENTS_WINDOW_SEC"
DEVICE_ENV = "VODRIP_EVENTS_DEVICE"
CLASSES_ENV = "VODRIP_EVENTS_CLASSES"
CHECKPOINT_ENV = "VODRIP_EVENTS_CHECKPOINT"

DEFAULT_THRESHOLD = 0.5
DEFAULT_MIN_SEC = 0.4
DEFAULT_WINDOW_SEC = 30.0

# The paste's interest set (laughter, scream, clapping, cheering, crying, …)
# mapped onto AudioSet v1 label names; entries missing from the model's 527
# labels are dropped at runtime (e.g. 'Scream' is not a v1 label).
DEFAULT_CLASSES = [
    "Laughter",
    "Clapping",
    "Cheering",
    "Applause",
    "Crying, sobbing",
    "Cough",
    "Music",
    "Singing",
    "Gunshot, gunfire",
    "Explosion",
    "Shout",
    "Whoop",
    "Sigh",
    "Gasp",
    "Whistle",
]

_sed: Any = None  # lazily-built SoundEventDetection (module-global, like the whisper cache)


# --- configuration -------------------------------------------------------

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        logger.warning("Bad %s=%r — using %s", name, os.environ.get(name), default)
        return default


def events_enabled() -> bool:
    """True when events auto-run after each transcribe job."""
    return os.environ.get(ENABLED_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def event_classes(available: Optional[list[str]] = None) -> list[str]:
    """Interest classes for detection: env override (comma list) else default set.

    available: the model's label list; classes it lacks are dropped (pure —
    tests pass a fake list). None = trust the env/default (used at runtime)."""
    raw = os.environ.get(CLASSES_ENV, "").strip()
    wanted = [c.strip() for c in raw.split(",") if c.strip()] if raw else list(DEFAULT_CLASSES)
    if available is None:
        return wanted
    return [c for c in wanted if c in available]


def _effective_device() -> str:
    forced = os.environ.get(DEVICE_ENV, "").strip().lower()
    if forced in ("cuda", "cpu"):
        return forced
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:  # torch import failing -> CPU is the honest fallback
        return "cpu"


def _checkpoint_path() -> str:
    env = os.environ.get(CHECKPOINT_ENV, "").strip()
    if env:
        return env
    return str(Path.home() / "panns_data" / "Cnn14_DecisionLevelMax_mAP=0.385.pth")


# --- model ---------------------------------------------------------------

def _sed_model() -> Any:
    """Lazy singleton SoundEventDetection (heavy imports stay out of module load)."""
    global _sed
    if _sed is None:
        import os as _os

        _os.environ.setdefault("MPLBACKEND", "Agg")  # panns imports matplotlib
        from panns_inference import SoundEventDetection

        ckpt = _checkpoint_path()
        if not Path(ckpt).is_file():
            raise FileNotFoundError(
                f"PANNs checkpoint missing: {ckpt} — download Cnn14_DecisionLevelMax_"
                "mAP=0.385.pth into ~/panns_data (zenodo record 3987831)"
            )
        t0 = time.monotonic()
        _sed = SoundEventDetection(checkpoint_path=ckpt, device=_effective_device())
        logger.info("PANNs SED loaded from %s in %.1fs (device=%s)",
                    ckpt, time.monotonic() - t0, _effective_device())
    return _sed


# --- pure detection core (no model — unit-testable) ----------------------

def extract_events(
    framewise_probs: "Any",
    classes: list[str],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    min_sec: float = DEFAULT_MIN_SEC,
    offset_sec: float = 0.0,
) -> list[dict]:
    """Turn a (n_frames, len(classes)) probability matrix into event rows.

    For each class: frames at/above the threshold form runs; runs shorter
    than min_sec are dropped; the row score is the run's max probability.
    Pure numpy — the unit tests feed synthetic matrices."""
    import numpy as np

    probs = np.asarray(framewise_probs, dtype=np.float64)
    if probs.ndim != 2 or probs.shape[1] != len(classes):
        raise ValueError(f"framewise_probs must be (n_frames, {len(classes)})")
    min_frames = max(1, int(round(min_sec / FRAME_SEC)))
    out: list[dict] = []
    for ci, label in enumerate(classes):
        mask = probs[:, ci] >= threshold
        i = 0
        n = len(mask)
        while i < n:
            if not mask[i]:
                i += 1
                continue
            j = i
            while j + 1 < n and mask[j + 1]:
                j += 1
            if j - i + 1 >= min_frames:
                seg = probs[i : j + 1, ci]
                out.append({
                    "start_sec": round(offset_sec + i * FRAME_SEC, 3),
                    "end_sec": round(offset_sec + (j + 1) * FRAME_SEC, 3),
                    "event": label,
                    "score": round(float(seg.max()), 4),
                })
            i = j + 1
    return out


def merge_events(events: Iterable[dict], gap_sec: float = 0.25) -> list[dict]:
    """Merge same-class runs closer than gap_sec (window boundaries can split
    one continuous event); the merged score is the max of the parts."""
    merged: list[dict] = []
    for ev in sorted(events, key=lambda e: (e["event"], e["start_sec"])):
        if merged and (
            merged[-1]["event"] == ev["event"]
            and ev["start_sec"] - merged[-1]["end_sec"] <= gap_sec
        ):
            merged[-1]["end_sec"] = max(merged[-1]["end_sec"], ev["end_sec"])
            merged[-1]["score"] = max(merged[-1]["score"], ev["score"])
        else:
            merged.append(dict(ev))
    return merged


# --- video-level pipeline ------------------------------------------------

def detect_events(
    audio_16k: "Any",
    speech: list[tuple[float, float]],
    classes: Optional[list[str]] = None,
    *,
    progress_cb: Optional[Callable[[float, float], None]] = None,
) -> list[dict]:
    """Score VAD speech regions with the SED model; returns event rows.

    audio_16k: mono 16 kHz float32 samples (decode_audio output). speech:
    [(start_sec, end_sec)] regions — only these are scored. The audio is
    resampled to the model's 32 kHz once, then sliced into fixed windows
    (VODRIP_EVENTS_WINDOW_SEC, default 30 s) per region."""
    import numpy as np
    import librosa

    if classes is None:
        classes = event_classes(_labels_of(_sed_model()))
    if not classes:
        return []
    sed = _sed_model()
    threshold = _env_float(THRESHOLD_ENV, DEFAULT_THRESHOLD)
    min_sec = _env_float(MIN_SEC_ENV, DEFAULT_MIN_SEC)
    window = max(5.0, _env_float(WINDOW_ENV, DEFAULT_WINDOW_SEC))

    t0 = time.monotonic()
    audio = librosa.resample(np.asarray(audio_16k, dtype=np.float32),
                             orig_sr=SR_16K, target_sr=SR_32K)
    logger.info("resampled %d 16k samples -> 32k in %.1fs",
                len(audio_16k), time.monotonic() - t0)

    label_to_ix = {lab: i for i, lab in enumerate(sed.labels)}
    cls_idx = [label_to_ix[c] for c in classes]
    win_samples = int(window * SR_32K)
    events: list[dict] = []
    done_sec = 0.0
    for start, end in speech:
        seg = audio[int(start * SR_32K) : int(end * SR_32K)]
        for ws in range(0, len(seg), win_samples):
            chunk = seg[ws : ws + win_samples]
            if len(chunk) < win_samples:  # pad the tail window with zeros
                chunk = np.pad(chunk, (0, win_samples - len(chunk)))
            frame = sed.inference(chunk[None, :])  # (1, n_frames, 527)
            frame = np.asarray(frame[0])[:, cls_idx]
            events.extend(extract_events(
                frame, classes, threshold=threshold, min_sec=min_sec,
                offset_sec=start + ws / SR_32K,
            ))
        done_sec += end - start
        if progress_cb:
            progress_cb(done_sec, sum(e - s for s, e in speech))
    merged = merge_events(events)
    logger.info("PANNs: %d raw runs -> %d events for %.1fs speech (%.1fs wall)",
                len(events), len(merged), done_sec, time.monotonic() - t0)
    return merged


def _labels_of(sed: Any) -> list[str]:
    return list(getattr(sed, "labels", []) or [])


def detect_events_video(
    platform: str,
    video_id: str,
    *,
    progress_cb: Optional[Callable[[float, float], None]] = None,
) -> dict:
    """Run the events stage for one archived video; replaces old event rows.

    Mirrors transcribe_video: resolve the row, decode, VAD, score, upsert
    (delete-then-insert). Returns a stats dict for job reporting. No-speech
    videos skip without loading the model."""
    from services.archive_transcribe import decode_audio, vad_speech_seconds

    rows = archive_db.query(
        "SELECT * FROM videos WHERE platform = ? AND video_id = ?",
        (platform, video_id),
    )
    if not rows:
        raise KeyError(f"no archived video {platform}/{video_id}")
    path = rows[0]["archive_path"]
    if not path or not os.path.isfile(path):
        raise FileNotFoundError(f"archive file missing for {platform}/{video_id}: {path}")

    t0 = time.monotonic()
    audio = decode_audio(path)
    total_sec = audio.size / SR_16K
    speech = vad_speech_seconds(audio)
    speech_sec = sum(e - s for s, e in speech)
    dead_air_sec = max(0.0, total_sec - speech_sec)
    stats = {
        "platform": platform,
        "video_id": video_id,
        "model": "Cnn14_DecisionLevelMax",
        "device": _effective_device(),
        "total_sec": round(total_sec, 3),
        "speech_sec": round(speech_sec, 3),
        "dead_air_sec": round(dead_air_sec, 3),
        "dead_air_pct": round(dead_air_sec / total_sec * 100.0, 1) if total_sec else 0.0,
    }

    # No-speech skip: same 3 s rule as transcription — report without ever
    # loading the model (which costs ~10 s + VRAM).
    if speech_sec < 3.0:
        stats.update({"events": 0, "wall_sec": round(time.monotonic() - t0, 3),
                      "speed_x": 0.0, "skipped": "no-speech"})
        return stats

    classes = event_classes(_labels_of(_sed_model()))
    events = detect_events(audio, speech, classes, progress_cb=progress_cb)
    archive_db.delete_audio_events(platform, video_id)  # replace-on-rerun
    archive_db.insert_audio_events(platform, video_id, events)
    wall = time.monotonic() - t0
    stats.update({
        "events": len(events),
        "classes": len(classes),
        "wall_sec": round(wall, 3),
        "speed_x": round(total_sec / wall, 2) if wall > 0 else 0.0,
    })
    logger.info("events %s/%s: %d rows (%.1fs total, %.1f%% dead air skipped)",
                platform, video_id, len(events), total_sec, stats["dead_air_pct"])
    return stats


# --- module self-check (pure logic — no model load, no GPU) ---------------

_classes = ["Laughter", "Clapping", "Scream"]
_frames = [[0.9, 0.1, 0.8], [0.9, 0.1, 0.8], [0.9, 0.9, 0.8],
           [0.1, 0.9, 0.8], [0.1, 0.9, 0.1], [0.1, 0.9, 0.1]]
_ev = extract_events(_frames, _classes, threshold=0.5, min_sec=0.0)
# Laughter 0.00-0.03 (frames 0-2), Clapping 0.02-0.06 (2-5), Scream 0.00-0.04 (0-3)
assert [e["event"] for e in _ev] == ["Laughter", "Clapping", "Scream"], _ev
assert abs(_ev[0]["start_sec"] - 0.0) < 1e-9 and abs(_ev[0]["end_sec"] - 0.03) < 1e-9
assert _ev[0]["score"] == 0.9 and _ev[1]["score"] == 0.9, "run score = max frame prob"
assert _ev[2]["score"] == 0.8 and abs(_ev[2]["end_sec"] - 0.04) < 1e-9
_ev2 = extract_events(_frames, _classes, threshold=0.5, min_sec=0.05)
assert _ev2 == [], "runs shorter than min_sec must drop (0.03-0.04s < 0.05s)"
_merged = merge_events([
    {"start_sec": 0.0, "end_sec": 1.0, "event": "Laughter", "score": 0.7},
    {"start_sec": 1.1, "end_sec": 2.0, "event": "Laughter", "score": 0.9},
    {"start_sec": 5.0, "end_sec": 6.0, "event": "Laughter", "score": 0.5},
])
assert len(_merged) == 2 and _merged[0]["end_sec"] == 2.0 and _merged[0]["score"] == 0.9

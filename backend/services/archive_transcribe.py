"""Transcription worker — faster-whisper (CTranslate2) + Silero VAD for the local archive.

Consumes ``archive_jobs`` rows with kind='transcribe' and writes word-timestamped
segments into the ``transcripts`` table (see archive_db.insert_transcript).

Design decisions:
  * VAD pre-pass: Silero VAD splits the audio into speech regions; ONLY those are
    fed to the model. Non-speech never reaches the model and is NOT counted in
    progress — progress = speech seconds transcribed / total speech seconds.
  * Resume: each run writes a JSONL manifest next to the archive DB mapping
    chunk -> seg_idx range; re-runs skip chunks whose range is fully present
    (verified against transcript_for()). Segments are inserted one row per
    insert_transcript() call, so a crash loses at most the in-flight segment.
  * Model cache: one process-global WhisperModel, lazy-loaded on first job,
    unloaded after VODRIP_WHISPER_IDLE_CLOSE seconds (default 600) without use.
  * Device: detect_gpu_vendor() — 'nvidia' -> cuda/float16, everything else
    cpu/int8 (honest: this machine has no NVIDIA GPU, so real runs are CPU).
  * Default model 'large-v3-turbo'; override with env VODRIP_WHISPER_MODEL
    (e.g. 'freds0/distil-whisper-large-v3-ptbr' or 'small').

Opt-in by design: app.py does NOT import this module. Start the worker with
``python -m services.archive_transcribe`` or ``start_worker()`` from a launcher.
"""
from __future__ import annotations

import gc
import json
import logging
import os
import re
import subprocess as sp
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Optional

from services import archive_db
from services.gpu_detect import detect_gpu_vendor
from services.os_services import _NO_WINDOW
from services.ytdlp_ffmpeg import _resolve_ffmpeg_exe

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "large-v3-turbo"
SAMPLE_RATE = 16000

# Env knobs (all optional).
MODEL_ENV = "VODRIP_WHISPER_MODEL"
LANG_ENV = "VODRIP_WHISPER_LANGUAGE"
CACHE_ENV = "VODRIP_WHISPER_CACHE"
WORKERS_ENV = "VODRIP_TRANSCRIBE_WORKERS"
IDLE_ENV = "VODRIP_WHISPER_IDLE_CLOSE"

# --- device / compute -----------------------------------------------------

@lru_cache(maxsize=1)
def _detect_device() -> tuple[str, str]:
    """(device, compute_type) — nvidia GPU or honest CPU fallback.

    VODRIP_WHISPER_DEVICE=cpu|cuda forces the choice (used by tests/benchmarks).
    """
    forced = os.environ.get("VODRIP_WHISPER_DEVICE", "").strip().lower()
    if forced:
        if forced == "cuda":
            return "cuda", "float16"
        if forced == "cpu":
            return "cpu", "int8"
        logger.warning("Unknown VODRIP_WHISPER_DEVICE=%r — ignoring", forced)
    vendor = detect_gpu_vendor()
    if vendor == "nvidia":
        return "cuda", "float16"
    return "cpu", "int8"


_device_override: Optional[tuple[str, str]] = None  # set after a CUDA failure


def _effective_device() -> tuple[str, str]:
    return _device_override or _detect_device()


def device_settings() -> tuple[str, str]:
    return _detect_device()


def model_name() -> str:
    return os.environ.get(MODEL_ENV, "").strip() or DEFAULT_MODEL


def _cache_dir() -> Path:
    override = os.environ.get(CACHE_ENV, "").strip()
    if override:
        return Path(override)
    from services.settings import _get_appdata_dir

    return _get_appdata_dir() / "whisper-models"


# --- model cache ----------------------------------------------------------

_model_lock = threading.Lock()
_model: Any = None
_model_name: Optional[str] = None
_model_last_used = 0.0
_word_ts_ok = True  # distil models have no alignment heads; flipped per process


def _idle_close_seconds() -> float:
    try:
        return float(os.environ.get(IDLE_ENV, "600") or "600")
    except ValueError:
        return 600.0


def _ensure_cuda_libs() -> None:
    """Expose pip-installed NVIDIA runtime DLLs (nvidia-*-cu12 wheels) on PATH.

    ctranslate2 loads cublas64_12.dll lazily at first CUDA inference; machines
    with a CUDA-13-era driver but no full CUDA 12 toolkit otherwise fail with
    "Library cublas64_12.dll is not found". Set VODRIP_NO_CUDA_LIBS to skip.
    """
    if os.environ.get("VODRIP_NO_CUDA_LIBS"):
        return
    try:
        import site as _site
    except Exception:
        return
    for lib in ("cublas", "cuda_runtime"):
        try:
            for root in _site.getsitepackages():
                d = Path(root) / "nvidia" / lib / "bin"
                if d.is_dir() and str(d) not in os.environ.get("PATH", ""):
                    os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
        except Exception:
            # ponytail: best-effort — missing wheels just mean CPU fallback
            pass


def _get_model() -> Any:
    """Return the process-global WhisperModel, lazy-loading on first use.

    Re-loads if VODRIP_WHISPER_MODEL changed since last load. Not thread-safe
    for *concurrent* transcribe() calls — callers serialize via _infer_lock.
    """
    global _model, _model_name, _model_last_used, _device_override
    name = model_name()
    with _model_lock:
        if _model is not None and _model_name == name:
            _model_last_used = time.monotonic()
            return _model
        _close_model_unlocked()  # different model env -> drop the old one first
        _ensure_cuda_libs()  # must precede the ctranslate2 import/DLL load
        from faster_whisper import WhisperModel

        device, compute_type = _effective_device()
        t0 = time.monotonic()
        logger.info(
            "Loading whisper model %r (device=%s compute_type=%s, cache=%s)...",
            name, device, compute_type, _cache_dir(),
        )
        try:
            _model = WhisperModel(
                name,
                device=device,
                compute_type=compute_type,
                download_root=str(_cache_dir()),
            )
        except Exception as exc:
            if device == "cuda":
                # Driver/GPU hiccup — fall back to CPU for the process lifetime
                # instead of failing every queued job.
                logger.warning("CUDA model load failed (%s) — falling back to CPU", exc)
                _device_override = ("cpu", "int8")
                device, compute_type = _device_override
                _model = WhisperModel(
                    name,
                    device=device,
                    compute_type=compute_type,
                    download_root=str(_cache_dir()),
                )
            else:
                raise
        _model_name = name
        _model_last_used = time.monotonic()
        logger.info("Whisper model %r loaded in %.1fs", name, time.monotonic() - t0)
        return _model


def _close_model_unlocked() -> None:
    """Drop the cached model — caller MUST hold _model_lock."""
    global _model, _model_name
    model, _model = _model, None
    _model_name = None
    if model is not None:
        logger.info("Unloading whisper model")
        del model
        gc.collect()


def close_model() -> None:
    """Unload the cached model, freeing its RAM. Safe mid-transcription: workers
    hold a local reference, so the object lives until their last use."""
    with _model_lock:
        _close_model_unlocked()


def _maybe_close_idle_model() -> None:
    """Close the model after VODRIP_WHISPER_IDLE_CLOSE seconds without use."""
    if _model is None:
        return
    idle = time.monotonic() - _model_last_used
    if idle > _idle_close_seconds():
        logger.info("Model idle for %.0fs — unloading", idle)
        close_model()


# --- audio decode ---------------------------------------------------------

def decode_audio(path: str, ffmpeg_bin: Optional[str] = None) -> "Any":
    """Decode any media file to mono 16 kHz float32 samples via ffmpeg.

    Returns a numpy float32 1-D array (16k samples/sec).
    """
    import numpy as np

    if ffmpeg_bin is None:
        ffmpeg_bin = _resolve_ffmpeg_exe()
    cmd = [
        ffmpeg_bin, "-nostdin", "-v", "error", "-i", str(path),
        "-f", "f32le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-",
    ]
    proc = sp.run(cmd, capture_output=True, timeout=3600, creationflags=_NO_WINDOW)
    if proc.returncode != 0:
        stderr = (proc.stderr or b"").decode("utf-8", "replace")[-300:]
        raise RuntimeError(f"ffmpeg decode failed for {path}: {stderr}")
    samples = np.frombuffer(proc.stdout, dtype=np.float32)
    if samples.size == 0:
        raise RuntimeError(f"ffmpeg produced no audio from {path}")
    return samples.copy()  # writable copy: torch.from_numpy needs writable memory


# --- VAD pre-pass ---------------------------------------------------------

_vad_lock = threading.Lock()
_vad: Any = None


def _get_vad() -> Any:
    """Lazy Silero VAD model (bundled in the package — no download)."""
    global _vad
    with _vad_lock:
        if _vad is None:
            from silero_vad import load_silero_vad

            _vad = load_silero_vad(onnx=False)
        return _vad


def vad_speech_seconds(audio: "Any") -> list[tuple[float, float]]:
    """Return [(start_sec, end_sec), ...] speech regions from Silero VAD."""
    import torch
    from silero_vad import get_speech_timestamps

    tensor = torch.from_numpy(audio)
    timestamps = get_speech_timestamps(
        tensor,
        _get_vad(),
        sampling_rate=SAMPLE_RATE,
        threshold=0.5,
        min_speech_duration_ms=250,
        min_silence_duration_ms=200,
        speech_pad_ms=30,
        return_seconds=True,
    )
    return [(float(t["start"]), float(t["end"])) for t in timestamps]


def _plan_chunks(
    speech: list[tuple[float, float]],
    merge_gap: float = 0.8,
    min_len: float = 0.25,
) -> list[tuple[float, float]]:
    """Merge nearby speech regions into transcribe chunks; drop sub-minimum ones.

    Deterministic — resume relies on identical chunks across runs.
    """
    chunks: list[tuple[float, float]] = []
    cur_s = cur_e = None
    for s, e in speech:
        if cur_s is None:
            cur_s, cur_e = s, e
        elif s - cur_e <= merge_gap:
            cur_e = max(cur_e, e)
        else:
            if cur_e - cur_s >= min_len:
                chunks.append((cur_s, cur_e))
            cur_s, cur_e = s, e
    if cur_s is not None and cur_e - cur_s >= min_len:
        chunks.append((cur_s, cur_e))
    return chunks


# --- resume manifest (JSONL) ----------------------------------------------
# One line per completed chunk: {"ci": i, "first": first_seg_idx, "count": n}.
# Header line (rewritten each run) carries the chunk plan so stale entries
# (audio changed / model changed) are ignored instead of misapplied.

def _sanitize_key(key: str) -> str:
    return re.sub(r"[^\w.-]", "_", key)


def _manifest_path(platform: str, video_id: str) -> Path:
    base = Path(archive_db._db_path()).parent / "whisper_manifest"
    return base / f"{platform}__{_sanitize_key(video_id)}.jsonl"


def _read_manifest(path: Path) -> tuple[Optional[dict], dict[int, dict]]:
    """Return (header, {ci: entry}) — missing/corrupt file yields empty plan."""
    header: Optional[dict] = None
    entries: dict[int, dict] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "chunks" in data:
                    header = data
                elif isinstance(data.get("ci"), int):
                    entries[data["ci"]] = data
    except OSError:
        pass
    return header, entries


def _write_manifest_header(path: Path, chunks: list[tuple[float, float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"chunks": chunks, "model": model_name()}) + "\n")


def _append_manifest_entry(path: Path, ci: int, first: int, count: int) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ci": ci, "first": first, "count": count}) + "\n")


def _resume_plan(
    chunks: list[tuple[float, float]],
    header: Optional[dict],
    entries: dict[int, dict],
    existing: set[int],
) -> tuple[list[int], int]:
    """Return (chunk indices to transcribe, next free seg_idx).

    Entries are trusted only when the header matches the current plan and model.
    A chunk is also re-transcribed when any row in its recorded seg_idx range is
    missing (manual delete / partial write), and the next free index is the
    LOWEST gap in existing — so deleted rows are restored at their old index
    and the seg_idx sequence stays contiguous."""
    if not chunks:
        return [], 0
    next_idx = 0
    while next_idx in existing:
        next_idx += 1
    if not (header and entries):
        return list(range(len(chunks))), next_idx
    if header.get("chunks") != chunks or header.get("model") != model_name():
        return list(range(len(chunks))), next_idx
    missing: list[int] = []
    for ci in range(len(chunks)):
        entry = entries.get(ci)
        if entry is None:
            missing.append(ci)
            continue
        rng = range(entry["first"], entry["first"] + entry["count"])
        if any(i not in existing for i in rng):
            missing.append(ci)  # rows deleted since the last run
    return missing, next_idx


# --- transcription --------------------------------------------------------

_infer_lock = threading.Lock()  # CTranslate2 model instances are not thread-safe


def _transcribe_chunk(
    model: Any,
    audio: "Any",
    chunk_start: float,
    chunk_end: float,
    language: Optional[str],
) -> list[dict]:
    """Transcribe audio[chunk_start:chunk_end] into absolute-time segments."""
    import numpy as np

    global _word_ts_ok, _device_override
    lo, hi = int(chunk_start * SAMPLE_RATE), int(chunk_end * SAMPLE_RATE)
    chunk_audio = audio[lo:hi] if isinstance(audio, np.ndarray) else audio
    kwargs: dict[str, Any] = dict(
        language=language,
        beam_size=5,
        condition_on_previous_text=False,
        vad_filter=False,  # our own VAD pre-pass already gated the audio
    )
    if _word_ts_ok:
        kwargs["word_timestamps"] = True
    with _infer_lock:
        try:
            raw = list(model.transcribe(chunk_audio, **kwargs)[0])
        except ValueError as exc:
            if not _word_ts_ok:
                raise
            # distil models lack alignment heads — fall back to plain segments.
            logger.info("Word timestamps unsupported (%s) — falling back", exc)
            _word_ts_ok = False
            kwargs.pop("word_timestamps", None)
            raw = list(model.transcribe(chunk_audio, **kwargs)[0])
        except RuntimeError as exc:
            if _effective_device()[0] != "cuda" or _device_override is not None:
                raise
            # GPU present but inference broken (missing cuBLAS, driver hiccup):
            # drop to CPU for the process lifetime and retry this chunk once.
            logger.warning("CUDA inference failed (%s) — falling back to CPU", exc)
            _device_override = ("cpu", "int8")
            model = _get_model()  # reload on CPU
            if not _word_ts_ok:
                kwargs.pop("word_timestamps", None)
            raw = list(model.transcribe(chunk_audio, **kwargs)[0])
    out: list[dict] = []
    for seg in raw:
        words = [
            {
                "word": w.word,
                "start": round(chunk_start + w.start, 3),
                "end": round(chunk_start + w.end, 3),
            }
            for w in (seg.words or [])
        ]
        out.append({
            "start_sec": round(chunk_start + seg.start, 3),
            "end_sec": round(chunk_start + seg.end, 3),
            "text": (seg.text or "").strip(),
            "words": words,
        })
    return out


def transcribe_video(
    platform: str,
    video_id: str,
    *,
    language: Optional[str] = None,
    progress_cb: Optional[Callable[[float, float, int, int], None]] = None,
) -> dict:
    """Transcribe one archived video into the transcripts table (resume-aware).

    progress_cb(speech_done_sec, speech_total_sec, chunk_done, chunk_total) —
    non-speech time is deliberately excluded from the denominator.
    Returns a stats dict (also suitable for job reporting).
    """
    rows = archive_db.query(
        "SELECT * FROM videos WHERE platform = ? AND video_id = ?",
        (platform, video_id),
    )
    if not rows:
        raise KeyError(f"no archived video {platform}/{video_id}")
    path = rows[0]["archive_path"]
    if not path or not os.path.isfile(path):
        raise FileNotFoundError(f"archive file missing for {platform}/{video_id}: {path}")

    if language is None:
        language = os.environ.get(LANG_ENV, "").strip() or None

    model = _get_model()
    t0 = time.monotonic()
    audio = decode_audio(path)
    total_sec = audio.size / SAMPLE_RATE
    speech = vad_speech_seconds(audio)
    chunks = _plan_chunks(speech)
    speech_sec = sum(e - s for s, e in chunks)
    dead_air_sec = max(0.0, total_sec - speech_sec)
    dead_air_pct = (dead_air_sec / total_sec * 100.0) if total_sec > 0 else 0.0

    logger.info(
        "VAD: %.1fs total, %.1fs speech (%.0f%%), %.1fs dead air skipped",
        total_sec, speech_sec, speech_sec / total_sec * 100 if total_sec else 0,
        dead_air_sec,
    )

    existing = {int(r["seg_idx"]) for r in archive_db.transcript_for(platform, video_id)}
    header, entries = _read_manifest(_manifest_path(platform, video_id))
    missing, seg_idx = _resume_plan(chunks, header, entries, existing)
    if missing != list(range(len(chunks))):
        logger.info("Resume: %d/%d chunks already transcribed — skipping",
                    len(chunks) - len(missing), len(chunks))

    manifest = _manifest_path(platform, video_id)
    _write_manifest_header(manifest, chunks)

    segments = 0
    words = 0
    speech_done = 0.0
    for ci, (cs, ce) in enumerate(chunks):
        if ci not in missing:
            speech_done += ce - cs
            continue
        chunk_segs = _transcribe_chunk(model, audio, cs, ce, language)
        # Per-segment insert: a crash loses at most the in-flight segment.
        first_idx = seg_idx
        for seg in chunk_segs:
            if seg_idx in existing:
                seg_idx += 1
                continue
            row = {
                "seg_idx": seg_idx,
                "start_sec": seg["start_sec"],
                "end_sec": seg["end_sec"],
                "text": seg["text"],
                "words": seg["words"],
            }
            archive_db.insert_transcript(platform, video_id, [row])
            segments += 1
            words += len(seg["words"])
            seg_idx += 1
        _append_manifest_entry(manifest, ci, first_idx, len(chunk_segs))
        speech_done += ce - cs
        if progress_cb:
            progress_cb(speech_done, speech_sec, ci + 1, len(chunks))

    wall = time.monotonic() - t0
    stats = {
        "platform": platform,
        "video_id": video_id,
        "model": model_name(),
        "device": _effective_device()[0],
        "compute_type": _effective_device()[1],
        "total_sec": round(total_sec, 3),
        "speech_sec": round(speech_sec, 3),
        "dead_air_sec": round(dead_air_sec, 3),
        "dead_air_pct": round(dead_air_pct, 1),
        "segments": segments,
        "words": words,
        "resumed_chunks": len(chunks) - len(missing),
        "wall_sec": round(wall, 3),
        "speed_x": round(speech_sec / wall, 2) if wall > 0 else 0.0,
    }
    logger.info(
        "transcribe %s/%s done: %d segs, %d words, %.1f%% dead air skipped, "
        "%.2fx realtime on %s/%s",
        platform, video_id, segments, words, dead_air_pct,
        stats["speed_x"], stats["device"], stats["compute_type"],
    )
    return stats


# --- queue worker ---------------------------------------------------------

_STALE_JOB_TIMEDELTA = timedelta(minutes=30)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _claim_next_job() -> Optional[dict]:
    """Atomically claim the newest queued transcribe job (crash-stale ones too).

    A 'running' job is reclaimed only if untouched for 30 min — a single
    chunk can legitimately take that long on CPU."""
    now = datetime.now(timezone.utc)
    stale_cutoff = (now - _STALE_JOB_TIMEDELTA).isoformat(timespec="seconds")
    # String comparison is valid: both sides come from _now_iso (UTC, same width).
    rows = archive_db.query(
        """SELECT * FROM archive_jobs
           WHERE kind = 'transcribe'
             AND (status = 'queued' OR (status = 'running' AND updated_at < ?))
           ORDER BY created_at DESC
           LIMIT 8""",
        (stale_cutoff,),
    )
    for row in rows:
        if row["status"] == "queued":
            cur = archive_db.execute(
                "UPDATE archive_jobs SET status = 'running', updated_at = ? "
                "WHERE id = ? AND status = 'queued'",
                (_now_iso(), row["id"]),
            )
        else:
            cur = archive_db.execute(
                "UPDATE archive_jobs SET status = 'running', updated_at = ? "
                "WHERE id = ? AND status = 'running'",
                (_now_iso(), row["id"]),
            )
        if cur.rowcount == 1:
            return dict(row)
    return None


def _process_job(job: dict) -> dict:
    """Run one claimed job; never raises — failures land in archive_jobs.error."""
    job_id = job["id"]
    platform, video_id = job["platform"], job["video_id"]
    try:
        archive_db.update_job(job_id, status="running", progress=0.0)

        def _progress(done: float, total: float, _ci: int, _n: int) -> None:
            if total > 0:
                archive_db.update_job(job_id, progress=min(0.999, done / total))

        stats = transcribe_video(platform, video_id, progress_cb=_progress)
        archive_db.update_job(job_id, status="done", progress=1.0)
        return stats
    except Exception as exc:  # job-level failure — worker keeps going
        logger.exception("transcribe job %s failed", job_id)
        archive_db.update_job(job_id, status="failed",
                              error=f"{type(exc).__name__}: {exc}"[:400])
        return {"job_id": job_id, "error": str(exc)}


def run_worker(
    *,
    once: bool = False,
    poll_interval: float = 2.0,
    max_workers: Optional[int] = None,
) -> None:
    """Blocking worker loop over the transcribe queue.

    GPU jobs serialize (max_workers=1); CPU jobs run 2–3 at a time via a
    ThreadPoolExecutor (decode/VAD/DB run truly parallel; model inference is
    serialized by _infer_lock since CTranslate2 model instances aren't
    thread-safe — real parallel inference needs per-worker model copies).
    """
    device, compute_type = _effective_device()
    if max_workers is None:
        if device == "cuda":
            max_workers = 1
        else:
            try:
                max_workers = max(2, min(3, int(os.environ.get(WORKERS_ENV, "2") or "2")))
            except ValueError:
                max_workers = 2
    logger.info("archive transcribe worker: device=%s compute_type=%s workers=%d",
                device, compute_type, max_workers)
    try:
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="transcribe") as pool:
            while True:
                _maybe_close_idle_model()
                claimed = []
                for _ in range(max_workers):
                    job = _claim_next_job()
                    if job is None:
                        break
                    claimed.append(job)
                if not claimed:
                    if once:
                        break
                    time.sleep(poll_interval)
                    continue
                futures = [pool.submit(_process_job, j) for j in claimed]
                for fut in futures:
                    try:
                        fut.result()
                    except Exception:
                        logger.exception("worker future crashed")  # belt & braces
                if once:
                    break
    finally:
        close_model()


def start_worker(**kwargs: Any) -> threading.Thread:
    """Spawn the worker loop in a daemon thread (never blocks the caller)."""
    thread = threading.Thread(
        target=run_worker, kwargs=kwargs, name="archive-transcribe", daemon=True
    )
    thread.start()
    return thread


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    run_worker()

# --- module self-check (pure logic — no model load, no GPU, no downloads) --

_speech = [(0.0, 5.0), (5.8, 6.2), (20.0, 30.0)]
assert _plan_chunks(_speech, merge_gap=1.0, min_len=0.25) == [(0.0, 6.2), (20.0, 30.0)], (
    "speech regions within the merge gap must fuse"
)
assert _plan_chunks([(0.0, 0.2)]) == [], "sub-minimum chunks must be dropped"
assert _plan_chunks([]) == [], "empty VAD output must plan no chunks"
assert _plan_chunks([(0.0, 5.0), (10.0, 15.0)]) == [(0.0, 5.0), (10.0, 15.0)], (
    "wide gaps must stay separate chunks"
)
assert _detect_device() in (("cuda", "float16"), ("cpu", "int8")), (
    "device settings must be a known pair (nvidia -> cuda/float16, else cpu/int8)"
)
assert _sanitize_key("abc/def:123") == "abc_def_123"
_header = {"chunks": [(0.0, 5.0), (10.0, 15.0), (20.0, 25.0)], "model": model_name()}
_entries = {0: {"ci": 0, "first": 0, "count": 2}, 1: {"ci": 1, "first": 2, "count": 3}}
_missing, _next = _resume_plan(_header["chunks"], _header, _entries, {0, 1, 2, 3, 4})
assert _missing == [2] and _next == 5, "manifest-matched chunks must be skipped"
_missing, _next = _resume_plan(_header["chunks"], _header, _entries, {0, 3, 4})
assert _missing == [0, 1, 2] and _next == 1, "deleted rows must re-mark their chunk and reuse the lowest gap"
_missing, _next = _resume_plan(_header["chunks"], None, _entries, {0, 1})
assert _missing == [0, 1, 2] and _next == 2, "missing manifest -> full re-run"
_stale = dict(_header, model="different-model")
_missing, _ = _resume_plan(_header["chunks"], _stale, _entries, {0, 1})
assert _missing == [0, 1, 2], "model change must invalidate stale manifest entries"
_missing, _next = _resume_plan([], None, {}, set())
assert _missing == [] and _next == 0, "no chunks -> nothing to do"

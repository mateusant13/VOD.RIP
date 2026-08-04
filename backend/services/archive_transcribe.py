"""Transcription worker — faster-whisper (CTranslate2) + Silero VAD for the local archive.

Consumes ``archive_jobs`` rows with kind='transcribe' (and kind='events' —
the PANNs acoustic-event stage, see services.archive_events) and writes
word-timestamped segments into the ``transcripts`` table (see
archive_db.insert_transcript).

Design decisions:
  * VAD pre-pass: Silero VAD splits the audio into speech regions; ONLY those are
    fed to the model. Non-speech never reaches the model and is NOT counted in
    progress — progress = speech seconds transcribed / total speech seconds.
  * Resume: each run writes a JSONL manifest next to the archive DB mapping
    chunk -> seg_idx range; re-runs skip chunks whose range is fully present
    (verified against transcript_for()). A chunk's segments are inserted with
    ONE insert_transcript() batch call, so a crash loses at most the
    in-flight chunk. A FULL re-run (no manifest, rows already present)
    replaces the old rows instead of appending a duplicate copy.
  * Model cache: one process-global WhisperModel by default (budget 1),
    lazy-loaded on first job, unloaded after VODRIP_WHISPER_IDLE_CLOSE
    seconds (default 600) without use. Multi-copy mode (budget > 1 — CPU
    workers or opt-in VODRIP_TRANSCRIBE_GPU_COPIES) gives each pool thread
    its own model so inference runs in parallel.
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
from services.archive_events import detect_events_video, events_enabled
from services.disk_hygiene import active_whisper_model_id, whisper_cache_dir
from services.gpu_detect import detect_gpu_vendor
from services.os_services import _NO_WINDOW
from services.ytdlp_ffmpeg import _resolve_ffmpeg_exe

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "large-v3-turbo"
SAMPLE_RATE = 16000

# Env knobs (all optional).
LANG_ENV = "VODRIP_WHISPER_LANGUAGE"
WORKERS_ENV = "VODRIP_TRANSCRIBE_WORKERS"
IDLE_ENV = "VODRIP_WHISPER_IDLE_CLOSE"
GPU_COPIES_ENV = "VODRIP_TRANSCRIBE_GPU_COPIES"
BEAM_ENV = "VODRIP_WHISPER_BEAM"
BATCH_ENV = "VODRIP_WHISPER_BATCH"

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
    # Shared resolver: VODRIP_WHISPER_MODEL env override -> settings.whisper_model -> default.
    return active_whisper_model_id()


def _cache_dir() -> Path:
    # Shared resolver: VODRIP_WHISPER_CACHE env -> settings.whisper_model_cache -> appdata.
    return whisper_cache_dir()


# --- parallelism budget ---------------------------------------------------

def _clamp_cuda_copies(copies: int, free_vram_bytes: int) -> int:
    """min(copies, max(1, free_vram // 2 GiB)) — the GPU copy budget clamp.

    Pure shape so the module self-check can pin it without a GPU: env 1 -> 1
    (never probe), env >1 -> VRAM-capped but never below 1 copy."""
    if copies <= 1:
        return 1
    vram_cap = max(1, free_vram_bytes // (2 * 1024 ** 3))
    return max(1, min(copies, vram_cap))


def _worker_budget() -> int:
    """Max concurrent transcribe jobs: GPU model copies or CPU threads.

    CUDA: VODRIP_TRANSCRIBE_GPU_COPIES (default 1) clamped by free VRAM
    (probed once via torch.cuda.mem_get_info() — torch is already imported
    by the VAD path); the clamp degrades gracefully to 1 when the probe
    fails. CPU: VODRIP_TRANSCRIBE_WORKERS (default 2).

    budget == 1 is the EXACT legacy path: one process-global model,
    _infer_lock serializing inference. budget > 1 (opt-in GPU copies, or the
    CPU default) gives each pool thread its own model copy so inference
    truly runs in parallel."""
    device, _ = _effective_device()
    if device == "cpu":
        try:
            return max(1, int(os.environ.get(WORKERS_ENV, "2") or "2"))
        except ValueError:
            return 2
    try:
        copies = int(os.environ.get(GPU_COPIES_ENV, "1") or "1")
    except ValueError:
        copies = 1
    if copies <= 1:
        return 1
    free_vram = 0
    try:
        import torch

        if torch.cuda.is_available():
            free_vram = int(torch.cuda.mem_get_info()[0])
    except Exception:
        pass  # probe failed — trust the env cap
    if free_vram <= 0:
        return copies
    return _clamp_cuda_copies(copies, free_vram)


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
    hold a local reference, so the object lives until their last use.

    Multi-copy mode: closes every pool thread's model too; threads lazily
    reload on their next job (the registry is cleared, so a fresh slot is
    created)."""
    closed_any = False
    with _model_lock:
        _close_model_unlocked()
        for slot in _thread_slots.values():
            model, slot.model = slot.model, None
            if model is not None:
                logger.info("Unloading whisper thread model")
                del model
                closed_any = True
        _thread_slots.clear()
    if closed_any:
        gc.collect()


def _maybe_close_idle_model() -> None:
    """Close the model after VODRIP_WHISPER_IDLE_CLOSE seconds without use.

    Applies to the process-global model only: thread models die with the
    pool (close_model on worker shutdown)."""
    if _model is None:
        return
    idle = time.monotonic() - _model_last_used
    if idle > _idle_close_seconds():
        logger.info("Model idle for %.0fs — unloading", idle)
        close_model()


# --- per-thread model copies (multi-copy mode, budget > 1) ------------------
# Each pool thread owns one WhisperModel instance so inference runs truly in
# parallel (no global _infer_lock). The registry is keyed by thread ident —
# the same per-thread keying CPython's threading.local uses internally.
# Model CREATION is serialized by _model_lock (shared hub download);
# inference never takes it. A thread whose CUDA inference OOM'd marks itself
# cpu_fallback and reloads on CPU — only that thread degrades.

_multi_tls = threading.local()  # per-thread: .active, .cpu_fallback


class _ThreadModelSlot:
    """One pool thread's lazy model state."""
    __slots__ = ("model", "model_name")

    def __init__(self) -> None:
        self.model: Any = None
        self.model_name: Optional[str] = None


_thread_slots: dict[int, _ThreadModelSlot] = {}


def _in_multi_mode() -> bool:
    return bool(getattr(_multi_tls, "active", False))


def _thread_cpu_fallback() -> bool:
    return bool(getattr(_multi_tls, "cpu_fallback", False))


def _thread_mark_cpu_fallback() -> None:
    _multi_tls.cpu_fallback = True


def _thread_slot() -> _ThreadModelSlot:
    """The calling thread's model slot, created on first use."""
    tid = threading.get_ident()
    slot = _thread_slots.get(tid)
    if slot is None:
        with _model_lock:
            slot = _thread_slots.get(tid)
            if slot is None:
                slot = _ThreadModelSlot()
                _thread_slots[tid] = slot
    return slot


def _thread_model() -> Any:
    """Lazy per-thread WhisperModel copy (multi-copy mode only).

    Mirrors _get_model's lazy load, keyed to the calling thread; creation is
    serialized by _model_lock, inference is not. A thread marked cpu_fallback
    loads on CPU even when CUDA is healthy (its copy OOM'd earlier)."""
    slot = _thread_slot()
    name = model_name()
    if slot.model is not None and slot.model_name == name:
        return slot.model
    with _model_lock:
        if slot.model is not None and slot.model_name == name:
            return slot.model
        if slot.model is not None:
            del slot.model  # stale model env / degraded copy — drop first
            slot.model = None
        _ensure_cuda_libs()
        from faster_whisper import WhisperModel

        device, compute_type = _effective_device()
        if _thread_cpu_fallback() and device == "cuda":
            device, compute_type = "cpu", "int8"
        t0 = time.monotonic()
        logger.info(
            "Loading whisper thread model %r (device=%s compute_type=%s)...",
            name, device, compute_type,
        )
        try:
            slot.model = WhisperModel(
                name,
                device=device,
                compute_type=compute_type,
                download_root=str(_cache_dir()),
            )
        except Exception as exc:
            if device == "cuda":
                # This thread's copy cannot load on CUDA (driver hiccup) —
                # degrade only this thread to CPU, not the whole process.
                logger.warning(
                    "thread CUDA model load failed (%s) — thread falls back to CPU", exc
                )
                _thread_mark_cpu_fallback()
                slot.model = WhisperModel(
                    name,
                    device="cpu",
                    compute_type="int8",
                    download_root=str(_cache_dir()),
                )
            else:
                raise
        slot.model_name = name
        logger.info("Thread whisper model %r loaded in %.1fs", name, time.monotonic() - t0)
        return slot.model


def _current_model() -> Any:
    """The model for the current context: the calling thread's own copy in
    multi-copy mode, else the process-global one. Direct callers of
    transcribe_video()/_get_model() (tests, API) always get the global path."""
    if _in_multi_mode():
        return _thread_model()
    return _get_model()


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
    if audio is None or len(audio) == 0:
        return []  # empty audio -> no speech
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
    # JSON round-trip turns the plan's tuples into lists — compare shapes.
    if [tuple(c) for c in header.get("chunks", [])] != chunks or header.get("model") != model_name():
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


def _beam_size() -> int:
    """Whisper beam width: VODRIP_WHISPER_BEAM (default 1).

    Measured on PT-BR VOD slices (corpus-anchored word overlap) beam 1 ==
    beam 5 for this workload, and greedy halves decoder work; raise via env
    when max quality is wanted."""
    try:
        return max(1, int(os.environ.get(BEAM_ENV, "1") or "1"))
    except ValueError:
        return 1


def _batch_size() -> int:
    """Clips decoded per batched call: VODRIP_WHISPER_BATCH (CUDA 16, CPU 4).

    CUDA default 16 keeps a 5080-class GPU saturated; CPU (int8) defaults to
    4 so the batched features stay small. The env cap applies to both."""
    default = 16 if _effective_device()[0] == "cuda" else 4
    try:
        return max(1, int(os.environ.get(BATCH_ENV, str(default)) or str(default)))
    except ValueError:
        return default


def _transcribe_batch(
    model: Any,
    audio: "Any",
    chunks: list[tuple[float, float]],
    language: Optional[str],
) -> list[tuple[list[dict], Optional[str]]]:
    """Batch-decode [start,end] clips via faster-whisper's batched pipeline.

    BatchedInferencePipeline decodes many 30 s windows in one GPU call, so
    thousands of VAD clips stop paying per-call launch overhead (measured
    26.9x -> ~118x realtime on a 5080; 164x with beam 1). Segments carry
    absolute video timestamps; each clip's segments are returned in input
    order, so the per-clip insert/manifest/resume contract is unchanged."""
    from faster_whisper import BatchedInferencePipeline

    global _word_ts_ok, _device_override
    clips = [{"start": s, "end": e} for s, e in chunks]
    kwargs: dict[str, Any] = dict(
        language=language,
        beam_size=_beam_size(),
        vad_filter=False,  # our own VAD pre-pass already gated the audio
        batch_size=_batch_size(),
        without_timestamps=False,  # keep timestamp-driven segment splitting
    )
    if _word_ts_ok:
        kwargs["word_timestamps"] = True

    def run(pipe: Any) -> tuple[list[Any], Any]:
        seg_iter, info = pipe.transcribe(audio, clip_timestamps=clips, **kwargs)
        return list(seg_iter), info

    if _in_multi_mode():
        # Multi-copy mode: the model is THIS thread's own copy — no global
        # lock needed. A CUDA OOM degrades only this thread to CPU.
        try:
            raw, info = run(BatchedInferencePipeline(model))
        except ValueError as exc:
            if not _word_ts_ok:
                raise
            # distil models lack alignment heads — fall back to plain segments.
            logger.info("Word timestamps unsupported (%s) — falling back", exc)
            _word_ts_ok = False
            kwargs.pop("word_timestamps", None)
            raw, info = run(BatchedInferencePipeline(model))
        except RuntimeError as exc:
            if _effective_device()[0] != "cuda" or _thread_cpu_fallback():
                raise
            # This thread's copy hit a CUDA inference failure (OOM, driver
            # hiccup): degrade ONLY this thread to CPU and retry once.
            logger.warning("CUDA inference failed (%s) — degrading this thread to CPU", exc)
            _thread_mark_cpu_fallback()
            raw, info = run(BatchedInferencePipeline(_thread_model()))
    else:
        with _infer_lock:
            try:
                raw, info = run(BatchedInferencePipeline(model))
            except ValueError as exc:
                if not _word_ts_ok:
                    raise
                # distil models lack alignment heads — fall back to plain segments.
                logger.info("Word timestamps unsupported (%s) — falling back", exc)
                _word_ts_ok = False
                kwargs.pop("word_timestamps", None)
                raw, info = run(BatchedInferencePipeline(model))
            except RuntimeError as exc:
                if _effective_device()[0] != "cuda" or _device_override is not None:
                    raise
                # GPU present but inference broken (missing cuBLAS, driver hiccup):
                # drop to CPU for the process lifetime and retry this batch once.
                logger.warning("CUDA inference failed (%s) — falling back to CPU", exc)
                _device_override = ("cpu", "int8")
                raw, info = run(BatchedInferencePipeline(_get_model()))

    detected_lang = getattr(info, "language", None) or None
    # Attribute each segment to its clip by start time (clips are disjoint
    # and sorted; the pipeline offsets segments by their clip start).
    per: list[list[Any]] = [[] for _ in chunks]
    idx = 0
    for seg in raw:
        s = float(seg.start)
        while idx < len(chunks) - 1 and s >= chunks[idx][1]:
            idx += 1  # advance past clips that ended before this segment
        per[idx].append(seg)
    out: list[tuple[list[dict], Optional[str]]] = []
    for i, segs in enumerate(per):
        cs, ce = chunks[i]
        items: list[dict] = []
        for seg in segs:
            words = [
                {
                    "word": w.word,
                    "start": round(float(w.start), 3),
                    "end": round(float(w.end), 3),
                }
                for w in (seg.words or [])
            ]
            items.append({
                "start_sec": round(float(seg.start), 3),
                "end_sec": round(float(seg.end), 3),
                "text": (seg.text or "").strip(),
                "words": words,
            })
        out.append((items, detected_lang))
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

    # No-speech skip: less than 3 s of planned speech is noise — report it
    # WITHOUT loading the model and WITHOUT a resume manifest (nothing to
    # resume). _process_job maps this to status 'done' (captions-first
    # precedent).
    if speech_sec < 3.0:
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
            "segments": 0,
            "words": 0,
            "resumed_chunks": 0,
            "wall_sec": round(wall, 3),
            "speed_x": 0.0,
            "skipped": "no-speech",
        }
        logger.info(
            "transcribe %s/%s skipped: planned speech %.1fs < 3s — no model load",
            platform, video_id, speech_sec,
        )
        return stats

    # Model load happens only now: VAD + planning are model-free, so a
    # no-speech video never pays the (large) load cost.
    model = _current_model()
    existing = {int(r["seg_idx"]) for r in archive_db.transcript_for(platform, video_id)}
    header, entries = _read_manifest(_manifest_path(platform, video_id))
    missing, seg_idx = _resume_plan(chunks, header, entries, existing)
    if len(missing) == len(chunks) and existing:
        # Full re-run of an already-transcribed video: the manifest is gone
        # (previous run finished and cleaned it), so the old rows are stale
        # output of an earlier transcription — replace, never append beside.
        logger.info("Re-transcribe %s/%s: replacing %d existing rows",
                    platform, video_id, len(existing))
        archive_db.delete_transcripts(platform, video_id)
        existing = set()
        seg_idx = 0
    if missing != list(range(len(chunks))):
        logger.info("Resume: %d/%d chunks already transcribed — skipping",
                    len(chunks) - len(missing), len(chunks))

    manifest = _manifest_path(platform, video_id)
    _write_manifest_header(manifest, chunks)

    segments = 0
    words = 0
    speech_done = 0.0
    detected_lang: Optional[str] = None
    missing_set = set(missing)
    ci = 0
    n_chunks = len(chunks)
    while ci < n_chunks:
        cs, ce = chunks[ci]
        if ci not in missing_set:
            speech_done += ce - cs
            ci += 1
            continue
        # Batch consecutive missing clips into one GPU call; resume gaps only
        # shrink the run, never break the per-clip insert/manifest contract.
        run: list[tuple[int, tuple[float, float]]] = []
        while ci < n_chunks and ci in missing_set and len(run) < _batch_size():
            run.append((ci, chunks[ci]))
            ci += 1
        batch_out = _transcribe_batch(model, audio, [c for _, c in run], language)
        for (ci2, _), (chunk_segs, detected) in zip(run, batch_out):
            if detected_lang is None and detected:
                detected_lang = detected  # first batch's detection wins
            lang = language or detected_lang  # env wins; else detected; else None
            # Batch insert: one insert_transcript() call per chunk (it accepts
            # a list); a crash loses at most the in-flight chunk.
            first_idx = seg_idx
            batch_rows = []
            for seg in chunk_segs:
                if seg_idx in existing:
                    seg_idx += 1
                    continue
                batch_rows.append({
                    "seg_idx": seg_idx,
                    "start_sec": seg["start_sec"],
                    "end_sec": seg["end_sec"],
                    "text": seg["text"],
                    "words": seg["words"],
                })
                words += len(seg["words"])
                seg_idx += 1
            if batch_rows:
                archive_db.insert_transcript(platform, video_id, batch_rows, lang=lang)
            segments += len(batch_rows)
            _append_manifest_entry(manifest, ci2, first_idx, len(chunk_segs))
            speech_done += chunks[ci2][1] - chunks[ci2][0]
            if progress_cb:
                progress_cb(speech_done, speech_sec, ci2 + 1, n_chunks)

    # Disk hygiene: the job finished — the crash-resume manifest has served
    # its purpose. Best-effort: a failed unlink just leaves it for the next
    # run (which would resume into an empty plan and rewrite it anyway).
    try:
        manifest.unlink(missing_ok=True)
    except OSError:
        pass

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
    """Atomically claim the newest queued transcribe/events job (crash-stale too).

    A 'running' job is reclaimed only if untouched for 30 min — a single
    chunk can legitimately take that long on CPU."""
    now = datetime.now(timezone.utc)
    stale_cutoff = (now - _STALE_JOB_TIMEDELTA).isoformat(timespec="seconds")
    # String comparison is valid: both sides come from _now_iso (UTC, same width).
    rows = archive_db.query(
        """SELECT * FROM archive_jobs
           WHERE kind IN ('transcribe','events')
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


def _process_events_job(job_id: str, platform: str, video_id: str) -> dict:
    """Run one kind='events' job (PANNs acoustic-event detection).

    Same shape as the transcribe path: progress tracks speech seconds, the
    job ends 'done' with a stats dict (or 'failed' with an error message)."""
    def _progress(done: float, total: float) -> None:
        if total > 0:
            archive_db.update_job(job_id, progress=min(0.999, done / total))

    stats = detect_events_video(platform, video_id, progress_cb=_progress)
    archive_db.update_job(job_id, status="done", progress=1.0)
    return stats


def _captions_first_skip(platform: str, video_id: str) -> bool:
    """True when YouTube captions already cover the video and the user opted
    into captions-first (settings.yt_subtitles_first, default True).

    Whisper still runs when the toggle is off, the platform isn't YouTube,
    or no caption rows exist. ponytail: there is no force-transcribe path
    (archive_jobs has no force flag) — add one there if a job ever needs to
    bypass this.
    """
    if platform != "youtube":
        return False
    from deps import settings_mgr  # lazy: archive_transcribe is opt-in by design

    if not getattr(settings_mgr.get(), "yt_subtitles_first", True):
        return False
    return bool(archive_db.transcript_for(platform, video_id))


def _process_job(job: dict, *, multi: bool = False) -> dict:
    """Run one claimed job; never raises — failures land in archive_jobs.error.

    multi=True (worker with budget > 1): the job runs on the calling pool
    thread's own model copy — no global _infer_lock, thread-local CUDA
    fallback. Default False keeps the single-global-model path for direct
    callers and tests."""
    job_id = job["id"]
    platform, video_id = job["platform"], job["video_id"]
    if multi:
        _multi_tls.active = True
    try:
        archive_db.update_job(job_id, status="running", progress=0.0)

        if job.get("kind") == "events":
            return _process_events_job(job_id, platform, video_id)

        if _captions_first_skip(platform, video_id):
            logger.info("captions already present for %s/%s — skipping whisper", platform, video_id)
            archive_db.update_job(job_id, status="done", progress=1.0)
            return {
                "job_id": job_id,
                "platform": platform,
                "video_id": video_id,
                "skipped": "captions-first",
            }

        def _progress(done: float, total: float, _ci: int, _n: int) -> None:
            if total > 0:
                archive_db.update_job(job_id, progress=min(0.999, done / total))

        stats = transcribe_video(platform, video_id, progress_cb=_progress)
        archive_db.update_job(job_id, status="done", progress=1.0)
        if "skipped" not in stats:
            # Heavy batch writes just finished — merge the FTS b-tree
            # segments so search stays fast (best-effort inside).
            archive_db.optimize_fts()
        if "skipped" not in stats and events_enabled():
            # Optional PANNs stage (VODRIP_EVENTS_ENABLED=1): the transcribe
            # job is already 'done' — events are best-effort enrichment and
            # never fail the job.
            try:
                ev_stats = detect_events_video(platform, video_id)
                stats["events"] = ev_stats.get("events", 0)
            except Exception:
                logger.exception("events stage failed for %s/%s — transcribe already done",
                                 platform, video_id)
        return stats
    except Exception as exc:  # job-level failure — worker keeps going
        logger.exception("transcribe job %s failed", job_id)
        archive_db.update_job(job_id, status="failed",
                              error=f"{type(exc).__name__}: {exc}"[:400])
        return {"job_id": job_id, "error": str(exc)}
    finally:
        if multi:
            _multi_tls.active = False


def run_worker(
    *,
    once: bool = False,
    poll_interval: float = 2.0,
    max_workers: Optional[int] = None,
) -> None:
    """Blocking worker loop over the transcribe queue.

    Parallelism budget: _worker_budget() — 1 on CUDA by default (the legacy
    single-global-model path with _infer_lock, byte-for-byte today's
    behavior); >1 (CPU workers or opt-in VODRIP_TRANSCRIBE_GPU_COPIES) gives
    each pool thread its own model copy so inference truly runs in parallel.
    max_workers overrides the budget (used by tests/launchers).
    """
    device, compute_type = _effective_device()
    budget = _worker_budget() if max_workers is None else max(1, int(max_workers))
    multi = budget > 1
    logger.info("archive transcribe worker: device=%s compute_type=%s workers=%d",
                device, compute_type, budget)
    try:
        with ThreadPoolExecutor(max_workers=budget, thread_name_prefix="transcribe") as pool:
            while True:
                _maybe_close_idle_model()
                archive_db.worker_heartbeat("transcribe")
                claimed = []
                for _ in range(budget):
                    job = _claim_next_job()
                    if job is None:
                        break
                    claimed.append(job)
                if not claimed:
                    if once:
                        break
                    time.sleep(poll_interval)
                    continue
                futures = [pool.submit(_process_job, j, multi=multi) for j in claimed]
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
# worker budget: no GPU_COPIES env -> 1 copy regardless of VRAM; the VRAM
# clamp caps copies at free_vram // 2 GiB (never below 1); CPU honors
# VODRIP_TRANSCRIBE_WORKERS and defaults to 2.
assert _clamp_cuda_copies(1, 100 << 30) == 1, "no GPU copies env -> 1 (probe skipped)"
assert _clamp_cuda_copies(4, 10 << 30) == 4, "env within the VRAM budget passes through"
assert _clamp_cuda_copies(8, 5 << 30) == 2, "VRAM budget clamps copies (5 GiB -> 2)"
assert _clamp_cuda_copies(8, 1 << 30) == 1, "VRAM budget never drops below 1"
_saved_override, _saved_workers = _device_override, os.environ.get(WORKERS_ENV)
try:
    _device_override = ("cpu", "int8")
    os.environ[WORKERS_ENV] = "4"
    assert _worker_budget() == 4, "CPU budget must honor VODRIP_TRANSCRIBE_WORKERS"
    os.environ.pop(WORKERS_ENV, None)
    assert _worker_budget() == 2, "CPU budget defaults to 2 workers"
finally:
    _device_override = _saved_override
    if _saved_workers is None:
        os.environ.pop(WORKERS_ENV, None)
    else:
        os.environ[WORKERS_ENV] = _saved_workers

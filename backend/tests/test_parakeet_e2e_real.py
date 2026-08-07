"""Real end-to-end check: Parakeet TDT v3 on the CPU lane through the REAL
worker path (queue -> _claim_next_job -> _process_job -> transcribe_video).

Isolation: a scratch archive DB (never the live one), VODRIP_WHISPER_DEVICE
=cpu so the worker is deterministic CPU-only (no GPU loads, no 60 s VRAM
median), and per-run model caches.

Model seeding:
  * Parakeet: VODRIP_PARAAKEET_SEED may point at a dir holding the four
    model files (encoder/decoder/joiner .onnx + tokens.txt — e.g. the A/B
    scratch models/ dir); otherwise VODRIP_SHERRPA_CACHE points at a fresh
    dir and the model auto-downloads from HF on first use (670 MB).
  * Whisper: VODRIP_WHISPER_MODEL / VODRIP_WHISPER_CACHE override the
    defaults ('small', fresh dir) so a pre-cached model can be reused.

Audio: VODRIP_PARAAKEET_AUDIO may point at a real speech file (16 kHz mono
wav or any ffmpeg-decodable media); otherwise an English TTS fixture is
built with Windows System.Speech (no downloads).

Run:  python backend/tests/test_parakeet_e2e_real.py
      (or pytest -s backend/tests/test_parakeet_e2e_real.py)
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import pathlib
import shutil
import subprocess as sp
import sys
import tempfile
import threading
import time

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="vodrip-parakeet-e2e-"))
os.environ["VODRIP_ARCHIVE_DB"] = str(_TMP / "archive.db")
os.environ.setdefault("VODRIP_APP_DATA", str(_TMP / "app"))
os.environ.setdefault("VODRIP_CACHE_DIR", str(_TMP / "cache"))
os.environ.setdefault("VODRIP_DATA_DIR", str(_TMP / "data"))
os.environ["VODRIP_WHISPER_DEVICE"] = "cpu"  # deterministic CPU-only worker
os.environ["VODRIP_EVENTS_ENABLED"] = "0"  # ASR-only measurement (PANNs is default-ON in production)
os.environ.setdefault("VODRIP_TRANSCRIBE_WORKERS", "2")
os.environ.setdefault("VODRIP_WHISPER_IDLE_CLOSE", "60")
os.environ.setdefault("VODRIP_WHISPER_MODEL", "small")
os.environ.setdefault("VODRIP_WHISPER_CACHE", str(_TMP / "whisper-models"))
# Parakeet model: seed dir when given (files at its root), else fresh cache
# -> auto-download on first use.
_SEED = os.environ.get("VODRIP_PARAAKEET_SEED", "").strip()
os.environ.setdefault("VODRIP_SHERRPA_CACHE", _SEED or str(_TMP / "sherpa-cache"))

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services import archive_db, archive_transcribe as at  # noqa: E402
from services.archive_transcribe import (  # noqa: E402
    run_worker,
    _manifest_path,
    _resolve_ffmpeg_exe,
    SAMPLE_RATE,
)

PLATFORM = "twitch"
PT_VIDEO = "__parakeet_e2e_pt__"
JA_VIDEO = "__parakeet_e2e_ja__"
PT2_VIDEO = "__parakeet_e2e_pt2__"
_AUDIO = os.environ.get("VODRIP_PARAAKEET_AUDIO", "").strip()

logger = logging.getLogger("test_parakeet_e2e")


# --- machine-state + RSS helpers (stdlib-only, mirrors the worker's probes) --

def _free_vram_gib() -> float:
    try:
        out = sp.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        return float(out.stdout.strip()) / 1024.0
    except Exception:
        return 0.0


def _free_ram_gib() -> float:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return status.ullAvailPhys / (1024 ** 3)
    return 0.0


def _machine_state() -> str:
    return (
        f"VRAM free {_free_vram_gib():.1f} GiB | RAM free {_free_ram_gib():.1f} GiB"
    )


def _peak_rss_bytes() -> int:
    """OS-tracked peak working set of THIS process (Windows; 0 elsewhere)."""
    if os.name != "nt":
        return 0

    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    _pmc = PROCESS_MEMORY_COUNTERS()
    _pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
    _psapi = ctypes.WinDLL("psapi", use_last_error=True)
    _psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), ctypes.c_ulong,
    ]
    _psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    if _psapi.GetProcessMemoryInfo(
        ctypes.c_void_p(ctypes.windll.kernel32.GetCurrentProcess()),
        ctypes.byref(_pmc), ctypes.sizeof(PROCESS_MEMORY_COUNTERS),
    ):
        return int(_pmc.PeakWorkingSetSize)
    return 0


class _RssMonitor(threading.Thread):
    """Samples the process working set every 0.2 s; peak() per run (the OS
    PeakWorkingSetSize is process-lifetime, so a per-run peak needs sampling).
    NOTE: the stop flag must not be named _stop — it shadows the private
    Thread._stop method that join() calls."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self._done = False
        self._peak = 0

    def run(self) -> None:
        while not self._done:
            rss = _peak_rss_bytes()
            if rss > self._peak:
                self._peak = rss
            time.sleep(0.2)

    def stop(self) -> int:
        self._done = True
        self.join(timeout=2.0)
        return self._peak


class _LogCollector(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record.getMessage())


def _collector() -> _LogCollector:
    handler = _LogCollector()
    at.logger.addHandler(handler)
    return handler


def _slice(path: pathlib.Path, start_s: float, dur_s: float, out: pathlib.Path) -> pathlib.Path:
    from services.os_services import _NO_WINDOW

    ffmpeg = _resolve_ffmpeg_exe()
    proc = sp.run(
        [ffmpeg, "-y", "-v", "error", "-ss", str(start_s), "-t", str(dur_s),
         "-i", str(path), "-ar", "16000", "-ac", "1", str(out)],
        capture_output=True, timeout=120, creationflags=_NO_WINDOW,
    )
    if proc.returncode != 0 or not out.is_file():
        raise RuntimeError(f"slice failed: {proc.stderr.decode('utf-8', 'replace')}")
    return out


def _wav_seconds(path: pathlib.Path) -> float:
    import wave

    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


def _tts_speech(wav: pathlib.Path, text: str) -> pathlib.Path:
    from services.os_services import _NO_WINDOW

    ps = (
        "Add-Type -AssemblyName System.Speech; "
        f"$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.SetOutputToWaveFile('{wav}'); "
        f"$s.Speak('{text.replace(chr(39), '')}'); $s.Dispose()"
    )
    proc = sp.run(["powershell", "-NoProfile", "-Command", ps],
                  capture_output=True, timeout=120, creationflags=_NO_WINDOW)
    if proc.returncode != 0 or not wav.is_file():
        raise RuntimeError(
            "Windows TTS unavailable — set VODRIP_PARAAKEET_AUDIO to a real "
            "speech file instead: " + (proc.stderr or b"").decode("utf-8", "replace")[-300:]
        )
    return wav


def _build_fixtures() -> tuple[pathlib.Path, pathlib.Path, pathlib.Path, float]:
    """(pt_fixture, ja_fixture, pt_slice, pt_fixture_sec)."""
    if _AUDIO:
        pt = pathlib.Path(_AUDIO)
        if not pt.is_file():
            raise RuntimeError(f"VODRIP_PARAAKEET_AUDIO not found: {pt}")
        pt_sec = _wav_seconds(pt) if pt.suffix.lower() == ".wav" else 60.0
    else:
        pt = _TMP / "pt.wav"
        _tts_speech(pt, "This is the transcription fixture for the parakeet lane.")
        pt_sec = _wav_seconds(pt)
    ja = _slice(pt, 0.0, min(15.0, max(5.0, pt_sec / 4)), _TMP / "ja.wav")
    pt_slice = _slice(pt, 0.0, min(15.0, max(5.0, pt_sec / 4)), _TMP / "pt_slice.wav")
    return pt, ja, pt_slice, pt_sec


def _enqueue_and_run(job_id: str, video_id: str) -> dict:
    archive_db.enqueue_job(job_id, "transcribe", PLATFORM, video_id)
    run_worker(once=True, poll_interval=0.5)
    jobs = {j["id"]: j for j in archive_db.list_jobs()}
    return jobs[job_id]


def _assert_segment_contract(video_id: str) -> list[dict]:
    # raw=True: storage contract (contiguous seg_idx) — the display read
    # path dedupes overlapping cues and may skip rows.
    segs = archive_db.transcript_for(PLATFORM, video_id, raw=True)
    idxs = [int(s["seg_idx"]) for s in segs]
    assert idxs == list(range(len(segs))), f"seg_idx must be contiguous: {idxs}"
    words = [w for s in segs for w in json.loads(s["words_json"] or "[]")]
    assert all({"word", "start", "end"} <= set(w) for w in words), (
        "each word must carry word/start/end"
    )
    return segs


def _run_parakeet(pt: pathlib.Path, pt_sec: float) -> None:
    """pt job through the real worker -> parakeet engine on a CPU lane."""
    collector = _collector()
    archive_db.upsert_video({
        "platform": PLATFORM, "video_id": PT_VIDEO, "channel": "parakeet-e2e-pt",
        "title": "parakeet pt fixture", "status": "ready",
        "archive_path": str(pt), "duration_sec": pt_sec,
    })
    archive_db.set_channel_language(PLATFORM, "parakeet-e2e-pt", "pt")
    before = _machine_state()
    mon = _RssMonitor(); mon.start()
    t0 = time.monotonic()
    job = _enqueue_and_run("parakeet-e2e-pt", PT_VIDEO)
    wall = time.monotonic() - t0
    peak_rss = mon.stop()
    after = _machine_state()
    assert job["status"] == "done", f"parakeet job not done: {job}"
    assert job["progress"] == 1.0, job
    assert any("Parakeet recognizer loaded" in r for r in collector.records), (
        "parakeet job must load the sherpa-onnx recognizer: " + repr(collector.records[-3:])
    )
    segs = _assert_segment_contract(PT_VIDEO)
    assert segs, "parakeet run must write transcript segments"
    assert not _manifest_path(PLATFORM, PT_VIDEO).exists(), (
        "completed job must delete its resume manifest"
    )
    print("=== run 1: pt job -> PARAKEET (CPU lane) ===")
    print(f"  job: {job}")
    print(f"  machine before: {before} | after: {after}")
    print(f"  wall: {wall:.2f}s | RTFx: {pt_sec / wall:.2f} (audio {pt_sec:.0f}s)")
    print(f"  peak RSS: {peak_rss / 1e6:.0f} MB | segments: {len(segs)} | "
          f"words: {sum(len(json.loads(s['words_json'] or '[]')) for s in segs)}")
    print(f"  sample text: {segs[0]['text'][:100]!r}")


def _run_whisper_ja(ja: pathlib.Path) -> None:
    """ja job (known-other language) -> whisper int8 through the real worker."""
    collector = _collector()
    archive_db.upsert_video({
        "platform": PLATFORM, "video_id": JA_VIDEO, "channel": "parakeet-e2e-ja",
        "title": "ja fixture", "status": "ready",
        "archive_path": str(ja), "duration_sec": _wav_seconds(ja),
    })
    archive_db.set_channel_language(PLATFORM, "parakeet-e2e-ja", "ja")
    before = _machine_state()
    mon = _RssMonitor(); mon.start()
    t0 = time.monotonic()
    job = _enqueue_and_run("parakeet-e2e-ja", JA_VIDEO)
    wall = time.monotonic() - t0
    peak_rss = mon.stop()
    after = _machine_state()
    assert job["status"] == "done", f"ja job not done: {job}"
    assert any("whisper" in r.lower() for r in collector.records if "Loading" in r), (
        "ja job must load the whisper model: " + repr(collector.records[-3:])
    )
    assert not any("Parakeet recognizer" in r for r in collector.records), (
        "ja job must NOT load parakeet"
    )
    archive_db.transcript_for(PLATFORM, JA_VIDEO)  # contract holds (may be empty on mislabeled audio)
    print("=== run 2: ja job -> WHISPER int8 (CPU lane) ===")
    print(f"  job: {job}")
    print(f"  machine before: {before} | after: {after}")
    print(f"  wall: {wall:.2f}s | peak RSS: {peak_rss / 1e6:.0f} MB")


def _run_kill_switch(pt_slice: pathlib.Path) -> None:
    """VODRIP_PARAAKEET=0 -> the same pt job falls back to whisper int8."""
    os.environ["VODRIP_PARAAKEET"] = "0"
    try:
        collector = _collector()
        archive_db.upsert_video({
            "platform": PLATFORM, "video_id": PT2_VIDEO, "channel": "parakeet-e2e-pt",
            "title": "parakeet kill-switch fixture", "status": "ready",
            "archive_path": str(pt_slice), "duration_sec": _wav_seconds(pt_slice),
        })
        archive_db.set_channel_language(PLATFORM, "parakeet-e2e-pt", "pt")
        before = _machine_state()
        mon = _RssMonitor(); mon.start()
        t0 = time.monotonic()
        job = _enqueue_and_run("parakeet-e2e-pt2", PT2_VIDEO)
        wall = time.monotonic() - t0
        peak_rss = mon.stop()
        after = _machine_state()
        assert job["status"] == "done", f"kill-switch job not done: {job}"
        assert any("whisper" in r.lower() for r in collector.records if "Loading" in r), (
            "kill-switch job must load the whisper model: " + repr(collector.records[-3:])
        )
        assert not any("Parakeet recognizer" in r for r in collector.records), (
            "VODRIP_PARAAKEET=0 must keep parakeet unloaded"
        )
        segs = _assert_segment_contract(PT2_VIDEO)
        print("=== run 3: pt job + VODRIP_PARAAKEET=0 -> WHISPER int8 (fallback) ===")
        print(f"  job: {job}")
        print(f"  machine before: {before} | after: {after}")
        print(f"  wall: {wall:.2f}s | peak RSS: {peak_rss / 1e6:.0f} MB | "
              f"segments: {len(segs)}")
        print(f"  sample text: {segs[0]['text'][:100]!r}" if segs else "  (no segments)")
    finally:
        os.environ.pop("VODRIP_PARAAKEET", None)


def _run() -> None:
    pt, ja, pt_slice, pt_sec = _build_fixtures()
    print(f"fixtures: pt={pt} ({pt_sec:.0f}s) ja={ja} slice={pt_slice}")
    _run_parakeet(pt, pt_sec)
    _run_whisper_ja(ja)
    _run_kill_switch(pt_slice)
    print("\nE2E OK — parakeet CPU lane verified through the real worker path.")


def test_parakeet_e2e_real() -> None:
    if pathlib.Path(os.environ["VODRIP_ARCHIVE_DB"]) != archive_db._db_path():
        raise AssertionError("archive DB env mismatch — run the test file directly")
    try:
        _run()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("services.archive_transcribe").setLevel(logging.INFO)
    test_parakeet_e2e_real()

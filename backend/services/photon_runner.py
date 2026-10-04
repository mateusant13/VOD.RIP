"""Photon ASR sidecar — the process that actually touches the GPU.

Executed by the ISOLATED Photon venv's interpreter (services/photon_asr.py
spawns it), never imported into the VOD.RIP API or transcription worker. The
split is mandatory, not stylistic:

  * the venv is 5.29 GB with its own torch (2.11.0+cu128) — it cannot be
    merged into the app interpreter, which runs the guaranteed sherpa-onnx
    path and must stay untouched;
  * the Photon engine HANGS instead of raising (measured: 2 of 3 launches hung
    in engine creation, 1 hung on its third call — 0% CPU, resident set
    frozen, no exception). A hang inside the worker process is a wedged VOD.RIP;
    the same hang inside a child we can KILL costs one batch and falls back to
    sherpa-onnx;
  * there is no CPU path. kestrel's native extension reports
    ``ternary_gemm_isa() == "scalar"`` and raises NotImplementedError, so
    Photon can never be the only engine.

Protocol (one job per process — see photon_asr.py for the caller):

    argv[1]  path to a JSON request file:
             {"model": str, "sample_rate": int, "timestamps": str,
              "pcm_path": str, "pcm_dtype": "float32",
              "clips": [{"start": float, "end": float}, ...]}
             clip start/end are seconds into the single PCM file; the runner
             slices it and hands each clip to the model on its own, so
             clip-local timestamps are what comes back.
    stdout   exactly one JSON line:
             {"ok": true,  "clips": [{"language": str|None,
                                      "segments": [{"text", "start", "end",
                                                    "words": [...]}, ...]}]}
             or      {"ok": false, "error": "..."}

Timestamps in the response are CLIP-RELATIVE; the caller owns the absolute
offsets, exactly as _clip_items does for sherpa-onnx. This module therefore
needs nothing from ``services`` — it must stay importable by a venv that has
no VOD.RIP dependencies. Keep it that way.
"""
from __future__ import annotations

import json
import sys
import traceback

# A response line larger than this is a runaway transcript, not a result.
MAX_RESPONSE_BYTES = 32 * 1024 * 1024


def _fail(message: str) -> "int":
    """Emit the failure envelope on stdout and exit non-zero."""
    sys.stdout.write(json.dumps({"ok": False, "error": str(message)[:4000]}) + "\n")
    sys.stdout.flush()
    return 2


def _segments_from(result: object) -> list[dict]:
    """Normalize a Photon transcribe() result into plain JSON-able dicts.

    kestrel's TranscriptionResult.as_dict() already returns the shape we want
    ({text, start, end, words:[{word, start, end, probability?}]}); this only
    flattens the values to JSON scalars so the caller's json.loads never sees a
    numpy type, and drops anything structurally wrong rather than forwarding
    it as a good-looking transcript.
    """
    out: list[dict] = []
    for seg in (getattr(result, "segments", None) or []):
        text = str(getattr(seg, "text", "") or "").strip()
        if not text:
            continue  # parakeet emits no segment for empty text (no hallucination)
        words: list[dict] = []
        for word in (getattr(seg, "words", None) or []):
            wtext = str(getattr(word, "text", "") or "")
            if not wtext:
                continue
            try:
                start = float(getattr(word, "start"))
                end = float(getattr(word, "end"))
            except (TypeError, ValueError):
                continue
            if not (start >= 0.0 and end >= start):
                continue
            item = {"word": wtext, "start": start, "end": end}
            prob = getattr(word, "probability", None)
            if isinstance(prob, (int, float)):
                item["probability"] = float(prob)
            words.append(item)
        try:
            start = float(getattr(seg, "start"))
            end = float(getattr(seg, "end"))
        except (TypeError, ValueError):
            continue
        if not (start >= 0.0 and end >= start):
            continue
        out.append({"text": text, "start": start, "end": end, "words": words})
    return out


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        return _fail("photon_runner: missing request file argument")
    try:
        with open(argv[1], "r", encoding="utf-8") as fh:
            req = json.load(fh)
    except Exception as exc:  # unreadable/corrupt request -> clean failure
        return _fail(f"bad request file: {exc}")

    clips = req.get("clips") or []
    if not isinstance(clips, list) or not clips:
        return _fail("photon_runner: request carries no clips")
    sample_rate = int(req.get("sample_rate") or 16000)
    model_name = str(req.get("model") or "").strip()
    if not model_name:
        return _fail("photon_runner: request carries no model")
    pcm_path = str(req.get("pcm_path") or "")
    if not pcm_path:
        return _fail("photon_runner: request carries no pcm_path")

    # Imported here, not at module scope: a missing/broken Photon install must
    # fail as a clean error envelope on stdout, never as an import traceback
    # that the caller has to guess at.
    try:
        import numpy as np
        from moondream import photon
    except Exception as exc:
        return _fail(f"photon import failed: {exc}")

    try:
        pcm = np.fromfile(pcm_path, dtype=np.float32)
    except Exception as exc:
        return _fail(f"pcm read failed: {exc}")
    if pcm.ndim != 1 or pcm.size == 0:
        return _fail("pcm file is empty or not mono")

    try:
        client = photon(model_name)
    except Exception as exc:
        return _fail(f"photon engine creation failed: {exc}")

    transcribe = getattr(client, "transcribe", None)
    if not callable(transcribe):
        return _fail("photon client has no transcribe()")

    out: list[dict] = []
    for clip in clips:
        try:
            start = float(clip["start"])
            end = float(clip["end"])
        except (KeyError, TypeError, ValueError):
            return _fail(f"photon_runner: malformed clip {clip!r}")
        lo = int(round(start * sample_rate))
        hi = int(round(end * sample_rate))
        if lo < 0 or hi <= lo or hi > pcm.size:
            # An out-of-range clip means the caller and runner disagree about
            # the audio — better a clean failure than a wrong transcript.
            return _fail(
                f"photon_runner: clip {start}-{end}s outside pcm "
                f"({pcm.size / float(sample_rate):.3f}s)"
            )
        try:
            # language is deliberately NOT passed: parakeet TDT detects it and
            # REJECTS a forced language (kestrel contract.parse_request), so
            # forwarding the job's language would fail every clip.
            result = transcribe(
                audio=pcm[lo:hi],
                sample_rate=sample_rate,
                timestamps=str(req.get("timestamps") or "word"),
                stream=False,
            )
        except Exception as exc:
            return _fail(f"transcribe failed on {start}-{end}s: {exc}")
        if isinstance(result, dict):
            segments = _segments_from(result)
            language = result.get("language")
        else:
            # transcribe() also returns a PhotonStream when stream=True; we ask
            # for stream=False, so anything else is a contract change upstream.
            return _fail(
                f"photon returned an unsupported result type: {type(result).__name__}"
            )
        out.append({
            "language": language if isinstance(language, str) and language else None,
            "segments": segments,
        })

    payload = json.dumps({"ok": True, "clips": out})
    if len(payload) > MAX_RESPONSE_BYTES:
        return _fail("photon response exceeds the size cap")
    sys.stdout.write(payload + "\n")
    sys.stdout.flush()
    try:
        client.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except SystemExit:
        raise
    except BaseException:  # last-resort envelope: a bare traceback on stderr
        # would leave the caller reading an empty stdout as "no speech".
        _fail("unhandled runner error:\n" + traceback.format_exc())
        sys.exit(3)

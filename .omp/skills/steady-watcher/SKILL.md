---
name: steady-watcher
description: Local resource governor on this PC (Steady Watcher). Use whenever you launch heavy work (ffmpeg, cargo, rustc, whisper, CUDA, Chromium) or when the user may be gaming (League of Legends, Marvel Rivals). Read http://127.0.0.1:47891/status before GPU jobs. Keep working — slowness is the governor, not a crash. If you must block, GET /wait so the session pulses before 4:40.
---

# Steady Watcher (this PC)

A local governor is running so **all four projects plus any other heavy work can run at once**, including while the user is in League of Legends or Marvel Rivals.

It does **not** stop your jobs. It pins batch work to efficiency cores, caps the shared CPU budget, and drops GPU/IO class of background compute so the game keeps FPS.

## Before heavy or GPU work

```
GET http://127.0.0.1:47891/status
```

Fallback file (same JSON): `I:\!watcher\status\now.json`

| `mood` | What you do |
|---|---|
| `gaming` | **Continue.** Do not kill ffmpeg/cargo/whisper/chrome. Do not start a second copy because the first looks slow. GPU jobs keep running at idle GPU class. |
| `steady` | Continue. User is at the desktop; you are already capped. |
| `watching` | Continue gently — user is watching media. Do not spike. |
| `cruise` | User is AFK. You may use more CPU. Do not allocate until the box OOMs. |
| `emergency` | Continue at a hard clamp (CPU or RAM). Do not retry-loop. Do not kill. |

`defer_gpu` is false on purpose: Whisper/VLM stay resident so CUDA caches do not go cold. The watcher lowers their GPU scheduling class instead of pausing them.

## If you would block / wait / queue

Do **not** sit in a silent `sleep` that lasts more than ~4 minutes. Call one of:

```
GET http://127.0.0.1:47891/wait?max_ms=250000
python -m watcher wait
```

That returns **HTTP 200 / exit 0** with JSON at least every **4 minutes 10 seconds** (under 4:40) so ChatGPT/OMP/browser sessions and local caches stay warm. Read `still_gaming` and loop if you truly must wait for a match to end (`/wait?until=clear`).

Never invent a fake failure to "wake" yourself — the pulse *is* the wake.

## Never do this

- EcoQoS / taskkill / Ctrl+C on a slow worker because the user is in-game.
- EmptyWorkingSet / RAM-trim on jcode, omp, bun, or Chromium.
- Write caches to `C:` (BrandOps rule). Watcher state lives on `I:\!watcher`.

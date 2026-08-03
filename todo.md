# Archive search v2 — "super smart" pipeline

Goal: searches find what the user wants faster, indexing work is parallel, GPU-friendly,
compressed, and targeted (specific-first) instead of exhaustive.

## 1. Specific-first targeted enrichment (search stays instant)
- After hits, rank enrichment candidates INSIDE the search scope (channel/platform/date/kind):
  - chat source: chat-less Twitch VODs → backfill top-2. Relevance = title token overlap with
    query tokens (fuzzy-tolerant, reuses fts5vocab vocabulary), tie → newest first.
  - transcript source: videos with `archive_path` on disk but no transcript rows → enqueue
    `transcribe` job for top-1 (title-relevance first). Skip if a queued/running job exists
    (archive_jobs) or transcripts already exist.
- Throttles: per-video cooldown 10 min, global min gap 15 s (backfill) / 30 s (transcribe),
  in-flight sets; fires via create_task, never blocks the response.
- Response contract: `{hits, enriching: [{platform, video_id, kind, channel, title}]}`
  (always present; [] when idle).
- New settings toggle `archive_smart_enrich` (default True): AppSettings + SettingsUpdate +
  settings.py apply.
- New archive_db helpers: `has_job(platform, video_id, kind, statuses)`, enrichment
  candidate query (chat-less twitch / transcript-less with file, scoped, relevance-ranked).

## 2. More GPU without lagging the system
- faster-whisper 1.2.1 has NO num_workers/batch path (verified in installed source):
  real parallel inference = N model copies (CT2 instances are per-thread).
- `_worker_budget()`: CUDA → min(VODRIP_TRANSCRIBE_GPU_COPIES (default 1),
  floor((mem_total − 512 MB headroom) / model_vram_estimate)); estimate from the first
  load (mem_get_info before/after) or 2 GB default for large-v3-turbo fp16.
  CPU → VODRIP_TRANSCRIBE_WORKERS (default 2, small models so RAM stays sane).
- run_worker spawns ThreadPoolExecutor(max_workers=budget); each thread lazily owns its own
  model via threading.local(); the global `_infer_lock` around inference is REMOVED
  (per-thread instances make it unnecessary); lock only guards model creation.
- GPU OOM guard: copy load failure → halve budget and retry once → else CPU fallback
  (existing _device_override path reused).
- Multiple videos transcribe concurrently; decode/VAD/DB writes already parallel-safe.

## 3. Don't look when quiet (complete the loop)
- VAD pre-pass already exists (Silero + chunk plan + dead-air skip). Add: planned speech
  < 3 s → job done with `skipped: "no-speech"` (no model load at all).
- Keep merge/min-len constants as-is unless review disagrees (avoid config sprawl).

## 4. Compress info
- Chat spam collapse at ingest: insert_messages collapses consecutive identical
  (username, text) within a 60 s window into one row with `spam_count`
  (additive migration `messages.spam_count INTEGER NOT NULL DEFAULT 1`). FTS indexes one
  row instead of N; chat_window returns spam_count (UI shows ×N when > 1).
- FTS `optimize` after bulk inserts (backfill/ingest batches).
- words_json untouched (word-level seek value); future note: gzip old segments.

## 5. UI (thin)
- ArchiveSearchPopup: "Indexing N video(s)…" status line when `enriching` non-empty;
  cleared on next response; no polling.
- Nearby-chat window: ×N badge when spam_count > 1.

## Contracts (fixed)
- GET /api/archive/search → {hits, enriching}
- messages.spam_count (additive); insert_messages collapses; chat_window includes spam_count
- transcribe stats: `skipped_no_speech` flag → job done
- Env: VODRIP_TRANSCRIBE_GPU_COPIES (default 1), VODRIP_TRANSCRIBE_WORKERS (default 2)
- Settings: archive_smart_enrich default True
- No new dependencies; skip formatters/linters/project-wide suites during work

## Worktree split (overlap-safe: disjoint functions in archive_db.py)
- WT-A backend-search: routers/archive.py, models/schemas.py, services/settings.py,
  archive_db.py: has_job + enrichment candidates ONLY
- WT-B backend-transcribe: services/archive_transcribe.py, archive_db.py: insert_messages/
  chat_window/spam_count migration ONLY
- WT-C ui: components/ArchiveSearchPopup.tsx(+test), chat window component(+test)

## Verification
- Targeted tests per worktree; full backend pytest + vitest on main after merge;
- Live E2E: CPU worker + real archive DB (real Twitch backfill, real whisper on a short
  fixture video), browser check of enriching status line.

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
- (The faster-whisper engine that motivated this is gone — parakeet is one
  int8 model; the copy-budget design below carries over unchanged.)
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
- Live E2E: CPU worker + real archive DB (real Twitch backfill, real parakeet on a short
  fixture video), browser check of enriching status line.

---
# Feature backlog — 9 user workstreams (appended 2026-08-04)

Purpose: task backlog for future turns / subagents / worktrees. Decomposes the user's 9
feature requests into phased, worktree-ready work items. Implementers work in worktrees,
never commit (main agent commits), and follow repo conventions:
`.github/copilot-instructions.md` (ponytail lazy senior rules), `report.md` (34 audit
findings — reduce the count), `.cursor/rules/verify-before-ship.mdc` (real integration
tests, strict body assertions, content over status codes).

## Cross-cutting rules (apply to every workstream)
- No new npm/PyPI dependency unless a stdlib alternative is >10 lines; check installed
  deps and existing code first (rung ladder: YAGNI → stdlib → native → installed dep).
- One `assert`-based self-check (or one small test following existing conventions, e.g.
  `src/archiveSearchUtils.test.ts`, `backend/tests/test_archive_watchdog.py`) per
  non-trivial change; no test frameworks.
- Mark shortcuts with a `ponytail:` comment naming the ceiling + upgrade path.
- Clean cutover: migrate every caller, delete shims/aliases/deprecated paths.
- Real user data lives in `%APPDATA%\VOD.RIP` (archive.db ~790 videos; channels:
  titiltei, guiven, srdogg, mandiocaa, arthur lanches, gaveta). Tests MUST isolate via
  `VODRIP_APP_DATA` / `VODRIP_ARCHIVE_DB` env overrides — never touch the real profile.
- Backend state is SQLite (archive.db) with FTS5; schema migrations follow the table
  rebuild pattern in `backend/services/archive_db.py::_ensure_jobs_kind_events`.
- ASR: parakeet TDT v3 int8 (sherpa-onnx) on CPU/CUDA — the ONLY engine
  (faster-whisper removed); captions exist in
  `backend/services/archive_transcribe.py`; semantic embeddings just added in
  `backend/services/archive_embed.py`.

## Real-device / live verification matrix
- Need live/E2E verification: WS-1 queue priority (real preview + queued transcribe jobs),
  WS-3/WS-4 language accuracy (real titiltei/gaveta/srdogg transcripts + real YouTube
  titles), WS-5 synthetic-ID cleanup (browser check of titiltei channel list),
  WS-6 fullscreen double-toggle (browser repro), WS-7 entity watcher (real titiltei VODs),
  WS-8 cache relocation (real multi-drive machine).
- Pure UI: WS-9 240Hz panel resize (browser performance trace only).

---

## WS-1: Preview-queue priority
Goal: clicking preview on any VOD/video bumps that video to the top of any background
queue that makes sense (transcribe first; chat backfill/ingest where sensible), so the
previewed video is the next one processed.

Research first:
- Queue schema/API: `archive_jobs` table + `enqueue_job()` — `backend/services/archive_db.py`
  (~lines 141-153, 990). No priority column today; `list_jobs()` orders by created_at;
  `run_worker` (`archive_transcribe.py` ~1719) polls queued work — find its exact pick
  query (ORDER BY?) and the in-flight/retry semantics.
- All enqueue call sites that must learn priority: transcribe enqueue in
  `archive_transcribe.py`, chat_backfill + transcribe enrichment in `routers/archive.py`
  (archive_smart_enrich), ingest paths (`archive_twitch.py` / `archive_kick.py` /
  `archive_ytdlp.py`).
- Preview flow: `routers/preview.py` session create → `services/preview/session.py` +
  `warm.py`; check whether preview already enqueues transcribe (and where) so the
  prioritize hook plugs in once.
- Migration pattern for the table rebuild (CHECK-constraint widen) already exists:
  `_ensure_jobs_kind_events` — reuse for adding a `priority` column.

Tasks:
1. Add `priority INTEGER NOT NULL DEFAULT 0` to `archive_jobs` via rebuild migration
   mirroring `_ensure_jobs_kind_events` + index on (status, priority, created_at).
   Acceptance: fresh + existing DB both get the column (PRAGMA table_info assert).
2. Extend `enqueue_job(..., priority=0)` and thread priority through every enqueue call
   site (archive_transcribe, archive.py enrichment, ingest). Acceptance: grep shows all
   enqueue sites pass priority; `has_job` dedupe still prevents double jobs.
3. Worker pick query: queued jobs ordered priority DESC, created_at ASC (FIFO within
   priority). Acceptance: assert-based self-check — 2 normal + 1 high job → high picked first.
4. Preview hook: on preview session create for a (platform, video_id) with no transcript
   yet, insert a high-priority transcribe job (or bump an existing *queued* job; never a
   running one). Acceptance: unit test — preview for transcript-less video yields a
   high-priority transcribe job; running job untouched.
5. Frontend: preview open path (App.tsx / ChannelExplorePopup.tsx) calls the prioritize
   endpoint fire-and-forget (no UI block); Queue tab reflects order. Acceptance: browser
   e2e — enqueue 2 videos' transcribe, preview a third → third runs first (real parakeet on
   a short fixture or job-pick order in worker logs).

## WS-2: Preview chat panel
Goal: preview player gets a right-side "chat" section showing chat history, transcription,
or subtitles for the video, synced to playback position.

Research first:
- Data sources: `messages` table (chat rows with offset_sec, spam_count, FTS5-indexed),
  transcript rows (ts offsets, `archive_transcribe.py`), and existing read APIs in
  `routers/archive.py` (search, chat_window, transcripts endpoints — reuse, don't duplicate).
- Preview player structure: App.tsx preview state (~lines 426-492), ChannelExplorePopup.tsx,
  `hooks/usePreviewPlayer.ts`, `previewPlayerUtils.ts` — currentTime/seek plumbing to sync
  the panel; panel resize must reuse the existing drag pattern (`startExplorePanelWidthResize`).
- Subtitles: preview HLS session (`services/preview/session.py`) rewrites playlists — check
  if captions track passthrough exists or must be added; else render ASR text as an overlay.

Tasks:
1. Backend: per-video panel payload — time-ordered transcript rows and/or chat rows
   (offset_sec, text, username, spam_count) with a strict response shape.
   Acceptance: real integration test on a titiltei VOD with transcripts — assert shape + order.
2. Backend: expose cheap "has transcript / has chat" capability flags (piggyback on the
   preview session response). Acceptance: preview payload includes flags; empty states
   derivable.
3. UI: right-side collapsible panel on the preview surface, tabs Chat / Transcript /
   Subtitles. Acceptance: browser check on titiltei VOD — each tab renders rows; empty tab
   shows empty state, no player breakage.
4. UI: playback sync — on currentTime change (seek + play) highlight/scroll the active row;
   subtitles tab shows the current segment's caption line. Acceptance: browser — seek to a
   timestamp, matching row highlighted; subtitle line updates.
5. UI: chat spam_count ×N badge (reuse archive-search pattern). Acceptance: row with
   spam_count > 1 shows badge.
6. Performance: panel must not re-render the player per frame — memoize/virtualize long
   lists. Acceptance: dragging/resizing panel keeps player fps (trace).
7. E2E: scripted browser check — open preview of a transcripted VOD, switch tabs, seek.
   Acceptance: check passes with strict assertions on rendered text.

## WS-3: Channel language detection + Spanish support
Goal: permanently and accurately detect each channel's language (pt-BR / en / es / …)
from platform clues plus transcript evidence; add Spanish anywhere the pipeline needs it.

Research first:
- Platform clues: Twitch Helix `broadcaster_language` (`services/twitch_gql_service.py`),
  Kick API channel payload language field (`kick_api_service.py`), YouTube Data API
  `snippet.defaultAudioLanguage`/`defaultLanguage` + yt-dlp language info
  (`youtube_service.py`, `youtube_innertube.py`, `channel_cache.py`, `models/schemas.py`
  SavedChannel).
- Transcript evidence: transcript rows carry the job language (parakeet has no
  language detection — `archive_transcribe.py` stores the explicit job/channel
  language on each row; there is no per-word probability); `archive_embed.py`
  embeddings (multilingual? check model); FTS5 vocab for aggregate stats.
- Where channel language should live: videos table / channels table / SavedChannel schema
  + archive search hits — pick one owner and migrate cleanly.

Tasks:
1. Backend: fetch language clues at channel fetch time per platform; persist on channel/
   videos rows (additive migration). Acceptance: real fetch of titiltei (pt), srdogg (en),
   mandiocaa/gaveta (yt) populates the field.
2. Backend: ASR language routing — per-channel/auto language via the existing
   `asr_language`/`channel_asr_languages` settings (parakeet supports
   `PARAKEET_LANG_CANDIDATES` + auto-detect-from-channel); Spanish rows captioned.
   Acceptance: real parakeet run on a short es fixture yields es captions —
   assert language tag on rows.
3. Backend: aggregation heuristic — when clues are missing/conflicting, tally the
   job language across many videos/sections; confidence + staleness rules;
   persistent per-channel language. Acceptance: self-check on synthetic rows; accuracy
   check on real titiltei/gaveta/srdogg transcripts (assert pt/pt/en).
4. Backend: expose channel language in channel payloads + archive search hits.
   Acceptance: API response contains language.
5. Frontend: language badge on channel cards + preview panel; captions language preference.
   Acceptance: browser check on real channels.
6. Settings: default ASR language (auto / pt-BR / en / es) + per-channel override.
   Acceptance: setting persists (settings.json round-trip) and worker honors it.
7. E2E: language detection accuracy on real channels (titiltei→pt, srdogg→en, gaveta→pt).
   Acceptance: reported detected vs expected per channel.

## WS-4: YouTube original-language titles
Goal: for YouTube, store/fetch the original (non-auto-translated) title + original
language — currently titles are auto-translated to English by YouTube for the user's
interface language.

Research first:
- Mechanism: YouTube localizes titles to viewer `hl` (English here). Candidate sources:
  yt-dlp `--extractor-args "youtube:lang=pt"` / `hl` param; innertube playerResponse
  `microformat.playerMicroformatRenderer` (original title/description under correct hl)
  and `captionTracks[].languageCode` / `vssId` (original vs translation) — `youtube_innertube.py`
  has no original-title handling today; YouTube Data API v3 `videos.list?part=snippet` →
  `defaultAudioLanguage` / `defaultLanguage`. Verify each on a real gaveta / arthur lanches
  / mandiocaa video whose title is currently English-translated.
- Where titles are written: videos table via `archive_ytdlp.py` channel walk — check which
  title field is stored and whether a re-fetch/backfill path exists.

Tasks:
1. Investigate + decide: compare hl=pt innertube fetch vs Data API `defaultAudioLanguage`
   vs yt-dlp lang arg on 3 real PT YouTube channels. Acceptance: recorded decision in this
   file or code comment; prefer no new deps.
2. Backend: derive original title + original language at ingest; store new columns
   (videos.original_title, videos.original_language) via additive migration + backfill
   for existing rows. Acceptance: real re-fetch shows original PT titles for gaveta videos
   that were English-translated.
3. Backend: prefer original_title in API payloads / search hits / display paths.
   Acceptance: API + UI show the PT title for a known gaveta video.
4. Wire `original_language` into WS-3 detection as a high-confidence clue.
   Acceptance: channel detection consumes it (assert in unit test).
5. E2E: real check — a known gaveta video title is now Portuguese in UI/API.
   Acceptance: strict assertion on real data.

## WS-5: Synthetic live-ID link cleanup
Goal: watchdog synthetic rows (`twitch-live-<slug>-<ms>` / `kick-live-...`) must never
surface as clickable video URLs in channel lists (user found
`https://www.twitch.tv/videos/twitch-live-titiltei-1785788650977` and
`https://kick.com/titiltei/videos/kick-live-titiltei-1785788650972`).

Research first:
- Root cause identified: `buildVodUrl` (`src/channelUtils.ts` ~420-470) guards
  `isSyntheticArchiveId` only in the YouTube branch; the Twitch/Kick branches build
  `https://www.twitch.tv/videos/${id}` / `kick.com/.../videos/${id}` unconditionally —
  that produces exactly the leaked URLs.
- Backend: channel list path (`routers/channels.py` `channel_videos` / `channel_clips`,
  `merge_youtube_playlists`; archive_db videos table accumulates watchdog `kind='live'`
  synthetic rows — `services/archive_watchdog.py` ~223). `routers/archive.py` already
  excludes synthetic rows from backfill via `video_id GLOB '[0-9]*'` — check the channel
  list queries don't.
- Frontend merge/filter: App.tsx `mergeVodLists` / `mergeClipLists` + ChannelExplorePopup /
  ChannelLinkCard render; `isSyntheticArchiveId` exists + tested
  (`src/channelUtils.test.ts` ~324, `channelUtils.ts` ~421).

Tasks:
1. Frontend: hoist the `isSyntheticArchiveId` guard to the top of `buildVodUrl` — any
   synthetic id returns '' regardless of branch. Acceptance: extend channelUtils.test.ts —
   twitch + kick synthetic ids return ''.
2. Frontend: filter synthetic rows out of channel vod/clip lists at merge time (or render
   sites) so the card disappears, not just the URL. Acceptance: unit test — merge drops
   synthetic rows.
3. Backend: channel list queries exclude synthetic rows (`video_id GLOB '*live-*'` or kind
   filter) so the API never returns them for saved channels. Acceptance: real API call on
   titiltei returns no `twitch-live-` / `kick-live-` ids.
4. Preserve archive-search + watchdog behavior (synthetic rows stay stored for chat
   history; search/backfill exclusion stays). Acceptance: existing
   `test_archive_watchdog.py` + `test_archive_enrich_v2.py` still pass.
5. E2E: browser — titiltei channel list shows no synthetic URLs; live capture during a real
   stream still works. Acceptance: scripted/manual check.

## WS-6: Preview fullscreen toggle bug
Goal: fullscreen enters/exits consistently — hitting it twice must not land in two
different fullscreen modes (user: "hitting it twice makes me fullscreen differently two
times").

Research first:
- Three fullscreen implementations to compare: App.tsx `togglePreviewFullscreen` (~2678) +
  `fullscreenchange` handler (~2750, sets state + rAF `syncPreviewPlaybackToViewport(fs)`);
  ChannelExplorePopup.tsx `toggleFullscreen` (~894) + handler (~939); LivePlayerPopup.tsx
  (~691). 'f' key handlers in App.tsx (~2725) and ChannelExplorePopup (~925) — double
  invocation risk (button click + key).
- Async race: `if (!document.fullscreenElement)` is a synchronous check; two rapid clicks
  before the promise settles both see null → double `requestFullscreen()` or
  enter/exit interleaving on different elements (container vs inner video).
- The "differently" symptom: `syncPlaybackToViewport(fullscreenOverride)` +
  `effectivePreviewHeight` / `playbackHeightFromRequest` (`previewPlayerUtils.ts` ~1177-1206)
  switch stream height per fullscreen toggle → each toggle may land in a different
  quality/layout. CSS `.preview-fs-host:fullscreen` (`src/index.css` ~109) +
  `applyExplorePopupFullscreenPosition` (`explorePopupUtils.tsx` ~97).

Tasks:
1. Reproduce in browser: open preview, click fullscreen twice (and 'f' twice), record
   `document.fullscreenElement` host + layout + stream height each time. Acceptance: repro
   notes; confirm which path causes the double-mode.
2. Fix race: one in-flight guard (ref) per toggle; derive state only from
   `fullscreenchange`; remove synchronous element checks from the toggle body.
   Acceptance: 2 rapid clicks → exactly one enter + one exit; state consistent.
3. Consolidate 'f' key handler with the click path (shared handler, ignore during
   transition). Acceptance: click then 'f' rapidly → no double toggle.
4. Ensure viewport/quality sync fires exactly once per transition
   (VIEWPORT_PREVIEW_FULLSCREEN_DEBOUNCE_MS = 120); exiting restores the docked quality.
   Acceptance: trace shows one quality switch per enter/exit; exit restores previous height.
5. Fix siblings sharing the bug (explore popup + live player) — simple ≠ incomplete.
   Acceptance: all three surfaces behave identically under the repro script.
6. Escape-exit path: `fullscreenchange` must not leave controls hidden
   (previewFsControlsVisible). Acceptance: after Escape, controls visible + state false.

## WS-7: Saved-words / entity watcher
Goal: watch for saved words/phrases AND saved channels across all VODs' transcriptions.
Auto mode: saved channels (titiltei, guiven, srdogg, mandiocaa, arthur lanches) auto-detected
when they appear in OTHER channels' transcripts (e.g. titiltei mentions guiven) with strong
highlight/reminder/notification. Manual mode: user marks words/phrases. Phrase-level
detection implies entity ("o guiven é muito ruim"); ASR-variant handling ("srdogg" heard as
"senhor dog"/"senior dog"; "mandiocaa" ≠ "mandioca"; "lanche" ≠ "arthur lanches"). A watcher
runs ~every 1 minute, optimized/cached. User explicitly asks to TEST both modes on titiltei.

Research first:
- Reuse: FTS5 in archive_db (fts5vocab), `archive_embed.py` semantic embeddings (just
  added), watchdog-style daemon thread pattern (`archive_watchdog.py`), messages/transcripts
  tables with offsets, search API (`routers/archive.py`), chat_window.
- Matching semantics: word-boundary exact + ASR-variant alias tables (per-entity variants)
  + phrase→entity inference; decide engine: FTS5 tokenization + custom scorer vs
  embeddings for phrases; Portuguese phonetic variants (srdogg vs "senhor dog").
- Persistence: new tables `watched_entities` / `entity_hits` via the archive_db rebuild
  migration pattern; settings UI in SettingsTab.tsx (DiskSection-style section).

Tasks:
1. Backend: schema — watched_entities (text, kind auto|manual, variant aliases, enabled),
   entity_hits (entity_id, platform, video_id, offset_sec, snippet, first/last seen).
   Acceptance: migration on fresh + existing DB (PRAGMA assert).
2. Backend: matcher — word-boundary exact + variant aliases + phrase→entity heuristics;
   guards: "mandioca" alone must NOT match "mandiocaa"; "lanche" must NOT match
   "arthur lanches"; "senhor dog"/"senior dog" match "srdogg". Acceptance: assert-based
   self-check covering every user example (positive + false-positive cases).
3. Backend: watcher daemon — ~1min loop over new transcript segments since a per-channel
   watermark; auto mode derives entities from saved channels (guiven just added → auto mode
   must detect it in titiltei). Acceptance: real run on titiltei VODs finds "guiven".
4. Backend: API — CRUD for manual entities, hits query (grouped by entity/video, recent
   first), ack/read. Acceptance: real integration test CRUD + hits on a DB copy.
5. Frontend: saved-words settings section (add/remove/enable auto mode); hits
   panel/notification badge; strong highlight when a hit's video is opened (transcript/chat
   viewer). Acceptance: browser — add word, open hit, see highlight + badge.
6. Performance: watermark per channel+entity, indexed queries, no full-table rescans on
   repeat passes. Acceptance: second watcher pass over same data is near-zero work
   (assert via query counts/logs).
7. E2E (user-requested): auto + manual modes on titiltei real transcripts — auto detects
   "guiven"; manual phrase "o guiven é muito ruim" hits. Acceptance: scripted check on a
   copy of the real archive.db.

## WS-8: Cache on biggest disk + settings
Goal: stored caches (models, ytdlp, temp/media, DB) default to the drive with most free
space; users can change it in settings.

Research first:
- Where caches live today: `_get_appdata_dir` (`backend/services/settings.py`) →
  %APPDATA%/VOD.RIP (archive.db, settings.json); AI-models root — the models cache that
  `backend/tests/test_whisper_model_settings.py` covers (env → settings
  `whisper_model_cache` → best-ROI drive → appdata; the sherpa parakeet cache is a subdir
  of it), `ytdlp_cache.py`, `disk_hygiene.py`, `download_persistence.py` (download_folder
  setting already exists), `os_services.py`.
- Disk detection with NO new deps (psutil NOT in requirements.txt): enumerate drive letters
  via ctypes `GetLogicalDriveStringsA` (app runs Python 3.11; os.listdrives is 3.12+) +
  `shutil.disk_usage(letter)`; prefer fixed drives over removable; sort by free space.
- Settings plumbing: `models/schemas.py` AppSettings + `services/settings.py` apply +
  `routers/settings.py` + `src/components/SettingsTab.tsx` / `DiskSection.tsx`.

Tasks:
1. Backend: drive enumeration + free-space ranking helper (ctypes + shutil.disk_usage,
   fixed drives first). Acceptance: assert-based self-check returns the real drive with
   most free space on this machine.
2. Backend: AppSettings new field `cache_dir` (default '' = auto → biggest free drive);
   settings apply/migration; settings.json round-trip. Acceptance: unit save/load test.
3. Backend: route cache roots through the setting — AI-models cache, ytdlp cache,
   preview temp, archive DB location (keep VODRIP_APP_DATA / VODRIP_ARCHIVE_DB env
   overrides for tests). Acceptance: with cache_dir set, a probe file lands there.
4. Backend: relocation helper — move existing caches to the new drive on first run
   (copy + verify + remove old; skip on same volume). Acceptance: self-check simulates the
   move with tmp dirs.
5. Frontend: DiskSection.tsx — cache location row: current dir, free space, "biggest
   drive (auto)" + custom picker. Acceptance: browser check; setting persists.
6. Real-machine verification: default lands on the biggest drive; app restarts clean with
   relocated caches (user has multiple drives). Acceptance: real check report.

## WS-9: 240Hz UI optimization
Goal: panel resizes/drags and general UI feel smooth up to 240Hz — current resize feels
slow/laggy. Websearch first, then refactor as needed.

Research first (websearch, record findings before coding):
- Current resize paths: App.tsx `startExplorePanelWidthResize` / `startFloatingPanelDrag` +
  `PersistedPanelLayout` / `PanelPos` types; setState-per-mousemove on a ~283KB App.tsx →
  full re-render per frame; `transition-[transform,...] duration-150` classes on resized
  elements; `--ui-scale` CSS var.
- Known patterns to evaluate: rAF-throttled direct style mutation during drag + state
  commit on pointerup; pointer capture; `content-visibility: auto` / CSS `contain` on
  offscreen panels; read/write batching to avoid layout thrash; `will-change: transform`;
  `useSyncExternalStore` for external layout state; ResizeObserver vs window resize.

Tasks:
1. Websearch current best practice for 240Hz panel resize in React (rAF + refs + pointer
   capture + direct DOM writes; React 18/19 notes). Acceptance: findings + sources recorded
   in this file before implementation.
2. Profile baseline: browser performance trace during panel drag — fps, long tasks,
   re-render count, layout thrash. Acceptance: baseline numbers recorded.
3. Refactor drag/resize to rAF-driven direct style mutation with pointer capture; commit
   state on pointerup (or useSyncExternalStore). Acceptance: trace shows no React
   re-render per mousemove; drag at refresh rate.
4. CSS containment: `contain: layout paint` / content-visibility on heavy panels (preview
   player, lists). Acceptance: trace improvement; no visual regression.
5. Remove resize-hampering transitions on drag targets; keep animations on non-drag
   interactions. Acceptance: no stutter during drag; animations intact elsewhere.
6. Verify at 240Hz: trace shows consistent frame pacing on a 240Hz display across preview
   panel, explore popup, and live popup resizes. Acceptance: real trace, fps ≥ refresh rate.

---
## WS-9 findings (websearch, recorded 2026-08-04 before implementation)

### Consensus (sources: MDN Web Docs — Pointer Events / requestAnimationFrame /
`content-visibility`; React docs — `useSyncExternalStore`, "keep external state external";
React Labs blog — future `use(store)` still research; community: Framer Motion / react-spring
architecture; Chrome DevTools docs — forced reflow/layout thrash)

1. Treat pointer drag as an imperative animation problem, not React rendering work.
   React 18/19 add NO drag API; pointermove streams can exceed the display refresh rate
   (144–240 Hz displays, high-polling-rate mice) and setState-per-move causes schedule +
   reconcile + re-render per event, GC churn, and lost layout/paint headroom. React
   explicitly recommends `useSyncExternalStore` for external mutable layout state (and a
   future `use(store)` replacement is still research, not production guidance).
2. Canonical pattern: pointerdown → `setPointerCapture(pointerId)` → pointermove updates
   refs only → schedule ONE pending `requestAnimationFrame` → rAF callback writes the
   latest value to `el.style` once per frame → pointerup → `releasePointerCapture` +
   commit final value with one `setState`. Multiple pointer events collapse into one
   paint; rAF naturally syncs to 60/120/144/165/240 Hz display rates.
3. Avoid forced reflow: read geometry (`offsetWidth`/`offsetHeight`, `innerWidth`/
   `innerHeight`) ONCE at drag start (or batch reads before writes); never interleave
   `style.width = …` with a following `offsetHeight` read (that flushes layout per move).
4. CSS transitions on the dragged/resized element add input latency ("cursor chasing") —
   every move restarts a 150 ms ease. Disable transitions while dragging (e.g. a
   `.dragging`/`.resizing` class → `transition: none`), restore after pointerup so
   non-drag animations stay intact.
5. CSS containment: `contain: layout paint` (or `contain: content`) limits layout
   invalidation to the contained subtree — right tool for panels. `content-visibility:
   auto` skips rendering off-screen subtrees (with `contain-intrinsic-size` to reserve
   space) — use for long lists/offscreen panels; pitfalls: scrollbar jumps, deferred
   layout confusing synchronous measurement, ResizeObserver loop warnings (never mutate
   layout synchronously inside a ResizeObserver callback; batch in rAF).
6. `will-change` only while dragging (set on pointerdown, clear on pointerup); leaving it
   permanently raises memory/compositing cost.
7. `startTransition` must NOT wrap pointer-move geometry updates (lowers urgency → worse
   INP); reserve transitions for secondary derived work. Transforms beat top/left when
   geometry allows, but width-resize inherently triggers layout — containment isolates it.
8. Pointer Events (one API for mouse/touch/pen + capture) beat document-level
   mousemove/mouseup listeners.

### Applied decision (recorded before coding)
- Refactor ALL drag/resize helpers to the rAF-coalesced direct-DOM-write pattern with
  state commit on pointerup. Helpers in `src/layoutUtils.ts`
  (`startPanelResizeDrag`, `startPanelWidthResize`) and `src/explorePopupUtils.tsx`
  (`startExplorePanelWidthResize`, `startExplorePanelBoxResize`, `startFloatingPanelDrag`)
  share a tiny internal rAF loop; geometry reads hoisted to drag start; no setState in
  move path. Callers unchanged (same signatures) — App.tsx onResizeMove callbacks keep
  firing per frame with the LATEST coalesced value.
- No `useSyncExternalStore` rewrite: drag state is already ref-backed; only the final
  value needs React state, and commit-on-pointerup satisfies that. (ponytail: if later a
  second React consumer needs live drag geometry, introduce an external store + uSES then.)
- CSS: `contain: layout paint` + `content-visibility: auto` on heavy panels/lists in
  index.css; `.resizing` class toggles `transition: none` on the dragged element;
  `will-change` stays pointerdown→pointerup scoped (already the pattern).
- Baseline profiling runs against a fresh throwaway backend profile (VODRIP_APP_DATA
  override, real user data untouched) + Vite dev server; drag driven via Playwright CDP
  real input; rAF-delay sampler + longtask observer + React commit counter for numbers.

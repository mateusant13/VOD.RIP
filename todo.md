# Archive Search Overhaul — todo.md

User batch: 8 items on the Archive Search panel (VOD.RIP). Split into 2 worktree
subagents (UI / backend) + 1 live scout, run in parallel, merged back, then
verified live.

## Ground truth (probed the real archive DB, 2026-08-03)

- `titiltei` = merged channel: 74 rows across youtube/twitch/kick. YouTube has
  transcripts (3 videos: 7lc1GxEEvhM 8095 segs, 3sCcLEsYw3M 10120, aexkXGl9Gr4
  10952) + chat; **Twitch VODs all have 0 chat / 0 transcripts**; Kick rows ready.
- `caedrel`: 50 rows (youtube + twitch), ALL zero chat/transcripts — the test
  target for #7.
- Only 3 videos have transcripts; 17 have messages (watchdog live capture).
- `chat_backfill` runs DO work in this env: 5 done jobs, 2 videos (2834554822 →
  307 msgs, 2833683318 → 221 msgs). **There is simply no runtime trigger** — no
  router, no job consumer calls `archive_twitch.backfill_chat`.
- `transcripts` table has NO `lang` column.
- Watchdog synthetic rows (`twitch-live-<slug>-<ms>`, `youtube-live-...`) have
  real chat (552/86 msgs) — those are live captures, not backfill.

## The 8 items → slices

### UI worktree (branch `feat/archive-ui`, cwd ../VOD.RIP-ui)
1. **Channel dedup** — `ArchiveSearchPopup.channelOptions`:
   - Group archived channel strings case-insensitively; collect EVERY distinct
     casing variant (backend matches `v.channel` exactly, case-insensitively
     after the backend change).
   - Saved channels win: if a saved channel's slug (case-insensitive) matches
     the group, use its display name (`deriveChannelDisplayName`) + value =
     union of saved slugs AND archived variants.
   - Else canonical label = most-frequent casing in the group (fallback
     first-seen); value = all distinct variants comma-joined.
   - Dropdown shows one row per group; case-insensitive search works because
     backend lowercases both sides.
2. **Every-day filter** — `everyDay` state, default `true`. Checked → search
   ignores stored dates. Picking a date sets the value AND unchecks. Re-checking
   keeps the values but ignores them. Unchecking re-applies the stored dates.
   (Existing "EVERY DAY" clear button becomes the toggle; keep values on toggle.)
3. **Vertical panel** — floating mode seeds height `min(88vh, 760px)` instead
   of auto-height; width stays 460.
4. **Language chips** — new `langFilter` state (`'' | 'pt' | 'en'`); a chips row
   (PT-BR / EN) renders ONLY when the current hits contain ≥2 distinct
   transcript `lang` values (per user: "for when both languages exist"). Sends
   `lang` via `buildSearchUrl`. Hit rows may show a tiny lang badge.
   - `archiveSearchUtils.ts`: `ArchiveSearchHit.lang?: string | null`,
     `ArchiveSearchFilterParams.lang`, buildSearchUrl emits `lang`.
   - Keep `ArchiveSearchPopup.test.tsx` green; add tests: dedup grouping,
   every-day toggle behavior, lang chips visibility + param emission.

### Backend worktree (branch `feat/archive-backend`, cwd ../VOD.RIP-backend)
5. **Fuzzy search** — `archive_db.search` token expansion:
   - Build FTS5 vocab (`fts5vocab(messages_fts/transcripts_fts, 'row')`), cache
     in memory: top ~25k tokens by count, refresh TTL ~5 min.
   - Per query token (len ≥ 3): candidates = exact + Levenshtein ≤
     `max(1, len//5)` over length-filtered (±2) vocab; cap ~8 candidates/token
     (least distance, then frequency); cache per-token expansions.
   - Pattern stays `"a" OR "b"` quoted phrases. Fall back to current behavior
     if vocab unavailable. Self-check: "arthur" expands to "artur".
6. **Twitch chat backfill wired at runtime**:
   - New `POST /api/archive/videos/{platform}/{video_id}/chat/backfill`
     (twitch only, else 404/400). Background `create_task` →
     `archive_twitch.backfill_chat(channel, video_id)`; in-flight dedupe set;
     returns `{ok, status: queued|running|already|failed, inserted}`.
   - Auto-trigger in `archive_search`: when source includes chat and platform
     filter includes twitch, lazily fire backfill for the 2 newest chat-less
     Twitch videos in scope (per-video once per process, rate-limited
     ~15s between kicks). Chat appears on the next search.
   - No transcription for Twitch: rows are status 'known' with no file;
     whisper needs the file. Documented limitation.
7. **YT native captions both langs** — `archive_ytdlp._pick_caption_for` +
   `ingest_video`: fetch the best pt-ish track AND the best en track when both
   exist (existing single-track preference preserved when only one exists);
   `insert_transcript(..., lang=...)` tags rows (`'pt'` normalized from
   pt/pt-br/pt-pt, `'en'`, else the raw code). Whisper rows: lang from
   `VODRIP_WHISPER_LANGUAGE` if set, else NULL.
8. **Schema + search** — `transcripts.lang TEXT` additive migration
   (`_ensure_lang_column`, same pattern as `_ensure_kind_column`);
   `insert_transcript(lang=None)`; `search(lang=None)`:
   - `lang='pt'` → `lang IS NULL OR lang LIKE 'pt%'` (whisper rows untagged
     are PT content)
   - `lang='en'` → `lang = 'en'`
   - hits carry `lang`.
   - Channel filter becomes case-insensitive: `lower(v.channel) IN (...)` with
     lowercased slugs (user: "titiltei vs TiTiltei both work").
   - Router: `lang: str | None = None` param.
   - Tests: extend `test_archive_search_filters.py` (fuzzy, lang, CI channel);
   keep `test_channel_index.py`, `test_archive_yt_captions.py`,
   `test_archive_retention.py` green.

### Scout (read-only, main checkout)
- Live GQL probe: pick caedrel recent Twitch VODs (2835141679, 2834270468,
  2833412357, 2832598016, 2831780226) + titiltei 2832716983/2832714554; run the
  same video-comments fetch `archive_twitch.backfill_chat` uses into a SCRATCH
  DB (env `VODRIP_ARCHIVE_DB`, never the real one); report: pages/time, 429
  behavior, and whether "god"/"omg" appear case-insensitively in caedrel chat,
  plus the exact GQL function names/args to call.
- This is the risk gate: if anonymous GQL chat fetch fails for those VODs, #6
  needs a different route (report back, do not improvise).

## Contracts (decided up front — do not renegotiate)

- Search endpoint: `lang: str | None`; channel matching case-insensitive.
- Hits JSON gains `lang` (transcript rows: transcripts.lang; message rows: null).
- `POST /api/archive/videos/{platform}/{video_id}/chat/backfill` shape as above.
- `buildSearchUrl` + `ArchiveSearchHit` gain `lang`.
- No new npm/PyPI deps. One assert-based self-check per non-trivial change.
- No transcription for Twitch/Kick without a file on disk.

"""Local archive store — SQLite WAL + FTS5 for chat, transcripts, and video index.

The "local Google" contract every ingestion/chat/transcription/search slice
builds against. Single-writer design: the app is a desktop process, so one
module-level connection guarded by a lock is sufficient.

Storage layout (all offsets are seconds into the stream, monotonic):
  videos        — one row per (platform, video_id); canonical_key dedupes
                  the same live/VOD simulcast across platforms
  messages      — chat rows, append-only; FTS5 contentless index
  transcripts   — word-timestamped segments (optional lang tag); FTS5
                  contentless index
  video_aliases — manual canonical_key overrides for cross-platform dedupe
  archive_jobs  — ingest / chat / transcribe / events queue

DB location: %APPDATA%/VOD.RIP/archive.db (same dir as settings.json);
override with env VODRIP_ARCHIVE_DB (used by tests).
"""
from __future__ import annotations

import collections
import functools
import html
import json
import logging
import os
import pickle
import re
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from services import settings as _settings

logger = logging.getLogger(__name__)

PLATFORMS = ("youtube", "twitch", "kick")
# "stream" = YouTube was_live content from the /streams tab (recorded live
# broadcasts). Without it, _normalize_kind mapped every stream row to "vod",
# so stream VODs were indistinguishable from regular uploads in the index.
KINDS = ("vod", "clip", "short", "live", "stream", "video")

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
  platform      TEXT NOT NULL CHECK (platform IN ('youtube','twitch','kick')),
  video_id      TEXT NOT NULL,
  channel       TEXT NOT NULL,
  title         TEXT NOT NULL,
  started_at    TEXT,
  ended_at      TEXT,
  duration_sec  REAL,
  archive_path  TEXT,
  canonical_key TEXT,
  -- SHA-256 of the archived media file bytes (content dedup: two rows may
  -- share one archive_path when their files are byte-identical).
  content_sha256 TEXT,
  -- WS-4: original (non-auto-translated) YouTube title + its language.
  -- YouTube localizes titles to the viewer's hl (the channel walk's yt-dlp
  -- default is en), so `title` may hold an auto-translated English copy for
  -- PT channels; original_title is captured from an hl-free InnerTube player
  -- fetch (youtube_innertube.innertube_original_meta) and preferred for
  -- display. NULL until backfilled; original_language feeds WS-3 detection.
  original_title TEXT,
  original_language TEXT,
  status        TEXT NOT NULL DEFAULT 'known'
                CHECK (status IN ('known','downloading','ready','failed')),
  kind          TEXT NOT NULL DEFAULT 'vod'
                CHECK (kind IN ('vod','clip','short','live','stream','video')),
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  PRIMARY KEY (platform, video_id)
);
CREATE INDEX IF NOT EXISTS idx_videos_channel  ON videos(channel);
CREATE INDEX IF NOT EXISTS idx_videos_canonical ON videos(canonical_key);

CREATE TABLE IF NOT EXISTS messages (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  platform   TEXT NOT NULL,
  video_id   TEXT NOT NULL,
  offset_sec REAL NOT NULL,
  user_id    TEXT,
  username   TEXT NOT NULL,
  -- Resolved platform chat display name (YouTube only: captured chat carries
  -- the @handle in username; the display name is resolved per UC channel id
  -- and cached here so the USER search filter matches what viewers see).
  display_name TEXT,
  text       TEXT NOT NULL,
  badges     TEXT NOT NULL DEFAULT '[]',
  emotes     TEXT NOT NULL DEFAULT '[]',
  ts         TEXT,
  -- Platform chat username color (#RRGGBB, NULL = use deterministic palette).
  color      TEXT,
  -- Collapsed-duplicate counter: identical consecutive chat rows within 60 s
  -- merge into one stored row and this column counts the merged messages.
  spam_count INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_messages_video ON messages(platform, video_id, offset_sec);

-- External-content FTS5 (text stored once in the content table; the index
-- holds token data only). The AFTER INSERT/UPDATE/DELETE triggers created by
-- _migrate_fts_contentless() own the index — contentless-style tables cannot
-- be updated directly. Fresh DBs get this DDL; legacy regular-FTS DBs are
-- converted by the migration on first open.
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  text, content='messages', content_rowid='id');

CREATE TABLE IF NOT EXISTS transcripts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  platform   TEXT NOT NULL,
  video_id   TEXT NOT NULL,
  seg_idx    INTEGER NOT NULL,
  start_sec  REAL NOT NULL,
  end_sec    REAL NOT NULL,
  text       TEXT NOT NULL,
  words_json TEXT NOT NULL DEFAULT '[]',
  lang       TEXT
);
CREATE INDEX IF NOT EXISTS idx_transcripts_video ON transcripts(platform, video_id, start_sec);

CREATE VIRTUAL TABLE IF NOT EXISTS transcripts_fts USING fts5(
  text, content='transcripts', content_rowid='id');

-- Semantic-search vectors, one row per transcript segment (float32 blob,
-- 1536 bytes for multilingual-e5-small). Produced lazily by the semantic
-- search pass; the DELETE trigger keeps the row in sync with its transcript
-- (mirrors the FTS trigger pattern).
CREATE TABLE IF NOT EXISTS transcript_embeddings (
  transcript_id INTEGER PRIMARY KEY,
  vec           BLOB NOT NULL
);
CREATE TRIGGER IF NOT EXISTS transcript_embeddings_ad AFTER DELETE ON transcripts BEGIN
  DELETE FROM transcript_embeddings WHERE transcript_id = old.id;
END;

-- Model-version stamp for stored semantic vectors: a fingerprint of the
-- embed-model files that produced them. A mismatch with the current model
-- triggers a full re-embed — vectors from two different embedders are not
-- comparable, so a model upgrade must never silently mix vector spaces.
CREATE TABLE IF NOT EXISTS semantic_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- Acoustic-event rows from the PANNs stage (kind='events' jobs, or the
-- VODRIP_EVENTS_ENABLED auto-run after transcription): laughs, claps,
-- screams, music, ... with real boundaries and a confidence score.
CREATE TABLE IF NOT EXISTS audio_events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  platform   TEXT NOT NULL,
  video_id   TEXT NOT NULL,
  start_sec  REAL NOT NULL,
  end_sec    REAL NOT NULL,
  event      TEXT NOT NULL,
  score      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audio_events_video ON audio_events(platform, video_id, start_sec);

-- Saved-word / entity watching: entities the user (or auto mode from saved
-- channels) wants detected across all transcriptions. entity_hits rows are
-- the detection log; the unique key (entity, platform, video_id, seg_idx)
-- makes repeated scans idempotent (INSERT OR IGNORE + last_seen refresh).
CREATE TABLE IF NOT EXISTS watched_entities (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  text           TEXT NOT NULL UNIQUE,
  kind           TEXT NOT NULL DEFAULT 'manual' CHECK (kind IN ('auto','manual')),
  source_channel TEXT,
  aliases        TEXT NOT NULL DEFAULT '[]',
  enabled        INTEGER NOT NULL DEFAULT 1,
  created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entity_hits (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_id  INTEGER NOT NULL,
  platform   TEXT NOT NULL,
  video_id   TEXT NOT NULL,
  seg_idx    INTEGER NOT NULL,
  offset_sec REAL NOT NULL,
  snippet    TEXT NOT NULL,
  variant    TEXT,
  seen_count INTEGER NOT NULL DEFAULT 1,
  first_seen TEXT NOT NULL,
  last_seen  TEXT NOT NULL,
  acked      INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_hits_dedup
  ON entity_hits(entity_id, platform, video_id, seg_idx);
CREATE INDEX IF NOT EXISTS idx_entity_hits_recent ON entity_hits(last_seen);
-- Watcher watermark: highest transcripts.id already scanned for entities.
CREATE TABLE IF NOT EXISTS entity_watch_state (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS entity_hits_entity_ad AFTER DELETE ON watched_entities BEGIN
  DELETE FROM entity_hits WHERE entity_id = old.id;
END;

CREATE TABLE IF NOT EXISTS video_aliases (
  platform      TEXT NOT NULL,
  video_id      TEXT NOT NULL,
  canonical_key TEXT NOT NULL,
  note          TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (platform, video_id)
);

CREATE TABLE IF NOT EXISTS archive_jobs (
  id         TEXT PRIMARY KEY,
  kind       TEXT NOT NULL CHECK (kind IN ('ingest','chat','transcribe','events')),
  platform   TEXT NOT NULL,
  video_id   TEXT NOT NULL,
  status     TEXT NOT NULL DEFAULT 'queued'
             CHECK (status IN ('queued','running','done','failed')),
  progress   REAL NOT NULL DEFAULT 0,
  error      TEXT,
  priority   INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  heartbeat  TEXT
);
-- ponytail: the (status, priority, created_at) index is created by
-- _ensure_jobs_priority AFTER the column migration — SCHEMA's executescript
-- runs before the rebuilds, so an index on a not-yet-migrated column would
-- fail on any legacy DB (CREATE INDEX resolves columns at definition time).

-- Per-channel/platform last-refresh time for the channel VOD index. The
-- videos table accumulates fetched channel lists forever (upsert-only);
-- snapshots decide when a background delta refresh is due.
CREATE TABLE IF NOT EXISTS channel_snapshots (
  platform    TEXT NOT NULL CHECK (platform IN ('youtube','twitch','kick')),
  channel_key TEXT NOT NULL,
  fetched_at  TEXT NOT NULL,
  PRIMARY KEY (platform, channel_key)
);

-- Archive-scheduler top-priority windows: a channel marked here (newly
-- added, or the user viewing its page) is processed ahead of the queued
-- older backlog until priority_until (ISO UTC) expires — bounded, so the
-- backlog can never starve permanently. channel_key is lowercased so the
-- scheduler's lower(channel) joins match YouTube @Handles too.
CREATE TABLE IF NOT EXISTS channel_priorities (
  platform       TEXT NOT NULL CHECK (platform IN ('youtube','twitch','kick')),
  channel_key    TEXT NOT NULL,
  priority_until TEXT NOT NULL,
  PRIMARY KEY (platform, channel_key)
);

-- Worker liveness: the transcribe worker stamps a heartbeat every poll
-- iteration; search enrichment checks worker_live() before enqueueing jobs
-- so the honest 'Indexing…' line never sits on a queue nobody consumes.
CREATE TABLE IF NOT EXISTS worker_heartbeats (
  tag TEXT PRIMARY KEY,
  at  TEXT NOT NULL
);
"""


def _db_path() -> Path:
    override = os.environ.get("VODRIP_ARCHIVE_DB", "").strip()
    if override:
        return Path(override)
    from services.disk_hygiene import data_dir  # lazy: keeps module import light

    return data_dir() / "archive.db"


def _migrate_db_to_data_dir(target: Path) -> None:
    """One-time move of the DB (and sidecars) to the configured data disk.

    Runs before the first connection opens at a new data_dir: if a DB exists
    at the default app-data location and the target is elsewhere, copy
    archive.db (+ -wal/-shm) and the whisper resume manifests so the switch
    is seamless. Idempotent — no-op when nothing to move or target exists.
    """
    src_dir = _settings._get_appdata_dir()
    if src_dir == target.parent:
        return
    src = src_dir / "archive.db"
    if not src.exists() or target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        p = src_dir / f"archive.db{suffix}"
        if p.exists():
            shutil.copy2(p, target.parent / f"archive.db{suffix}")
    src_manifest = src_dir / "whisper_manifest"
    if src_manifest.is_dir():
        shutil.copytree(src_manifest, target.parent / "whisper_manifest", dirs_exist_ok=True)


# RLock: query()/execute() hold the lock while calling get_conn(), which
# re-acquires it — a plain Lock would self-deadlock on first read.
_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None
_conn_path: Optional[str] = None
_schema_ready = False


def get_conn() -> sqlite3.Connection:
    """Return the shared WAL connection, initializing schema on first use.

    The cache is keyed on the resolved DB path: if VODRIP_ARCHIVE_DB (or the
    appdata dir) changes at runtime — test modules rebind it per suite — the
    old connection is dropped and a fresh one for the new path is opened.
    In production the path never changes, so this is a no-op there."""
    global _conn, _conn_path, _schema_ready
    with _lock:
        path = _db_path()
        if _conn is not None and _conn_path != str(path):
            try:
                _conn.close()
            except sqlite3.Error:
                pass
            _conn = None
            _schema_ready = False
        if _conn is None:
            _conn_path = str(path)
            _migrate_db_to_data_dir(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            # check_same_thread=False: workers (archive_transcribe, chat
            # backfill, chat sinks) call into this module from pool threads;
            # the module RLock serializes every access, so the C-level safety
            # check is redundant paranoia here.
            conn = sqlite3.connect(str(path), timeout=10.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(SCHEMA)
            conn.commit()
            # Disk hygiene (fresh open only): checkpoint a stale -wal left by
            # a killed process, then VACUUM when the freelist outgrows 10% of
            # the file (heavy insert/delete churn). Both are best-effort — a
            # busy/locked DB fails and is retried next start.
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            try:
                page_count = conn.execute("PRAGMA page_count").fetchone()[0]
                freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
                if page_count > 0 and freelist > page_count // 10:
                    conn.execute("VACUUM")
                    # In WAL mode VACUUM rewrites through the -wal, so the
                    # main file only shrinks once the new frames are
                    # checkpointed back in.
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            _conn = conn
        if not _schema_ready:
            _conn.executescript(SCHEMA)
            _ensure_kind_column(_conn)
            _ensure_kind_check_includes_stream(_conn)
            _ensure_channel_columns(_conn)
            _ensure_content_sha_column(_conn)
            _ensure_channel_language_column(_conn)
            _ensure_original_columns(_conn)
            _ensure_captions_unavailable_column(_conn)
            _ensure_original_failed_column(_conn)
            _ensure_lang_column(_conn)
            _ensure_spam_column(_conn)
            _ensure_message_color_column(_conn)
            _ensure_message_display_name_column(_conn)
            _ensure_jobs_kind_events(_conn)
            _ensure_jobs_priority(_conn)
            _ensure_jobs_kind_chat(_conn)
            _ensure_jobs_heartbeat_column(_conn)
            rebuilt = _migrate_fts_contentless(_conn)
            # One-time data migrations on transcripts (entity + lang backfill).
            # Runs after the FTS rebuild so the current trigger set re-indexes.
            _migrate_transcript_data(_conn)
            _conn.commit()
            if rebuilt:
                # Legacy index rebuilds leave the old FTS shadow-table pages
                # on the freelist; VACUUM + checkpoint so the file actually
                # shrinks (same pattern as the fresh-open hygiene above).
                try:
                    _conn.execute("VACUUM")
                    _conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.Error:
                    pass
            _schema_ready = True
        return _conn


def _ensure_kind_column(conn: sqlite3.Connection) -> None:
    """Idempotent migration: add videos.kind (vod|clip|short|live|stream, 'vod' default).

    Safe on pre-kind DBs: ADD COLUMN with NOT NULL DEFAULT is immediate and
    backfills existing rows with 'vod'. PRAGMA table_info guard makes repeated
    calls (re-imports, reloads) no-ops."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(videos)")}
    if "kind" not in cols:
        conn.execute(
            "ALTER TABLE videos ADD COLUMN kind TEXT NOT NULL DEFAULT 'vod'"
            " CHECK (kind IN ('vod','clip','short','live','stream','video'))"
        )


_VIDEO_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_videos_channel ON videos(channel)",
    "CREATE INDEX IF NOT EXISTS idx_videos_canonical ON videos(canonical_key)",
)


def _ensure_kind_check_includes_stream(conn: sqlite3.Connection) -> None:
    """Rebuild videos once so its kind CHECK accepts 'stream'.

    The original CHECK (vod/clip/short/live) predates the recorded-broadcast
    kind, and SQLite cannot alter constraints in place — so the table is
    swapped via rename+copy when the stored DDL lacks 'stream'. The rebuild
    is safe for the FTS shadow tables: transcripts/messages link to videos by
    (platform, video_id) columns, never rowid. A crash mid-swap leaves
    videos_old behind; the next open restores it if the copy never landed.
    """
    def _recreate_indexes() -> None:
        for sql in _VIDEO_INDEX_SQL:
            conn.execute(sql)

    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if "videos_old" in tables:
        # Crash-leftover from a prior swap: restore when the new table is an
        # empty shell (copy never landed), else the leftover is dead weight.
        n_videos = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        n_old = conn.execute("SELECT COUNT(*) FROM videos_old").fetchone()[0]
        if n_videos == 0 and n_old > 0:
            conn.execute("DROP TABLE videos")
            conn.execute("ALTER TABLE videos_old RENAME TO videos")
            _recreate_indexes()
        else:
            conn.execute("DROP TABLE videos_old")
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='videos'"
    ).fetchone()
    ddl = (row[0] or "") if row else ""
    if not ddl or "'video'" in ddl:
        return
    _KIND_CHECK = "CHECK (kind IN ('vod','clip','short','live','stream','video'))"
    ddl = re.sub(r"CHECK \(kind IN \([^)]+\)\)", _KIND_CHECK, ddl, count=1)
    if "kind" not in ddl:
        # kind was added via ALTER TABLE (pre-kind DBs) — ALTER never touches
        # sqlite_master.sql, so the stored DDL lacks the column. Rebuild with
        # kind appended (same position ALTER would have used: last column).
        pk_idx = ddl.rfind("PRIMARY KEY")
        if pk_idx == -1:
            ddl = ddl.rstrip().rstrip(")").rstrip() + (
                ", kind TEXT NOT NULL DEFAULT 'vod'"
                " CHECK (kind IN ('vod','clip','short','live','stream','video')))"
            )
        else:
            ddl = ddl[:pk_idx] + (
                "  kind TEXT NOT NULL DEFAULT 'vod'"
                " CHECK (kind IN ('vod','clip','short','live','stream','video')),\n"
            ) + ddl[pk_idx:]
    conn.execute("ALTER TABLE videos RENAME TO videos_old")
    conn.execute(ddl)
    conn.execute("INSERT INTO videos SELECT * FROM videos_old")
    conn.execute("DROP TABLE videos_old")
    _recreate_indexes()


def _ensure_channel_columns(conn: sqlite3.Connection) -> None:
    """Idempotent migration: add channel-view metadata columns to videos.

    The channel VOD index (upsert_channel_video) stores what the channel
    list payload needs — duration_string, views, thumbnail_url — so the
    list can be served straight from the disk index. Additive only; nothing
    is ever dropped or reset."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(videos)")}
    if "duration_string" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN duration_string TEXT")
    if "views" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN views INTEGER")
    if "thumbnail_url" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN thumbnail_url TEXT")


def _ensure_content_sha_column(conn: sqlite3.Connection) -> None:
    """Idempotent migration: add videos.content_sha256 (content-dedup hash).

    The ingest path hashes each freshly written media file and stores the
    SHA-256 here; a second row with byte-identical content reuses the first
    row's archive_path instead of keeping a second copy. Additive only;
    NULL means "not yet hashed" (pre-dedup rows). PRAGMA table_info guard
    makes repeated calls no-ops. The index is created here (not in SCHEMA)
    so legacy DBs — where the column does not exist yet when SCHEMA's DDL
    runs — never see a CREATE INDEX on a missing column."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(videos)")}
    if "content_sha256" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN content_sha256 TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_videos_content_sha ON videos(content_sha256)"
    )


def _ensure_channel_language_column(conn: sqlite3.Connection) -> None:
    """Idempotent migration: add videos.channel_language (per-channel language).

    Owner of the per-channel language detection (WS-3): every video row of a
    channel carries the channel's language ('pt'/'en'/'es'/raw code, NULL =
    unknown). Populated at channel-fetch time from platform clues and by the
    transcript-evidence aggregation (services/channel_language.py). Additive
    only; PRAGMA table_info guard makes repeated calls no-ops."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(videos)")}
    if "channel_language" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN channel_language TEXT")
def _ensure_original_columns(conn: sqlite3.Connection) -> None:
    """Idempotent migration: add videos.original_title / original_language.

    WS-4: YouTube localizes titles to the viewer's hl (the channel walk's
    yt-dlp default is en), so the stored title of a PT channel may be an
    auto-translated English copy. These columns hold the original
    (hl-free fetch) title and its caption-derived language. Plain nullable
    TEXT columns — ALTER ADD COLUMN matches _ensure_channel_columns; the
    table-rebuild pattern (_ensure_kind_check_includes_stream) is only
    needed when a CHECK constraint must widen, which is not the case here.
    The rebuild runs BEFORE this migration in get_conn, so its
    `INSERT INTO videos SELECT * FROM videos_old` never sees the new
    columns at a different position. PRAGMA table_info guard makes
    repeated calls (re-imports, reloads) no-ops."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(videos)")}
    if "original_title" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN original_title TEXT")
    if "original_language" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN original_language TEXT")


def _ensure_captions_unavailable_column(conn: sqlite3.Connection) -> None:
    """Idempotent migration: add videos.captions_unavailable_at (no-captions marker).

    Persists the "this YouTube video has no auto captions" verdict so the
    scheduler stops re-extracting captionless videos every pass/boot (the
    in-memory _yt_attempted_at 1h backoff dies with the process). Stamped
    by archive_ytdlp.ingest_video when an ingest stores zero caption
    segments; cleared on any successful caption ingest. NULL = unknown or
    captions exist. Plain nullable TEXT (ISO UTC) — ALTER ADD COLUMN is
    immediate and additive; PRAGMA table_info guard makes repeated calls
    (re-imports, reloads) no-ops."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(videos)")}
    if "captions_unavailable_at" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN captions_unavailable_at TEXT")


def _ensure_original_failed_column(conn: sqlite3.Connection) -> None:
    """Idempotent migration: add videos.original_fetch_failed_at (orig-title cooldown).

    Persists the WS-4 original-title backfill failure cooldown (in-memory
    _original_failed_at in archive_ytdlp dies with the process, so a
    permanently failing video is re-fetched once per app restart forever).
    Stamped by archive_ytdlp._mark_original_failed; read by
    _original_failed_recently. NULL = no recent failure. Plain nullable
    TEXT (ISO UTC) — additive ALTER ADD COLUMN; PRAGMA table_info guard
    makes repeated calls no-ops."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(videos)")}
    if "original_fetch_failed_at" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN original_fetch_failed_at TEXT")


def _ensure_lang_column(conn: sqlite3.Connection) -> None:
    """Idempotent migration: add transcripts.lang (subtitle/whisper language).

    YT captions and whisper rows carry an optional ISO-ish language tag
    ('pt', 'en', or raw codes); search() filters on it. Additive only; NULL
    means "unknown — treated as PT content" (whisper rows without a
    detected language are Portuguese). PRAGMA table_info guard makes
    repeated calls no-ops."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(transcripts)")}
    if "lang" not in cols:
        conn.execute("ALTER TABLE transcripts ADD COLUMN lang TEXT")


def _clean_legacy_transcript_text(text: str) -> str:
    """Twin of archive_ytdlp._clean_caption_text for the data backfill.

    Kept inline (not imported) because archive_ytdlp imports this module at
    module level — a top-level import here would be circular; a lazy import
    from get_conn would still run mid-import for the archive_db self-check.
    html.unescape once, then strip '>>' speaker-turn markers exactly like
    the ingest path so old rows converge to the same stored shape."""
    text = html.unescape(text)
    return re.sub(r"\s*>{2,}\s*", " ", text).strip()


def _migrate_transcript_data(conn: sqlite3.Connection) -> None:
    """Idempotent one-time data migration on transcripts (batch-3).

    Two steps, both naturally no-op on repeat runs:
    1. Entity backfill: legacy rows stored caption text VERBATIM from a
       pre-fix build — '&gt;&gt;' speaker markers, '&amp;' etc. Ingest now
       unescapes (_clean_caption_text) but never rewrote old rows. Only
       rows that still contain entities are touched; each UPDATE fires the
       transcripts_ai FTS trigger so search re-indexes the cleaned text.
    2. Lang backfill: rows with lang IS NULL get the owning video's channel
       language family (pt/en/es) — makes the channel-family search
       exclusion (_channel_lang_exclusion) effective for pre-lang builds
       where every row is untagged. Unknown channel languages stay NULL.
    """
    # (1) entity unescape + turn-marker strip.
    legacy = conn.execute(
        "SELECT id, text FROM transcripts "
        "WHERE text LIKE '%&amp;%' OR text LIKE '%&lt;%' OR text LIKE '%&gt;%'"
    ).fetchall()
    for r in legacy:
        clean = _clean_legacy_transcript_text(r["text"])
        if clean != r["text"]:
            conn.execute(
                "UPDATE transcripts SET text = ? WHERE id = ?", (clean, r["id"])
            )
    # (2) lang backfill from the video's channel language family. Only
    # known families (pt/en/es) are stamped — the exclusion only fires for
    # them; raw codes ('ja', ...) keep NULL so the tally never mistakes a
    # derived tag for independent evidence.
    # The UPDATE itself takes SQLite's write lock for its whole scan, so
    # it is gated behind a reader-only EXISTS probe: on a current corpus
    # (every lang already stamped) no write lock is ever taken — a fresh
    # backend process used to hold a multi-minute write lock on connect,
    # stalling the archive worker and every other writer on the DB.
    need_lang = conn.execute(
        """SELECT 1 FROM transcripts t WHERE t.lang IS NULL AND EXISTS (
               SELECT 1 FROM videos v
               WHERE v.platform = t.platform AND v.video_id = t.video_id
                 AND v.channel_language IS NOT NULL AND v.channel_language != ''
                 AND lower(substr(v.channel_language, 1,
                      instr(v.channel_language || '-', '-') - 1)) IN ('pt','en','es')
           ) LIMIT 1"""
    ).fetchone()
    if need_lang is not None:
        conn.execute(
            """UPDATE transcripts SET lang = (
                   SELECT lower(substr(v.channel_language, 1,
                          instr(v.channel_language || '-', '-') - 1))
                   FROM videos v
                   WHERE v.platform = transcripts.platform
                     AND v.video_id = transcripts.video_id
                     AND v.channel_language IS NOT NULL AND v.channel_language != ''
               )
               WHERE lang IS NULL AND EXISTS (
                   SELECT 1 FROM videos v2
                   WHERE v2.platform = transcripts.platform
                     AND v2.video_id = transcripts.video_id
                     AND lower(substr(v2.channel_language, 1,
                          instr(v2.channel_language || '-', '-') - 1))
                         IN ('pt','en','es')
               )"""
        )


def _ensure_spam_column(conn: sqlite3.Connection) -> None:
    """Idempotent migration: add messages.spam_count (collapsed-dup counter).

    insert_messages merges identical consecutive chat rows within 60 s into
    one stored row; spam_count counts how many messages that row represents
    (1 = a plain row, >1 = a collapsed spam burst). Additive only; PRAGMA
    table_info guard makes repeated calls no-ops."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    if "spam_count" not in cols:
        conn.execute(
            "ALTER TABLE messages ADD COLUMN spam_count INTEGER NOT NULL DEFAULT 1"
        )


def _ensure_message_color_column(conn: sqlite3.Connection) -> None:
    """Idempotent migration: add messages.color (platform chat username color).

    YouTube live-chat renderers carry authorNameTextColor; Twitch GQL VOD
    comments do not (clients fall back to a deterministic palette). The
    column is NULL for rows without a platform-provided color — the UI
    applies the per-platform palette hash then. Additive only; PRAGMA
    table_info guard makes repeated calls no-ops."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    if "color" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN color TEXT")


def _ensure_message_display_name_column(conn: sqlite3.Connection) -> None:
    """Idempotent migration: add messages.display_name (chat display name).

    YouTube live-chat payloads only carry the @handle (username); the name
    viewers actually see is resolved from the author's UC channel id and
    cached here. NULL for rows whose display name is unresolved (Twitch and
    Kick already store the displayed name in username, so their rows stay
    NULL and COALESCE(display_name, username) reads them correctly).
    Additive only; PRAGMA table_info guard makes repeated calls no-ops."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    if "display_name" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN display_name TEXT")


def _ensure_jobs_kind_events(conn: sqlite3.Connection) -> None:
    """Idempotent migration: widen archive_jobs.kind CHECK to include 'events'.

    SQLite cannot ALTER a CHECK constraint, so the table is rebuilt
    (rename -> create -> copy -> drop) only when the stored DDL lacks it.
    The rebuild also normalizes legacy kind='chat_backfill' rows to 'chat'
    (the single chat-job kind) so the pre-chat kind never re-appears."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='archive_jobs'"
    ).fetchone()
    if row and "'events'" in (row[0] or ""):
        return
    conn.execute("ALTER TABLE archive_jobs RENAME TO archive_jobs_old")
    conn.execute(
        """CREATE TABLE archive_jobs (
             id         TEXT PRIMARY KEY,
             kind       TEXT NOT NULL CHECK (kind IN ('ingest','chat','transcribe','events')),
             platform   TEXT NOT NULL,
             video_id   TEXT NOT NULL,
             status     TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued','running','done','failed')),
             progress   REAL NOT NULL DEFAULT 0,
             error      TEXT,
             created_at TEXT NOT NULL,
             updated_at TEXT NOT NULL,
             heartbeat  TEXT
           )"""
    )
    conn.execute(
        "INSERT INTO archive_jobs (id, kind, platform, video_id, status, progress, error, created_at, updated_at, heartbeat) "
        "SELECT id, CASE kind WHEN 'chat_backfill' THEN 'chat' ELSE kind END, "
        "platform, video_id, status, progress, error, created_at, updated_at, NULL AS heartbeat "
        "FROM archive_jobs_old"
    )
    conn.execute("DROP TABLE archive_jobs_old")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON archive_jobs(status, created_at)")


def _ensure_jobs_priority(conn: sqlite3.Connection) -> None:
    """Idempotent migration: add archive_jobs.priority (preview-queue priority).

    Same rebuild pattern as _ensure_jobs_kind_events (SQLite cannot ALTER a
    NOT NULL DEFAULT column): rename -> create -> copy -> drop. Legacy rows
    copy with priority=0; the rebuild DDL is the final shape (wider kind
    CHECK included), so a DB lacking both columns converges in two rebuilds
    and the (status, priority, created_at) index replaces the old one.
    The index is created HERE (after any rebuild) and NOT in SCHEMA: on a
    pre-priority archive_jobs, SCHEMA's unconditional CREATE INDEX would
    fail with 'no such column: priority' before this migration could run —
    that is exactly the DB shape the real %APPDATA% archive.db has."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(archive_jobs)")}
    if "priority" in cols:
        # Column already migrated (fresh DB or intermediate version) — the
        # index is no longer part of SCHEMA, so this migration owns it.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_status_priority ON archive_jobs(status, priority, created_at)"
        )
        return
    conn.execute("ALTER TABLE archive_jobs RENAME TO archive_jobs_old")
    conn.execute(
        """CREATE TABLE archive_jobs (
             id         TEXT PRIMARY KEY,
             kind       TEXT NOT NULL CHECK (kind IN ('ingest','chat','transcribe','events')),
             platform   TEXT NOT NULL,
             video_id   TEXT NOT NULL,
             status     TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued','running','done','failed')),
             progress   REAL NOT NULL DEFAULT 0,
             error      TEXT,
             priority   INTEGER NOT NULL DEFAULT 0,
             created_at TEXT NOT NULL,
             updated_at TEXT NOT NULL,
             heartbeat  TEXT
           )"""
    )
    conn.execute(
        "INSERT INTO archive_jobs (id, kind, platform, video_id, status, progress, error, priority, created_at, updated_at, heartbeat) "
        "SELECT id, CASE kind WHEN 'chat_backfill' THEN 'chat' ELSE kind END, "
        "platform, video_id, status, progress, error, 0, created_at, updated_at, NULL AS heartbeat "
        "FROM archive_jobs_old"
    )
    conn.execute("DROP TABLE archive_jobs_old")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_status_priority ON archive_jobs(status, priority, created_at)"
    )


def _ensure_jobs_kind_chat(conn: sqlite3.Connection) -> None:
    """Idempotent migration: switch chat jobs to the single 'chat' kind.

    The pre-background-worker builds tracked chat backfills as
    'chat_backfill'; the queue is now drained by the archive worker and
    chat fetches use kind 'chat'. SQLite cannot ALTER a CHECK constraint,
    so the table is rebuilt when the stored DDL lacks 'chat'. Legacy
    'chat_backfill' rows (queued/running/done/failed) become 'chat' so
    pending backfills are picked up by the worker instead of sitting
    orphaned. Runs AFTER _ensure_jobs_priority, so the rebuild DDL is the
    final shape (priority column included)."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='archive_jobs'"
    ).fetchone()
    if row and "'chat'" in (row[0] or ""):
        return
    conn.execute("ALTER TABLE archive_jobs RENAME TO archive_jobs_old")
    conn.execute(
        """CREATE TABLE archive_jobs (
             id         TEXT PRIMARY KEY,
             kind       TEXT NOT NULL CHECK (kind IN ('ingest','chat','transcribe','events')),
             platform   TEXT NOT NULL,
             video_id   TEXT NOT NULL,
             status     TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued','running','done','failed')),
             progress   REAL NOT NULL DEFAULT 0,
             error      TEXT,
             priority   INTEGER NOT NULL DEFAULT 0,
             created_at TEXT NOT NULL,
             updated_at TEXT NOT NULL,
             heartbeat  TEXT
           )"""
    )
    conn.execute(
        "INSERT INTO archive_jobs (id, kind, platform, video_id, status, progress, error, priority, created_at, updated_at, heartbeat) "
        "SELECT id, CASE kind WHEN 'chat_backfill' THEN 'chat' ELSE kind END, "
        "platform, video_id, status, progress, error, 0, created_at, updated_at, NULL AS heartbeat "
        "FROM archive_jobs_old"
    )
    conn.execute("DROP TABLE archive_jobs_old")
    conn.execute(
        "UPDATE archive_jobs SET kind = 'chat' WHERE kind = 'chat_backfill'"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_status_priority ON archive_jobs(status, priority, created_at)"
    )


def _ensure_jobs_heartbeat_column(conn: sqlite3.Connection) -> None:
    """Idempotent migration: add archive_jobs.heartbeat (job-liveness touch).

    The archive worker stamps heartbeat (via update_job) as a job makes
    progress; _claim_next_job reclaims a 'running' chat job whose heartbeat
    went stale instead of waiting out the flat 2h window. NULL for rows
    that never heartbeated (pre-heartbeat builds, YouTube chat downloads
    that touch the row only at start/end) — the reclaim falls back to
    updated_at. Additive only; PRAGMA table_info guard makes repeated calls
    no-ops."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(archive_jobs)")}
    if "heartbeat" not in cols:
        conn.execute("ALTER TABLE archive_jobs ADD COLUMN heartbeat TEXT")


# (fts_table, content_table) pairs kept in sync by FTS triggers.
_FTS_TABLES = (
    ("messages_fts", "messages"),
    ("transcripts_fts", "transcripts"),
)

# One trigger triple per FTS table. Contentless/external-content indexes
# cannot be updated in place: the AFTER INSERT/UPDATE/DELETE triggers on the
# content tables own every index entry. The 'delete' command needs the old
# row's text, which is why each trigger supplies it explicitly.
_FTS_TRIGGERS = (
    """CREATE TRIGGER IF NOT EXISTS {fts}_ai AFTER INSERT ON {content} BEGIN
  INSERT INTO {fts}(rowid, text) VALUES (new.id, new.text);
END""",
    """CREATE TRIGGER IF NOT EXISTS {fts}_ad AFTER DELETE ON {content} BEGIN
  INSERT INTO {fts}({fts}, rowid, text) VALUES('delete', old.id, old.text);
END""",
    """CREATE TRIGGER IF NOT EXISTS {fts}_au AFTER UPDATE ON {content} BEGIN
  INSERT INTO {fts}({fts}, rowid, text) VALUES('delete', old.id, old.text);
  INSERT INTO {fts}(rowid, text) VALUES (new.id, new.text);
END""",
)


def _migrate_fts_contentless(conn: sqlite3.Connection) -> bool:
    """Idempotent migration: convert FTS5 indexes to external-content mode.

    Legacy DBs have `USING fts5(text)` — the index duplicated every
    message/transcript text. This rebuilds each index as
    `USING fts5(text, content='<table>', content_rowid='id')` (tokens only;
    text lives once in the content table) and installs the triggers that own
    the index from then on. Detects legacy tables by the absence of the
    content= option in their sqlite_master SQL; PRAGMA table_xinfo cannot
    tell the modes apart. Runs inside one transaction; returns True when a
    rebuild happened so the caller can VACUUM the freed pages."""
    changed = False
    with conn:
        for fts, content in _FTS_TABLES:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (fts,)
            ).fetchone()
            if row is None or "content=" in (row[0] or ""):
                continue
            # Create the new index beside the legacy one, copy the tokens,
            # then swap names (FTS5 shadow tables follow the rename).
            conn.execute(
                f"CREATE VIRTUAL TABLE {fts}_new USING fts5("
                f"text, content='{content}', content_rowid='id')"
            )
            conn.execute(
                f"INSERT INTO {fts}_new(rowid, text) SELECT id, text FROM {content}"
            )
            conn.execute(f"DROP TABLE {fts}")
            conn.execute(f"ALTER TABLE {fts}_new RENAME TO {fts}")
            changed = True
        for fts, content in _FTS_TABLES:
            # Also covers fresh DBs (SCHEMA creates the external-content
            # tables; triggers are installed here).
            for tpl in _FTS_TRIGGERS:
                conn.execute(tpl.format(fts=fts, content=content))
    return changed


# ponytail: single global lock + connection; upgrade path is per-thread
# connections (threading.local) or aiosqlite if the app ever goes async.
def _bind(params: Any) -> Any:
    # Named :placeholders need the dict itself — tuple() on a dict would bind
    # its KEYS as positional values and silently corrupt every row.
    return params if isinstance(params, dict) else tuple(params)


def execute(sql: str, params: Any = ()) -> sqlite3.Cursor:
    with _lock:
        cur = get_conn().execute(sql, _bind(params))
        get_conn().commit()
        return cur


def query(sql: str, params: Any = ()) -> list[sqlite3.Row]:
    with _lock:
        return get_conn().execute(sql, _bind(params)).fetchall()


# --- videos ---------------------------------------------------------------

def _normalize_kind(value: Any) -> str:
    k = str(value or "vod").strip().lower()
    return k if k in KINDS else "vod"


def upsert_video(video: dict) -> None:
    now = _now_iso()
    row = {
        "platform": video["platform"],
        "video_id": video["video_id"],
        "channel": video.get("channel", ""),
        "title": video.get("title", ""),
        "kind": _normalize_kind(video.get("kind")),
        "started_at": video.get("started_at"),
        "ended_at": video.get("ended_at"),
        "duration_sec": video.get("duration_sec"),
        "archive_path": video.get("archive_path"),
        "canonical_key": video.get("canonical_key"),
        "content_sha256": video.get("content_sha256"),
        "original_title": video.get("original_title"),
        "original_language": video.get("original_language"),
        "status": video.get("status", "known"),
        "updated_at": now,
    }
    execute(
        """INSERT INTO videos (platform, video_id, channel, title, started_at,
           ended_at, duration_sec, archive_path, canonical_key, content_sha256,
           original_title, original_language, status, kind, created_at, updated_at)
           VALUES (:platform, :video_id, :channel, :title, :started_at,
           :ended_at, :duration_sec, :archive_path, :canonical_key,
           :content_sha256, :original_title, :original_language, :status, :kind,
           :created_at, :updated_at)
           ON CONFLICT(platform, video_id) DO UPDATE SET
             channel=excluded.channel, title=excluded.title,
             started_at=excluded.started_at, ended_at=excluded.ended_at,
             duration_sec=excluded.duration_sec,
             archive_path=excluded.archive_path,
             canonical_key=excluded.canonical_key,
             -- Derived, ingest-owned state: an absent dict key must never
             -- NULL out a stored hash (metadata refreshes, re-ingests).
             content_sha256=COALESCE(excluded.content_sha256, videos.content_sha256),
             -- WS-4: same preserve rule — a channel-walk upsert that does
             -- not know the original title must not clobber a backfilled one.
             original_title=COALESCE(excluded.original_title, videos.original_title),
             original_language=COALESCE(excluded.original_language, videos.original_language),
             status=excluded.status, kind=excluded.kind,
             updated_at=excluded.updated_at""",
        {**row, "created_at": now},
    )


def upsert_channel_video(video: dict) -> None:
    """Upsert a channel-list metadata row WITHOUT touching archive fields.

    The channel view accumulates every VOD it has ever seen for a channel
    (perma, never pruned). On conflict only list metadata is updated —
    archive_path, canonical_key, status and ended_at belong to the archive
    pipeline and must survive intact (a download in flight is never
    clobbered by a list refresh)."""
    now = _now_iso()
    row = {
        "platform": video["platform"],
        "video_id": video["video_id"],
        "channel": video.get("channel", ""),
        "title": video.get("title", ""),
        "kind": _normalize_kind(video.get("kind")),
        "started_at": video.get("started_at"),
        "duration_sec": video.get("duration_sec"),
        "duration_string": video.get("duration_string"),
        "views": video.get("views"),
        "thumbnail_url": video.get("thumbnail_url"),
        "original_title": video.get("original_title"),
        "original_language": video.get("original_language"),
        "updated_at": now,
    }
    execute(
        """INSERT INTO videos (platform, video_id, channel, title, started_at,
           duration_sec, duration_string, views, thumbnail_url, kind,
           original_title, original_language, created_at, updated_at)
           VALUES (:platform, :video_id, :channel, :title, :started_at,
           :duration_sec, :duration_string, :views, :thumbnail_url, :kind,
           :original_title, :original_language, :created_at, :updated_at)
           ON CONFLICT(platform, video_id) DO UPDATE SET
             channel=excluded.channel, title=excluded.title,
             started_at=excluded.started_at,
             duration_sec=excluded.duration_sec,
             duration_string=excluded.duration_string,
             views=excluded.views, thumbnail_url=excluded.thumbnail_url,
             kind=excluded.kind,
             -- WS-4: an absent original key (plain list refresh) must never
             -- clobber a backfilled original title/language.
             original_title=COALESCE(excluded.original_title, videos.original_title),
             original_language=COALESCE(excluded.original_language, videos.original_language),
             updated_at=excluded.updated_at""",
        {**row, "created_at": now},
    )


def touch_channel_snapshot(platform: str, channel_key: str) -> None:
    """Record that a platform fetch for a channel just succeeded."""
    execute(
        """INSERT INTO channel_snapshots (platform, channel_key, fetched_at)
           VALUES (?, ?, ?)
           ON CONFLICT(platform, channel_key) DO UPDATE SET
             fetched_at=excluded.fetched_at""",
        (platform, channel_key, _now_iso()),
    )


def channel_snapshot_age_sec(platform: str, channel_key: str) -> Optional[float]:
    """Seconds since the last successful fetch, or None when never fetched."""
    row = query(
        "SELECT fetched_at FROM channel_snapshots WHERE platform = ? AND channel_key = ?",
        (platform, channel_key),
    )
    if not row:
        return None
    try:
        from datetime import datetime, timezone

        fetched = datetime.fromisoformat(row[0]["fetched_at"])
    except (ValueError, TypeError):
        return None
    age = (datetime.now(timezone.utc) - fetched).total_seconds()
    return max(0.0, age)


# --- scheduler top-priority windows --------------------------------------

# Default window a channel stays top-priority after being added or viewed.
# 30 min at the scheduler's 180 s pass cadence = ~10 passes — enough for a
# fresh channel's first ingests without starving the older backlog forever.
PRIORITY_WINDOW_S = 1800.0


def mark_channel_priority(
    platform: str, channel_key: str, *, window_s: float = PRIORITY_WINDOW_S
) -> None:
    """Top-priority this channel for the archive scheduler until now+window_s.

    The key is lowercased to match the scheduler's lower(channel) joins.
    Upsert semantics: repeat marks (repeated page views) extend the window."""
    key = (channel_key or "").strip().lower()
    if not key or platform not in PLATFORMS:
        return
    from datetime import datetime, timedelta, timezone

    until = (datetime.now(timezone.utc) + timedelta(seconds=window_s)).isoformat(
        timespec="seconds"
    )
    execute(
        """INSERT INTO channel_priorities (platform, channel_key, priority_until)
           VALUES (?, ?, ?)
           ON CONFLICT(platform, channel_key) DO UPDATE SET
             priority_until=excluded.priority_until""",
        (platform, key, until),
    )


def priority_channel_keys() -> set[tuple[str, str]]:
    """(platform, channel_key) pairs still inside their priority window."""
    now = _now_iso()
    rows = query(
        "SELECT platform, channel_key FROM channel_priorities WHERE priority_until > ?",
        (now,),
    )
    return {(r["platform"], r["channel_key"]) for r in rows}


def expire_channel_priorities() -> int:
    """Drop expired priority rows (lazy housekeeping); returns count removed."""
    now = _now_iso()
    cur = execute("DELETE FROM channel_priorities WHERE priority_until <= ?", (now,))
    return cur.rowcount


def list_videos(platform: Optional[str] = None, channel: Optional[str] = None) -> list[dict]:
    sql = "SELECT * FROM videos WHERE 1=1"
    params: list[Any] = []
    if platform:
        sql += " AND platform = ?"
        params.append(platform)
    if channel:
        sql += " AND channel = ?"
        params.append(channel)
    sql += " ORDER BY started_at DESC"
    return [dict(r) for r in query(sql, params)]


# --- channel language (WS-3) ---------------------------------------------

def set_channel_language(platform: str, channel: str, language: Optional[str]) -> None:
    """Persist the per-channel language on every video row of the channel.

    This is the single owner of the detected channel language — the API/UI
    read it from videos.channel_language. Called by the platform-clue path
    at fetch time and by the transcript aggregation (channel_language.py)."""
    execute(
        "UPDATE videos SET channel_language = ?, updated_at = updated_at "
        "WHERE platform = ? AND lower(channel) = lower(?)",
        (language, platform, channel),
    )


def video_channel(platform: str, video_id: str) -> Optional[str]:
    """Channel slug of an archived video (None when the row is absent)."""
    row = query(
        "SELECT channel FROM videos WHERE platform = ? AND video_id = ?",
        (platform, video_id),
    )
    return row[0]["channel"] if row else None


def video_channel_language(platform: str, video_id: str) -> Optional[str]:
    """Stored channel_language of the video's channel (None = unknown)."""
    row = query(
        "SELECT channel_language FROM videos WHERE platform = ? AND video_id = ?",
        (platform, video_id),
    )
    return row[0]["channel_language"] if row and row[0]["channel_language"] else None


def channel_language_tally(platform: str, channel: str) -> list[dict]:
    """Transcript-language evidence per channel: [{language, segments, videos}].

    Whisper/YT-caption rows carry a per-segment lang tag (transcripts.lang);
    the tally over all of a channel's transcribed sections is the empirical
    language distribution the aggregation heuristic uses."""
    return [
        dict(r)
        for r in query(
            """SELECT t.lang AS language, COUNT(*) AS segments,
                      COUNT(DISTINCT t.video_id) AS videos
               FROM transcripts t
               JOIN videos v ON v.platform = t.platform AND v.video_id = t.video_id
               WHERE v.platform = ? AND lower(v.channel) = lower(?)
                 AND t.lang IS NOT NULL AND t.lang != ''
               GROUP BY t.lang
               ORDER BY segments DESC""",
            (platform, channel),
        )
    ]


def channel_video_languages(platform: str, channel: str) -> list[dict]:
    """Platform-clue evidence: distinct stored channel_language + row counts.

    The clue fetch stamps videos.channel_language at channel-list refresh
    time; the aggregation reads it back here to weigh clue vs tally."""
    return [
        dict(r)
        for r in query(
            """SELECT channel_language AS language, COUNT(*) AS videos
               FROM videos
               WHERE platform = ? AND lower(channel) = lower(?)
                 AND channel_language IS NOT NULL AND channel_language != ''
               GROUP BY channel_language
               ORDER BY videos DESC""",
            (platform, channel),
        )
    ]


def channel_original_languages(platform: str, channel: str) -> Optional[list[dict]]:
    """WS-4 clue: videos.original_language consensus, read DEFENSIVELY.

    WS-4 (original titles) adds videos.original_language in parallel — the
    column may not exist on this build, so the read is guarded by a PRAGMA
    table_info check instead of crashing on sqlite3.OperationalError.
    Returns None when the column is absent (no evidence)."""
    cols = {row[1] for row in query("PRAGMA table_info(videos)")}
    if "original_language" not in cols:
        return None
    return [
        dict(r)
        for r in query(
            """SELECT original_language AS language, COUNT(*) AS videos
               FROM videos
               WHERE platform = ? AND lower(channel) = lower(?)
                 AND original_language IS NOT NULL AND original_language != ''
               GROUP BY original_language
               ORDER BY videos DESC""",
            (platform, channel),
        )
    ]


def videos_missing_original_title(platform: str, channel: str, limit: int) -> list[dict]:
    """YouTube rows for a channel still lacking original_title (WS-4 backfill).

    Newest first; the caller (archive_ytdlp.backfill_original_titles) applies
    its own throttle + failure cooldown on top."""
    rows = query(
        """SELECT video_id, title FROM videos
           WHERE platform = ? AND lower(channel) = lower(?) AND (original_title IS NULL OR original_title = '')
           ORDER BY started_at DESC LIMIT ?""",
        (platform, channel, max(1, int(limit))),
    )
    return [dict(r) for r in rows]


def set_original_title(
    platform: str,
    video_id: str,
    original_title: Optional[str],
    original_language: Optional[str],
) -> None:
    """Store the WS-4 original title/language for one video.

    COALESCE keeps existing values, so a caller that only knows one of the
    two fields (e.g. the language without a trustworthy title) never wipes
    the other. Stored `title` is deliberately NOT touched — display paths
    prefer original_title, the archive keeps the walk-time copy."""
    execute(
        """UPDATE videos
           SET original_title = COALESCE(?, original_title),
               original_language = COALESCE(?, original_language),
               updated_at = ?
           WHERE platform = ? AND video_id = ?""",
        (original_title, original_language, _now_iso(), platform, video_id),
    )

# --- messages -------------------------------------------------------------

# Cross-writer dedupe window: multiple capture paths write the same live
# message at slightly different offsets (watchdog live sink anchored to the
# stream-start epoch vs Twitch GQL video-relative backfill vs YouTube replay
# ingest), so an identical (username, text) row can arrive from a second
# writer a couple of seconds apart. Rows inside this window are skipped.
_CROSS_FLUSH_DEDUPE_WIN_S = 2.0

# Bounded-commit size for insert_messages: caps how long one SQLite write
# transaction can hold the DB busy. A 100k-row backfill batch would
# otherwise lock out concurrent readers/checkpoints for the whole insert.
_MESSAGES_COMMIT_CHUNK = 5000


def insert_messages(platform: str, video_id: str, rows: Iterable[dict]) -> int:
    """Batch insert chat rows; each row: offset_sec, user_id, username, text,
    badges (list), emotes (list), ts (optional ISO).

    Spam collapse: consecutive rows with IDENTICAL username+text whose offset
    delta is within 60 s merge into a single stored row; the stored row's
    spam_count counts the merged messages (chat spam floods one row instead
    of a thousand). Collapse runs within the batch AND across flushes: the
    batch's first run merges into the LAST stored row for this video when it
    matches (chat_sinks flush every 5 s / 100 rows, so a burst spans flushes).

    Cross-writer dedupe: every remaining run is skipped when an identical
    (username, text) row already exists within +/-_CROSS_FLUSH_DEDUPE_WIN_S
    (the (platform, video_id, offset_sec) index bounds the probe to the few
    rows in the window). Multiple writers clock the same message differently,
    so without this gate each writer appends its own copy of every message.

    Returns the ACCEPTED count — every row that arrived, collapsed or not
    (chat_sinks/base.py rows_flushed and the ingest API 'inserted' field
    build on this). Idempotent: a re-sent row whose offset is <= the stored
    row's (delta 0) is consumed without bumping spam_count, so replaying a
    flush never double-counts.

    Commits in _MESSAGES_COMMIT_CHUNK-sized chunks (not one giant
    transaction) so a 100k-row batch never holds the SQLite write lock for
    the whole insert; a mid-batch failure leaves the earlier chunks
    committed and rolls back only the current one. Backfill pages and live
    sink flushes are far below the chunk size, so their per-call atomicity
    is unchanged.

    ponytail: only the batch's FIRST run merges cross-flush (per contract,
    the last stored row). A burst whose text differs from the last row but
    matches an earlier one starts a new row; upgrade path is merging against
    the last row per username+text, or per-message ids."""
    batch = sorted(
        (dict(r) for r in rows),
        key=lambda r: float(r["offset_sec"]),
    )
    if not batch:
        return 0

    # Collapse within the batch first: each run of identical username+text
    # with 0 < offset delta <= 60 s becomes ONE anchor row carrying the run's
    # count. A delta <= 0 duplicate (same offset re-send) is consumed without
    # bumping, keeping replays idempotent.
    runs: list[tuple[dict, int]] = []
    anchor: Optional[dict] = None
    count = 0
    for r in batch:
        if anchor is None:
            anchor, count = r, 1
            continue
        same = anchor["username"] == r.get("username", "") and anchor["text"] == r["text"]
        delta = float(r["offset_sec"]) - float(anchor["offset_sec"])
        if same and 0 < delta <= 60.0:
            count += 1  # merge into the anchor
        elif same and delta <= 0:
            pass  # re-sent duplicate — consumed, no bump
        else:
            runs.append((anchor, count))
            anchor, count = r, 1
    runs.append((anchor, count))

    conn = get_conn()
    accepted = len(batch)
    with _lock:
        with conn:  # transaction
            first_anchor, first_count = runs[0]
            stored = conn.execute(
                """SELECT id, offset_sec, username, text, spam_count
                   FROM messages WHERE platform = ? AND video_id = ?
                   ORDER BY offset_sec DESC, id DESC LIMIT 1""",
                (platform, video_id),
            ).fetchone()
            if stored is not None:
                same = (
                    stored["username"] == first_anchor.get("username", "")
                    and stored["text"] == first_anchor["text"]
                )
                delta = float(first_anchor["offset_sec"]) - float(stored["offset_sec"])
                if same and 0 < delta <= 60.0:
                    # Continuation of the previous flush's burst: bump the
                    # stored row and re-anchor it at the newest offset (a
                    # re-sent duplicate then lands at delta 0 and is consumed).
                    # COALESCE keeps a stored platform color when the new
                    # anchor carries none.
                    conn.execute(
                        "UPDATE messages SET spam_count = ?, offset_sec = ?, "
                        "color = COALESCE(?, color) WHERE id = ?",
                        (int(stored["spam_count"]) + first_count,
                         first_anchor["offset_sec"], first_anchor.get("color"),
                         stored["id"]),
                    )
                    runs = runs[1:]
                elif same and delta <= 0:
                    runs = runs[1:]  # re-send of the stored row — already counted

            # Cross-writer dedupe: after the first-run merge above, drop any
            # remaining anchor whose identical (username, text) row is already
            # stored within +/-_CROSS_FLUSH_DEDUPE_WIN_S. The index bounds the
            # probe to the few rows in the window; skipped rows are consumed
            # (accepted still counts them) without touching spam_count — a
            # parallel writer's copy is not spam continuation.
            kept: list[tuple[dict, int]] = []
            for anchor, count in runs:
                offset = float(anchor["offset_sec"])
                hit = conn.execute(
                    """SELECT 1 FROM messages
                       WHERE platform = ? AND video_id = ?
                         AND username = ? AND text = ?
                         AND offset_sec BETWEEN ? AND ?
                       LIMIT 1""",
                    (platform, video_id, anchor.get("username", ""), anchor["text"],
                     offset - _CROSS_FLUSH_DEDUPE_WIN_S,
                     offset + _CROSS_FLUSH_DEDUPE_WIN_S),
                ).fetchone()
                if hit is None:
                    kept.append((anchor, count))
            runs = kept

            # Chunked commits: a 100k-row batch must not hold the SQLite
            # write transaction for the whole insert. The cross-flush merge
            # and dedupe probes ran above, before any insert, so chunk
            # boundaries cannot change their outcome. On failure mid-batch
            # only the CURRENT chunk rolls back (earlier chunks stay
            # committed) — callers are idempotent re-runs (backfill seeds
            # from MAX(offset_sec), sinks re-send rows the dedupe window
            # absorbs), so partial batches are safe.
            for i, (anchor, count) in enumerate(runs, 1):
                conn.execute(
                    """INSERT INTO messages (platform, video_id, offset_sec,
                       user_id, username, display_name, text, badges, emotes, ts, color, spam_count)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        platform,
                        video_id,
                        float(anchor["offset_sec"]),
                        anchor.get("user_id"),
                        anchor.get("username", ""),
                        anchor.get("display_name"),
                        anchor["text"],
                        json.dumps(anchor.get("badges", []), ensure_ascii=False),
                        json.dumps(anchor.get("emotes", []), ensure_ascii=False),
                        anchor.get("ts"),
                        anchor.get("color"),
                        count,
                    ),
                )
                # FTS index entry is written by the messages_ai trigger.
                if i % _MESSAGES_COMMIT_CHUNK == 0:
                    conn.commit()
    return accepted


def dedupe_messages() -> int:
    """Delete exact-duplicate chat rows, keeping the MIN rowid per
    (platform, video_id, offset_sec, username, text); returns the deleted
    count.

    Pre-fix builds wrote the same message more than once when multiple
    capture paths landed the IDENTICAL (offset_sec, username, text) row
    (watchdog live sink vs GQL backfill vs YouTube replay ingest, and the
    yt_live tail's post-rename full re-send). The key is exact — no window —
    so two genuinely distinct messages never merge, and the operation is
    idempotent: a second run finds nothing and deletes 0 rows.

    Bounded: one grouped pass over messages (cheap at the real ~215k-row
    scale; FTS entries cascade via the AFTER DELETE triggers). Called once
    per boot from the app lifespan."""
    conn = get_conn()
    with _lock:
        with conn:  # transaction
            cur = conn.execute(
                """DELETE FROM messages WHERE id NOT IN (
                     SELECT MIN(id) FROM messages
                     GROUP BY platform, video_id, offset_sec, username, text
                   )"""
            )
            deleted = cur.rowcount
    if deleted:
        # Bulk delete leaves the FTS index fragmented — merge it like the
        # post-backfill path does so searches stay fast.
        optimize_fts()
    return deleted


CHAT_WINDOW_HALF_LIMIT = 200
CHAT_FROM_OFFSET_LIMIT = 5000


def chat_window(
    platform: str,
    video_id: str,
    offset_sec: float,
    half: float = 30.0,
    limit: Optional[int] = None,
) -> tuple[list[dict], bool]:
    """Chat rows for a video, time-ordered. Returns (rows, truncated).

    half > 0 → the classic ±half window around offset_sec (BETWEEN, capped at
    CHAT_WINDOW_HALF_LIMIT rows; truncated always False). half <= 0 → "from
    offset onward": every row with offset_sec >= offset_sec, capped at
    `limit` rows (default CHAT_FROM_OFFSET_LIMIT) — the popup's
    whole-history-from-hit view. The +1 probe distinguishes "hit the cap
    exactly" from "tail cut off".

    A truncated tail is paginated by re-calling with offset_sec = the last
    delivered row's offset_sec: the inclusive >= boundary re-returns rows
    sharing that offset (so a page cut inside a same-offset run never skips
    the run's tail), and the client dedupes by row identity. ponytail: if
    archive data ever produces a run of identical offset_sec longer than the
    page cap, this would stall (the page never advances past the run) —
    upgrade path: keyset pagination on (offset_sec, id)."""
    if half is not None and half > 0:
        rows = query(
            """SELECT * FROM messages
               WHERE platform = ? AND video_id = ?
                 AND offset_sec BETWEEN ? AND ?
               ORDER BY offset_sec LIMIT ?""",
            (platform, video_id, offset_sec - half, offset_sec + half, CHAT_WINDOW_HALF_LIMIT),
        )
        return [dict(r) for r in rows], False
    cap = CHAT_FROM_OFFSET_LIMIT if limit is None else max(1, int(limit))
    rows = query(
        """SELECT * FROM messages
           WHERE platform = ? AND video_id = ? AND offset_sec >= ?
           ORDER BY offset_sec LIMIT ?""",
        (platform, video_id, offset_sec, cap + 1),
    )
    truncated = len(rows) > cap
    return [dict(r) for r in rows[:cap]], truncated


def chat_group_members(platform: str, video_id: str) -> list[dict]:
    """Every (platform, video_id) member of the video's canonical dedupe
    group — the set of platforms where the same live/VOD exists (video_aliases
    overrides included), requested video first, then the rest in dedupe-view
    order (platform name). Videos with no canonical key (orphan rows) return
    just the requested video, so single-platform behavior is unchanged."""
    rows = query(
        """SELECT v.platform, v.video_id,
                  COALESCE(a.canonical_key, v.canonical_key) AS key
           FROM videos v
           LEFT JOIN video_aliases a USING (platform, video_id)
           WHERE v.platform = ? AND v.video_id = ?""",
        (platform, video_id),
    )
    if not rows or not rows[0]["key"]:
        return [{"platform": platform, "video_id": video_id}]
    key = rows[0]["key"]
    members = query(
        """SELECT v.platform, v.video_id
           FROM videos v
           LEFT JOIN video_aliases a USING (platform, video_id)
           WHERE COALESCE(a.canonical_key, v.canonical_key) = ?
           ORDER BY v.platform""",
        (key,),
    )
    out = [dict(r) for r in members]
    out.sort(key=lambda m: (m["platform"] != platform, m["platform"]))
    return out


# --- preview chat panel (WS-2) --------------------------------------------

def has_transcript(platform: str, video_id: str) -> bool:
    """True when the video has at least one transcript row (cheap EXISTS)."""
    return bool(
        query(
            "SELECT 1 FROM transcripts WHERE platform = ? AND video_id = ? LIMIT 1",
            (platform, video_id),
        )
    )


def youtube_chat_user_ids_without_display_name(limit: int = 20) -> list[str]:
    """Distinct YouTube chat author channel ids whose display name is unresolved.

    YouTube live-chat payloads carry the @handle (username) and the author's
    UC channel id, but NOT the displayed name — the resolver fetches it from
    the channel page and caches it in messages.display_name. Rows whose
    display name is already resolved (or that have no channel id) never
    come back, so repeated runs stay cheap."""
    rows = query(
        """SELECT DISTINCT user_id FROM messages
           WHERE platform = 'youtube'
             AND user_id IS NOT NULL AND user_id != ''
             AND user_id LIKE 'UC%'
             AND display_name IS NULL
           LIMIT ?""",
        (int(limit),),
    )
    return [r["user_id"] for r in rows]


def set_message_display_name(platform: str, user_id: str, display_name: str) -> int:
    """Cache one author's display name on every stored row (messages table).

    The user filter matches COALESCE(display_name, username), so a resolved
    name takes effect immediately for all videos the author appears in."""
    with _lock:
        with get_conn() as conn:
            cur = conn.execute(
                "UPDATE messages SET display_name = ? WHERE platform = ? AND user_id = ?",
                (display_name, platform, user_id),
            )
            return cur.rowcount


def has_chat(platform: str, video_id: str) -> bool:
    """True when the video has at least one chat row (cheap EXISTS)."""
    return bool(
        query(
            "SELECT 1 FROM messages WHERE platform = ? AND video_id = ? LIMIT 1",
            (platform, video_id),
        )
    )


# YouTube auto-caption tracks emit the same cue text twice (same words at
# ~+10 ms and again ~+3.3 s — overlapping ASR cues) and the pre-fix ingest
# stored them row-for-row; the display reads collapse such repeats. A legit
# repeat of the same phrase further apart is real re-spoken content and kept.
TRANSCRIPT_DUPE_MIN_GAP_SEC = 1.0


def _collapse_transcript_dupes(hits: list[dict]) -> list[dict]:
    """Drop transcript hits that repeat the same moment of the same video:
    identical (offset, text) rows (duplicate caption rows in the archive —
    re-fetched VTTs re-inserted instead of upserting), one caption that is
    a substring of another at the same offset (whisper split artifacts —
    the longer caption survives), or identical text < 1s later (YouTube
    caption overlap, same rule as _dedupe_transcript_rows).

    The search merge only dedupes by per-video cap, so duplicate caption
    rows used to eat cap slots and show the same sentence twice in a row.
    Preserves the input order of the survivors."""
    by_video: dict[tuple[str, str], list[dict]] = {}
    for h in hits:
        if h.get("hit_kind") == "transcript" or h.get("kind") == "transcript":
            by_video.setdefault((h["platform"], h["video_id"]), []).append(h)
    if not by_video:
        return hits
    dropped: set[int] = set()
    for video_hits in by_video.values():
        video_hits.sort(key=lambda h: float(h.get("offset_sec") or 0.0))
        kept: list[dict] = []
        for h in video_hits:
            text = str(h.get("text") or "")
            off = float(h.get("offset_sec") or 0.0)
            if (
                kept
                and text
                and text == str(kept[-1].get("text") or "")
                and off - float(kept[-1].get("offset_sec") or 0.0)
                < TRANSCRIPT_DUPE_MIN_GAP_SEC
            ):
                dropped.add(id(h))
                continue
            # Same-moment (≤50ms) caption pair where one text is a
            # substring of the other: whisper emitted the same sentence
            # twice, once truncated. Keep the longer caption, whichever
            # row arrives first.
            replaced_idx: Optional[int] = None
            for k_idx, k in enumerate(kept):
                if abs(float(k.get("offset_sec") or 0.0) - off) < 0.05:
                    kt = str(k.get("text") or "")
                    if text == kt or (text and kt and (text in kt or kt in text)):
                        replaced_idx = -1 if len(text) <= len(kt) else k_idx
                        break
            if replaced_idx == -1:
                dropped.add(id(h))
                continue
            if replaced_idx is not None:
                dropped.add(id(kept[replaced_idx]))
                kept[replaced_idx] = h  # in-place: keeps the time order
                continue
            kept.append(h)
    if not dropped:
        return hits
    return [h for h in hits if id(h) not in dropped]


def _dedupe_transcript_rows(rows: list[dict]) -> list[dict]:
    """Drop a row whose text equals the previous KEPT row's text AND starts
    < TRANSCRIPT_DUPE_MIN_GAP_SEC later (YouTube auto-caption overlap).

    Single order-preserving pass over a time-ordered window; comparing to
    the previous kept row collapses whole duplicate chains (0.0, 0.01, 0.02)
    down to the first row.
    ponytail: dedupe lives at read, not ingest — a UNIQUE(platform,
    video_id, seg_idx) is structural but overlapping CUE text is a source
    artifact; a real fix is deduping in _parse_vtt before insert."""
    out: list[dict] = []
    prev_text: Optional[str] = None
    prev_start: Optional[float] = None
    for r in rows:
        start = float(r.get("start_sec") if r.get("start_sec") is not None else r.get("offset_sec") or 0.0)
        text = r.get("text") or ""
        if (
            prev_text is not None
            and text == prev_text
            and prev_start is not None
            and start - prev_start < TRANSCRIPT_DUPE_MIN_GAP_SEC
        ):
            continue
        out.append(r)
        prev_text = text
        prev_start = start
    return out


def transcript_offsets(platform: str, video_id: str, limit: int = 200_000) -> list[dict]:
    """Transcript rows as preview-panel payload rows, time-ordered by start_sec.

    Same transcripts table the search/transcript_for paths read; the panel
    payload only needs (offset_sec, text) per row, so the heavy word/lang
    columns are not selected. When the video has no rows of its own, the
    canonical twin's rows (youtube > twitch > kick) are served instead, so
    a Twitch VOD with a transcribed YouTube mirror shows its transcript."""
    rows = query(
        "SELECT start_sec AS offset_sec, text FROM transcripts "
        "WHERE platform = ? AND video_id = ? ORDER BY start_sec LIMIT ?",
        (platform, video_id, limit),
    )
    out = [dict(r) for r in rows]
    if not out:
        src = transcript_source(platform, video_id)
        if src is not None and src != (platform, video_id):
            return transcript_offsets(src[0], src[1], limit)
        return []
    return _dedupe_transcript_rows(out)


def chat_for(platform: str, video_id: str, limit: int = 200_000) -> list[dict]:
    """All chat rows for a video as preview-panel payload rows, time-ordered.

    Thin projection of the same messages table chat_window/insert_messages
    use; explicit ORDER BY offset_sec because live-capture inserts can land
    out of order. The (platform, video_id, offset_sec) index serves it.
    platform/video_id stay on every row so group-aware consumers (multi-
    platform canonical VODs) can attribute merged rows."""
    rows = query(
        "SELECT platform, video_id, offset_sec, text, username, spam_count, color "
        "FROM messages WHERE platform = ? AND video_id = ? ORDER BY offset_sec LIMIT ?",
        (platform, video_id, limit),
    )
    return [dict(r) for r in rows]


def count_messages(platform: str, video_id: str) -> int:
    """Total chat rows for one video (cheap COUNT over the video index)."""
    return int(
        query(
            "SELECT COUNT(*) n FROM messages WHERE platform = ? AND video_id = ?",
            (platform, video_id),
        )[0]["n"]
    )


# Bounded preview-panel chat window (WS-2): while a Twitch backfill runs the
# panel polls every ~2.5 s and must not re-serialize the whole growing
# archive. Row-based (not seconds) so dense chat is not cut by a time span.
_PANEL_CHAT_SLICE_ROWS = 4000


def chat_slice_for(
    platform: str,
    video_id: str,
    offset_sec: Optional[float],
    slice_rows: int = _PANEL_CHAT_SLICE_ROWS,
) -> tuple[list[dict], int]:
    """Bounded preview-panel chat window around *offset_sec*, time-ordered.

    Returns (rows, total_rows) — total_rows is the full message count and
    the returned slice is only a window of it. The window is
    ±slice_rows/2 rows around the first row at/after *offset_sec* (None →
    the head of the timeline), so the panel's ±150-row render + binary
    search stay exact while responses stay bounded. The
    (platform, video_id, offset_sec) index serves every query."""
    total = count_messages(platform, video_id)
    if total == 0:
        return [], 0
    half = max(1, slice_rows // 2)
    if offset_sec is None:
        anchor = 0
    else:
        anchor = min(
            int(
                query(
                    "SELECT COUNT(*) n FROM messages "
                    "WHERE platform = ? AND video_id = ? AND offset_sec < ?",
                    (platform, video_id, float(offset_sec)),
                )[0]["n"]
            ),
            total - 1,
        )
    start = max(0, anchor - half)
    take = min(total - start, slice_rows)
    rows = query(
        "SELECT platform, video_id, offset_sec, text, username, spam_count, color "
        "FROM messages WHERE platform = ? AND video_id = ? ORDER BY offset_sec LIMIT ? OFFSET ?",
        (platform, video_id, take, start),
    )
    return [dict(r) for r in rows], total


# --- transcripts ----------------------------------------------------------

def _normalize_lang(lang: Any) -> Optional[str]:
    """Normalize a caption/whisper language tag for transcripts.lang.

    pt / pt-br / pt-pt -> 'pt'; en / en-* -> 'en'; anything else keeps the
    raw code lowercased; empty -> None (untagged = PT content for search)."""
    if not lang:
        return None
    text = str(lang).strip().lower()
    base = text.split("-")[0]
    if base in ("pt", "en"):
        return base
    return text or None


def insert_transcript(
    platform: str, video_id: str, segments: Iterable[dict], *, lang: Optional[str] = None
) -> int:
    """Segments: seg_idx, start_sec, end_sec, text, words (list of
    {word, start, end}). lang: optional ISO-ish language tag, normalized
    (pt-br -> pt, en-US -> en, raw codes kept). Returns count inserted."""
    conn = get_conn()
    count = 0
    lang_norm = _normalize_lang(lang)
    with _lock:
        with conn:
            for seg in segments:
                conn.execute(
                    """INSERT INTO transcripts (platform, video_id, seg_idx,
                       start_sec, end_sec, text, words_json, lang)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        platform,
                        video_id,
                        int(seg["seg_idx"]),
                        float(seg["start_sec"]),
                        float(seg["end_sec"]),
                        seg["text"],
                        json.dumps(seg.get("words", []), ensure_ascii=False),
                        lang_norm,
                    ),
                )
                # FTS index entry is written by the transcripts_ai trigger.
                count += 1
    return count


def delete_transcripts(platform: str, video_id: str) -> int:
    """Remove every transcript row for a video; returns the deleted count.

    Used when a full re-transcribe replaces stale rows instead of appending
    a duplicate copy beside them."""
    cur = execute("DELETE FROM transcripts WHERE platform = ? AND video_id = ?", (platform, video_id))
    return cur.rowcount


def transcript_for(platform: str, video_id: str, *, raw: bool = False) -> list[dict]:
    """All transcript rows for a video, seg_idx-ordered.

    Default is the display shape: overlapping YouTube auto-caption
    duplicates are dropped (see _dedupe_transcript_rows). raw=True returns
    every stored row — the whisper resume path needs the full seg_idx set
    so a deduped read can never make it re-insert an existing segment."""
    rows = [
        dict(r)
        for r in query(
            "SELECT * FROM transcripts WHERE platform = ? AND video_id = ? ORDER BY seg_idx",
            (platform, video_id),
        )
    ]
    return rows if raw else _dedupe_transcript_rows(rows)


def transcript_source(platform: str, video_id: str) -> Optional[tuple[str, str]]:
    """(src_platform, src_video_id) whose transcript rows should serve this
    video for display: the video's own rows when present, else the best
    canonical-group member that has rows, priority youtube > twitch > kick
    (mirrors _PLATFORM_TRANSCRIBE_PRIORITY). None when nothing has rows.

    Group resolution uses the same COALESCE(a.canonical_key, v.canonical_key)
    LEFT JOIN as dedupe_view/_attach_platforms; a video outside any group
    resolves to itself."""
    if has_transcript(platform, video_id):
        return (platform, video_id)
    if platform not in _PLATFORM_TRANSCRIBE_PRIORITY:
        return None
    key = _canonical_key_for(platform, video_id)
    if not key:
        return None
    best: Optional[tuple[str, str]] = None
    best_prio = len(_PLATFORM_TRANSCRIBE_PRIORITY)
    for v in _group_members(key):
        p = v["platform"]
        prio = _PLATFORM_TRANSCRIBE_PRIORITY.get(p)
        if prio is None or prio >= best_prio:
            continue
        if has_transcript(p, v["video_id"]):
            best = (p, v["video_id"])
            best_prio = prio
    return best


def transcript_available(platform: str, video_id: str) -> bool:
    """Display 'has transcript' flag: the video's own rows OR a canonical
    twin's rows. Raw has_transcript() stays the internal guard (the
    transcribe dedupe/skip paths need the video's OWN rows); display paths
    use this so a Twitch VOD with a transcribed YouTube twin is not an
    empty state."""
    return bool(has_transcript(platform, video_id) or transcript_source(platform, video_id))


def set_transcript_lang(platform: str, video_id: str, lang: Optional[str]) -> int:
    """Rewrite every transcript row's lang tag for one video.

    Used by the done-time channel-language correction: when the whisper
    detection disagrees with the now-known channel language, the stored
    rows are re-stamped so search filters agree with the channel decision.
    Returns the number of rows touched."""
    cur = execute(
        "UPDATE transcripts SET lang = ? WHERE platform = ? AND video_id = ?",
        (lang, platform, video_id),
    )
    return cur.rowcount


def insert_audio_events(platform: str, video_id: str, events: Iterable[dict]) -> int:
    """Insert acoustic-event rows {start_sec, end_sec, event, score}; returns count."""
    events = list(events)
    if not events:
        return 0
    conn = get_conn()
    with _lock:
        with conn:
            conn.executemany(
                """INSERT INTO audio_events (platform, video_id, start_sec, end_sec, event, score)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    (platform, video_id,
                     float(e["start_sec"]), float(e["end_sec"]),
                     e["event"], float(e["score"]))
                    for e in events
                ],
            )
    return len(events)


def delete_audio_events(platform: str, video_id: str) -> int:
    """Remove every event row for a video (replace-on-rerun); returns count."""
    cur = execute(
        "DELETE FROM audio_events WHERE platform = ? AND video_id = ?",
        (platform, video_id),
    )
    return cur.rowcount


def audio_events_for(platform: str, video_id: str) -> list[dict]:
    rows = query(
        "SELECT * FROM audio_events WHERE platform = ? AND video_id = ? ORDER BY start_sec, event",
        (platform, video_id),
    )
    return [dict(r) for r in rows]


# --- dedupe ---------------------------------------------------------------

def set_alias(platform: str, video_id: str, canonical_key: str, note: str = "") -> None:
    execute(
        """INSERT INTO video_aliases (platform, video_id, canonical_key, note)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(platform, video_id) DO UPDATE SET
             canonical_key=excluded.canonical_key, note=excluded.note""",
        (platform, video_id, canonical_key, note),
    )


def dedupe_view() -> list[dict]:
    """Videos grouped by canonical_key with per-platform members, so callers
    can skip a platform when the same live/VOD exists on a higher-priority one."""
    rows = query(
        """SELECT v.platform, v.video_id, v.channel, v.title,
                  COALESCE(a.canonical_key, v.canonical_key) AS key
           FROM videos v
           LEFT JOIN video_aliases a USING (platform, video_id)
           WHERE COALESCE(a.canonical_key, v.canonical_key) IS NOT NULL
           ORDER BY key, v.platform"""
    )
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["key"], []).append(dict(r))
    return [{"canonical_key": k, "videos": v} for k, v in groups.items()]


# Mirrors archive_kick._PRIORITY: the platform that owns a mirrored live/VOD.
_PLATFORM_TRANSCRIBE_PRIORITY = {"youtube": 0, "twitch": 1, "kick": 2}


def _canonical_key_for(platform: str, video_id: str) -> Optional[str]:
    """The video's canonical key, video_aliases override included
    (COALESCE(a.canonical_key, v.canonical_key)); None when the video row
    is absent or has no key of its own."""
    row = query(
        """SELECT COALESCE(a.canonical_key, v.canonical_key) AS key
           FROM videos v
           LEFT JOIN video_aliases a USING (platform, video_id)
           WHERE v.platform = ? AND v.video_id = ?""",
        (platform, video_id),
    )
    return row[0]["key"] if row else None


def _group_members(canonical_key: str) -> list[dict]:
    """All video rows sharing a canonical key, alias overrides included
    ({platform, video_id, channel, title, key}); [] when none."""
    return [
        dict(r)
        for r in query(
            """SELECT v.platform, v.video_id, v.channel, v.title,
                      COALESCE(a.canonical_key, v.canonical_key) AS key
               FROM videos v
               LEFT JOIN video_aliases a USING (platform, video_id)
               WHERE COALESCE(a.canonical_key, v.canonical_key) = ?""",
            (canonical_key,),
        )
    ]


def transcribed_on_higher_priority_platform(platform: str, video_id: str) -> bool:
    """True when the same canonical_key group has a member on a HIGHER-priority
    platform (youtube > twitch > kick) that already has transcript rows.

    Mirrors the kick download dedupe rule (archive_kick.dedupe_decision) for
    transcription: if the YouTube (or Twitch) mirror of a Kick VOD is already
    transcribed — free via auto-captions — whisper needn't burn GPU on the
    Kick copy. Kick itself is never a blocker for a higher-priority member
    (the priority direction is one-way)."""
    if platform not in _PLATFORM_TRANSCRIBE_PRIORITY:
        return False
    me = _PLATFORM_TRANSCRIBE_PRIORITY[platform]
    key = _canonical_key_for(platform, video_id)
    if not key:
        return False
    for v in _group_members(key):
        p = v["platform"]
        if p == platform or p not in _PLATFORM_TRANSCRIBE_PRIORITY:
            continue
        if _PLATFORM_TRANSCRIBE_PRIORITY[p] < me and has_transcript(p, v["video_id"]):
            return True
    return False


# --- content dedup (SHA-256) -----------------------------------------------

def find_content_duplicate(sha256: str) -> Optional[dict]:
    """First videos row whose stored media file has this content hash.

    Returns {platform, video_id, archive_path} or None. Only rows that still
    reference a file (archive_path set) are eligible — an evicted row's hash
    is kept but must never be a dedup target."""
    rows = query(
        """SELECT platform, video_id, archive_path FROM videos
           WHERE content_sha256 = ? AND archive_path IS NOT NULL AND archive_path != ''
           ORDER BY created_at LIMIT 1""",
        (sha256,),
    )
    return dict(rows[0]) if rows else None


def content_duplicates() -> list[dict]:
    """Videos grouped by content hash when >= 2 rows share one media file.

    Both rows must still reference a file, so evicted rows (hash kept,
    path cleared) never pollute the list — the UI reports duplicates that
    actually cost disk. Each group: {sha256, count, videos:[{platform,
    video_id, channel, title, archive_path}]}."""
    groups = query(
        """SELECT content_sha256 AS sha256, COUNT(*) AS n
           FROM videos
           WHERE content_sha256 IS NOT NULL AND content_sha256 != ''
             AND archive_path IS NOT NULL AND archive_path != ''
           GROUP BY content_sha256 HAVING COUNT(*) > 1
           ORDER BY n DESC, sha256"""
    )
    out = []
    for g in groups:
        members = query(
            """SELECT platform, video_id, channel, title, archive_path
               FROM videos WHERE content_sha256 = ?
               ORDER BY created_at""",
            (g["sha256"],),
        )
        out.append({"sha256": g["sha256"], "count": g["n"],
                    "videos": [dict(m) for m in members]})
    return out


def release_archive_path(path: str) -> bool:
    """Unlink a media file once no videos row references it anymore.

    Archive rows are the source of truth for references: the caller must
    delete its row (or clear its archive_path) BEFORE calling this. When
    another row still points at the same path the file is kept — content
    dedup makes shared paths the norm, and unlink would be data loss.
    Returns True when the file was removed."""
    if not path:
        return False
    remaining = query(
        "SELECT COUNT(*) AS n FROM videos WHERE archive_path = ?",
        (path,),
    )[0]["n"]
    if remaining:
        return False
    try:
        os.unlink(path)
        return True
    except OSError:
        return False


def delete_video(platform: str, video_id: str) -> Optional[str]:
    """Delete one videos row and release its archive file (reference-counted).

    The file is unlinked only when no other row points at it. Returns the
    released path, or None when no such row existed."""
    rows = query(
        "SELECT archive_path FROM videos WHERE platform = ? AND video_id = ?",
        (platform, video_id),
    )
    if not rows:
        return None
    path = rows[0]["archive_path"]
    execute(
        "DELETE FROM videos WHERE platform = ? AND video_id = ?",
        (platform, video_id),
    )
    if path:
        release_archive_path(path)
    return path


# --- jobs -----------------------------------------------------------------


def maybe_enqueue_transcribe(platform: str, video_id: str, *, archive_path: Optional[str] = None, priority: int = 0) -> bool:
    """Queue whisper when a real archive file exists and no transcript yet."""
    plat = (platform or "").strip().lower()
    vid = (video_id or "").strip()
    if plat not in PLATFORMS or not vid:
        return False
    path = archive_path
    if not path:
        rows = query(
            "SELECT archive_path FROM videos WHERE platform = ? AND video_id = ?",
            (plat, vid),
        )
        path = rows[0]["archive_path"] if rows else None
    if not path or not Path(path).is_file():
        return False
    if plat == "youtube" and captions_cover(plat, vid):
        return False
    if transcript_for(plat, vid):
        return False
    job_id = f"transcribe-{plat}-{vid}"
    try:
        enqueue_job(job_id, "transcribe", plat, vid, priority=priority)
        return True
    except sqlite3.IntegrityError:
        return False


def enqueue_job(job_id: str, kind: str, platform: str, video_id: str, *, priority: int = 0) -> None:
    now = _now_iso()
    execute(
        """INSERT INTO archive_jobs (id, kind, platform, video_id, status,
           priority, created_at, updated_at, heartbeat)
           VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?)""",
        (job_id, kind, platform, video_id, priority, now, now, now),
    )


def update_job(job_id: str, *, status: Optional[str] = None,
               progress: Optional[float] = None, error: Optional[str] = None) -> None:
    sets = ["updated_at = ?", "heartbeat = ?"]
    params: list[Any] = [_now_iso(), _now_iso()]
    if status is not None:
        sets.append("status = ?")
        params.append(status)
    if progress is not None:
        sets.append("progress = ?")
        params.append(progress)
    if error is not None:
        sets.append("error = ?")
        params.append(error)
    params.append(job_id)
    execute(f"UPDATE archive_jobs SET {', '.join(sets)} WHERE id = ?", params)


def list_jobs(limit: int = 50) -> list[dict]:
    rows = query(
        """SELECT * FROM archive_jobs
           ORDER BY CASE status
             WHEN 'running' THEN 0
             WHEN 'queued' THEN 1
             WHEN 'failed' THEN 2
             ELSE 3 END,
             updated_at DESC
           LIMIT ?""",
        (limit,),
    )
    return [dict(r) for r in rows]


def has_pending_jobs() -> bool:
    """True when any job is queued or running (a worker has real work).

    The app boot uses this to decide between spawning the detached archive
    worker and keeping the in-process one."""
    return bool(
        query(
            "SELECT 1 FROM archive_jobs WHERE status IN ('queued','running') LIMIT 1"
        )
    )


def latest_job(platform: str, video_id: str, kind: Optional[str] = None) -> Optional[dict]:
    """Newest archive_jobs row for a video (optionally restricted to one
    kind), or None when no job was ever enqueued. Search enrichment uses it
    to skip videos with queued/running work and recently-failed jobs."""
    sql = "SELECT * FROM archive_jobs WHERE platform = ? AND video_id = ?"
    params: list[Any] = [platform, video_id]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " ORDER BY created_at DESC LIMIT 1"
    rows = query(sql, params)
    return dict(rows[0]) if rows else None


# --- entity watch (saved words / saved channels) ---------------------------

def list_watched_entities() -> list[dict]:
    """All watched entities with live hit counts (recent 30d + total)."""
    rows = query(
        """SELECT e.*,
                  COUNT(h.id) AS hit_count,
                  SUM(CASE WHEN h.acked = 0 THEN 1 ELSE 0 END) AS unacked_count
           FROM watched_entities e
           LEFT JOIN entity_hits h ON h.entity_id = e.id
           GROUP BY e.id
           ORDER BY e.kind, e.created_at"""
    )
    out = []
    for r in rows:
        d = dict(r)
        d["aliases"] = json.loads(d.get("aliases") or "[]")
        out.append(d)
    return out


def get_watched_entity(entity_id: int) -> Optional[dict]:
    rows = query("SELECT * FROM watched_entities WHERE id = ?", (entity_id,))
    if not rows:
        return None
    d = dict(rows[0])
    d["aliases"] = json.loads(d.get("aliases") or "[]")
    return d


def upsert_watched_entity(
    text: str,
    *,
    kind: str = "manual",
    source_channel: Optional[str] = None,
    aliases: Optional[list[str]] = None,
    enabled: bool = True,
) -> int:
    """Insert or update-by-text. Returns the entity id."""
    text = text.strip()
    if not text:
        raise ValueError("entity text must not be empty")
    now = _now_iso()
    aliases_json = json.dumps(
        [a.strip() for a in (aliases or []) if a.strip()], ensure_ascii=False
    )
    existing = query("SELECT id FROM watched_entities WHERE text = ?", (text,))
    if existing:
        eid = existing[0]["id"]
        execute(
            """UPDATE watched_entities
               SET aliases = ?, enabled = ?, source_channel = COALESCE(?, source_channel)
               WHERE id = ?""",
            (aliases_json, 1 if enabled else 0, source_channel, eid),
        )
        return eid
    execute(
        """INSERT INTO watched_entities (text, kind, source_channel, aliases, enabled, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (text, kind, source_channel, aliases_json, 1 if enabled else 0, now),
    )
    return query("SELECT last_insert_rowid() AS id")[0]["id"]


def set_watched_entity(entity_id: int, *, aliases: Optional[list[str]] = None,
                       enabled: Optional[bool] = None) -> None:
    sets: list[str] = []
    params: list[Any] = []
    if aliases is not None:
        sets.append("aliases = ?")
        params.append(
            json.dumps([a.strip() for a in aliases if a.strip()], ensure_ascii=False)
        )
    if enabled is not None:
        sets.append("enabled = ?")
        params.append(1 if enabled else 0)
    if not sets:
        return
    params.append(entity_id)
    execute(f"UPDATE watched_entities SET {', '.join(sets)} WHERE id = ?", params)


def delete_watched_entity(entity_id: int) -> None:
    execute("DELETE FROM watched_entities WHERE id = ?", (entity_id,))


def record_entity_hits(hits: list[dict]) -> None:
    """Idempotent hit insert: the (entity, platform, video_id, seg_idx) unique
    key refreshes last_seen/seen_count instead of duplicating."""
    now = _now_iso()
    with _lock:
        conn = get_conn()
        with conn:
            for h in hits:
                conn.execute(
                    """INSERT INTO entity_hits
                       (entity_id, platform, video_id, seg_idx, offset_sec,
                        snippet, variant, seen_count, first_seen, last_seen, acked)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 0)
                       ON CONFLICT(entity_id, platform, video_id, seg_idx) DO UPDATE SET
                         last_seen = excluded.last_seen,
                         seen_count = seen_count + 1,
                         snippet = excluded.snippet,
                         variant = excluded.variant""",
                    (
                        h["entity_id"], h["platform"], h["video_id"], h["seg_idx"],
                        h["offset_sec"], h["snippet"][:200], h.get("variant"),
                        now, now,
                    ),
                )


def list_entity_hits(*, entity_id: Optional[int] = None, platform: Optional[str] = None,
                     video_id: Optional[str] = None, acked_only: Optional[bool] = None,
                     limit: int = 100) -> list[dict]:
    where: list[str] = []
    params: list[Any] = []
    if entity_id is not None:
        where.append("h.entity_id = ?")
        params.append(entity_id)
    if platform:
        where.append("h.platform = ?")
        params.append(platform)
    if video_id:
        where.append("h.video_id = ?")
        params.append(video_id)
    if acked_only is not None:
        where.append("h.acked = ?")
        params.append(1 if acked_only else 0)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    rows = query(
        f"""SELECT h.*, e.text AS entity_text, e.kind AS entity_kind,
                   v.title AS video_title, v.channel AS video_channel
            FROM entity_hits h
            JOIN watched_entities e ON e.id = h.entity_id
            LEFT JOIN videos v ON v.platform = h.platform AND v.video_id = h.video_id
            {where_sql}
            ORDER BY h.last_seen DESC, h.id DESC
            LIMIT ?""",
        (*params, max(1, min(int(limit), 500))),
    )
    return [dict(r) for r in rows]


def ack_entity_hit(hit_id: int) -> None:
    execute("UPDATE entity_hits SET acked = 1 WHERE id = ?", (hit_id,))


def entity_watch_cursor() -> int:
    rows = query("SELECT value FROM entity_watch_state WHERE key = 'transcript_cursor'")
    if not rows:
        return 0
    try:
        return int(rows[0]["value"])
    except (TypeError, ValueError):
        return 0


def set_entity_watch_cursor(cursor: int) -> None:
    execute(
        """INSERT INTO entity_watch_state (key, value) VALUES ('transcript_cursor', ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (str(cursor),),
    )


def transcript_rows_after(cursor: int, limit: int) -> list[dict]:
    return [
        dict(r)
        for r in query(
            "SELECT * FROM transcripts WHERE id > ? ORDER BY id LIMIT ?",
            (cursor, limit),
        )
    ]


def recent_transcripts(platform: str, video_id: str, limit: int = 200) -> list[dict]:
    """Transcript rows for one video (used to highlight hits in the viewer)."""
    return [
        dict(r)
        for r in query(
            "SELECT * FROM transcripts WHERE platform = ? AND video_id = ? "
            "ORDER BY seg_idx LIMIT ?",
            (platform, video_id, limit),
        )
    ]


def worker_heartbeat(tag: str) -> None:
    """Stamp a worker liveness row (upsert); workers call this every poll
    iteration so search enrichment can tell an honest 'Indexing…' line from
    a queue nobody consumes."""
    execute(
        "INSERT INTO worker_heartbeats (tag, at) VALUES (?, ?) "
        "ON CONFLICT(tag) DO UPDATE SET at = excluded.at",
        (tag, _now_iso()),
    )


def worker_live(age_s: int = 30, tag: str = "transcribe") -> bool:
    """True when the *tag*'s heartbeat is younger than age_s.

    'transcribe' (default) = the archive worker owns the queue; the app's
    interactive layer stamps 'app-activity' so the worker can back off
    background YouTube work while the user is actively using the app. Both
    sides of the comparison are _now_iso() output (UTC, fixed width), so a
    lexicographic compare is a valid time compare. Missing table (pre-v2
    DB) or any SQL error means no heartbeat has ever been stamped → False."""
    from datetime import datetime, timedelta, timezone

    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat(
            timespec="seconds"
        )
        return bool(
            query(
                "SELECT 1 FROM worker_heartbeats WHERE tag = ? AND at >= ?",
                (tag, cutoff),
            )
        )
    except sqlite3.Error:
        return False


def captions_cover(platform: str, video_id: str, *, subtitles_first: Optional[bool] = None) -> bool:
    """True when YouTube captions already cover the video (captions-first on).

    Mirrors archive_transcribe._captions_first_skip: yt_subtitles_first
    (default True) AND transcript rows exist. The subtitles_first override
    lets tests probe the helper without the settings singleton."""
    if platform != "youtube":
        return False
    if subtitles_first is None:
        try:
            from deps import settings_mgr  # lazy: same pattern as archive_transcribe

            subtitles_first = bool(getattr(settings_mgr.get(), "yt_subtitles_first", True))
        except Exception:
            subtitles_first = True
    if not subtitles_first:
        return False
    return bool(transcript_for(platform, video_id))


def mark_captions_unavailable(platform: str, video_id: str) -> None:
    """Stamp the no-captions marker (persistent re-extract cooldown).

    Set by ingest_video when an ingest stored zero caption segments; the
    scheduler skips re-extract while the stamp is fresh
    (CAPTIONS_UNAVAILABLE_FRESH_S). No row (platform+video_id absent)
    writes nothing — the marker only ever rides an existing row."""
    execute(
        "UPDATE videos SET captions_unavailable_at = ? WHERE platform = ? AND video_id = ?",
        (_now_iso(), platform, video_id),
    )


def clear_captions_unavailable(platform: str, video_id: str) -> None:
    """Clear the no-captions marker after a successful caption ingest.

    A later ingest that DID find captions must immediately make the video
    a re-extract candidate again (e.g. captions were added to the upload
    after the marker was stamped)."""
    execute(
        "UPDATE videos SET captions_unavailable_at = NULL WHERE platform = ? AND video_id = ?",
        (platform, video_id),
    )


def captions_unavailable_at(platform: str, video_id: str) -> Optional[str]:
    """Stored no-captions marker (ISO UTC string) or None."""
    row = query(
        "SELECT captions_unavailable_at FROM videos WHERE platform = ? AND video_id = ?",
        (platform, video_id),
    )
    return row[0]["captions_unavailable_at"] if row else None


def optimize_fts() -> None:
    """Run FTS5 optimize on both indexes (merge b-trees after bulk writes).

    Called after a transcribe job finishes and after a chat backfill; cheap
    no-op on quiet indexes, wrapped in try/except per index."""
    for fts in ("transcripts_fts", "messages_fts"):
        try:
            execute(f"INSERT INTO {fts}({fts}) VALUES('optimize')")
        except sqlite3.Error:
            logger.warning("fts optimize failed for %s", fts)


# --- search ---------------------------------------------------------------

_HITS_PER_VIDEO_CAP = 3  # dedupe ceiling: never let one video flood a result page
# Lifted per-video cap for large-limit "every match" batches: not unlimited —
# a single video's chat can hold 1900+ repeated rows ('cellbit' 3948 hits),
# and the user wants a bounded, varied page, not one video's whole timeline.
# 60 rows per video keeps the top of a 300-row literal page to ~5 videos.
_LITERAL_PER_VIDEO_CAP = 60
_PHRASE_BOOST = 1.5      # exact-phrase matches get +50% before the cross-table merge
# All-query-tokens-present (any order/position) matches rank between exact
# phrase (1.5) and the tier-0 OR noise floor (1.0). This is FTS5's implicit
# multi-word AND semantics: a row that contains every word the user typed is
# a real match; a row with only one word is partial.
_AND_BOOST = 1.25
# Cross-segment phrase span pass: capped at 8 query tokens. The split loop
# is O(tokens^2) per adjacent segment pair and the LIKE prefilter carries
# one clause per long-token variant, so a long sentence would hang the
# search for nothing — a >8-token "phrase" is not something a user searches
# for, and FTS5 phrases within one segment already cover those cases.
_SPAN_MAX_TOKENS = 8
# Expansion work bound: _expand_query flattens every query token's fuzzy
# candidates. Beyond ~64 tokens the tier pattern would be capped out anyway
# (_MAX_EXPANDED_TERMS), so stop scanning the vocab for the rest — the
# exact-token fallback still covers them, and the work is bounded.
_QUERY_TOKENS_EXPAND_CAP = 64
# Title pass token cap: _titles_search iterates every query token against
# every video's title tokens, so an unbounded query is O(q_tokens × videos ×
# title_tokens) — a 2000-token query took ~4 minutes on the real archive.
# Realistic title queries are 1-4 words; the score denominator stays the
# FULL token count, so a title matching only the first few of a huge query
# keeps ranking as noise.
_TITLES_MAX_TOKENS = 16
# Repetitive-row downweight: rows that repeat the same 1-2 tokens 4+ times
# ('CELLBIT CELLBIT CELLBIT CELLBIT', 'LO CELLBIT LO CELLBIT') are hype/
# autocaption spam, not signal — they dominated single-token result pages
# (score 1.5, partial=False, first page) because the exact-phrase tier
# ranks every row carrying the word identically. 1.5 → 0.3 sinks them below
# every real phrase hit while keeping them findable. Rows with ≥3 distinct
# tokens (real sentences) are untouched.
_SPAM_DOWNWEIGHT = 0.2
_SPAM_MIN_TOKENS = 4
_SPAM_MAX_UNIQUE_TOKENS = 2


def _spam_penalty(text: str) -> float:
    """1.0 for real content, _SPAM_DOWNWEIGHT for repeated-token spam."""
    toks = re.findall(r"[^\W_]+", str(text or "").casefold())
    if len(toks) < _SPAM_MIN_TOKENS or len(set(toks)) > _SPAM_MAX_UNIQUE_TOKENS:
        return 1.0
    return _SPAM_DOWNWEIGHT


def search(
    q: str,
    *,
    platform: Optional[str] = None,
    channel: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    kind: Optional[str] = None,
    source: str = "both",
    video_id: Optional[str] = None,
    lang: Optional[str] = None,
    limit: int = 20,
    semantic: bool = False,
    mode: str = "broad",
    _channel_hint_out: Optional[list] = None,
    username: Optional[str] = None,
) -> list[dict]:
    """BM25 across transcripts + messages. Returns unified hits ordered by
    relevance: complete matches lead (exact phrase, then all-words), partial
    (subset-of-tokens) hits follow — each group by score desc, the owning
    video's started_at desc as the within-score tiebreak and NULL dates (no
    videos row) last; each hit carries enough to seek: platform, video_id,
    offset_sec, plus the owning video's channel/title/started_at (date),
    video_kind and lang (transcripts: transcripts.lang; messages: None).

    Merge semantics: each table is fetched ~3x limit (no per-table cap below
    that), scores are normalized per table (divided by the batch max, so the
    best hit of a table scores 1.0 — BM25 scales are not comparable across
    tables), and hits are deduped by (platform, video_id) with a ~3-hit cap
    per video — lifted for large limits (literal "every match" mode), so a
    targeted word surfaces every mention instead of one video's top 3. When
    the raw query as a quoted FTS5 phrase MATCHes, those
    hits get a +50% score boost before the cross-table merge (phrase pass
    runs first, then the fuzzy OR pass, unioned by rowid — phrase wins).

    Query understanding: when no explicit channel is given and the query's
    FIRST token case-insensitively matches a known videos.channel slug, the
    channel filter is applied implicitly. For ≥2-token queries the slug
    token is stripped from the query; a single-token query keeps its token
    (a bare channel search scopes to the channel while still matching the
    name inside its content — 'gaveta' no longer means 'drawer' archive-
    wide). The whole pass only runs when a _channel_hint_out list is passed
    (None = feature off, e.g. a UI that dismissed the hint); the matched
    slug (as stored in the DB) is appended to the box and the search router
    surfaces it as channel_hint.

    Filters: platform exact; channel exact or comma-separated slug list
    ("a,b" → IN clause, empty segments dropped, case-insensitive); kind is a
    comma-separated list ("vod,clip" → IN clause, unknown values dropped);
    date_from/date_to are inclusive YYYY-MM-DD bounds on the video's
    started_at date part; source narrows to one content kind ("chat" →
    messages only, "transcript" → transcripts only, "both" default);
    video_id scopes to a single archived video; lang filters transcripts
    only ('pt' → lang IS NULL OR LIKE 'pt%' — untagged whisper rows are PT
    content; 'en' → lang = 'en'; other values ignored). The videos join is
    LEFT so rows whose video was never indexed still surface when no
    video-backed filter is active.

    username narrows to chat rows from one or more authors — comma-separated
    ("a,b" → OR set, '@' stripped per token, empty segments dropped).
    Case-insensitive exact match on the @-stripped username OR the resolved
    display name (YouTube rows store the @handle; Twitch/Kick store the
    displayed name, so "scriptingkata" finds both '@Scriptingkata' and the
    resolved display name). Setting it implies source='chat' — transcript/
    title rows have no author. With an empty q it becomes a pure author
    history: every message from those authors, newest video first, no
    per-video cap (the FTS passes never run).

    Query tokens are fuzzy-expanded from the FTS5 vocab (exact + close
    Levenshtein matches, length-filtered, capped per token and in total);
    the expansion falls back to the exact tokens when the vocab is
    unavailable or the query is huge. Multi-token queries additionally
    apply a relevance floor to partial hits: an OR-only row carrying none
    of the query's rarest (lowest merged-corpus-frequency) tokens is
    single-token noise ('vale' ~3106 rows) and is dropped, so a common
    word can no longer flood the literal-results page; partials carrying
    the rare token ('estranheza') survive as closest matches. Single-token
    queries skip the floor — their phrase pass marks every exact row
    non-partial, preserving the "every mention" literal mode.

    Robustness: the query is sanitized (control chars -> spaces) so NUL can
    never corrupt a MATCH string; every quoted pattern escapes embedded
    quotes and every MATCH pass degrades to the remaining passes instead of
    raising (a query like 'a"b' used to 500). Partial words of >= 4 chars
    ABSENT from the corpus get native FTS5 prefix reach ("estranh" finds
    estranheza rows in chat/transcript content, not just titles); present
    tokens never prefix-flood tier 0 (the 'vale' -> valendo/valeu regression
    guard). The cross-segment span pass is capped at _SPAN_MAX_TOKENS and
    expands variants with ONE vocab/bigram load, so long queries return
    promptly instead of hanging the request path."""
    mode = (mode or "broad").strip().lower()
    if mode not in ("exact", "broad", "semantic"):
        mode = "broad"
    if mode == "semantic" or semantic:
        semantic = True
        mode = "broad"
    if not q.strip() and not (username or "").strip():
        return []
    # Control characters (NUL and friends) can never appear in indexed
    # tokens but corrupt FTS5 MATCH strings — sqlite truncates the query at
    # the NUL, leaving an unterminated quote that raises OperationalError
    # (a 500). Replace them with spaces; tabs/newlines stay separators.
    q = "".join(c if ord(c) >= 32 or c in "\t\n\r" else " " for c in q)
    raw_q = q.strip()
    kinds_raw = [k.strip().lower() for k in (kind or "").split(",") if k.strip()]
    want_yt_video = "video" in kinds_raw
    kinds = [k for k in kinds_raw if k in KINDS]
    platforms = (
        [p for p in (p.strip().lower() for p in platform.split(",")) if p in PLATFORMS]
        if platform
        else []
    )
    if not raw_q:
        # Chat-author-only mode: every message from the chosen author(s),
        # newest video first then newest message — no text matching, no
        # per-video cap (the point is the author's whole history).
        return _attach_platforms(_username_only_search(
            fetch=max(int(limit) * 3, 3),
            platforms=platforms, video_id=video_id, channel=channel,
            kinds=kinds, date_from=date_from, date_to=date_to,
            username=username, want_yt_video=want_yt_video,
        )[:int(limit)])
    if channel is None and _channel_hint_out is not None:
        hint = _channel_hint_for(raw_q)
        if hint is not None:
            channel = hint
            q = " ".join(q.split()[1:]) or q
            _channel_hint_out.append(hint)
    loops = (
        ("transcript", "transcripts_fts", "transcripts", "t.start_sec", "t.lang"),
        ("message", "messages_fts", "messages", "t.offset_sec", "NULL"),
    )
    if username:
        # Chat-only: transcripts/title rows have no author.
        source = "chat"
    # Multi-select source: comma-joined subset ("video,transcript") maps to
    # the tables that stay in the loop; "both" (or empty) = everything.
    wanted = {s.strip() for s in source.split(",") if s.strip()} or {"both"}
    if "both" in wanted:
        wanted = {"chat", "transcript", "video"}
    loops = []
    if "transcript" in wanted:
        loops.append(("transcript", "transcripts_fts", "transcripts", "t.start_sec", "t.lang"))
    if "chat" in wanted:
        loops.append(("message", "messages_fts", "messages", "t.offset_sec", "NULL"))
    # All-tokens AND pattern: FTS5's implicit multi-word semantics. Quoted
    # tokens joined with AND match rows containing EVERY query word (any
    # order/position). 1-2 char tokens ("da") are OR-noise and phrase-only
    # (mirrors the fuzzy expansion filter) — dropping them here means
    # "vale estranheza" still finds rows that say "vale da estranheza".
    q_tokens_all = re.findall(r"[^\W_]+", raw_q.casefold())
    q_tokens = [t for t in q_tokens_all if len(t) >= 3]
    # Multi-token relevance floor: an OR-only (partial) row is only useful
    # if it carries a DISCRIMINATIVE query token — one of the rarest in the
    # merged corpus. Rows matching only common tokens ('vale' 3106 rows)
    # are the recall noise that flooded literal-results pages.
    # A token ABSENT from the vocab snapshot (freq 0 — rare words, typos,
    # accented spellings the diacritic-stripped fts5vocab keys by) is the
    # rarest possible signal, and its rows ARE reachable through the fuzzy
    # tiers ('estranheza' → 'estranha' tier 2) — so absent tokens join the
    # keep set together with their vocabulary expansions, instead of
    # letting a common sibling token define the floor ('vale da
    # estranheza' on a corpus lacking 'estranheza' used to keep 'VALE VALE'
    # rows and drop every genuine 'estranha' match — inverted floor).
    # Single-token queries skip the floor.
    q_freq: dict[str, int] = {}
    for vocab in (_load_vocab(t[2]) for t in loops):
        if not vocab:
            continue
        for bucket in vocab.values():
            for term, n in bucket:
                q_freq[term] = q_freq.get(term, 0) + n
    q_keep_tokens: set[str] = set()
    if len(q_tokens) >= 2:
        q_absent = [t for t in q_tokens if q_freq.get(t, 0) == 0]
        q_present = {t: q_freq[t] for t in q_tokens if q_freq.get(t, 0) > 0}
        if q_absent:
            # Absent tokens (freq 0) are the rarest signal; their rows only
            # surface through fuzzy expansions, so they (and their variants,
            # added below) define the floor. Present tokens below the
            # chat-spam gate are genuinely discriminative too and survive
            # alongside ('estranheza fantasma' must keep estranheza rows);
            # common present tokens ('vale' ~3106) stay out or the floor
            # loses its bite on corpora where the rare word is the absent one.
            q_keep_tokens = set(q_absent) | {
                t for t, n in q_present.items() if n <= _SUPPRESS_DIST1_FREQ
            }
        elif q_present:
            q_keep_tokens = {t for t, n in q_present.items() if n == min(q_present.values())}
    # Tiered OR pattern (exact/fuzzy expansions + native prefix reach for
    # partial words) — see _fuzzy_pattern. The fallback quotes every raw
    # token (embedded quotes escaped) so a query like 'a"b' can never build
    # a malformed MATCH string.
    pattern = _fuzzy_pattern(q, [t[2] for t in loops], q_freq=q_freq)
    if pattern is None:
        pattern = {0: " OR ".join(_fts_phrase(w) for w in q.split() if w) or _fts_phrase(q)}
    phrase_pattern = _fts_phrase(raw_q) if raw_q else None
    and_pattern = " AND ".join(_fts_phrase(t) for t in q_tokens) if len(q_tokens) >= 2 else None
    # Cross-segment phrase matching: multi-word queries whose tokens are
    # split across two ADJACENT transcript segments ("…vale" | "da
    # estranheza…"). FTS5 phrases cannot span rows, so the span pass scans
    # the transcript table directly (see _phrase_span_rows). Capped at
    # _SPAN_MAX_TOKENS — the split loop is O(tokens^2) per adjacent pair,
    # so an unbounded sentence would hang the search (and a >8-token
    # "phrase" never meaningfully spans two segments).
    span_tokens = q_tokens_all
    span_variants: dict[str, list[str]] = {}
    if len(span_tokens) >= 2 and len(span_tokens) <= _SPAN_MAX_TOKENS:
        # The span pass's LIKE prefilter (see _phrase_span_rows) gates on
        # the literal long tokens, so a segment holding a dist-1 ASR
        # variant ("da estranhesa") would be filtered out before the
        # _tok_eq span match could see it. Expand each long token once and
        # pass the variant map down so the prefilter admits ASR variants
        # too. ONE vocab/bigram load for the whole query: the previous
        # per-token _expand_query calls re-paid the bigram row-count
        # re-checks (2 COUNT(*) on million-row tables) for every token —
        # an N-token query cost N × ~1s on the real archive.
        span_tables = [t2[2] for t2 in loops]
        span_vocabs = [v for v in (_load_vocab(t) for t in span_tables) if v is not None]
        span_bigrams = _load_bigrams(span_tables)
        span_variants = {
            t: [t] + [
                term for term, _ in _token_expansions(t, span_vocabs, span_bigrams, q_freq)
                if term != t
            ]
            for t in span_tokens
            if len(t) >= 4
        }
        # Floor coverage for vocab-absent tokens: their rows only surface
        # through fuzzy expansions ('estranheza' → 'estranha'), so the
        # keep set must admit those variants — otherwise the floor drops
        # exactly the typo/ASR matches the tiers were built to find.
        if q_keep_tokens:
            missing = [t for t in q_keep_tokens if q_freq.get(t, 0) == 0]
            for t in missing:
                for term, _ in _token_expansions(t, span_vocabs, span_bigrams, q_freq):
                    q_keep_tokens.add(term)
    if mode == "exact":
        pattern = {}
        and_pattern = None
        span_variants = {}
    fetch = max(int(limit) * 3, 3)  # ~3x batch; no per-table cap below 3x
    merged: list[dict] = []
    for tbl_idx, (hit_kind, fts, src, offcol, langcol) in enumerate(loops):
        base = dict(
            hit_kind=hit_kind, fts=fts, src=src, offcol=offcol, langcol=langcol,
            platforms=platforms, video_id=video_id, channel=channel, kinds=kinds,
            date_from=date_from, date_to=date_to, lang=lang,
            username=username, want_yt_video=want_yt_video,
        )
        # Distance tiers: one MATCH pass per tier, unioned by rowid (lowest
        # tier wins). Scores are discounted by 0.5^tier so cross-table merges
        # prefer the intended matches over rare expansion noise.
        by_row: dict[int, dict] = {}
        for dist, tier_pat in pattern.items():
            try:
                tier_rows = _table_search(tier_pat, fetch, **base)
            except sqlite3.Error:
                # Pattern not parseable even quoted (defense in depth — the
                # builders escape quotes, but a pathological token can slip
                # through); skip the tier instead of failing the search.
                tier_rows = []
            for r in tier_rows:
                r["_tier"] = dist
                by_row.setdefault(r["_rowid"], r)
        rows = list(by_row.values())
        # When the fallback exact pattern IS the quoted phrase (single-token
        # queries with no fuzzy expansions, e.g. "o"), the phrase pass would
        # re-run the IDENTICAL MATCH over the same rows — mark the tier-0
        # rows as phrase instead. Same rowids, same +50% boost, one full
        # bm25 pass saved (~700ms on a common word). Only fires for
        # single-token queries (multi-token OR patterns never equal the
        # quoted full phrase), so and_rows are always empty here and the
        # overwrite order below is unchanged.
        phrase_is_tier0 = (
            phrase_pattern is not None
            and len(pattern) == 1
            and next(iter(pattern.values())) == phrase_pattern
        )
        if phrase_is_tier0:
            for r in rows:
                r["_phrase"] = True
                r.pop("_tier", None)  # tier rows carry _tier; phrase rows never do
        and_rows: dict[int, dict] = {}
        if and_pattern:
            try:
                for r in _table_search(and_pattern, fetch, **base):
                    r["_and"] = True  # all query tokens present: +25% before merging
                    and_rows[r["_rowid"]] = r
            except sqlite3.Error:
                and_rows = {}  # pattern not parseable — degrade to phrase/fuzzy
        phrase_rows: dict[int, dict] = {}
        if phrase_pattern and not phrase_is_tier0:
            try:
                for r in _table_search(phrase_pattern, fetch, **base):
                    r["_phrase"] = True  # exact-phrase hit: +50% before merging
                    phrase_rows[r["_rowid"]] = r
            except sqlite3.Error:
                phrase_rows = {}  # phrase not parseable — degrade to fuzzy-only
        # Union by rowid; a phrase-marked row replaces its fuzzy twin, and
        # an AND-marked row replaces its OR-only twin (a row matching every
        # query word is a stronger signal than a single-word fuzzy hit).
        by_row: dict[int, dict] = {}
        for r in rows:
            by_row[r["_rowid"]] = r
        for rid, r in and_rows.items():
            by_row[rid] = r
        for rid, r in phrase_rows.items():
            by_row[rid] = r
        table_rows = list(by_row.values())
        # Span pass: the exact phrase split across two adjacent segments.
        # Row scores are re-based to the batch max so they normalize to 1.0
        # and receive the same +50% phrase boost as within-row hits.
        # Skipped when the within-segment phrase pass already matched: the
        # span scan is a full-table LIKE pass (~1-4s on 1.8M rows) that
        # only ADDS split-case recall — when the exact phrase exists in
        # single segments the user already has real hits, so the scan is
        # pure redundant cost ('vale da estranheza' was 15s → ~5s).
        if hit_kind == "transcript" and len(span_tokens) >= 2 and not phrase_rows:
            try:
                span_rows = _phrase_span_rows(
                    span_tokens, fetch, span_variants=span_variants,
                    platforms=platforms,
                    video_id=video_id, channel=channel, kinds=kinds,
                    date_from=date_from, date_to=date_to, lang=lang,
                    want_yt_video=want_yt_video, exact=(mode == "exact"),
                )
            except sqlite3.Error:
                span_rows = []  # scan failed — degrade to phrase/fuzzy-only
            if span_rows:
                sbase = max((h["score"] for h in table_rows), default=1.0)
                for r in span_rows:
                    r["_phrase"] = True
                    r["score"] = sbase or 1.0
                    r["_raw"] = r["score"]
                    by_row[r["_rowid"]] = r
                table_rows = list(by_row.values())
        if not table_rows:
            continue
        # Phrase and AND rows are coverage-tiered (flat), never normalized:
        # raw BM25 degenerates on small tables (idf ~0 for terms in >half
        # the rows) and a multi-rare-term OR row can out-score a phrase row
        # after max-normalization. OR rows normalize against their own max.
        or_max = max(
            (h["score"] for h in table_rows if not h.get("_phrase") and not h.get("_and")),
            default=0.0,
        )
        for h in table_rows:
            phr = h.pop("_phrase", False)
            andf = h.pop("_and", False)
            if phr:
                h["score"] = _PHRASE_BOOST  # exact phrase: 1.5
            elif andf:
                h["score"] = _AND_BOOST  # every query word present: 1.25
            else:
                if or_max > 0:  # guard div-by-zero: keep raw scores if OR max is 0
                    h["score"] = h["score"] / or_max
                h["score"] = h["score"] * 0.5 ** h.pop("_tier", 0)
                # Multi-token relevance floor: an OR-only row carrying none
                # of the query's rarest tokens is single-token noise — drop
                # it instead of letting a common word flood the results
                # page with "vale"-only mentions.
                if q_keep_tokens:
                    row_toks = set(re.findall(r"[^\W_]+", h["text"].casefold()))
                    if not row_toks.intersection(q_keep_tokens):
                        continue
            # Repetitive-token rows (hype/autocaption spam) are not
            # signal even when they contain the exact query word — sink
            # them below every real phrase hit.
            h["score"] *= _spam_penalty(h["text"])
            # A multi-word hit that reached only the fuzzy OR tier matched a
            # subset of the query — flag it so UIs can say "closest match".
            h["partial"] = not (phr or andf)
            h["_tbl"] = tbl_idx
            merged.append(h)
    # Video-title pass: matching titles surface saved-channel uploads that
    # have no transcript/chat yet (the channel index accumulates every
    # upload the panel has ever fetched). Included in "both" (titles are
    # neither chat nor transcript) and alone in "video" (the dedicated
    # title filter). Same normalization rule as the content tables: best
    # title hit scores 1.0.
    if "video" in wanted:
        title_rows = _titles_search(
            q,
            fetch,
            q_freq=q_freq,
            platforms=platforms,
            video_id=video_id,
            channel=channel,
            kinds=kinds,
            date_from=date_from,
            date_to=date_to,
            want_yt_video=want_yt_video,
            exact=(mode == "exact"),
        )
        if title_rows:
            # No ÷tmax normalization: a title matching only a common token
            # of a 4-word query ('quem foi que gritou' → "foi") used to
            # normalize to 1.0 and outrank every genuine partial content
            # hit. Score stays matched/len(q_tokens) with partial flagged,
            # so a 1.0 means a full title match and the global merge sorts
            # the rest (a "foi"-only title ranks at 0.25 with the partials).
            merged.extend(title_rows)
    # Dedupe by (platform, video_id), capping ~3 hits per video, then slice.
    # Relevance is the primary order: complete matches (partial False —
    # exact phrase 1.5, then all-words 1.25) lead, partial (subset-of-tokens)
    # hits follow, score desc within each group. The owning video's
    # started_at desc (ISO strings — the same lexicographic order the
    # author-history path sorts by in SQL) is the within-score tiebreak, so
    # a strong hit from an older video outranks a weak hit from a newer one.
    # NULL dates (LEFT JOIN miss — no videos row) sort last so archived
    # content never hides behind orphan rows. Equal scores resolve by table
    # priority (transcripts before messages), then by raw score — raw BM25
    # is only comparable WITHIN a table, so it must never be the
    # cross-table tie-break.
    # Duplicate/overlapping transcript rows (re-fetched VTTs, whisper split
    # artifacts) collapse BEFORE the cap, so one video's caption dups never
    # eat the page or the per-video slots.
    merged = _collapse_transcript_dupes(merged)
    # The per-video cap exists so a common fuzzy word never lets one video
    # flood the default result page. A caller asking for a big batch (the
    # FE's "infinite literal results" mode sends ~2000) wants every match
    # of a targeted word — the cap lifts but stays bounded so one video's
    # chat can't take the whole page; small/default limits keep the tight
    # cap (the multi-token relevance floor above already culled
    # single-token noise for multi-word queries).
    cap = (
        _HITS_PER_VIDEO_CAP
        if int(limit) <= _HITS_PER_VIDEO_CAP * 10
        else _LITERAL_PER_VIDEO_CAP
    )
    per_video: dict[tuple[str, str], int] = {}
    out: list[dict] = []
    for h in sorted(
        merged,
        key=lambda h: (not h["partial"], h["score"], h["date"] is not None, h["date"] or "", h.pop("_tbl", 0), h.pop("_raw", 0.0)),
        reverse=True,
    ):
        key = (h["platform"], h["video_id"])
        if per_video.get(key, 0) >= cap:
            continue
        per_video[key] = per_video.get(key, 0) + 1
        h.pop("_rowid", None)
        out.append(h)
    if semantic and "transcript" in wanted:
        # Concept pass: tiered hybrid merge (exact lexical > semantic >
        # partial lexical). Any embedding failure degrades to pure lexical.
        try:
            sem = _semantic_search(
                q, fetch, platforms=platforms, video_id=video_id,
                channel=channel, kinds=kinds, date_from=date_from,
                date_to=date_to, lang=lang, want_yt_video=want_yt_video,
            )
        except Exception:
            sem = None
        if sem:
            out = _merge_semantic_hits(out, sem, limit=int(limit))
            return _attach_platforms(out)
    return _attach_platforms(out[:limit])


def _fold_tokens(text: str) -> list[str]:
    """Lowercase, accent-folded, non-alphanumeric-split tokens (titles)."""
    import unicodedata
    folded = "".join(
        c for c in unicodedata.normalize("NFD", str(text or "").lower())
        if unicodedata.category(c) != "Mn"
    )
    return [t for t in re.split(r"[^a-z0-9]+", folded) if t]


def _titles_search(
    q: str,
    fetch: int,
    *,
    q_freq: Optional[dict[str, int]] = None,
    platforms: list[str],
    video_id: Optional[str],
    channel: Optional[str],
    kinds: list[str],
    date_from: Optional[str],
    date_to: Optional[str],
    want_yt_video: bool = False,
    exact: bool = False,
) -> list[dict]:
    """Video-title match over the videos table (folded-token coverage).

    Titles are short and the videos table is small (hundreds of rows), so a
    pure-Python pass is cheaper than an FTS5 titles index. A query token
    matches when it equals a title token, is an edit-distance-1 ASR variant
    of one (_tok_eq), or is a ≥4-char SUBSTRING of one ("estranh" finds
    "ESTRANHEZA"). Substring reach mirrors the R2 gate in
    _token_expansions: a token PRESENT in the merged corpus is a complete
    word, so its embedded matches ('vale' inside "valendo"/"cavaleiro")
    are recall noise and must not surface — 'vale da estranheza' used to
    pull "CAMPEONATO DO BOGUR VALENDO…" and "…Cavaleiro dos 7 Reinos…".
    Only tokens ABSENT from the corpus or below _PREFIX_GATE_FREQ (partial
    words/mishearings like "estranh") keep the substring reach; equality
    and _tok_eq always match. The reverse substring never matches:
    "cara"/"car" inside a "caralho" query must not pull titles like "Pé na
    porta, I.A. na cara!" — that surfaced partial=False noise at rank 1.0
    above real fuzzy chat hits. Score = fraction of query tokens matched.
    ponytail: when videos grows past ~10k rows, move to an FTS5
    external-content titles table with a unicode61 tokenizer and reuse the
    tier/merge machinery of the content tables."""
    q_tokens = _fold_tokens(q)
    # 1-2 char tokens are substring noise in titles ("da" ⊂ "day", "mudam").
    # The content passes keep them for phrase adjacency; here they only
    # match half the catalog. An all-short query simply skips the pass.
    q_tokens = [t for t in q_tokens if len(t) >= 3][:_TITLES_MAX_TOKENS]
    if not q_tokens:
        return []
    freq = q_freq or {}
    sql = ("SELECT platform, video_id, channel, title, original_title, "
           "started_at AS date, kind AS video_kind, channel_language FROM videos")
    where: list[str] = []
    params: list[Any] = []
    if platforms:
        where.append(f"platform IN ({','.join('?' * len(platforms))})")
        params.extend(platforms)
    if video_id:
        where.append("video_id = ?")
        params.append(video_id)
    if channel:
        slugs = [c.strip() for c in channel.split(",") if c.strip()]
        if slugs:
            where.append(f"lower(channel) IN ({','.join('?' * len(slugs))})")
            params.extend(s.lower() for s in slugs)
    kind_sql, kind_params = _kind_match_sql(kinds, want_yt_video, "")
    if kind_sql:
        where.append(kind_sql)
        params.extend(kind_params)
    if date_from:
        where.append("date(started_at) >= date(?)")
        params.append(date_from)
    if date_to:
        where.append("date(started_at) <= date(?)")
        params.append(date_to)
    if where:
        sql += " WHERE " + " AND ".join(where)
    out: list[dict] = []
    for r in query(sql, params):
        # Matching folds BOTH titles: the stored copy (what the walk saw) and
        # the WS-4 original (what the channel actually titled the video) —
        # "fim" finds "The END of Physical Media…" via "O FIM da Mídia…".
        title = str(r["title"] or "")
        original = str(r["original_title"] or "")
        toks = _fold_tokens(f"{title} {original}")
        if not toks:
            continue
        if exact:
            hay = f"{title} {original}".casefold()
            needle = " ".join(q_tokens)
            if needle not in " ".join(toks) and q.strip().casefold() not in hay:
                continue
            matched = len(q_tokens)
        else:
            matched = sum(
                1
                for qt in q_tokens
                if any(
                    tt == qt
                    or (
                        len(qt) >= 4
                        and freq.get(qt, 0) <= _PREFIX_GATE_FREQ
                        and qt in tt
                    )
                    or _tok_eq(tt, qt)
                    for tt in toks
                )
            )
        if not matched:
            continue
        score = matched / len(q_tokens)
        display = original or title
        out.append({
            "kind": "title",
            "platform": r["platform"],
            "video_id": r["video_id"],
            "offset_sec": 0,
            "text": display,
            "score": score,
            "lang": None,
            "channel": r["channel"],
            "title": display,
            "date": r["date"],
            "video_kind": r["video_kind"],
            "channel_language": r["channel_language"],
            "partial": matched < len(q_tokens),
            "_rowid": f"t:{r['platform']}:{r['video_id']}",
            "_raw": score,
        })
    out.sort(key=lambda h: h["score"], reverse=True)
    return out[:fetch]


def _table_search(
    pattern: str,
    fetch: int,
    *,
    hit_kind: str,
    fts: str,
    src: str,
    offcol: str,
    langcol: str,
    platforms: list[str],
    video_id: Optional[str],
    channel: Optional[str],
    kinds: list[str],
    date_from: Optional[str],
    date_to: Optional[str],
    lang: Optional[str],
    username: Optional[str] = None,
    want_yt_video: bool = False,
) -> list[dict]:
    """One MATCH pass over one FTS table with the shared filter set.

    score is -bm25 (positive, higher = better); hits carry a private _rowid
    key so the tier/phrase passes can dedupe by row identity, and a _phrase
    flag set by callers that need the exact-phrase boost."""
    author_col = (
        "COALESCE(t.display_name, t.username) AS author, "
        if hit_kind == "message"
        else "NULL AS author, "
    )
    sql = (
        f"SELECT t.rowid AS _rowid, -bm25({fts}) AS score, "
        f"t.platform, t.video_id, {offcol} AS offset_sec, t.text, "
        f"{langcol} AS lang, "
        f"{author_col}"
        "v.channel, "
        # WS-4: hit titles display the original (non-translated) YouTube
        # title when the backfill stored one; content matching is on t.text.
        "COALESCE(NULLIF(v.original_title, ''), v.title) AS title, "
        "v.started_at AS date, v.kind AS video_kind, v.channel_language AS channel_language "
        f"FROM {fts} f JOIN {src} t ON t.id = f.rowid "
        "LEFT JOIN videos v ON v.platform = t.platform AND v.video_id = t.video_id "
        f"WHERE {fts} MATCH ?"
    )
    params: list[Any] = [pattern]
    if platforms:
        sql += f" AND t.platform IN ({','.join('?' * len(platforms))})"
        params.extend(platforms)
    if video_id:
        sql += " AND t.video_id = ?"
        params.append(video_id)
    if channel:
        slugs = [c.strip() for c in channel.split(",") if c.strip()]
        if slugs:
            sql += f" AND lower(v.channel) IN ({','.join('?' * len(slugs))})"
            params.extend(s.lower() for s in slugs)
    kind_sql, kind_params = _kind_match_sql(kinds, want_yt_video, "v")
    if kind_sql:
        sql += f" AND {kind_sql}"
        params.extend(kind_params)
    if date_from:
        sql += " AND date(v.started_at) >= date(?)"
        params.append(date_from)
    if date_to:
        sql += " AND date(v.started_at) <= date(?)"
        params.append(date_to)
    if hit_kind == "transcript" and lang:
        lng = lang.strip().lower()
        if lng == "pt":
            # Untagged rows (whisper without detected language) are PT content.
            sql += " AND (t.lang IS NULL OR t.lang LIKE 'pt%')"
        elif lng == "en":
            sql += " AND t.lang = 'en'"
    if hit_kind == "transcript":
        # Channel-language-aware exclusion: when the owning video's channel
        # language is a known family, transcript rows of a DIFFERENT family
        # (YouTube caption ingest historically stored en next to pt for the
        # same segment) never surface.
        sql += f" AND {_channel_lang_exclusion()}"
    if username:
        # Author match: case-insensitive exact on the displayed name, with
        # '@' stripped on both sides (YouTube stores @handle; Twitch/Kick
        # store the displayed name). Once a display name is resolved it
        # takes precedence for display, but the @handle still matches — both
        # spellings are the same person. Comma-separated = OR set.
        tokens = _username_tokens(username)
        if tokens:
            clause, token_params = _username_match_clause(tokens, "t")
            sql += f" AND {clause}"
            params.extend(token_params)
    sql += f" ORDER BY score DESC LIMIT {int(fetch)}"
    return [
        {
            "kind": hit_kind,
            "platform": r["platform"],
            "video_id": r["video_id"],
            "offset_sec": r["offset_sec"],
            "text": r["text"],
            "author": r["author"],
            "score": r["score"],
            "lang": r["lang"],
            "channel": r["channel"],
            "title": r["title"],
            "date": r["date"],
            "video_kind": r["video_kind"],
            "channel_language": r["channel_language"],
            "_rowid": r["_rowid"],
            "_raw": r["score"],  # pre-normalization -bm25; tie-breaker only
        }
        for r in query(sql, params)
    ]


def _username_tokens(username: Optional[str]) -> list[str]:
    """Comma-separated chat authors → normalized tokens (lowercased,
    '@'-stripped; empty segments dropped)."""
    if not username:
        return []
    return [t.strip().lstrip("@").lower() for t in username.split(",") if t.strip()]


def _username_match_clause(tokens: list[str], alias: str) -> tuple[str, list[str]]:
    """WHERE fragment matching ANY token against the author's @-stripped
    username or resolved display_name (display name takes precedence for
    display, but the @handle still matches — both spellings are one person)."""
    parts: list[str] = []
    params: list[Any] = []
    for t in tokens:
        parts.append(
            f"(replace(lower({alias}.username), '@', '') = ?"
            f" OR ({alias}.display_name IS NOT NULL"
            f"     AND replace(lower({alias}.display_name), '@', '') = ?))"
        )
        params.extend((t, t))
    return "(" + " OR ".join(parts) + ")", params


def _username_only_search(
    fetch: int,
    *,
    platforms: list[str],
    video_id: Optional[str],
    channel: Optional[str],
    kinds: list[str],
    date_from: Optional[str],
    date_to: Optional[str],
    username: Optional[str],
    want_yt_video: bool = False,
) -> list[dict]:
    """All chat rows from the chosen author(s) — no text matching.

    Comma-separated usernames become an OR set; each token matches the
    @-stripped username or the resolved display name. Newest video first,
    then newest message within it. No per-video cap: the point is the
    author's whole history, so every row counts toward `fetch` (callers
    slice to their limit). Shared filters (platform/video/channel/kind/
    dates) apply; hits carry the same keys as _table_search message rows."""
    tokens = _username_tokens(username)
    if not tokens:
        return []
    where, params = _username_match_clause(tokens, "m")
    sql = (
        "SELECT m.id AS _rowid, 1.0 AS score, m.platform, m.video_id, m.offset_sec, "
        "m.text, NULL AS lang, COALESCE(m.display_name, m.username) AS author, "
        "v.channel AS channel, "
        "COALESCE(NULLIF(v.original_title, ''), v.title) AS title, "
        "v.started_at AS date, v.kind AS video_kind, "
        "v.channel_language AS channel_language "
        "FROM messages m LEFT JOIN videos v "
        "ON v.platform = m.platform AND v.video_id = m.video_id "
        f"WHERE {where}"
    )
    if platforms:
        sql += f" AND m.platform IN ({','.join('?' * len(platforms))})"
        params.extend(platforms)
    if video_id:
        sql += " AND m.video_id = ?"
        params.append(video_id)
    if channel:
        slugs = [c.strip().lower() for c in channel.split(",") if c.strip()]
        if slugs:
            sql += f" AND lower(COALESCE(v.channel, '')) IN ({','.join('?' * len(slugs))})"
            params.extend(slugs)
    kind_sql, kind_params = _kind_match_sql(kinds, want_yt_video, "v")
    if kind_sql:
        sql += f" AND {kind_sql}"
        params.extend(kind_params)
    if date_from:
        sql += " AND date(v.started_at) >= date(?)"
        params.append(date_from)
    if date_to:
        sql += " AND date(v.started_at) <= date(?)"
        params.append(date_to)
    sql += " ORDER BY v.started_at DESC, m.offset_sec DESC LIMIT ?"
    params.append(fetch)
    return [
        {
            "kind": "message",
            "platform": r["platform"],
            "video_id": r["video_id"],
            "offset_sec": r["offset_sec"],
            "text": r["text"],
            "author": r["author"],
            "score": 1.0,
            "lang": None,
            "channel": r["channel"],
            "title": r["title"],
            "date": r["date"],
            "video_kind": r["video_kind"],
            "channel_language": r["channel_language"],
        }
        for r in query(sql, params)
    ]


def _consonant_skeleton(text: str) -> str:
    """Consonant skeleton of a word: accent-folded, vowels stripped.

    Two words that differ only in their vowels share it — 'nautilus' /
    'nutilos' (a Brazilian mishearing the ASR layer must bridge) — while
    consonant-altering neighbors ('caralho' / 'cavalo') never do. Used by
    the fuzzy admission gate to separate vowel-mishearing bridges from
    fold-collapsed noise."""
    return "".join(
        c for c in str(text or "").translate(_ACCENT_FOLD) if c.isalpha() and c not in "aeiou"
    )


def _tok_eq(a: str, b: str) -> bool:
    """Token equality with ASR tolerance: exact for short tokens, edit
    distance <= 1 for tokens of >= 4 chars (whisper/captions variants like
    'estranheza' vs 'estranhesa' still match)."""
    if a == b:
        return True
    if len(a) < 4 or len(b) < 4:
        return False
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) < len(b):
        a, b = b, a
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) <= 1
    return any(a[:i] + a[i + 1:] == b for i in range(len(a)))



def _kind_match_sql(kinds: list[str], want_yt_video: bool, video: str) -> tuple[Optional[str], list[str]]:
    """SQL for videos.kind, plus virtual kind=video (YouTube long-form, not shorts)."""
    col = f"{video}.kind" if video else "kind"
    plat = f"{video}.platform" if video else "platform"
    clauses: list[str] = []
    params: list[str] = []
    if kinds:
        clauses.append(f"{col} IN ({','.join('?' * len(kinds))})")
        params.extend(kinds)
    if want_yt_video:
        clauses.append(f"({plat} = 'youtube' AND {col} NOT IN ('short', 'clip'))")
    if not clauses:
        return None, []
    if len(clauses) == 1:
        return clauses[0], params
    return "(" + " OR ".join(clauses) + ")", params


def _append_content_filters(
    parts: list[str],
    params: list[Any],
    *,
    platforms: list[str],
    video_id: Optional[str],
    channel: Optional[str],
    kinds: list[str],
    date_from: Optional[str],
    date_to: Optional[str],
    lang: Optional[str],
    table: str = "t",
    video: str = "v",
    apply_lang: bool = True,
    want_yt_video: bool = False,
) -> None:
    """Shared WHERE fragment for content-table scans (transcripts/messages).

    Alias-aware so the span pass, the semantic pass and _table_search build
    identical filters: table aliases default to t (content) / v (videos)."""
    if platforms:
        parts.append(f"{table}.platform IN ({','.join('?' * len(platforms))})")
        params.extend(platforms)
    if video_id:
        parts.append(f"{table}.video_id = ?")
        params.append(video_id)
    if channel:
        slugs = [c.strip() for c in channel.split(",") if c.strip()]
        if slugs:
            parts.append(f"lower({video}.channel) IN ({','.join('?' * len(slugs))})")
            params.extend(s.lower() for s in slugs)
    kind_sql, kind_params = _kind_match_sql(kinds, want_yt_video, video)
    if kind_sql:
        parts.append(kind_sql)
        params.extend(kind_params)
    if date_from:
        parts.append(f"date({video}.started_at) >= date(?)")
        params.append(date_from)
    if date_to:
        parts.append(f"date({video}.started_at) <= date(?)")
        params.append(date_to)
    if apply_lang and lang:
        lng = lang.strip().lower()
        if lng == "pt":
            # Untagged rows (whisper without detected language) are PT content.
            parts.append(f"({table}.lang IS NULL OR {table}.lang LIKE 'pt%')")
        elif lng == "en":
            parts.append(f"{table}.lang = 'en'")


# --- channel-language-aware transcript filtering + cross-platform marking --

# Language families the pipeline knows (mirrors channel_language.KNOWN);
# transcript rows of a DIFFERENT family are hidden when the owning video's
# channel language is one of these. Anything else (unknown, 'ja', 'ko', ...)
# keeps the old all-families behavior.
_KNOWN_CHANNEL_LANGS = ("pt", "en", "es")

# Chunk size for IN-clause lookups: safely under SQLITE_MAX_VARIABLE_NUMBER
# on every supported build (defaults range 999..250000).
_SQLITE_IN_CHUNK = 900


def _lang_family(lang: Optional[str]) -> Optional[str]:
    """Family code of a lang tag ('pt-br' -> 'pt', 'en-US' -> 'en'); None when empty."""
    if not lang:
        return None
    return str(lang).strip().lower().split("-")[0]


def _channel_lang_exclusion(table: str = "t", video: str = "v") -> str:
    """SQL fragment hiding transcript rows whose lang family differs from
    their video's channel language, when that channel language is a known
    family (pt/en/es). Per-row, so global searches spanning channels stay
    correct; untagged rows and unknown channel languages flow through
    (non-destructive — rows are hidden, never deleted). Python twin:
    _lang_matches_channel (the semantic pass scans by embedding id, so it
    post-filters instead of filtering SQL)."""
    return (
        f"({video}.channel_language IS NULL"
        f" OR {video}.channel_language NOT IN ('pt','en','es')"
        f" OR {table}.lang IS NULL"
        f" OR lower(substr({table}.lang, 1, instr({table}.lang || '-', '-') - 1))"
        f" = lower({video}.channel_language))"
    )


def _lang_matches_channel(lang: Optional[str], channel_language: Optional[str]) -> bool:
    """Python twin of _channel_lang_exclusion for the semantic pass."""
    fam = _lang_family(channel_language)
    if fam is None or fam not in _KNOWN_CHANNEL_LANGS:
        return True
    if not lang:
        return True
    return _lang_family(lang) == fam


def _attach_platforms(hits: list[dict]) -> list[dict]:
    """Attach `platforms` to every hit in place: all platforms where the same
    canonical VOD exists (dedupe group members, video_aliases overrides
    included), always containing the hit's own platform. Hits with no
    canonical key (orphan rows, never deduped) get [platform]. Two bounded
    lookups (hit videos -> keys, keys -> group platforms), chunked to stay
    under SQLite's IN-list variable limit."""
    pairs = sorted({(h["platform"], h["video_id"]) for h in hits})
    if not pairs:
        return hits
    key_by_video: dict[tuple[str, str], Optional[str]] = {}
    for i in range(0, len(pairs), _SQLITE_IN_CHUNK):
        chunk = pairs[i : i + _SQLITE_IN_CHUNK]
        marks = ",".join("(?,?)" for _ in chunk)
        params = [p for pair in chunk for p in pair]
        for r in query(
            f"""SELECT v.platform, v.video_id,
                       COALESCE(a.canonical_key, v.canonical_key) AS key
                FROM videos v
                LEFT JOIN video_aliases a USING (platform, video_id)
                WHERE (v.platform, v.video_id) IN ({marks})""",
            params,
        ):
            key_by_video[(r["platform"], r["video_id"])] = r["key"]
    keys = sorted({k for k in key_by_video.values() if k})
    platforms_by_key: dict[str, list[str]] = {}
    for i in range(0, len(keys), _SQLITE_IN_CHUNK):
        chunk = keys[i : i + _SQLITE_IN_CHUNK]
        marks = ",".join("?" * len(chunk))
        for r in query(
            f"""SELECT COALESCE(a.canonical_key, v.canonical_key) AS key, v.platform
                FROM videos v
                LEFT JOIN video_aliases a USING (platform, video_id)
                WHERE COALESCE(a.canonical_key, v.canonical_key) IN ({marks})
                ORDER BY v.platform""",
            chunk,
        ):
            platforms_by_key.setdefault(r["key"], []).append(r["platform"])
    for h in hits:
        key = key_by_video.get((h["platform"], h["video_id"]))
        plats = platforms_by_key.get(key) if key else None
        h["platforms"] = plats or [h["platform"]]
    return hits


def _phrase_span_rows(
    q_tokens: list[str],
    fetch: int,
    *,
    span_variants: Optional[dict[str, list[str]]] = None,
    platforms: list[str],
    video_id: Optional[str],
    channel: Optional[str],
    kinds: list[str],
    date_from: Optional[str],
    date_to: Optional[str],
    lang: Optional[str],
    want_yt_video: bool = False,
    exact: bool = False,
) -> list[dict]:
    """Exact-phrase hits whose tokens sit in two ADJACENT transcript
    segments: seg N ends with a phrase prefix and seg N+1 starts with the
    remainder. Scans the transcript table with the shared filter set; a
    per-row prefilter on the phrase's long (>= 4 char) tokens keeps the scan
    proportional to the phrase's rarity instead of the archive size.

    Returns hit dicts shaped like _table_search rows; score is a placeholder
    (1.0) that the caller re-bases to the table batch max, so span hits
    normalize like within-row phrase hits. ponytail: O(archive rows that
    contain a long query token) per phrase search — the prefilter keeps it
    fast for typical phrases; a trigram index would make it O(log n)."""
    if len(q_tokens) < 2 or len(q_tokens) > _SPAN_MAX_TOKENS:
        # Over the cap the split loop below is O(tokens^2) per adjacent
        # pair and the LIKE prefilter carries one clause per variant — a
        # long sentence would hang the request. search() caps the span
        # query before calling; this guard defends direct callers.
        return []
    long_toks = [t for t in q_tokens if len(t) >= 4]
    if span_variants:
        # Admit the query tokens' ASR/fuzzy variants in the prefilter: the
        # literal LIKE gate would skip a segment whose text holds only a
        # dist-1 twin ("estranhesa") before _tok_eq could match it below.
        long_toks = sorted({v for t in long_toks for v in span_variants.get(t, [t])})
    sql = (
        "SELECT t.rowid AS _rowid, t.platform, t.video_id, t.seg_idx, "
        "t.start_sec AS offset_sec, t.text, t.lang AS lang, "
        "v.channel, "
        "COALESCE(NULLIF(v.original_title, ''), v.title) AS title, "
        "v.started_at AS date, v.kind AS video_kind, v.channel_language AS channel_language "
        "FROM transcripts t "
        "LEFT JOIN videos v ON v.platform = t.platform AND v.video_id = t.video_id "
        "WHERE 1=1"
    )
    params: list[Any] = []
    if long_toks:
        # LIKE '%tok%' is exactly equivalent to instr(lower(text), tok) > 0
        # for tokenizer-produced tokens ([^\W_]+ — never %, _): SQLite LIKE
        # is ASCII-case-insensitive, same as its built-in lower(). Measured
        # ~2x faster on the full-corpus scan (~0.8s vs ~1.5s at 1.8M rows).
        sql += " AND (" + " OR ".join("t.text LIKE '%' || ? || '%'" for _ in long_toks) + ")"
        params.extend(long_toks)
    parts: list[str] = []
    _append_content_filters(
        parts, params, platforms=platforms, video_id=video_id, channel=channel,
        kinds=kinds, date_from=date_from, date_to=date_to, lang=lang,
        want_yt_video=want_yt_video,
    )
    # Span hits are transcripts: same channel-language family exclusion as
    # _table_search (both segments of the pair must pass the row filter).
    parts.append(_channel_lang_exclusion())
    if parts:
        sql += " AND " + " AND ".join(parts)
    # No ORDER BY in SQL (a filesort of every matched row costs ~200ms): the
    # per-video seg_idx sort below plus the sorted() iteration replicate the
    # old (video_id, seg_idx) order exactly — all real video ids are ASCII,
    # so BINARY collation == Python str order. ponytail: if non-ASCII video
    # ids ever appear, switch back to the SQL ORDER BY.
    by_video: dict[str, list[dict]] = {}
    for r in query(sql, params):
        by_video.setdefault(r["video_id"], []).append(r)
    hits: list[dict] = []
    for _vid, segs in sorted(by_video.items()):
        segs.sort(key=lambda r: r["seg_idx"])
        for a, b in zip(segs, segs[1:]):
            if b["seg_idx"] != a["seg_idx"] + 1:
                continue
            a_toks = re.findall(r"[^\W_]+", a["text"].casefold())
            b_toks = re.findall(r"[^\W_]+", b["text"].casefold())
            tok_match = (lambda x, y: x == y) if exact else _tok_eq
            for split in range(1, len(q_tokens)):
                prefix, suffix = q_tokens[:split], q_tokens[split:]
                if len(a_toks) < len(prefix) or len(b_toks) < len(suffix):
                    continue
                if not all(
                    tok_match(x, y) for x, y in zip(a_toks[-len(prefix):], prefix)
                ):
                    continue
                if not all(
                    tok_match(x, y) for x, y in zip(b_toks[:len(suffix)], suffix)
                ):
                    continue
                hit = {
                    "kind": "transcript",
                    "platform": a["platform"],
                    "video_id": a["video_id"],
                    "offset_sec": a["offset_sec"],
                    "text": f"{a['text']} {b['text']}",
                    "score": 1.0,
                    "lang": a["lang"],
                    "channel": a["channel"],
                    "title": a["title"],
                    "date": a["date"],
                    "video_kind": a["video_kind"],
                    "channel_language": a["channel_language"],
                    "_rowid": a["_rowid"],
                    "_raw": 1.0,
                }
                hits.append(hit)
                break  # one hit per adjacent pair
    hits.sort(key=lambda h: h["offset_sec"])
    return hits[: max(fetch * 3, 3)]


def _stamp_embed_fingerprint() -> None:
    """Record the current embed-model fingerprint next to the vectors just
    written, so a later model swap is detected and re-embeds the corpus."""
    try:
        from services import archive_embed  # lazy: no import cycle

        fp = archive_embed.embed_fingerprint()
        if fp is None:
            return
        with _lock:
            with get_conn():
                get_conn().execute(
                    "INSERT INTO semantic_meta (key, value) VALUES ('embed_fingerprint', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (fp,),
                )
    except Exception:
        pass  # stamping is best-effort; a missing stamp just re-embeds once


def set_transcript_embedding(transcript_id: int, vec: bytes) -> None:
    """Upsert one segment's embedding blob (float32, 384 dims)."""
    with _lock:
        with get_conn():
            get_conn().execute(
                "INSERT INTO transcript_embeddings (transcript_id, vec) VALUES (?, ?) "
                "ON CONFLICT(transcript_id) DO UPDATE SET vec = excluded.vec",
                (transcript_id, vec),
            )
    _stamp_embed_fingerprint()


def set_transcript_embeddings(pairs: list[tuple[int, bytes]]) -> None:
    """Upsert many segment embedding blobs in one transaction (backfill)."""
    if not pairs:
        return
    with _lock:
        with get_conn():
            get_conn().executemany(
                "INSERT INTO transcript_embeddings (transcript_id, vec) VALUES (?, ?) "
                "ON CONFLICT(transcript_id) DO UPDATE SET vec = excluded.vec",
                pairs,
            )
    _stamp_embed_fingerprint()


def missing_embedding_segments(limit: int = 0) -> list[sqlite3.Row]:
    """Transcript rows without a stored vector (backfill work queue)."""
    sql = (
        "SELECT t.id AS transcript_id, t.text AS text FROM transcripts t "
        "LEFT JOIN transcript_embeddings e ON e.transcript_id = t.id "
        "WHERE e.vec IS NULL"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    return query(sql)


_EMBED_BACKFILL_CAP = 50_000  # segments embedded inline per semantic query
# Hard time budget for the semantic pass. The 300s wedge that killed the
# listener was a RAM blow-up: the matrix used to be copied out of its mmap
# (2.8GB) and converted to an fp16 GPU tensor (another 1.4GB this box
# cannot allocate) — swap thrash froze the process. The matrix now stays
# mmap'd, so a phase is bounded by disk read speed (~5-10s cold, ~1-2s
# warm); the budget is the second layer: it degrades to pure lexical when
# it expires BETWEEN phases (a running numpy op cannot be cancelled).
# 12s lets a fully cold first pass (model load ~2.5s + first matrix scan
# page-faulting 2.8GB) complete instead of silently dropping the concept
# tier on the user's very first semantic search.
_SEMANTIC_TIME_BUDGET_S = 12.0
# Semantic candidate pool cap: bounds the candidates query and the
# per-candidate metadata fetch. The top ~3x fetch get output; the rest of
# the pool is headroom for the per-video cap.
_SEMANTIC_RERANK_CAND_CAP = 60

# RAM + disk cache of the full (sorted transcript_id, vec) matrix for the
# semantic scan. Reading the corpus blobs is the dominant cost (~80s at
# 1.79M rows), so the matrix is persisted as <db>.embeddings.<maxid>.{ids,mat}.npy
# next to the DB (rebuilds only when MAX(transcript_id) moves) and kept in
# RAM for the process lifetime. COUNT(*) would scan the 2.8GB blob b-tree
# (~21s) — MAX alone is the stamp; a deleted highest-id row leaves a stale
# matrix row that no scope query can select (its transcript is gone), so it
# is harmless until the next insert bumps the stamp.
_embed_matrix_cache: Optional[tuple[tuple[str, int, Optional[str]], object, object]] = None
_embed_matrix_lock = threading.Lock()

# Per-process LRU cache for the semantic pass. The embed model is
# deterministic given a query, so an identical repeated query skips the
# ~2s cold embed. Keys carry the CALLABLE itself as an identity stamp:
# test suites monkeypatch it per-test and a model reload swaps the
# session, so a stale key can never serve a vector produced by a different
# model. Bounded by maxsize; popitem(last=False) evicts the
# least-recently-used entry.
# (The mmarco cross-encoder rerank used to run here too — it was removed:
# English-trained, it reordered good pt-BR cosine rankings into junk,
# e.g. 0.974 'estratagema magnam' over 0.28 'queimada estranha'. The
# multilingual e5 cosine order stands.)
_embed_query_cache: "collections.OrderedDict[tuple, object]" = collections.OrderedDict()
_EMBED_QUERY_CACHE_MAX = 64
# Session-level RESPONSE cache for the whole semantic pass: an identical
# repeat submit (same query + every filter + limit) skips the matrix scan
# entirely — the vector cache above only skips the embed piece. TTL ~60s
# so fresh ingest stays visible; keys carry the embed callable as an
# identity stamp (same convention as the cache above), so a model reload
# can never serve stale vectors. ponytail: a write-heavy session could
# keep re-serving pre-write vectors for the TTL window — upgrade path:
# stamp the key with the transcript_embeddings MAX(id) when the corpus
# churns faster than 60s.
_semantic_resp_cache: "collections.OrderedDict[tuple, tuple[float, list[dict]]]" = (
    collections.OrderedDict()
)
_SEMANTIC_RESP_CACHE_MAX = 32
_SEMANTIC_RESP_TTL_S = 60.0


def _embed_matrix_paths(mx: int) -> tuple[Path, Path]:
    # Plain f-string names: with_suffix would strip the trailing mx (the
    # last dot segment), collapsing every stamp to one clobbered file.
    dbp = Path(_db_path())
    stem = f".{dbp.name}.embeddings.{mx}"
    return dbp.parent / f"{stem}.ids.npy", dbp.parent / f"{stem}.mat.npy"


def _embed_matrix() -> tuple[object, object]:
    """(sorted transcript_ids, vec matrix) — lazy full-corpus cache.

    Loads from the persisted .npy pair when present, else builds + saves.
    The matrix stays MMAP'd: a RAM copy of the 2.8GB corpus is exactly
    what wedged the frozen build (MemoryError thrash → the listener hung
    past 300s), and numpy matmul reads the mapped pages lazily (~0.8s warm
    on 1.83M rows — the GPU tensor it used to build needed a 1.4GB fp16
    copy this box cannot allocate). The mmap pins the .npy file until the
    cache is replaced; the stale-file cleanup skips the live pair and any
    unlink of a file this process still maps raises, which is caught. The
    caller handles an empty corpus (never None)."""
    global _embed_matrix_cache
    import numpy as np

    mx = int(query(
        "SELECT COALESCE(MAX(transcript_id), 0) AS mx FROM transcript_embeddings"
    )[0]["mx"])
    # The embed-model fingerprint is part of the key: after a model swap the
    # corpus vectors are re-embedded (same ids, new values), so a matrix
    # cached under the old model must never be served.
    from services import archive_embed  # lazy: keeps onnxruntime out of boot

    key = (str(_db_path()), mx, archive_embed.embed_fingerprint())
    hit = _embed_matrix_cache
    if hit is not None and hit[0] == key:
        return hit[1], hit[2]
    with _embed_matrix_lock:
        hit = _embed_matrix_cache
        if hit is not None and hit[0] == key:
            return hit[1], hit[2]
        ids, mat = None, None
        ids_p, mat_p = _embed_matrix_paths(mx)
        try:
            if ids_p.exists() and mat_p.exists():
                ids = np.load(ids_p, mmap_mode="r")
                mat = np.load(mat_p, mmap_mode="r")
        except Exception:
            ids, mat = None, None
        if ids is None:
            rows = query(
                "SELECT transcript_id, vec FROM transcript_embeddings "
                "WHERE vec IS NOT NULL"
            )
            if rows:
                ids_arr = np.fromiter(
                    (r["transcript_id"] for r in rows), dtype=np.int64, count=len(rows)
                )
                order = np.argsort(ids_arr)
                ids = ids_arr[order]
                mat = np.frombuffer(
                    b"".join(r["vec"] for r in rows), dtype="<f4"
                ).reshape(len(rows), -1)
                mat = np.ascontiguousarray(mat[order])
            else:
                ids = np.empty(0, dtype=np.int64)
                mat = np.empty((0, 0), dtype=np.float32)
            try:
                np.save(ids_p, ids)
                np.save(mat_p, mat)
                for stale in Path(_db_path()).parent.glob(
                    f".{Path(_db_path()).name}.embeddings.*.npy"
                ):
                    if str(stale) not in (str(ids_p), str(mat_p)):
                        stale.unlink(missing_ok=True)
            except Exception:
                pass  # disk cache is best-effort; RAM cache still serves
        _embed_matrix_cache = (key, ids, mat)
        return ids, mat


def _matmul_scores(mat: object, qv: object, idx: object = None) -> object:
    """Cosine scores for the scope slice — numpy BLAS over the mmap'd
    matrix (read-only matmul is fine; ~0.8s at 1.79M rows when the pages
    are warm)."""
    import numpy as np

    sub = mat if idx is None else np.asarray(mat)[idx]
    return np.asarray(sub) @ np.asarray(qv)


def _semantic_noise(text: Optional[str]) -> bool:
    """True when a transcript row carries no meaningful words.

    ASR censorship/profanity masking leaves placeholder rows like
    '[&nbsp;__&nbsp;]', event tags ('Ã,', '[ ]') or punctuation runs. They
    embed to a dense, generic region (XLM-R special-token space) and rank
    near MANY queries, so they would pollute the semantic candidate pool —
    the FTS lexical passes never match them (no real tokens), but the
    cosine scan does. Rows with at least one 2+ char word token are real
    content and kept ('[risadas]' is a meaningful laugh marker)."""
    t = re.sub(r"&[a-z]+;", " ", str(text or ""))  # strip HTML entities (nbsp)
    return not [w for w in re.findall(r"[^\W_]+", t) if len(w) >= 2]


def _semantic_query_text(q: str) -> str:
    """Corpus-spelling-corrected query for embedding.

    e5 embeds the raw spelling; a typo'd or ASR-mangled token ("preato",
    "recita") yields a weaker vector than the corpus form ("preto",
    "receita"). Every query token ABSENT from the transcript vocab is
    replaced by its closest conservative expansion — a dist-0 variant
    (fold/digraph-equal) or a dist-1 true typo sharing the 3-char prefix
    or consonant skeleton. Intent-preserving by construction: 'molho' is
    never rewritten to the corpus word 'olho' (dist 1 but no prefix/
    skeleton bridge), and tokens present in the corpus are left untouched
    (they are already the intended word). An unavailable vocab degrades to
    the raw query. Deterministic per vocab snapshot, so the semantic
    response cache (keyed on the raw query) stays effective on repeats."""
    tokens = q.split()
    if not tokens:
        return q
    vocab = _load_vocab("transcripts")
    if not vocab:
        return q
    merged: dict[str, int] = {}
    for bucket in vocab.values():
        for term, n in bucket:
            merged[term] = merged.get(term, 0) + n
    bigrams = _load_bigrams(["transcripts"])
    out: list[str] = []
    for tok in tokens:
        w = re.sub(r"[^\w]+", "", tok).casefold()
        if len(w) < 4 or not w.isalnum() or merged.get(w, 0) > 0:
            out.append(tok)
            continue
        best: Optional[str] = None
        for term, dist in _token_expansions(w, [vocab], bigrams, merged):
            if term == w:
                continue
            if dist == 0 or (
                dist == 1
                and (
                    w[:3] == term[:3]
                    or _consonant_skeleton(w) == _consonant_skeleton(term)
                )
            ):
                best = term
                break
        out.append(best if best is not None else tok)
    return " ".join(out)


def _merge_semantic_hits(
    lex: list[dict], sem: list[dict], limit: int
) -> list[dict]:
    """Tiered hybrid merge of lexical and semantic passes.

    Exact lexical hits (phrase/all-words, partial=False) lead — they are
    literally what the user typed — then semantic concept hits, then
    partial lexical matches. Dedupes by (platform, video_id, offset_sec)
    with the higher tier winning (a semantic hit never shadows the exact
    lexical hit of the same segment), and the per-video cap applies across
    the merged stream so one video cannot flood the concept page with
    near-identical rows."""
    exact = [h for h in lex if not h.get("partial")]
    partial = [h for h in lex if h.get("partial")]
    per_video: dict[tuple[str, str], int] = {}
    seen: set[tuple] = set()
    out: list[dict] = []
    for h in exact + sem + partial:
        key = (h["platform"], h["video_id"])
        skey = (h["platform"], h["video_id"], h.get("offset_sec"))
        if skey in seen:
            continue
        seen.add(skey)
        if per_video.get(key, 0) >= _HITS_PER_VIDEO_CAP:
            continue
        per_video[key] = per_video.get(key, 0) + 1
        out.append(h)
        if len(out) >= limit:
            break
    return out


def _semantic_search(
    q: str,
    fetch: int,
    *,
    platforms: list[str],
    video_id: Optional[str],
    channel: Optional[str],
    kinds: list[str],
    date_from: Optional[str],
    date_to: Optional[str],
    lang: Optional[str],
    want_yt_video: bool = False,
) -> Optional[list[dict]]:
    """Concept pass over transcript embeddings: cosine scan of the filtered
    scope, segments embedded lazily (bounded per query), then an optional
    mmarco rerank of the top candidates. Returns hits shaped like
    _table_search rows with score (0..1) and a 'semantic' flag, or None when
    the embedding backend is unavailable — the caller then serves pure
    lexical results.

    Hot path: the scope query selects ids only (no vec blobs, no text), the
    scan is a GPU fp16 matmul over the cached matrix, and metadata is
    fetched for just the top candidates."""
    import numpy as np

    from services import archive_embed  # lazy: onnxruntime stays out of boot

    # Time budget: the pass must never wedge the request path on a slow
    # box. Checked between phases only (numpy ops cannot be cancelled); an
    # expired deadline degrades to pure lexical instead of returning a
    # half-ranked result. NOT cached — the next attempt may succeed warm.
    deadline = time.monotonic() + _SEMANTIC_TIME_BUDGET_S

    def _over_budget() -> bool:
        return time.monotonic() > deadline

    # Whole-pass response cache: identical params within the TTL return the
    # previous hit list (deep-copied — callers mutate hits, e.g. the
    # _attach_platforms pass). Key carries the model callable AND its file
    # fingerprint, so a session that swaps models (or re-exports a model
    # file) never reuses stale vectors.
    efp = archive_embed.embed_fingerprint()
    # Query spelling is corrected against the corpus vocab before embedding
    # (deterministic given q, so the cache stays effective on repeat
    # submits; both the raw and corrected forms ride in the key).
    q_embed = _semantic_query_text(q)
    resp_key = (
        q, q_embed, tuple(platforms), video_id, channel, tuple(kinds),
        date_from, date_to, lang, fetch, efp,
        archive_embed.embed_query,
    )
    cached = _semantic_resp_cache.get(resp_key)
    if cached is not None:
        if time.monotonic() - cached[0] <= _SEMANTIC_RESP_TTL_S:
            return [dict(h) for h in cached[1]]
        _semantic_resp_cache.pop(resp_key, None)

    qkey = (q_embed, efp, archive_embed.embed_query)
    qv = _embed_query_cache.get(qkey)
    if qv is None:
        if _over_budget():
            return None
        raw = archive_embed.embed_query(q_embed)
        if raw is None:
            return None
        qv = np.asarray(raw).reshape(-1).copy()  # (1, dim) -> (dim,)
        if len(_embed_query_cache) >= _EMBED_QUERY_CACHE_MAX:
            _embed_query_cache.popitem(last=False)
        _embed_query_cache[qkey] = qv

    # Bounded lazy backfill: only when the corpus grew past the last
    # embedded id (new transcripts always get higher ids). Full scan is
    # gated on that cheap MAX comparison, so steady state never pays it.
    mx_emb = int(query(
        "SELECT COALESCE(MAX(transcript_id), 0) AS mx FROM transcript_embeddings"
    )[0]["mx"])
    mx_tr = int(query("SELECT COALESCE(MAX(id), 0) AS mx FROM transcripts")[0]["mx"])
    # Model-version check: stored vectors carry the fingerprint of the
    # embedder that produced them. A missing/mismatched stamp means the
    # corpus was embedded by another model (or before fingerprinting) —
    # those vectors live in a different space, so the whole corpus is
    # re-embedded in bounded batches and the disk/RAM matrix caches are
    # invalidated first.
    stored_fp: Optional[str] = None
    fp_row = query("SELECT value FROM semantic_meta WHERE key = 'embed_fingerprint'")
    if fp_row:
        stored_fp = fp_row[0]["value"]
    model_stale = stored_fp != efp
    if model_stale and efp is not None:
        for p in _embed_matrix_paths(mx_emb):
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
        missing = query(
            "SELECT id AS transcript_id, text AS text FROM transcripts LIMIT ?",
            (_EMBED_BACKFILL_CAP,),
        )
    elif mx_tr > mx_emb:
        missing = missing_embedding_segments(_EMBED_BACKFILL_CAP)
    else:
        missing = []
    if missing:
        if _over_budget():
            return None
        vecs = archive_embed.embed_texts([r["text"] for r in missing], "passage: ")
        if vecs is None:
            return None
        for r, v in zip(missing, vecs):
            set_transcript_embedding(r["transcript_id"], v.astype("<f4").tobytes())

    if _over_budget():
        return None
    ids, mat = _embed_matrix()
    if len(ids) == 0:
        return None
    if _over_budget():
        return None

    # Scope ids: every matching transcript that has a vector. Unfiltered
    # searches (the common case) reuse the matrix ids — no per-query scan.
    if not (platforms or video_id or channel or kinds or date_from or date_to or lang):
        scope_ids = ids
    else:
        if _over_budget():
            return None
        sql = (
            "SELECT t.id AS id FROM transcripts t "
            "JOIN transcript_embeddings e ON e.transcript_id = t.id "
            "JOIN videos v ON v.platform = t.platform AND v.video_id = t.video_id "
            "WHERE 1=1"
        )
        params: list[Any] = []
        parts: list[str] = []
        _append_content_filters(
            parts, params, platforms=platforms, video_id=video_id, channel=channel,
            kinds=kinds, date_from=date_from, date_to=date_to, lang=lang,
            want_yt_video=want_yt_video,
        )
        if parts:
            sql += " AND " + " AND ".join(parts)
        scope_ids = np.asarray([r["id"] for r in query(sql, params)], dtype=np.int64)

    if len(scope_ids) == 0:
        return None
    if scope_ids is ids:
        # Full-corpus scope: score the matrix directly (indexing would copy
        # 2.8GB for zero benefit).
        scores = _matmul_scores(mat, qv)
    else:
        idx = np.searchsorted(ids, scope_ids)
        # Every scope id must exist in the matrix (scope JOINs on has-vec);
        # a miss means a concurrent write raced the cache — degrade, the
        # next query rebuilds.
        if np.any(idx >= len(ids)) or np.any(
            ids[np.minimum(idx, len(ids) - 1)] != scope_ids
        ):
            return None
        scores = _matmul_scores(mat, qv, idx=idx)
    if _over_budget():
        return None
    order = np.argsort(-scores)
    top_n = min(max(fetch * 2, 30), _SEMANTIC_RERANK_CAND_CAP)
    cand_ids = [int(scope_ids[int(i)]) for i in order[:top_n]]
    cand_scores = [float(scores[int(i)]) for i in order[:top_n]]
    if not cand_ids:
        return None
    rows = query(
        "SELECT t.id AS transcript_id, t.platform, t.video_id, t.start_sec, "
        "t.text, t.lang AS lang, v.channel, "
        "COALESCE(NULLIF(v.original_title, ''), v.title) AS title, "
        "v.started_at AS date, v.kind AS video_kind, v.channel_language AS channel_language "
        "FROM transcripts t "
        "LEFT JOIN videos v ON v.platform = t.platform AND v.video_id = t.video_id "
        f"WHERE t.id IN ({','.join('?' * len(cand_ids))})",
        cand_ids,
    )
    by_id = {r["transcript_id"]: dict(r) for r in rows}
    # Placeholder/empty rows ('[&nbsp;__&nbsp;]', 'Ã,') are semantically
    # empty — they embed to a dense region and rank near many queries, so
    # they never enter the rerank pool (see _semantic_noise).
    cand = []
    keep = []
    for i, s in zip(cand_ids, cand_scores):
        r = by_id.get(i)
        if r is not None and not _semantic_noise(r["text"]):
            cand.append(r)
            keep.append(s)
    cand_scores = keep
    # Candidate order IS the cosine order: the mmarco cross-encoder rerank
    # used to reorder these, but it is English-trained — on pt-BR queries
    # it ranked junk above real matches ('estratagema magnam' 0.974 over
    # 'queimada estranha' 0.28 for 'estranheza'; for 'vale da estranheza'
    # it demoted 0.90 cosine hits to 0.26). The multilingual e5 cosine
    # ranking stands as-is.
    out: list[dict] = []
    per_video: dict[tuple[str, str], int] = {}
    for cos, r in zip(cand_scores, cand):
        key = (r["platform"], r["video_id"])
        if per_video.get(key, 0) >= _HITS_PER_VIDEO_CAP:
            continue
        per_video[key] = per_video.get(key, 0) + 1
        out.append({
            "kind": "transcript",
            "platform": r["platform"],
            "video_id": r["video_id"],
            "offset_sec": r["start_sec"],
            "text": r["text"],
            "score": cos,
            "lang": r["lang"],
            "channel": r["channel"],
            "title": r["title"],
            "date": r["date"],
            "video_kind": r["video_kind"],
            "channel_language": r["channel_language"],
            "semantic": True,
        })
        if len(out) >= max(fetch * 3, 3):
            break
    # Same channel-language family exclusion as the lexical passes; the
    # embedding scan has no SQL row filter, so mismatched-family hits are
    # dropped here (Python twin of _channel_lang_exclusion).
    out = [h for h in out if _lang_matches_channel(h["lang"], h["channel_language"])]
    # Duplicate caption rows (re-fetched VTTs) embed identically and used
    # to rank as repeated hits ('Esther fênix' twice at 0.619 for
    # 'estranheza') — collapse them like the lexical merge does.
    out = _collapse_transcript_dupes(out)
    if len(_semantic_resp_cache) >= _SEMANTIC_RESP_CACHE_MAX:
        _semantic_resp_cache.popitem(last=False)
    _semantic_resp_cache[resp_key] = (time.monotonic(), [dict(h) for h in out])
    return out


# Channel-slug cache for the channel_hint query understanding: (loaded_at,
# db_path, content_stamp, {lower_slug: stored_slug}); TTL 300s mirrors the
# vocab cache. Reloads when the resolved DB path changed (test modules rebind
# VODRIP_ARCHIVE_DB) or when videos.content stamp moved — the test suites
# share one process-wide DB (last module-level env override wins), so a
# TTL-only cache would silently serve another module's slugs.
_CHANNEL_SLUG_TTL_S = 300.0
_channel_slug_lock = threading.Lock()
_channel_slug_cache: tuple[float, str, tuple[int, Optional[str]], dict[str, str]] = (
    0.0, "", (0, None), {},
)


def _videos_stamp() -> tuple[int, Optional[str]]:
    """(row count, max updated_at) — cheap content fingerprint for caches."""
    row = query("SELECT COUNT(*) AS n, MAX(updated_at) AS at FROM videos")[0]
    return row["n"], row["at"]


def _channel_hint_for(q: str) -> Optional[str]:
    """First-token channel slug match, or None.

    Fires for ≥2-token queries (scope + strip the leading slug) and for
    single-token queries that ARE a channel slug (scope only — the caller
    keeps the token, so 'gaveta' matches 'gaveta' inside the Gaveta
    channel's content instead of every drawer mention in the archive).
    Returns the slug exactly as stored in videos.channel so the UI can
    render a canonical chip."""
    tokens = q.split()
    if not tokens:
        return None
    first = tokens[0].lower()
    global _channel_slug_cache
    with _channel_slug_lock:
        now = time.monotonic()
        loaded_at, db_path, stamp, slugs = _channel_slug_cache
        if (
            now - loaded_at >= _CHANNEL_SLUG_TTL_S
            or db_path != str(_db_path())
            or stamp != _videos_stamp()
        ):
            slugs = {}
            for r in query("SELECT DISTINCT lower(channel) AS slug, channel FROM videos"):
                slugs.setdefault(r["slug"], r["channel"])
            _channel_slug_cache = (now, str(_db_path()), _videos_stamp(), slugs)
    return slugs.get(first)


# --- fuzzy expansion (FTS5 vocab) -------------------------------------------
# Query tokens are expanded with close vocabulary matches so typos still hit
# ("arthur" finds "artur"). The vocab is read once per table via fts5vocab
# (works on external-content tables — verified) and cached in memory for
# _VOCAB_TTL_S; per-token expansions are cached the same way. Everything
# degrades to the exact token pattern on failure.

_VOCAB_MAX_TOKENS = 25_000
# 30 min: the vocab rebuild is row-count-gated and invalidates per-token
# expansions via the generation bump, so a long TTL only defers the cold
# fuzzy-expansion cost (~200ms/token) — restart or corpus growth still
# refresh it promptly. Was 300s: every new backend process re-paid the
# full expansion scan for each unique word within minutes.
_VOCAB_TTL_S = 1800.0
# Disk snapshot of the vocab (pickle beside the DB): the fts5vocab GROUP BY
# costs ~0.5-1s per table on a fresh process — the dominant cold-search tax
# after a backend restart. The snapshot is keyed by the content row count
# and a TTL, so chat growth or an hour of uptime still rebuilds it.
_VOCAB_DISK_TTL_S = 3600.0
_TOKEN_EXPAND_CAP = 12
_MAX_EXPANDED_TERMS = 64
# R3/R2 tuning: dist>=1 expansions above the merged corpus frequency are
# dropped (chat-spam noise); short rare tokens unlock tier-0 prefix
# expansion (gate on the token's own merged freq, at most _TOKEN_PREFIX_CAP
# terms, ranked by frequency).
_SUPPRESS_DIST1_FREQ = 1000
_PREFIX_GATE_FREQ = 300
_TOKEN_PREFIX_CAP = 8

_vocab_lock = threading.Lock()
# content table -> (loaded_at_monotonic, {token_len: [(term, freq), ...]}, row_count)
# (db_path, loaded_at, by_len, row_count) per table — db_path guards against
# test suites rebinding VODRIP_ARCHIVE_DB mid-process.
_vocab_cache: dict[str, tuple[str, float, dict[int, list[tuple[str, int]]], int]] = {}
# query token -> (expanded_at_monotonic, [terms, ...])
_token_cache: dict[tuple[str, str, str], tuple[float, list[str]]] = {}
_vocab_generation: dict[str, int] = {}
# tables with a background vocab rebuild in flight (search-latency guard)
_vocab_rebuild_pending: set[str] = set()


def _vocab_disk_path(table: str) -> str:
    dbp = str(_db_path())
    return os.path.join(os.path.dirname(dbp), f".{os.path.basename(dbp)}.vocab.{table}.pkl")


def _load_vocab_disk(table: str) -> Optional[tuple[int, dict[int, list[tuple[str, int]]]]]:
    """Read the pickled vocab snapshot if fresh. Best-effort: any failure
    degrades to the GROUP BY rebuild.

    Returns (saved_row_count, by_len): validity is TTL AND the content
    row count the snapshot was built from — the caller compares it with
    the current COUNT(*) (a cheap indexed scan, same gate the warm path
    uses) so a corpus that grew since the snapshot never serves a vocab
    missing the new words. Old 2-tuple pickles unpickle as a length error
    and degrade to a rebuild."""
    try:
        path = _vocab_disk_path(table)
        if not os.path.exists(path):
            return None
        with open(path, "rb") as fh:
            saved_at, saved_rows, by_len = pickle.load(fh)
        if time.time() - saved_at > _VOCAB_DISK_TTL_S:
            return None
        return int(saved_rows), by_len
    except Exception:
        return None


def _save_vocab_disk(
    table: str, row_count: int, by_len: dict[int, list[tuple[str, int]]]
) -> None:
    """Persist the snapshot (with the content row count it was built from)
    for the next process. Best-effort: a failed write (read-only dir, disk
    full) only costs the next process a rebuild."""
    try:
        path = _vocab_disk_path(table)
        with open(path, "wb") as fh:
            pickle.dump((time.time(), int(row_count), by_len), fh, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception:
        pass


def _load_vocab(table: str) -> Optional[dict[int, list[tuple[str, int]]]]:
    """Top-N FTS5 tokens for one content table, bucketed by length.

    Reads the index vocabulary through an fts5vocab virtual table (verified
    to work on these external-content indexes) and keeps the ~25k most
    frequent tokens in memory for _VOCAB_TTL_S. The cache reloads when the
    content table's row count changed since last load — chat spam arriving
    between searches would otherwise skew the top-25k ranking and push
    content words out of the expansion vocabulary. Returns None when the
    vocab is unavailable so callers fall back to exact matching."""
    now = time.monotonic()
    with _vocab_lock:
        hit = _vocab_cache.get(table)
        if hit and now - hit[1] < _VOCAB_TTL_S and hit[0] == str(_db_path()):
            cur = query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
            if cur == hit[3]:
                return hit[2]
            stale = hit[2]
        else:
            stale = None
    if stale is not None:
        # Corpus grew since the snapshot (live chat lands between searches):
        # serve the STALE vocab right now — the rebuild is a 25k-row GROUP
        # BY worth ~600ms on a 200k-message archive and must never sit in
        # the search response — and refresh in the background (the lock is
        # released first: _kick_vocab_rebuild takes _vocab_lock itself). The
        # generation bump on completion invalidates per-token expansions, so
        # new chat words stay reachable within a TTL, not after it.
        _kick_vocab_rebuild(table, now)
        return stale
    # Cold path (new process or TTL expired): try the on-disk snapshot
    # before paying for the fts5vocab GROUP BY. Disk validity is TTL-only
    # (see _load_vocab_disk); the in-memory tuple keeps the content row
    # count as the warm-path frequency-churn proxy.
    disk = _load_vocab_disk(table)
    if disk is not None:
        saved_rows, by_len = disk
        row_count = query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
        if saved_rows == row_count:
            with _vocab_lock:
                dbp = str(_db_path())
                _vocab_generation[dbp] = _vocab_generation.get(dbp, 0) + 1
                _vocab_cache[table] = (dbp, now, by_len, row_count)
            return by_len
        # Corpus grew since the snapshot: serve the STALE snapshot right now
        # — the GROUP BY is ~0.5-1s/table and must never sit in the search
        # response — and refresh in the background. New corpus words become
        # reachable once the rebuild lands (~1s), not after the next TTL.
        _kick_vocab_rebuild(table, now)
        return by_len
    # No snapshot at all (first run ever / snapshot cleaned): serve NO vocab
    # — the query degrades to the exact-token pattern for this one search,
    # the established fallback when the vocab is unavailable — and rebuild in
    # the background so the next search is fully fuzzy.
    _kick_vocab_rebuild(table, now)
    return None


def _kick_vocab_rebuild(table: str, loaded_at: float) -> None:
    """Start one background vocab rebuild per table; no-ops when one is
    already in flight (the pending set also guards duplicate threads when
    concurrent searches observe the same staleness)."""
    with _vocab_lock:
        if table in _vocab_rebuild_pending:
            return
        _vocab_rebuild_pending.add(table)
        threading.Thread(
            target=_rebuild_vocab, args=(table, loaded_at), daemon=True
        ).start()


def _rebuild_vocab(table: str, loaded_at: float) -> None:
    """Background vocab rebuild — keeps the search request path free of the
    fts5vocab GROUP BY. One in-flight rebuild per table."""
    try:
        _load_vocab_uncached(table, loaded_at)
    except Exception:
        logger.warning("background vocab rebuild failed for %s", table, exc_info=True)
    finally:
        with _vocab_lock:
            _vocab_rebuild_pending.discard(table)


def _load_vocab_uncached(
    table: str, now: float
) -> Optional[dict[int, list[tuple[str, int]]]]:
    """The rebuild body: recreate the fts5vocab view if needed, read the
    top-N tokens bucketed by length, and store the snapshot."""
    get_conn().execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {table}_vocab "
        f"USING fts5vocab({table}_fts, 'row')"
    )
    rows = query(
        f"SELECT term, SUM(cnt) AS n FROM {table}_vocab "
        "GROUP BY term ORDER BY n DESC LIMIT ?",
        (_VOCAB_MAX_TOKENS,),
    )
    by_len: dict[int, list[tuple[str, int]]] = {}
    for r in rows:
        term = r["term"]
        by_len.setdefault(len(term), []).append((term, int(r["n"])))
    row_count = query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
    with _vocab_lock:
        # Bump the per-DB generation so cached token expansions (which are
        # vocab-derived) invalidate when the corpus grows — chat ingestion
        # must make new words reachable within the TTL, not after it.
        dbp = str(_db_path())
        _vocab_generation[dbp] = _vocab_generation.get(dbp, 0) + 1
        _vocab_cache[table] = (dbp, now, by_len, row_count)
    _save_vocab_disk(table, row_count, by_len)
    return by_len


_ACCENT_FOLD = str.maketrans(
    {
        "á": "a", "à": "a", "â": "a", "ã": "a", "ä": "a", "å": "a",
        "é": "e", "è": "e", "ê": "e", "ë": "e",
        "í": "i", "ì": "i", "î": "i", "ï": "i",
        "ó": "o", "ò": "o", "ô": "o", "õ": "o", "ö": "o",
        "ú": "u", "ù": "u", "û": "u", "ü": "u",
        "ç": "s", "ñ": "n",
    }
)


@functools.lru_cache(maxsize=65536)
def _fold_core(word: str) -> str:
    """Phonetic fold WITHOUT the final-unstressed-vowel step: digraphs,
    hard-c, silent-h, doubling collapse. Two words whose cores are equal
    are the same word modulo spelling ('chau'~'xau', 'xauuu'~'xau',
    'katarina'~'catarina'); the vowel step is the lossy one — it merges
    distinct words ('chão'->'xau', 'não'->'nau') and must not count as
    word equality on its own."""
    w = word.lower().translate(_ACCENT_FOLD)
    w = w.replace("ph", "f").replace("th", "t").replace("ch", "x").replace("qu", "k")
    out: list[str] = []
    for i, c in enumerate(w):
        if c == "y":
            c = "i"
        elif c == "h":
            # Silent h: dropped at word start and after vowels. After s it
            # is a foreign digraph ('shaco', 'Shen') — kept unless the s
            # itself sits after a vowel, where 'sh' is just a sibilant ASR
            # artifact ('nasho' from 'nasço').
            if i > 0 and w[i - 1] == "s" and (i == 1 or w[i - 2] not in "aeiou"):
                out.append("h")
            continue
        elif c == "c":
            # Hard c: s before e/i/o (soft — diacritic-stripped ç tokens
            # keep bridging: 'aco' from 'aço' -> 'asu', 'nasco' from
            # 'nasço' -> 'nasu'); k before a/u or word end (hard — 'cata'
            # -> 'kata' folds equal to 'kata'); c before other consonants
            # ('claro' stays 'claro').
            nxt = w[i + 1] if i + 1 < len(w) else ""
            c = "s" if nxt in "eio" else ("k" if nxt in "au" or nxt == "" else "c")
        elif c == "g":
            c = "j" if i + 1 < len(w) and w[i + 1] in "ei" else "g"
        elif c == "q":
            c = "k"
        elif c == "w":
            c = "v"
        out.append(c)
    return _DEDUP_RE.sub(r"\1", "".join(out))


@functools.lru_cache(maxsize=65536)
def _phonetic_fold(word: str) -> str:
    """Lightweight grapheme→phoneme fold that bridges ASR/typo spellings.

    Reimplements the published Brazilian-Portuguese phonetic rules (Várzea
    Paulista REDECA project, carlosjordao/metaphone-ptbr) minus the vowel
    dropping — dropping vowels is exactly what kills 'yasuo'↔'e aço'. Folds:
    accents, ç→s, ñ→n, y→i, ph→f, th→t, ch→x, qu→k, c→s before e/i/o and
    c→k before a/u/word-end ('cata' -> 'kata' bridges 'kata' while 'aco'
    from 'aço' still -> 'asu'), g→j before e/i, q→k, w→v, silent h
    dropped, doubled letters
    collapsed, final unstressed e→i and o→u. h drops word-initially and
    after vowels; the sh digraph is kept when the s starts the word or
    follows a consonant ('shaco' -> 'shasu' must NOT collapse onto
    'caso'/'saco'), while sibilant artifacts after vowels still drop
    ('nasho' -> 'nashu' keeps bridging 'nasço'). Deliberately does NOT fold
    intervocallic s→z ('aço' /asu/ must stay close to 'yasuo', not 'yazu')
    and does NOT fold sh→x (the h handling is enough: 'shen'->'shen' ~
    'suen')."""
    s = _fold_core(word)
    # Final unstressed vowels fold at any length — 'e aço' needs 'e' -> 'i'
    # so the bigram key 'iasu' matches 'yasuo'.
    if s:
        if s[-1] == "e":
            s = s[:-1] + "i"
        elif s[-1] == "o":
            s = s[:-1] + "u"
    # Collapse doubled letters AFTER the final-vowel fold so the fold-created
    # doubles collapse too ('yasuo' -> 'iasuu' -> 'iasu').
    return _DEDUP_RE.sub(r"\1", s)


def _damerau_levenshtein(a: str, b: str, max_dist: int) -> Optional[int]:
    """Damerau–Levenshtein (adjacent transposition counts as one edit) with
    an early bail when the row minimum exceeds max_dist.

    Hot path of the fuzzy expansion (~17k calls per unique query token):
    min() calls and the loop-invariant transposition guards (prev2 is not
    None / i > 1 / j > 1) cost ~30us/call. The rewritten inner loop hoists
    the guards out of the j-loop and uses if-chains instead of min() —
    identical results, ~3x faster."""
    la, lb = len(a), len(b)
    if abs(la - lb) > max_dist:
        return None
    if lb == 0:
        return la if la <= max_dist else None
    prev2: Optional[list[int]] = None
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        ai = a[i - 1]
        cur = [i] + [0] * lb
        row_min = i
        cur_jm1 = i  # cur[j - 1]; cur[0] = i for the first cell
        if prev2 is not None:
            prev2_row = prev2
            for j in range(1, lb + 1):
                bj = b[j - 1]
                cost = 0 if ai == bj else 1
                v = prev[j] + 1
                t = cur_jm1 + 1
                if t < v:
                    v = t
                t = prev[j - 1] + cost
                if t < v:
                    v = t
                if ai == b[j - 2] and a[i - 2] == bj:
                    t = prev2_row[j - 2] + cost
                    if t < v:
                        v = t
                cur[j] = v
                if v < row_min:
                    row_min = v
                cur_jm1 = v
        else:
            for j in range(1, lb + 1):
                cost = 0 if ai == b[j - 1] else 1
                v = prev[j] + 1
                t = cur_jm1 + 1
                if t < v:
                    v = t
                t = prev[j - 1] + cost
                if t < v:
                    v = t
                cur[j] = v
                if v < row_min:
                    row_min = v
                cur_jm1 = v
        if row_min > max_dist:
            return None
        prev2, prev = prev, cur
    dist = prev[lb]
    return dist if dist <= max_dist else None


def _levenshtein(a: str, b: str, max_dist: int) -> Optional[int]:
    """Levenshtein distance with an early bail when it exceeds max_dist."""
    if abs(len(a) - len(b)) > max_dist:
        return None
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        row_min = i
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            if cur[j] < row_min:
                row_min = cur[j]
        if row_min > max_dist:
            return None
        prev = cur
    dist = prev[-1]
    return dist if dist <= max_dist else None


_TOKEN_BIGRAM_CAP = 3
_BIGRAM_TTL_S = 900.0
_BIGRAM_EVICT_AT = 150_000
_BIGRAM_MAX_ROWS = 300_000

# "table1|table2" -> (loaded_at, {folded_pair_key: [(raw_pair, freq), ...]}, row_counts)
_bigram_cache: dict[str, tuple[float, dict[str, list[tuple[str, int]]], list[int]]] = {}
# cache keys with a background bigram rebuild in flight (search-latency guard)
_bigram_rebuild_pending: set[str] = set()
_TOKEN_RE = re.compile(r"[^\w]+")
_DEDUP_RE = re.compile(r"(.)\1+")  # fold's doubled-letter collapse — compiled once


def _load_bigrams(tables: list[str]) -> Optional[dict[str, list[tuple[str, int]]]]:
    """Folded adjacent-pair index merged across the content tables.

    Keys are phonetic folds of adjacent token pairs joined without a space
    ('e aço' -> 'iasu'), values the canonical raw pairs ranked by frequency.
    This is the mechanism that bridges cross-token ASR errors: query
    'yasuo' folds to 'iasu' and picks up the corpus phrase 'e aço'. Built by
    scanning content rows. The scan is bounded by an entry cap that evicts
    the MOST frequent pairs when over budget — rare pairs are exactly the
    ASR families this index exists for, while the top-frequency entries are
    caption artifacts ('&gt;&gt;', '&nbsp;') or trivial phrases that exact
    matching covers anyway. Cached like the vocab (TTL + row-count gate) so
    steady chat growth does not rebuild it on every search; tables over
    _BIGRAM_MAX_ROWS are skipped.

    The rebuild is a full-corpus Python scan worth ~20s at 200k rows (207k
    message rows -> 1.5M+ fold calls) and NEVER runs inline on the request
    path: a stale-but-cached index is served as-is with the rebuild kicked
    to a background thread, and a cold (uncached) index degrades to None —
    the bigram bridge is recall-only, exact/fuzzy matching still works —
    while the rebuild runs in the background."""
    key = str(_db_path()) + "|" + "|".join(tables)
    now = time.monotonic()
    with _vocab_lock:
        hit = _bigram_cache.get(key)
        if hit and now - hit[0] < _BIGRAM_TTL_S:
            counts_now = [
                query(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"] for t in tables
            ]
            # Steady chat growth must NOT rebuild the index on every search:
            # a live stream ingests rows continuously. Reuse the cache while
            # each table drifted by <10% (or <5000 rows for small tables).
            drift_ok = all(
                abs(cur - cached) <= max(5000, int(cached * 0.10))
                for cur, cached in zip(counts_now, hit[2])
            )
            if drift_ok:
                return hit[1]
        # Stale (TTL expired or material growth since load): serve the
        # cached index and rebuild in the background. Cold (no cache yet):
        # serve None and build in the background. Either way the request
        # path never pays the full-corpus scan. The pending set keeps one
        # rebuild per key even when concurrent searches observe the same
        # staleness.
        stale = hit[1] if hit is not None else None
        if key not in _bigram_rebuild_pending:
            _bigram_rebuild_pending.add(key)
            threading.Thread(
                target=_rebuild_bigrams, args=(key, tables, now), daemon=True
            ).start()
        return stale


def _build_bigrams(tables: list[str]) -> Optional[dict[str, list[tuple[str, int]]]]:
    """The bigram rebuild body: scan every content row, fold adjacent
    token pairs, evict the frequent tail over budget. Returns the merged
    pair index, or None when every table is empty or over the row cap."""
    counts: dict[tuple[str, str], int] = {}
    for table in tables:
        row_count = query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
        if row_count == 0 or row_count > _BIGRAM_MAX_ROWS:
            continue
        # Fold each UNIQUE token once: re-folding every adjacent pair
        # re-does the same word millions of times (207k message rows on the
        # real DB -> 1.5M+ fold calls -> ~20s+ on a rebuild).
        folds: dict[str, str] = {}
        for row in query(f"SELECT text FROM {table} WHERE text IS NOT NULL"):
            toks = [t for t in _TOKEN_RE.split((row["text"] or "").lower()) if t]
            for a, b in zip(toks, toks[1:]):
                fa = folds.get(a)
                if fa is None:
                    fa = folds[a] = _phonetic_fold(a)
                fb = folds.get(b)
                if fb is None:
                    fb = folds[b] = _phonetic_fold(b)
                fk = fa + fb
                if len(fk) < 4:
                    continue
                counts[(fk, a + " " + b)] = counts.get((fk, a + " " + b), 0) + 1
                if len(counts) > _BIGRAM_EVICT_AT:
                    # Drop the most frequent entries: the rare tail is the
                    # ASR-error vocabulary, the frequent head is artifacts.
                    for k in sorted(counts, key=counts.get, reverse=True)[
                        : len(counts) // 4
                    ]:
                        del counts[k]
    merged: dict[str, list[tuple[str, int]]] = {}
    for (fk, pair), n in counts.items():
        merged.setdefault(fk, []).append((pair, n))
    for cands in merged.values():
        cands.sort(key=lambda kv: -kv[1])
    return merged or None


def _rebuild_bigrams(key: str, tables: list[str], loaded_at: float) -> None:
    """Background bigram rebuild — keeps the search request path free of
    the full-corpus scan. One in-flight rebuild per cache key."""
    try:
        merged = _build_bigrams(tables)
        row_counts = [
            query(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"] for t in tables
        ]
        with _vocab_lock:
            _bigram_cache[key] = (loaded_at, merged, row_counts)
    except Exception:
        logger.warning("background bigram rebuild failed", exc_info=True)
    finally:
        with _vocab_lock:
            _bigram_rebuild_pending.discard(key)


def _token_expansions(
    token: str,
    vocabs: list[Optional[dict[int, list[tuple[str, int]]]]],
    bigrams: Optional[dict[str, list[tuple[str, int]]]],
    merged_freq: Optional[dict[str, int]] = None,
) -> list[tuple[str, int]]:
    """Exact + fuzzy candidates for one query token as (term, distance)
    pairs, best ~8 by (distance, frequency), plus folded-bigram phrases
    pair (distance 1 — recall-only, never rank-pattern material). Tokens shorter
    than 3 chars are never expanded. Dist>=1 candidates whose MERGED corpus
    frequency exceeds _SUPPRESS_DIST1_FREQ are dropped ('cara' 3106 is chat
    spam; the legit fuzzy tail peaks at 'chaco' 570); exact/fold-equal
    (dist 0) always survive. Short tokens (4-6 chars) get prefix
    expansions, raw or phonetically folded — 'kata' reaches 'catarina'
    via fold 'katarina' startswith 'kata'. Prefix matches of tokens
    ABSENT from the corpus sit at tier 0 (a partial word is the intended
    target); prefix matches of PRESENT tokens sit at tier 1 so a complete
    word never floods the top with longer forms ('vale' must not pull
    valendo/valeu into tier 0) — capped at _TOKEN_PREFIX_CAP. Falls back
    to the bare token (distance 0) when nothing matches.

    merged_freq (optional): the merged per-term corpus frequency, built
    ONCE per query by _expand_query — rebuilding it inside this function
    for every token is the dominant cold cost (2x 25k vocab terms per
    token). Tests call this helper with 3 args, so None falls back to
    building it here."""
    if len(token) < 3:
        return [(token, 0)]
    now = time.monotonic()
    dbp = str(_db_path())
    with _vocab_lock:
        gen = _vocab_generation.get(dbp, 0)
        hit = _token_cache.get((dbp, gen, token))
        if hit and now - hit[0] < _VOCAB_TTL_S:
            return hit[1]
    best: dict[str, tuple[int, int]] = {}  # term -> (dist, -freq)
    raw_max = max(1, len(token) // 4)  # Damerau budget on the raw spelling
    fold = _phonetic_fold(token)
    fold_max = max(1, len(fold) // 3)  # Damerau budget on the folded form
    # Merged corpus frequency per term (summed across the per-table vocabs).
    # A per-table check would miss real-DB noise: 'cara' alone has freq 755
    # in messages, 3106 once transcripts join in.
    if merged_freq is None:
        merged_freq = {}
        for vocab in vocabs:
            if not vocab:
                continue
            for bucket in vocab.values():
                for term, freq in bucket:
                    merged_freq[term] = merged_freq.get(term, 0) + freq
    for vocab in vocabs:
        if not vocab:
            continue
        for n in range(max(1, len(token) - 2), len(token) + 3):
            for term, freq in vocab.get(n, ()):
                # Chat-spam noise ("kk", "c++", emoji runs) skews the top-N
                # vocab; never offer it as an expansion candidate.
                if len(term) < 3 or not term.isalnum():
                    continue
                # Length-gate both distance calls before paying for the DP —
                # ~half of the bucket window is outside the raw budget, and
                # the folded form is cached (lru on _phonetic_fold), so the
                # per-candidate cost is a dict hit + two int compares.
                d = None
                if abs(len(token) - len(term)) <= raw_max:
                    d = _damerau_levenshtein(token, term, raw_max)
                fterm = _phonetic_fold(term)
                fd = None
                if d == 0:
                    fd = 0  # raw-exact: fold can't beat dist 0
                elif abs(len(fold) - len(fterm)) <= fold_max:
                    fd = 0 if fterm == fold else _damerau_levenshtein(fold, fterm, fold_max)
                if d is None and fd is None:
                    continue
                dist = min(d if d is not None else 99, fd if fd is not None else 99)
                # Tier-0 purity: fold equality is not word equality. The
                # final-vowel step of the fold collapses distinct words
                # ('chão' -> 'xau'); a fold-equal term whose raw spelling
                # is far is a recall bridge, not an exact match — tier 1,
                # unless the equality survives without the vowel step
                # (digraph/doubling variants: 'chau' ~ 'xau' via ch->x,
                # 'xauuu' ≡ 'xau'). 'katarina'/'catarina' (raw dist 1)
                # stays 0.
                if dist == 0 and (d is None or d > 1):
                    if _fold_core(token) != _fold_core(term):
                        dist = 1
                # R3: drop dist>=1 candidates that are chat-spam common in
                # the merged corpus ('cara' 3106, 'agora' 1184) — never the
                # intended fuzzy target; the legit tail peaks at 'chaco'
                # 570. Exact/fold-equal (dist 0) always survive.
                if dist >= 1 and merged_freq.get(term, 0) > _SUPPRESS_DIST1_FREQ:
                    continue
                # R4: admission gate — a fuzzy candidate must be near on the
                # RAW spelling, not just the phonetic fold. The fold
                # collapses distinct words ('caralho'->'karalu' is raw-3
                # from 'cavalo'/'carrasco', fold-1), so fold-only neighbors
                # flood the tier-1 OR pattern with unrelated terms. Survive
                # the gate: fold-EQUAL candidates (fd == 0 — the deliberate
                # phonetic bridges: 'yasuo'~'iaso', demoted from tier 0 by
                # the purity rule above); raw <= 1 (true typos/ASR
                # variants: 'estranheza'~'estranhesa'); raw <= 2 sharing a
                # 3-char prefix (partial-word stretches: 'caraio'->'cara',
                # 'estranheza'->'estranha'); and raw <= 2 with an equal
                # CONSONANT skeleton (vowel-mishearing bridges:
                # 'nautilus'~'nutilos' — 'caralho'/'cavalo' differ in
                # consonants and stay out). Everything else is fold-collapse
                # noise and is dropped.
                if dist >= 1 and not (
                    fd == 0
                    or (
                        d is not None
                        and (
                            d <= 1
                            or (
                                d <= 2
                                and (
                                    token[:3] == term[:3]
                                    or _consonant_skeleton(token) == _consonant_skeleton(term)
                                )
                            )
                        )
                    )
                ):
                    continue
                cur = best.get(term)
                if cur is None or (dist, -freq) < cur:
                    best[term] = (dist, -freq)
    # R2: prefix expansion for SHORT tokens below the spam-frequency gate.
    # An ABSENT token is likely a partial word or mishearing ('kata'
    # reaches 'catarina' via fold 'katarina' startswith 'kata') and its
    # prefix matches sit at tier 0. A PRESENT token is a complete word:
    # its prefix matches must never tie with the exact token at tier 0
    # ('vale' would pull valendo/valeu/valerá into every 'vale' search
    # via the 3-char fold 'val') — they are offered at tier 1 instead, so
    # exact forms rank first while the partial word stays reachable.
    # Tokens above the gate ('cara' 3106) are chat spam and emit nothing.
    if 4 <= len(token) <= 6 and merged_freq.get(token, 0) <= _PREFIX_GATE_FREQ:
        pref: dict[str, int] = {}
        for vocab in vocabs:
            if not vocab:
                continue
            for n in range(len(token) + 1, len(token) + 9):
                for term, freq in vocab.get(n, ()):
                    if len(term) < 3 or not term.isalnum():
                        continue
                    if term.startswith(token) or _phonetic_fold(term).startswith(fold):
                        pref[term] = pref.get(term, 0) + freq
        if pref:
            pd = 0 if merged_freq.get(token, 0) == 0 else 1
            for term, freq in sorted(pref.items(), key=lambda kv: -kv[1])[:_TOKEN_PREFIX_CAP]:
                cur = best.get(term)
                if cur is None or (pd, -freq) < cur:
                    best[term] = (pd, -freq)  # upgrade an existing dist-1 entry
    ranked = sorted(best.items(), key=lambda kv: (kv[1][0], kv[1][1], kv[0]))
    out: list[tuple[str, int]] = [
        (term, dist) for term, (dist, _) in ranked[:_TOKEN_EXPAND_CAP]
    ]
    # The user's own token is always distance 0, even when the FTS vocab
    # lacks it (the corpus may only hold the misheard form) — otherwise the
    # tier pattern would drop the exact token and never match it.
    if not any(t == token for t, _ in out):
        out.insert(0, (token, 0))
    if bigrams:
        for pair, _freq in bigrams.get(fold, ()):
            if not any(t == pair for t, _ in out):
                out.append((pair, 1))
    with _vocab_lock:
        # Unbounded user input -> bounded cache; a clear is cheaper than LRU.
        if len(_token_cache) > 4096:
            _token_cache.clear()
        _token_cache[(dbp, gen, token)] = (now, out)
    return out


def _expand_query(q: str, tables: list[str]) -> list[tuple[str, int]]:
    """Flatten every query token's expansion into one deduped (term, dist)
    list (duplicates keep the smallest distance).

    Adjacent query tokens also look up the folded bigram index, so a
    multi-word mishearing ('e aço') resolves to the corpus pair with the
    same pronunciation ('yasuo')."""
    terms: dict[str, int] = {}
    vocabs = [v for v in (_load_vocab(t) for t in tables) if v is not None]
    if not vocabs:
        return []
    bigrams = _load_bigrams(tables)
    # One merged-frequency build per query, shared by every token expansion
    # (was rebuilt inside _token_expansions per token — the dominant cold
    # cost for multi-word queries).
    merged_freq: dict[str, int] = {}
    for vocab in vocabs:
        if not vocab:
            continue
        for bucket in vocab.values():
            for term, freq in bucket:
                merged_freq[term] = merged_freq.get(term, 0) + freq
    for w in q.split()[:_QUERY_TOKENS_EXPAND_CAP]:
        if not w:
            continue
        for t, d in _token_expansions(w, vocabs, bigrams, merged_freq):
            if d < terms.get(t, 99):
                terms[t] = d
    if bigrams:
        q_toks = [w for w in q.split() if w][:_QUERY_TOKENS_EXPAND_CAP]
        for a, b in zip(q_toks, q_toks[1:]):
            fk = _phonetic_fold(a) + _phonetic_fold(b)
            for pair, _freq in bigrams.get(fk, ()):
                if 1 < terms.get(pair, 99):
                    terms[pair] = 1
    return sorted(terms.items(), key=lambda kv: (kv[1], kv[0]))


def _fts_phrase(token: str) -> str:
    """Quote a term as an FTS5 phrase, escaping embedded quotes ('a"b' ->
    '"a""b"'). Every MATCH pattern in the pipeline goes through this — a raw
    quote in a pattern is a syntax error that used to raise OperationalError
    out of search() (a 500)."""
    return '"' + str(token).replace('"', '""') + '"'


def _fuzzy_pattern(
    q: str, tables: list[str], q_freq: Optional[dict[str, int]] = None
) -> Optional[dict[int, str]]:
    """Distance-tiered quoted-phrase MATCH patterns, {dist: OR-pattern}.

    Tier 0 holds the user's own tokens plus fold-equal matches
    ('katarina'->'catarina'); higher tiers hold distance-1/2 expansions.
    Callers run one MATCH pass per tier and merge — BM25's IDF inflates
    low-frequency terms, so a single OR pattern ranks rare noise above the
    intended matches; tiering keeps distance-0 rows ahead of everything.

    Partial-word prefix reach: a user token ABSENT from the merged corpus
    (freq 0) is a partial word or mishearing, so it additionally emits a
    native FTS5 prefix term ('"estranh"*') at tier 0 — the vocab-based
    Damerau bridge stops at 1-2 edits, leaving >= 7-char partial words
    unreachable in chat/transcript content (titles had their own substring
    pass). A PRESENT rare token (freq <= _PREFIX_GATE_FREQ) is a complete
    word: its prefix forms sit at tier 1 so they never tie with the exact
    token ('vale' must not pull valendo/valeu into tier 0). Tokens above
    the gate (chat-spam common) emit nothing.

    Returns None to fall back to the exact token pattern: no expandable
    token, vocab unavailable, or the expansion exceeding the term cap."""
    if not any(len(w) >= 3 for w in q.split()):
        return None
    try:
        terms = _expand_query(q, tables)
        if not terms or len(terms) > _MAX_EXPANDED_TERMS:
            return None
        # Ultra-common 1-2 char tokens ("da", "de", "é") are OR-pattern
        # noise: they match a large fraction of every archive. The phrase
        # and span passes keep them for adjacency — only the OR expansion
        # drops them, and only when longer terms exist (an all-short query
        # falls back to the exact pattern below).
        long_terms = [(t, d) for t, d in terms if len(t) > 2]
        if long_terms:
            terms = long_terms
        tiers: dict[int, list[str]] = {}
        for t, d in terms:
            tiers.setdefault(d, []).append(_fts_phrase(t))
        if q_freq is None:
            q_freq = {}
            for vocab in (_load_vocab(t) for t in tables):
                if not vocab:
                    continue
                for bucket in vocab.values():
                    for term, n in bucket:
                        q_freq[term] = q_freq.get(term, 0) + n
        for w in q.split():
            toks = re.findall(r"[^\W_]+", w.casefold())
            if len(toks) == 1 and 4 <= len(toks[0]) <= 40:
                freq = q_freq.get(toks[0], 0)
                if freq > _PREFIX_GATE_FREQ:
                    continue
                pref = _fts_phrase(w) + "*"
                tier = 0 if freq == 0 else 1
                if pref not in tiers.setdefault(tier, []):
                    tiers[tier].append(pref)
        return {d: " OR ".join(ts) for d, ts in sorted(tiers.items())}
    except sqlite3.Error:
        logger.warning("fuzzy expansion failed — falling back to exact pattern")
        return None


# --- helpers --------------------------------------------------------------

def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Module-level self-check: contract invariants must hold on import.
# Idempotent: scrub leftovers first so re-import (tests, reloads) never trips.
# FTS index entries cascade via the AFTER DELETE triggers on the content
# tables — scrub content rows only (external-content FTS owns no row data).
def _run_module_selfcheck() -> None:
    _conn_selfcheck = get_conn()
    _selfcheck_platform = "twitch"
    _selfcheck_video = "__archive_selfcheck__"
    with _lock:
        _sc_conn = get_conn()
        with _sc_conn:
            _sc_conn.execute("DELETE FROM messages WHERE video_id=?", (_selfcheck_video,))
            _sc_conn.execute("DELETE FROM transcripts WHERE video_id=?", (_selfcheck_video,))
            _sc_conn.execute("DELETE FROM video_aliases WHERE video_id=?", (_selfcheck_video,))
            _sc_conn.execute("DELETE FROM videos WHERE video_id=?", (_selfcheck_video,))
    insert_messages(
        _selfcheck_platform,
        _selfcheck_video,
        [{"offset_sec": 1.0, "username": "checker", "text": "arquivo local google teste"}],
    )
    # Prime the vocab caches synchronously: the request path never builds them
    # inline anymore (cold searches serve exact tokens and rebuild in the
    # background), so the fuzzy assert below would otherwise race the background
    # thread — which loses on a real 200k-row archive and crashes import.
    _load_vocab_uncached("messages", time.monotonic())
    _load_vocab_uncached("transcripts", time.monotonic())
    # Every content assert below is scoped to the self-check video: the contract
    # is "FTS finds MY row", not "my row ranks top-N corpus-wide". Unscoped
    # asserts flip randomly on large archives (500k+ rows push the row out of
    # the result window) and would crash the backend at import.
    _hits = search("local", video_id=_selfcheck_video)
    assert any(h["kind"] == "message" and h["video_id"] == _selfcheck_video for h in _hits), (
        "FTS5 search must find inserted chat rows"
    )
    _selfcheck_chat, _selfcheck_truncated = chat_window(_selfcheck_platform, _selfcheck_video, 1.0)
    assert len(_selfcheck_chat) == 1
    assert _selfcheck_truncated is False
    # Bounded panel slice: playhead-centered window + honest total row count.
    _sc_slice, _sc_total = chat_slice_for(_selfcheck_platform, _selfcheck_video, 0.5)
    assert _sc_total == 1
    assert [r["offset_sec"] for r in _sc_slice] == [1.0]
    _sc_slice_head, _sc_total_head = chat_slice_for(_selfcheck_platform, _selfcheck_video, None)
    assert _sc_total_head == 1
    assert [r["offset_sec"] for r in _sc_slice_head] == [1.0]
    # Playhead past the last row clamps to the tail instead of returning nothing.
    _sc_slice_tail, _sc_total_tail = chat_slice_for(_selfcheck_platform, _selfcheck_video, 999.0)
    assert _sc_total_tail == 1
    assert [r["offset_sec"] for r in _sc_slice_tail] == [1.0]
    assert count_messages(_selfcheck_platform, _selfcheck_video) == 1
    insert_transcript(
        _selfcheck_platform,
        _selfcheck_video,
        [
            {"seg_idx": 0, "start_sec": 0.0, "end_sec": 1.0, "text": "transcrição de teste vale"},
            {"seg_idx": 1, "start_sec": 1.0, "end_sec": 2.0, "text": "da estranheza aconteceu"},
        ],
        lang="pt-br",
    )
    assert any(
        h["kind"] == "transcript" and h["video_id"] == _selfcheck_video
        for h in search("transcrição", video_id=_selfcheck_video)
    ), "FTS5 must find transcript segments (unicode61 tokenizer)"
    # Cross-segment phrase: tokens split across two adjacent segments must be
    # found with the exact-phrase boost (FTS5 phrases cannot span rows).
    _sc_span = next(
        (
            h
            for h in search("vale da estranheza", video_id=_selfcheck_video)
            if h["video_id"] == _selfcheck_video and h["kind"] == "transcript"
        ),
        None,
    )
    assert _sc_span is not None, "phrase split across adjacent segments must match"
    assert "vale da estranheza" in _sc_span["text"].casefold(), (
        "span hit text must join both segments"
    )
    # lang contract: message hits carry lang=None; 'pt-br' normalizes to 'pt';
    # the pt filter matches tagged AND untagged (whisper) rows.
    _sc_msg_hit = next(
        h for h in _hits
        if h["video_id"] == _selfcheck_video and h["kind"] == "message"
    )
    assert _sc_msg_hit["lang"] is None, "message hits must carry lang=None"
    assert query(
        "SELECT lang FROM transcripts WHERE platform=? AND video_id=?",
        (_selfcheck_platform, _selfcheck_video),
    )[0]["lang"] == "pt", "pt-br must normalize to pt in transcripts.lang"
    assert any(
        h["video_id"] == _selfcheck_video
        for h in search("transcrição", lang="pt", video_id=_selfcheck_video)
    ), "lang='pt' must match tagged and untagged transcript rows"
    # fuzzy expansion: a misspelled token still finds the row via the FTS5 vocab.
    assert any(
        h["video_id"] == _selfcheck_video
        for h in search("googl", video_id=_selfcheck_video)
    ), "fuzzy expansion must match 'google' from 'googl'"
    # phonetic fold: c/k, ç/ss, ph/f, y/i and final unstressed vowels collapse;
    # the ASR/typo pairs from the failure corpus must fold equal or within the
    # Damerau budget on the folded form.
    assert _phonetic_fold("katarina") == "katarina"
    assert _phonetic_fold("catarina") == "katarina", "hard c before a folds to k"
    assert _phonetic_fold("cata") == _phonetic_fold("kata") == "kata"
    assert _damerau_levenshtein(_phonetic_fold("katarina"), _phonetic_fold("catarina"), 1) == 0
    assert _phonetic_fold("ambessa") == _phonetic_fold("ambeça") == "ambesa"
    assert _phonetic_fold("seraphine") == _phonetic_fold("serafine") == "serafini"
    assert _phonetic_fold("yasuo") == "iasu" and _phonetic_fold("aço") == "asu"
    # FTS5 unicode61 strips diacritics before indexing: 'aço' arrives in the
    # vocab as 'aco'. The c-before-vowel rule must fold the stripped forms the
    # same way ('aco' -> 'asu', 'nasco' -> 'nasu').
    assert _phonetic_fold("aco") == "asu" and _phonetic_fold("nasco") == "nasu"
    assert _phonetic_fold("shen") == "shen" and _phonetic_fold("suen") == "suen"
    assert _damerau_levenshtein("shen", "suen", 1) == 1, "h/u substitution must fit the budget"
    # The sh digraph survives the fold at word start / after consonants, so the
    # champion 'shaco' does not collapse onto the common words 'caso'/'saco'
    # (dist >= 1, not 0); after a vowel the sh is a sibilant artifact ('nasho' ->
    # 'nashu' still bridges 'nasço').
    assert _phonetic_fold("shaco") == "shasu"
    assert _damerau_levenshtein(_phonetic_fold("shaco"), _phonetic_fold("caso"), 2) == 2, (
        "hard c keeps 'caso' ('kasu') two edits away from 'shaco' ('shasu')"
    )
    assert _phonetic_fold("nasho") == "nasu", "sibilant sh after a vowel still drops"
    assert _damerau_levenshtein("nasus", "nasu", 1) == 1, "nasho still bridges nasço/nasus"
    assert _damerau_levenshtein("asu", "sasu", 1) == 1, "prefix insertion must fit the budget"
    assert _damerau_levenshtein("aurora", "aunara", 2) == 2, "two edits must be detected"
    assert _damerau_levenshtein("abc", "acb", 1) == 1, "adjacent transposition is one edit"
    upsert_video({
        "platform": _selfcheck_platform,
        "video_id": _selfcheck_video,
        "channel": "selfcheck",
        "title": "selfcheck",
        "canonical_key": "selfcheck-key",
    })
    set_alias(_selfcheck_platform, _selfcheck_video, "selfcheck-key")
    assert any(
        g["canonical_key"] == "selfcheck-key" for g in dedupe_view()
    ), "dedupe view must surface aliased videos"
    # spam collapse: identical consecutive rows (0 < delta <= 60 s) collapse
    # into one row; a cross-flush continuation bumps the stored row; re-sending
    # the merged row is consumed without double-counting.
    _collapse_video = "__archive_spam_selfcheck__"
    with _lock:
        _sc_conn = get_conn()
        with _sc_conn:
            _sc_conn.execute("DELETE FROM messages WHERE video_id=?", (_collapse_video,))
    _n = insert_messages(_selfcheck_platform, _collapse_video, [
        {"offset_sec": 100.0 + i * 0.5, "username": "spammer", "text": "SPAM SPAM"}
        for i in range(50)
    ])
    assert _n == 50, "accepted count must include collapsed rows"
    _sc_rows = query(
        "SELECT spam_count FROM messages WHERE platform=? AND video_id=?",
        (_selfcheck_platform, _collapse_video),
    )
    assert len(_sc_rows) == 1 and _sc_rows[0]["spam_count"] == 50, (
        "50 identical rows must collapse to one stored row with spam_count=50"
    )
    _n = insert_messages(_selfcheck_platform, _collapse_video,
                         [{"offset_sec": 125.0, "username": "spammer", "text": "SPAM SPAM"}])
    assert _n == 1, "cross-flush continuation row must be accepted"
    _sc_rows = query(
        "SELECT spam_count FROM messages WHERE platform=? AND video_id=?",
        (_selfcheck_platform, _collapse_video),
    )
    assert len(_sc_rows) == 1 and _sc_rows[0]["spam_count"] == 51, (
        "cross-flush identical row must merge into the stored row (50 -> 51)"
    )
    _n = insert_messages(_selfcheck_platform, _collapse_video,
                         [{"offset_sec": 125.0, "username": "spammer", "text": "SPAM SPAM"}])
    _sc_rows = query(
        "SELECT spam_count FROM messages WHERE platform=? AND video_id=?",
        (_selfcheck_platform, _collapse_video),
    )
    assert _n == 1 and len(_sc_rows) == 1 and _sc_rows[0]["spam_count"] == 51, (
        "re-sending the merged row must not double-merge (idempotent)"
    )
    _n = insert_messages(_selfcheck_platform, _collapse_video,
                         [{"offset_sec": 126.0, "username": "spammer", "text": "different"}])
    _sc_rows = query(
        "SELECT spam_count FROM messages WHERE platform=? AND video_id=?",
        (_selfcheck_platform, _collapse_video),
    )
    assert _n == 1 and len(_sc_rows) == 2, "different text must not collapse"
    with _lock:
        _sc_conn = get_conn()
        with _sc_conn:
            _sc_conn.execute("DELETE FROM messages WHERE video_id=?", (_collapse_video,))
    
    
    # cleanup selfcheck rows
    with _lock:
        conn = get_conn()
        with conn:
            conn.execute("DELETE FROM messages WHERE video_id=?", (_selfcheck_video,))
            conn.execute("DELETE FROM transcripts WHERE video_id=?", (_selfcheck_video,))
            conn.execute("DELETE FROM video_aliases WHERE video_id=?", (_selfcheck_video,))
            conn.execute("DELETE FROM videos WHERE video_id=?", (_selfcheck_video,))




# The DB-backed suite above (vocab loads + inserts on the REAL archive) costs
# 25-40s and used to be paid on every app boot via routers.archive -> archive_db.
# pytest enables it (backend/conftest.py sets VODRIP_ARCHIVE_SELFCHECK=1); the app
# boots with it off.
if os.environ.get("VODRIP_ARCHIVE_SELFCHECK", "0") == "1":
    _run_module_selfcheck()

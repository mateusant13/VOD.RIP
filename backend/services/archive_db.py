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
  archive_jobs  — ingest / chat_backfill / transcribe queue

DB location: %APPDATA%/VOD.RIP/archive.db (same dir as settings.json);
override with env VODRIP_ARCHIVE_DB (used by tests).
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from services.settings import _get_appdata_dir

logger = logging.getLogger(__name__)

PLATFORMS = ("youtube", "twitch", "kick")
# "stream" = YouTube was_live content from the /streams tab (recorded live
# broadcasts). Without it, _normalize_kind mapped every stream row to "vod",
# so stream VODs were indistinguishable from regular uploads in the index.
KINDS = ("vod", "clip", "short", "live", "stream")

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
                CHECK (kind IN ('vod','clip','short','live','stream')),
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
  kind       TEXT NOT NULL CHECK (kind IN ('ingest','chat_backfill','transcribe','events')),
  platform   TEXT NOT NULL,
  video_id   TEXT NOT NULL,
  status     TEXT NOT NULL DEFAULT 'queued'
             CHECK (status IN ('queued','running','done','failed')),
  progress   REAL NOT NULL DEFAULT 0,
  error      TEXT,
  priority   INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
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
    return _get_appdata_dir() / "archive.db"


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
            _ensure_lang_column(_conn)
            _ensure_spam_column(_conn)
            _ensure_message_color_column(_conn)
            _ensure_jobs_kind_events(_conn)
            _ensure_jobs_priority(_conn)
            rebuilt = _migrate_fts_contentless(_conn)
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
            " CHECK (kind IN ('vod','clip','short','live','stream'))"
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
    if not row or "stream" in (row[0] or ""):
        return
    ddl = (row[0] or "").replace(
        "CHECK (kind IN ('vod','clip','short','live'))",
        "CHECK (kind IN ('vod','clip','short','live','stream'))",
    )
    if "kind" not in ddl:
        # kind was added via ALTER TABLE (pre-kind DBs) — ALTER never touches
        # sqlite_master.sql, so the stored DDL lacks the column. Rebuild with
        # kind appended (same position ALTER would have used: last column).
        pk_idx = ddl.rfind("PRIMARY KEY")
        if pk_idx == -1:
            ddl = ddl.rstrip().rstrip(")").rstrip() + (
                ", kind TEXT NOT NULL DEFAULT 'vod'"
                " CHECK (kind IN ('vod','clip','short','live','stream')))"
            )
        else:
            ddl = ddl[:pk_idx] + (
                "  kind TEXT NOT NULL DEFAULT 'vod'"
                " CHECK (kind IN ('vod','clip','short','live','stream')),\n"
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


def _ensure_jobs_kind_events(conn: sqlite3.Connection) -> None:
    """Idempotent migration: widen archive_jobs.kind CHECK to include 'events'.

    SQLite cannot ALTER a CHECK constraint, so the table is rebuilt
    (rename -> create -> copy -> drop) only when the stored DDL lacks it."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='archive_jobs'"
    ).fetchone()
    if row and "'events'" in (row[0] or ""):
        return
    conn.execute("ALTER TABLE archive_jobs RENAME TO archive_jobs_old")
    conn.execute(
        """CREATE TABLE archive_jobs (
             id         TEXT PRIMARY KEY,
             kind       TEXT NOT NULL CHECK (kind IN ('ingest','chat_backfill','transcribe','events')),
             platform   TEXT NOT NULL,
             video_id   TEXT NOT NULL,
             status     TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued','running','done','failed')),
             progress   REAL NOT NULL DEFAULT 0,
             error      TEXT,
             created_at TEXT NOT NULL,
             updated_at TEXT NOT NULL
           )"""
    )
    conn.execute(
        "INSERT INTO archive_jobs (id, kind, platform, video_id, status, progress, error, created_at, updated_at) "
        "SELECT id, kind, platform, video_id, status, progress, error, created_at, updated_at "
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
             kind       TEXT NOT NULL CHECK (kind IN ('ingest','chat_backfill','transcribe','events')),
             platform   TEXT NOT NULL,
             video_id   TEXT NOT NULL,
             status     TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued','running','done','failed')),
             progress   REAL NOT NULL DEFAULT 0,
             error      TEXT,
             priority   INTEGER NOT NULL DEFAULT 0,
             created_at TEXT NOT NULL,
             updated_at TEXT NOT NULL
           )"""
    )
    conn.execute(
        "INSERT INTO archive_jobs (id, kind, platform, video_id, status, progress, error, priority, created_at, updated_at) "
        "SELECT id, kind, platform, video_id, status, progress, error, 0, created_at, updated_at "
        "FROM archive_jobs_old"
    )
    conn.execute("DROP TABLE archive_jobs_old")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_status_priority ON archive_jobs(status, priority, created_at)"
    )


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
           WHERE platform = ? AND channel = ? AND (original_title IS NULL OR original_title = '')
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

def insert_messages(platform: str, video_id: str, rows: Iterable[dict]) -> int:
    """Batch insert chat rows; each row: offset_sec, user_id, username, text,
    badges (list), emotes (list), ts (optional ISO).

    Spam collapse: consecutive rows with IDENTICAL username+text whose offset
    delta is within 60 s merge into a single stored row; the stored row's
    spam_count counts the merged messages (chat spam floods one row instead
    of a thousand). Collapse runs within the batch AND across flushes: the
    batch's first run merges into the LAST stored row for this video when it
    matches (chat_sinks flush every 5 s / 100 rows, so a burst spans flushes).

    Returns the ACCEPTED count — every row that arrived, collapsed or not
    (chat_sinks/base.py rows_flushed and the ingest API 'inserted' field
    build on this). Idempotent: a re-sent row whose offset is <= the stored
    row's (delta 0) is consumed without bumping spam_count, so replaying a
    flush never double-counts.

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

            for anchor, count in runs:
                conn.execute(
                    """INSERT INTO messages (platform, video_id, offset_sec,
                       user_id, username, text, badges, emotes, ts, color, spam_count)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        platform,
                        video_id,
                        float(anchor["offset_sec"]),
                        anchor.get("user_id"),
                        anchor.get("username", ""),
                        anchor["text"],
                        json.dumps(anchor.get("badges", []), ensure_ascii=False),
                        json.dumps(anchor.get("emotes", []), ensure_ascii=False),
                        anchor.get("ts"),
                        anchor.get("color"),
                        count,
                    ),
                )
                # FTS index entry is written by the messages_ai trigger.
    return accepted


def chat_window(platform: str, video_id: str, offset_sec: float, half: float = 30.0) -> list[dict]:
    rows = query(
        """SELECT * FROM messages
           WHERE platform = ? AND video_id = ?
             AND offset_sec BETWEEN ? AND ?
           ORDER BY offset_sec LIMIT 200""",
        (platform, video_id, offset_sec - half, offset_sec + half),
    )
    return [dict(r) for r in rows]


# --- preview chat panel (WS-2) --------------------------------------------

def has_transcript(platform: str, video_id: str) -> bool:
    """True when the video has at least one transcript row (cheap EXISTS)."""
    return bool(
        query(
            "SELECT 1 FROM transcripts WHERE platform = ? AND video_id = ? LIMIT 1",
            (platform, video_id),
        )
    )


def has_chat(platform: str, video_id: str) -> bool:
    """True when the video has at least one chat row (cheap EXISTS)."""
    return bool(
        query(
            "SELECT 1 FROM messages WHERE platform = ? AND video_id = ? LIMIT 1",
            (platform, video_id),
        )
    )


def transcript_offsets(platform: str, video_id: str, limit: int = 200_000) -> list[dict]:
    """Transcript rows as preview-panel payload rows, time-ordered by start_sec.

    Same transcripts table the search/transcript_for paths read; the panel
    payload only needs (offset_sec, text) per row, so the heavy word/lang
    columns are not selected."""
    rows = query(
        "SELECT start_sec AS offset_sec, text FROM transcripts "
        "WHERE platform = ? AND video_id = ? ORDER BY start_sec LIMIT ?",
        (platform, video_id, limit),
    )
    return [dict(r) for r in rows]


def chat_for(platform: str, video_id: str, limit: int = 200_000) -> list[dict]:
    """All chat rows for a video as preview-panel payload rows, time-ordered.

    Thin projection of the same messages table chat_window/insert_messages
    use; explicit ORDER BY offset_sec because live-capture inserts can land
    out of order. The (platform, video_id, offset_sec) index serves it."""
    rows = query(
        "SELECT offset_sec, text, username, spam_count, color FROM messages "
        "WHERE platform = ? AND video_id = ? ORDER BY offset_sec LIMIT ?",
        (platform, video_id, limit),
    )
    return [dict(r) for r in rows]


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


def transcript_for(platform: str, video_id: str) -> list[dict]:
    rows = query(
        "SELECT * FROM transcripts WHERE platform = ? AND video_id = ? ORDER BY seg_idx",
        (platform, video_id),
    )
    return [dict(r) for r in rows]


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

def enqueue_job(job_id: str, kind: str, platform: str, video_id: str, *, priority: int = 0) -> None:
    now = _now_iso()
    execute(
        """INSERT INTO archive_jobs (id, kind, platform, video_id, status,
           priority, created_at, updated_at) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)""",
        (job_id, kind, platform, video_id, priority, now, now),
    )


def update_job(job_id: str, *, status: Optional[str] = None,
               progress: Optional[float] = None, error: Optional[str] = None) -> None:
    sets = ["updated_at = ?"]
    params: list[Any] = [_now_iso()]
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
        "SELECT * FROM archive_jobs ORDER BY priority DESC, created_at ASC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in rows]


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


def worker_live(age_s: int = 30) -> bool:
    """True when the transcribe worker heartbeat is younger than age_s.

    Both sides of the comparison are _now_iso() output (UTC, fixed width), so
    a lexicographic compare is a valid time compare. Missing table (pre-v2
    DB) or any SQL error means no worker has ever pinged → False."""
    from datetime import datetime, timedelta, timezone

    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat(
            timespec="seconds"
        )
        return bool(
            query(
                "SELECT 1 FROM worker_heartbeats WHERE tag = 'transcribe' AND at >= ?",
                (cutoff,),
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
_PHRASE_BOOST = 1.5      # exact-phrase matches get +50% before the cross-table merge
# All-query-tokens-present (any order/position) matches rank between exact
# phrase (1.5) and the tier-0 OR noise floor (1.0). This is FTS5's implicit
# multi-word AND semantics: a row that contains every word the user typed is
# a real match; a row with only one word is partial.
_AND_BOOST = 1.25


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
    _channel_hint_out: Optional[list] = None,
) -> list[dict]:
    """BM25 across transcripts + messages. Returns unified hits ordered by
    score; each hit carries enough to seek: platform, video_id, offset_sec,
    plus the owning video's channel/title/started_at (date), video_kind and
    lang (transcripts: transcripts.lang; messages: None).

    Merge semantics: each table is fetched ~3x limit (no per-table cap below
    that), scores are normalized per table (divided by the batch max, so the
    best hit of a table scores 1.0 — BM25 scales are not comparable across
    tables), and hits are deduped by (platform, video_id) with a ~3-hit cap
    per video. When the raw query as a quoted FTS5 phrase MATCHes, those
    hits get a +50% score boost before the cross-table merge (phrase pass
    runs first, then the fuzzy OR pass, unioned by rowid — phrase wins).

    Query understanding: when no explicit channel is given and the query has
    ≥2 tokens whose FIRST token case-insensitively matches a known
    videos.channel slug, the channel filter is applied implicitly and that
    token is stripped from the query. The whole pass only runs when a
    _channel_hint_out list is passed (None = feature off, e.g. a UI that
    dismissed the hint); the matched slug (as stored in the DB) is appended
    to the box and the search router surfaces it as channel_hint.

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

    Query tokens are fuzzy-expanded from the FTS5 vocab (exact + close
    Levenshtein matches, length-filtered, capped per token and in total);
    the expansion falls back to the exact tokens when the vocab is
    unavailable or the query is huge."""
    if not q.strip():
        return []
    raw_q = q.strip()
    if channel is None and _channel_hint_out is not None:
        hint = _channel_hint_for(raw_q)
        if hint is not None:
            channel = hint
            q = " ".join(q.split()[1:]) or q
            _channel_hint_out.append(hint)
    kinds = [k for k in (k.strip().lower() for k in (kind or "").split(",")) if k in KINDS]
    platforms = (
        [p for p in (p.strip().lower() for p in platform.split(",")) if p in PLATFORMS]
        if platform
        else []
    )
    loops = (
        ("transcript", "transcripts_fts", "transcripts", "t.start_sec", "t.lang"),
        ("message", "messages_fts", "messages", "t.offset_sec", "NULL"),
    )
    if source == "chat":
        loops = loops[1:]
    elif source == "transcript":
        loops = loops[:1]
    pattern = _fuzzy_pattern(q, [t[2] for t in loops])
    if pattern is None:
        pattern = {0: " OR ".join(f'"{w}"' for w in q.split() if w) or q}
    phrase_pattern = None
    if raw_q:
        # "raw query" quoted as one FTS5 phrase; embedded quotes are escaped.
        phrase_pattern = '"' + raw_q.replace('"', '""') + '"'
    # All-tokens AND pattern: FTS5's implicit multi-word semantics. Quoted
    # tokens joined with AND match rows containing EVERY query word (any
    # order/position). 1-2 char tokens ("da") are OR-noise and phrase-only
    # (mirrors the fuzzy expansion filter) — dropping them here means
    # "vale estranheza" still finds rows that say "vale da estranheza".
    q_tokens_all = re.findall(r"[^\W_]+", raw_q.casefold())
    q_tokens = [t for t in q_tokens_all if len(t) >= 3]
    and_pattern = " AND ".join(f'"{t.replace(chr(34), chr(34) * 2)}"' for t in q_tokens) if len(q_tokens) >= 2 else None
    # Cross-segment phrase matching: multi-word queries whose tokens are
    # split across two ADJACENT transcript segments ("…vale" | "da
    # estranheza…"). FTS5 phrases cannot span rows, so the span pass scans
    # the transcript table directly (see _phrase_span_rows).
    span_tokens = q_tokens_all
    fetch = max(int(limit) * 3, 3)  # ~3x batch; no per-table cap below 3x
    merged: list[dict] = []
    for tbl_idx, (hit_kind, fts, src, offcol, langcol) in enumerate(loops):
        base = dict(
            hit_kind=hit_kind, fts=fts, src=src, offcol=offcol, langcol=langcol,
            platforms=platforms, video_id=video_id, channel=channel, kinds=kinds,
            date_from=date_from, date_to=date_to, lang=lang,
        )
        # Distance tiers: one MATCH pass per tier, unioned by rowid (lowest
        # tier wins). Scores are discounted by 0.5^tier so cross-table merges
        # prefer the intended matches over rare expansion noise.
        by_row: dict[int, dict] = {}
        for dist, tier_pat in pattern.items():
            for r in _table_search(tier_pat, fetch, **base):
                r["_tier"] = dist
                by_row.setdefault(r["_rowid"], r)
        rows = list(by_row.values())
        and_rows: dict[int, dict] = {}
        if and_pattern:
            try:
                for r in _table_search(and_pattern, fetch, **base):
                    r["_and"] = True  # all query tokens present: +25% before merging
                    and_rows[r["_rowid"]] = r
            except sqlite3.Error:
                and_rows = {}  # pattern not parseable — degrade to phrase/fuzzy
        phrase_rows: dict[int, dict] = {}
        if phrase_pattern:
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
        if hit_kind == "transcript" and len(span_tokens) >= 2:
            try:
                span_rows = _phrase_span_rows(
                    span_tokens, fetch, platforms=platforms,
                    video_id=video_id, channel=channel, kinds=kinds,
                    date_from=date_from, date_to=date_to, lang=lang,
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
            # A multi-word hit that reached only the fuzzy OR tier matched a
            # subset of the query — flag it so UIs can say "closest match".
            h["partial"] = not (phr or andf)
            h["_tbl"] = tbl_idx
            merged.append(h)
    # Video-title pass: matching titles surface saved-channel uploads that
    # have no transcript/chat yet (the channel index accumulates every
    # upload the panel has ever fetched). Included only in the "both" source
    # (titles are neither chat nor transcript). Same normalization rule as
    # the content tables: best title hit scores 1.0.
    if source == "both":
        title_rows = _titles_search(
            q,
            fetch,
            platforms=platforms,
            video_id=video_id,
            channel=channel,
            kinds=kinds,
            date_from=date_from,
            date_to=date_to,
        )
        if title_rows:
            tmax = max(r["score"] for r in title_rows)
            for r in title_rows:
                if tmax > 0:
                    r["score"] = r["score"] / tmax
                merged.append(r)
    # Dedupe by (platform, video_id), capping ~3 hits per video, then slice.
    # Normalization collapses each table's best to exactly 1.0, so two
    # tables' best hits can TIE at 1.5 after the phrase boost. Ties resolve
    # by table priority (transcripts before messages), then by raw score —
    # raw BM25 is only comparable WITHIN a table, so it must never be the
    # cross-table tie-break: table stats shift with unrelated rows, making
    # the order depend on whatever else lives in the process DB.
    per_video: dict[tuple[str, str], int] = {}
    out: list[dict] = []
    for h in sorted(merged, key=lambda h: (h["score"], h.pop("_tbl", 0), h.pop("_raw", 0.0)), reverse=True):
        key = (h["platform"], h["video_id"])
        if per_video.get(key, 0) >= _HITS_PER_VIDEO_CAP:
            continue
        per_video[key] = per_video.get(key, 0) + 1
        h.pop("_rowid", None)
        out.append(h)
    if semantic and source != "chat":
        # Concept pass: embedding-based hits lead, lexical follows (deduped
        # by video). Any embedding failure degrades to pure lexical.
        try:
            sem = _semantic_search(
                q, fetch, platforms=platforms, video_id=video_id,
                channel=channel, kinds=kinds, date_from=date_from,
                date_to=date_to, lang=lang,
            )
        except Exception:
            sem = None
        if sem:
            sem_keys = {(h["platform"], h["video_id"]) for h in sem}
            out = (sem + [h for h in out if (h["platform"], h["video_id"]) not in sem_keys])[:limit]
            return out
    return out[:limit]


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
    platforms: list[str],
    video_id: Optional[str],
    channel: Optional[str],
    kinds: list[str],
    date_from: Optional[str],
    date_to: Optional[str],
) -> list[dict]:
    """Video-title match over the videos table (folded-token coverage).

    Titles are short and the videos table is small (hundreds of rows), so a
    pure-Python pass is cheaper than an FTS5 titles index. A query token
    matches when it is a substring of a title token (or vice-versa for
    tokens ≥3 chars — "estranheza" finds "ESTRANHEZA"). Score = fraction of
    query tokens matched. ponytail: when videos grows past ~10k rows, move
    to an FTS5 external-content titles table with a unicode61 tokenizer and
    reuse the tier/merge machinery of the content tables."""
    q_tokens = _fold_tokens(q)
    # 1-2 char tokens are substring noise in titles ("da" ⊂ "day", "mudam").
    # The content passes keep them for phrase adjacency; here they only
    # match half the catalog. An all-short query simply skips the pass.
    q_tokens = [t for t in q_tokens if len(t) >= 3]
    if not q_tokens:
        return []
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
    if kinds:
        where.append(f"kind IN ({','.join('?' * len(kinds))})")
        params.extend(kinds)
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
        matched = sum(
            1
            for qt in q_tokens
            if any(qt in tt or (len(tt) >= 3 and tt in qt) for tt in toks)
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
) -> list[dict]:
    """One MATCH pass over one FTS table with the shared filter set.

    score is -bm25 (positive, higher = better); hits carry a private _rowid
    key so the tier/phrase passes can dedupe by row identity, and a _phrase
    flag set by callers that need the exact-phrase boost."""
    sql = (
        f"SELECT t.rowid AS _rowid, -bm25({fts}) AS score, "
        f"t.platform, t.video_id, {offcol} AS offset_sec, t.text, "
        f"{langcol} AS lang, "
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
    if kinds:
        sql += f" AND v.kind IN ({','.join('?' * len(kinds))})"
        params.extend(kinds)
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
    sql += f" ORDER BY score DESC LIMIT {int(fetch)}"
    return [
        {
            "kind": hit_kind,
            "platform": r["platform"],
            "video_id": r["video_id"],
            "offset_sec": r["offset_sec"],
            "text": r["text"],
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
    if kinds:
        parts.append(f"{video}.kind IN ({','.join('?' * len(kinds))})")
        params.extend(kinds)
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


def _phrase_span_rows(
    q_tokens: list[str],
    fetch: int,
    *,
    platforms: list[str],
    video_id: Optional[str],
    channel: Optional[str],
    kinds: list[str],
    date_from: Optional[str],
    date_to: Optional[str],
    lang: Optional[str],
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
    if len(q_tokens) < 2:
        return []
    long_toks = [t for t in q_tokens if len(t) >= 4]
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
        sql += " AND (" + " OR ".join("instr(lower(t.text), ?) > 0" for _ in long_toks) + ")"
        params.extend(long_toks)
    parts: list[str] = []
    _append_content_filters(
        parts, params, platforms=platforms, video_id=video_id, channel=channel,
        kinds=kinds, date_from=date_from, date_to=date_to, lang=lang,
    )
    if parts:
        sql += " AND " + " AND ".join(parts)
    sql += " ORDER BY t.video_id, t.seg_idx"
    by_video: dict[str, list[dict]] = {}
    for r in query(sql, params):
        by_video.setdefault(r["video_id"], []).append(r)
    hits: list[dict] = []
    for segs in by_video.values():
        segs.sort(key=lambda r: r["seg_idx"])
        for a, b in zip(segs, segs[1:]):
            if b["seg_idx"] != a["seg_idx"] + 1:
                continue
            a_toks = re.findall(r"[^\W_]+", a["text"].casefold())
            b_toks = re.findall(r"[^\W_]+", b["text"].casefold())
            for split in range(1, len(q_tokens)):
                prefix, suffix = q_tokens[:split], q_tokens[split:]
                if len(a_toks) < len(prefix) or len(b_toks) < len(suffix):
                    continue
                if not all(
                    _tok_eq(x, y) for x, y in zip(a_toks[-len(prefix):], prefix)
                ):
                    continue
                if not all(
                    _tok_eq(x, y) for x, y in zip(b_toks[:len(suffix)], suffix)
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


def set_transcript_embedding(transcript_id: int, vec: bytes) -> None:
    """Upsert one segment's embedding blob (float32, 384 dims)."""
    with _lock:
        with get_conn():
            get_conn().execute(
                "INSERT INTO transcript_embeddings (transcript_id, vec) VALUES (?, ?) "
                "ON CONFLICT(transcript_id) DO UPDATE SET vec = excluded.vec",
                (transcript_id, vec),
            )


_EMBED_BACKFILL_CAP = 50_000  # segments embedded inline per semantic query


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
) -> Optional[list[dict]]:
    """Concept pass over transcript embeddings: cosine scan of the filtered
    scope, segments embedded lazily (bounded per query). Returns hits shaped
    like _table_search rows with score = cosine (0..1) and a 'semantic' flag,
    or None when the embedding backend is unavailable — the caller then
    serves pure lexical results."""
    from services import archive_embed  # lazy: torch stays out of boot

    qv = archive_embed.embed_query(q)
    if qv is None:
        return None
    import numpy as np

    qv = np.asarray(qv).reshape(-1)  # (1, dim) -> (dim,)

    sql = (
        "SELECT t.id AS transcript_id, t.platform, t.video_id, t.start_sec, "
        "t.text, t.lang AS lang, e.vec AS vec, "
        "v.channel, "
        "COALESCE(NULLIF(v.original_title, ''), v.title) AS title, "
        "v.started_at AS date, v.kind AS video_kind, v.channel_language AS channel_language "
        "FROM transcripts t "
        "LEFT JOIN transcript_embeddings e ON e.transcript_id = t.id "
        "LEFT JOIN videos v ON v.platform = t.platform AND v.video_id = t.video_id "
        "WHERE 1=1"
    )
    params: list[Any] = []
    parts: list[str] = []
    _append_content_filters(
        parts, params, platforms=platforms, video_id=video_id, channel=channel,
        kinds=kinds, date_from=date_from, date_to=date_to, lang=lang,
    )
    if parts:
        sql += " AND " + " AND ".join(parts)

    def scope_rows() -> list[dict]:
        return query(sql, params)

    rows = scope_rows()
    missing = [r for r in rows if r["vec"] is None]
    if missing:
        # Lazy backfill: embed the first _EMBED_BACKFILL_CAP missing segments
        # inline, then re-read (bounded cost per query; a background index
        # job is the upgrade path for cold multi-hundred-thousand archives).
        todo = missing[:_EMBED_BACKFILL_CAP]
        vecs = archive_embed.embed_texts([r["text"] for r in todo], "passage: ")
        if vecs is None:
            return None
        for r, v in zip(todo, vecs):
            set_transcript_embedding(r["transcript_id"], v.astype("<f4").tobytes())
        rows = scope_rows()
    scored: list[tuple[float, dict]] = []
    for r in rows:
        if r["vec"] is None:
            continue
        v = np.frombuffer(r["vec"], dtype="<f4")
        if v.shape[0] != qv.shape[0]:
            continue
        scored.append((float(np.dot(v, qv)), r))
    scored.sort(key=lambda x: -x[0])
    out: list[dict] = []
    per_video: dict[tuple[str, str], int] = {}
    for cos, r in scored:
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

    Fires only for queries with ≥2 tokens (a bare channel query would strip
    itself to nothing). Returns the slug exactly as stored in videos.channel
    so the UI can render a canonical chip."""
    tokens = q.split()
    if len(tokens) < 2:
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
_VOCAB_TTL_S = 300.0
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
            hit = None  # rows changed since load — rebuild the vocab
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
    s = "".join(out)
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
    an early bail when the row minimum exceeds max_dist."""
    if abs(len(a) - len(b)) > max_dist:
        return None
    prev2: Optional[list[int]] = None
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        row_min = i
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if (
                prev2 is not None
                and i > 1
                and j > 1
                and a[i - 1] == b[j - 2]
                and a[i - 2] == b[j - 1]
            ):
                cur[j] = min(cur[j], prev2[j - 2] + cost)
            if cur[j] < row_min:
                row_min = cur[j]
        if row_min > max_dist:
            return None
        prev2, prev = prev, cur
    dist = prev[-1]
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
    _BIGRAM_MAX_ROWS are skipped."""
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
            hit = None  # material growth since load — rebuild the index
    counts: dict[tuple[str, str], int] = {}
    for table in tables:
        row_count = query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
        if row_count == 0 or row_count > _BIGRAM_MAX_ROWS:
            continue
        # Fold each UNIQUE token once: re-folding every adjacent pair
        # re-does the same word millions of times (207k message rows on the
        # real DB -> 1.5M+ fold calls -> ~20s+ on the first search).
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
    row_counts = [
        query(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"] for t in tables
    ]
    with _vocab_lock:
        _bigram_cache[key] = (now, merged, row_counts)
    return merged or None


def _token_expansions(
    token: str,
    vocabs: list[Optional[dict[int, list[tuple[str, int]]]]],
    bigrams: Optional[dict[str, list[tuple[str, int]]]],
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
    to the bare token (distance 0) when nothing matches."""
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
    merged_freq: dict[str, int] = {}
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
                d = _damerau_levenshtein(token, term, raw_max)
                fterm = _phonetic_fold(term)
                fd = 0 if fterm == fold else _damerau_levenshtein(fold, fterm, fold_max)
                if d is None and fd is None:
                    continue
                dist = min(d if d is not None else 99, fd if fd is not None else 99)
                # R3: drop dist>=1 candidates that are chat-spam common in
                # the merged corpus ('cara' 3106, 'agora' 1184) — never the
                # intended fuzzy target; the legit tail peaks at 'chaco'
                # 570. Exact/fold-equal (dist 0) always survive.
                if dist >= 1 and merged_freq.get(term, 0) > _SUPPRESS_DIST1_FREQ:
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
    for w in q.split():
        if not w:
            continue
        for t, d in _token_expansions(w, vocabs, bigrams):
            if d < terms.get(t, 99):
                terms[t] = d
    if bigrams:
        q_toks = [w for w in q.split() if w]
        for a, b in zip(q_toks, q_toks[1:]):
            fk = _phonetic_fold(a) + _phonetic_fold(b)
            for pair, _freq in bigrams.get(fk, ()):
                if 1 < terms.get(pair, 99):
                    terms[pair] = 1
    return sorted(terms.items(), key=lambda kv: (kv[1], kv[0]))


def _fuzzy_pattern(q: str, tables: list[str]) -> Optional[dict[int, str]]:
    """Distance-tiered quoted-phrase MATCH patterns, {dist: OR-pattern}.

    Tier 0 holds the user's own tokens plus fold-equal matches
    ('katarina'->'catarina'); higher tiers hold distance-1/2 expansions.
    Callers run one MATCH pass per tier and merge — BM25's IDF inflates
    low-frequency terms, so a single OR pattern ranks rare noise above the
    intended matches; tiering keeps distance-0 rows ahead of everything.
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
            tiers.setdefault(d, []).append(t)
        return {d: " OR ".join(f'"{t}"' for t in ts) for d, ts in sorted(tiers.items())}
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
# Every content assert below is scoped to the self-check video: the contract
# is "FTS finds MY row", not "my row ranks top-N corpus-wide". Unscoped
# asserts flip randomly on large archives (500k+ rows push the row out of
# the result window) and would crash the backend at import.
_hits = search("local", video_id=_selfcheck_video)
assert any(h["kind"] == "message" and h["video_id"] == _selfcheck_video for h in _hits), (
    "FTS5 search must find inserted chat rows"
)
assert len(chat_window(_selfcheck_platform, _selfcheck_video, 1.0)) == 1
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

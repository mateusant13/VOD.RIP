"""Local archive store — SQLite WAL + FTS5 for chat, transcripts, and video index.

The "local Google" contract every ingestion/chat/transcription/search slice
builds against. Single-writer design: the app is a desktop process, so one
module-level connection guarded by a lock is sufficient.

Storage layout (all offsets are seconds into the stream, monotonic):
  videos        — one row per (platform, video_id); canonical_key dedupes
                  the same live/VOD simulcast across platforms
  messages      — chat rows, append-only; FTS5 contentless index
  transcripts   — word-timestamped segments; FTS5 contentless index
  video_aliases — manual canonical_key overrides for cross-platform dedupe
  archive_jobs  — ingest / chat_backfill / transcribe queue

DB location: %APPDATA%/VOD.RIP/archive.db (same dir as settings.json);
override with env VODRIP_ARCHIVE_DB (used by tests).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Optional

from services.settings import _get_appdata_dir

logger = logging.getLogger(__name__)

PLATFORMS = ("youtube", "twitch", "kick")
KINDS = ("vod", "clip", "short", "live")

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
  status        TEXT NOT NULL DEFAULT 'known'
                CHECK (status IN ('known','downloading','ready','failed')),
  kind          TEXT NOT NULL DEFAULT 'vod'
                CHECK (kind IN ('vod','clip','short','live')),
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
  ts         TEXT
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
  words_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_transcripts_video ON transcripts(platform, video_id, start_sec);

CREATE VIRTUAL TABLE IF NOT EXISTS transcripts_fts USING fts5(
  text, content='transcripts', content_rowid='id');

CREATE TABLE IF NOT EXISTS video_aliases (
  platform      TEXT NOT NULL,
  video_id      TEXT NOT NULL,
  canonical_key TEXT NOT NULL,
  note          TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (platform, video_id)
);

CREATE TABLE IF NOT EXISTS archive_jobs (
  id         TEXT PRIMARY KEY,
  kind       TEXT NOT NULL CHECK (kind IN ('ingest','chat_backfill','transcribe')),
  platform   TEXT NOT NULL,
  video_id   TEXT NOT NULL,
  status     TEXT NOT NULL DEFAULT 'queued'
             CHECK (status IN ('queued','running','done','failed')),
  progress   REAL NOT NULL DEFAULT 0,
  error      TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON archive_jobs(status, created_at);
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
    """Idempotent migration: add videos.kind (vod|clip|short|live, 'vod' default).

    Safe on pre-kind DBs: ADD COLUMN with NOT NULL DEFAULT is immediate and
    backfills existing rows with 'vod'. PRAGMA table_info guard makes repeated
    calls (re-imports, reloads) no-ops."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(videos)")}
    if "kind" not in cols:
        conn.execute(
            "ALTER TABLE videos ADD COLUMN kind TEXT NOT NULL DEFAULT 'vod'"
            " CHECK (kind IN ('vod','clip','short','live'))"
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
        "status": video.get("status", "known"),
        "updated_at": now,
    }
    execute(
        """INSERT INTO videos (platform, video_id, channel, title, started_at,
           ended_at, duration_sec, archive_path, canonical_key, status, kind,
           created_at, updated_at)
           VALUES (:platform, :video_id, :channel, :title, :started_at,
           :ended_at, :duration_sec, :archive_path, :canonical_key, :status,
           :kind, :created_at, :updated_at)
           ON CONFLICT(platform, video_id) DO UPDATE SET
             channel=excluded.channel, title=excluded.title,
             started_at=excluded.started_at, ended_at=excluded.ended_at,
             duration_sec=excluded.duration_sec,
             archive_path=excluded.archive_path,
             canonical_key=excluded.canonical_key,
             status=excluded.status, kind=excluded.kind,
             updated_at=excluded.updated_at""",
        {**row, "created_at": now},
    )


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


# --- messages -------------------------------------------------------------

def insert_messages(platform: str, video_id: str, rows: Iterable[dict]) -> int:
    """Batch insert chat rows; each row: offset_sec, user_id, username, text,
    badges (list), emotes (list), ts (optional ISO). Returns count inserted."""
    conn = get_conn()
    count = 0
    with _lock:
        with conn:  # transaction
            for r in rows:
                conn.execute(
                    """INSERT INTO messages (platform, video_id, offset_sec,
                       user_id, username, text, badges, emotes, ts)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        platform,
                        video_id,
                        float(r["offset_sec"]),
                        r.get("user_id"),
                        r.get("username", ""),
                        r["text"],
                        json.dumps(r.get("badges", []), ensure_ascii=False),
                        json.dumps(r.get("emotes", []), ensure_ascii=False),
                        r.get("ts"),
                    ),
                )
                # FTS index entry is written by the messages_ai trigger.
                count += 1
    return count


def chat_window(platform: str, video_id: str, offset_sec: float, half: float = 30.0) -> list[dict]:
    rows = query(
        """SELECT * FROM messages
           WHERE platform = ? AND video_id = ?
             AND offset_sec BETWEEN ? AND ?
           ORDER BY offset_sec LIMIT 200""",
        (platform, video_id, offset_sec - half, offset_sec + half),
    )
    return [dict(r) for r in rows]


# --- transcripts ----------------------------------------------------------

def insert_transcript(platform: str, video_id: str, segments: Iterable[dict]) -> int:
    """Segments: seg_idx, start_sec, end_sec, text, words (list of
    {word, start, end}). Returns count inserted."""
    conn = get_conn()
    count = 0
    with _lock:
        with conn:
            for seg in segments:
                conn.execute(
                    """INSERT INTO transcripts (platform, video_id, seg_idx,
                       start_sec, end_sec, text, words_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        platform,
                        video_id,
                        int(seg["seg_idx"]),
                        float(seg["start_sec"]),
                        float(seg["end_sec"]),
                        seg["text"],
                        json.dumps(seg.get("words", []), ensure_ascii=False),
                    ),
                )
                # FTS index entry is written by the transcripts_ai trigger.
                count += 1
    return count


def transcript_for(platform: str, video_id: str) -> list[dict]:
    rows = query(
        "SELECT * FROM transcripts WHERE platform = ? AND video_id = ? ORDER BY seg_idx",
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


# --- jobs -----------------------------------------------------------------

def enqueue_job(job_id: str, kind: str, platform: str, video_id: str) -> None:
    now = _now_iso()
    execute(
        """INSERT INTO archive_jobs (id, kind, platform, video_id, status,
           created_at, updated_at) VALUES (?, ?, ?, ?, 'queued', ?, ?)""",
        (job_id, kind, platform, video_id, now, now),
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
    rows = query("SELECT * FROM archive_jobs ORDER BY created_at DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]


# --- search ---------------------------------------------------------------

def search(
    q: str,
    *,
    platform: Optional[str] = None,
    channel: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    kind: Optional[str] = None,
    limit: int = 20,
) -> list[dict]:
    """BM25 across transcripts + messages. Returns unified hits ordered by
    score; each hit carries enough to seek: platform, video_id, offset_sec,
    plus the owning video's channel/title/started_at (date) and video_kind.

    Filters: platform/channel exact; kind is a comma-separated list
    ("vod,clip" → IN clause, unknown values dropped); date_from/date_to are
    inclusive YYYY-MM-DD bounds on the video's started_at date part. The
    videos join is LEFT so rows whose video was never indexed still surface
    when no video-backed filter is active."""
    if not q.strip():
        return []
    kinds = [k for k in (k.strip().lower() for k in (kind or "").split(",")) if k in KINDS]
    platforms = (
        [p for p in (p.strip().lower() for p in platform.split(",")) if p in PLATFORMS]
        if platform
        else []
    )
    pattern = " OR ".join(f'"{w}"' for w in q.split() if w) or q
    hits: list[dict] = []
    for hit_kind, fts, src, offcol in (
        ("transcript", "transcripts_fts", "transcripts", "t.start_sec"),
        ("message", "messages_fts", "messages", "t.offset_sec"),
    ):
        sql = (
            f"SELECT t.rowid, bm25({fts}) AS score, "
            f"t.platform, t.video_id, {offcol} AS offset_sec, t.text, "
            "v.channel, v.title, v.started_at AS date, v.kind AS video_kind "
            f"FROM {fts} f JOIN {src} t ON t.id = f.rowid "
            "LEFT JOIN videos v ON v.platform = t.platform AND v.video_id = t.video_id "
            f"WHERE {fts} MATCH ?"
        )
        params: list[Any] = [pattern]
        if platforms:
            sql += f" AND t.platform IN ({','.join('?' * len(platforms))})"
            params.extend(platforms)
        if channel:
            sql += " AND v.channel = ?"
            params.append(channel)
        if kinds:
            sql += f" AND v.kind IN ({','.join('?' * len(kinds))})"
            params.extend(kinds)
        if date_from:
            sql += " AND date(v.started_at) >= date(?)"
            params.append(date_from)
        if date_to:
            sql += " AND date(v.started_at) <= date(?)"
            params.append(date_to)
        sql += f" ORDER BY score LIMIT {int(limit)}"
        for r in query(sql, params):
            hits.append({
                "kind": hit_kind,
                "platform": r["platform"],
                "video_id": r["video_id"],
                "offset_sec": r["offset_sec"],
                "text": r["text"],
                "score": r["score"],
                "channel": r["channel"],
                "title": r["title"],
                "date": r["date"],
                "video_kind": r["video_kind"],
            })
    hits.sort(key=lambda h: h["score"])
    return hits[:limit]


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
_hits = search("local")
assert any(h["kind"] == "message" and h["video_id"] == _selfcheck_video for h in _hits), (
    "FTS5 search must find inserted chat rows"
)
assert len(chat_window(_selfcheck_platform, _selfcheck_video, 1.0)) == 1
insert_transcript(
    _selfcheck_platform,
    _selfcheck_video,
    [{"seg_idx": 0, "start_sec": 0.0, "end_sec": 1.0, "text": "transcrição de teste"}],
)
assert any(
    h["kind"] == "transcript" and h["video_id"] == _selfcheck_video
    for h in search("transcrição")
), "FTS5 must find transcript segments (unicode61 tokenizer)"
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
# cleanup selfcheck rows
with _lock:
    conn = get_conn()
    with conn:
        conn.execute("DELETE FROM messages WHERE video_id=?", (_selfcheck_video,))
        conn.execute("DELETE FROM transcripts WHERE video_id=?", (_selfcheck_video,))
        conn.execute("DELETE FROM video_aliases WHERE video_id=?", (_selfcheck_video,))
        conn.execute("DELETE FROM videos WHERE video_id=?", (_selfcheck_video,))

"""Session cookie bridge store — per-platform keep-listed cookies, encrypted at rest.

The local extension (Get-cookies.txt-LOCALLY fork) pushes cookie diffs for
kick.com / youtube.com / twitch.tv; this service keeps only the names each
downloader actually needs (see KEEP_LISTS) and stores values AES-encrypted
via token_crypto (machine-derived key, no user password).

Storage: same SQLite file as archive_db (%APPDATA%/VOD.RIP/archive.db, or env
VODRIP_ARCHIVE_DB) but in its own table/connection — the file is shared with
the archive store while each module keeps the single-writer-per-connection
pattern that already works in this app. Override with VODRIP_COOKIE_DB (used
by the module self-check to stay off the real DB).
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from services.token_crypto import decrypt_token, encrypt_token
from services.settings import _get_appdata_dir

logger = logging.getLogger(__name__)

PLATFORMS = ("youtube", "twitch", "kick")

# Per-platform keep-lists — the ONLY cookie names the bridge accepts/stores.
# kick.com: auth_token (+ g_session keeps the Kick session alive).
# youtube.com: the SID family the innertube client needs (plus the
# visitor-id cookie yt-dlp falls back to when the account is logged out).
# twitch.tv: auth-token (session token) + sp (device id, needed for GQL requests).
KEEP_LISTS: dict[str, frozenset[str]] = {
    "kick": frozenset({"auth_token", "g_session"}),
    "youtube": frozenset({
        "SID", "__Secure-1PAPISID", "__Secure-3PAPISID", "APISID", "SAPISID",
        "HSID", "SSID", "__Secure-1PSID", "__Secure-3PSID", "VISITOR_INFO1_LIVE",
    }),
    "twitch": frozenset({"auth-token", "sp"}),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS session_cookies (
  platform   TEXT NOT NULL CHECK (platform IN ('youtube','twitch','kick')),
  name       TEXT NOT NULL,
  domain     TEXT NOT NULL,
  path       TEXT NOT NULL DEFAULT '/',
  secure     INTEGER NOT NULL DEFAULT 0,
  http_only  INTEGER NOT NULL DEFAULT 0,
  value_enc  TEXT NOT NULL,
  expires    REAL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (platform, name, domain)
);
CREATE INDEX IF NOT EXISTS idx_session_cookies_platform ON session_cookies(platform);
"""


def _db_path() -> Path:
    override = os.environ.get("VODRIP_COOKIE_DB", "").strip()
    if override:
        return Path(override)
    return _get_appdata_dir() / "archive.db"


_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None
_schema_ready = False


def get_conn() -> sqlite3.Connection:
    """Shared WAL connection, schema initialized on first use (same pattern
    as archive_db — two module-level connections to one WAL file are safe)."""
    global _conn, _schema_ready
    with _lock:
        if _conn is None:
            path = _db_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            # check_same_thread=False: download workers + ASGI request threads
            # (and pytest's per-test loop threads) all share this one lazy
            # connection; every access is serialized by _lock below, same
            # arrangement archive_db.py already uses for its worker threads.
            conn = sqlite3.connect(str(path), timeout=10.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA synchronous=NORMAL")
            _conn = conn
        if not _schema_ready:
            # Schema init lives here (once): the connect branch above used to
            # executescript() too, doubling the cost of the first get_conn.
            _conn.executescript(SCHEMA)
            _conn.commit()
            _schema_ready = True
        return _conn


def _execute(sql: str, params: Any = ()) -> sqlite3.Cursor:
    with _lock:
        cur = get_conn().execute(sql, params)
        get_conn().commit()
        return cur


def _query(sql: str, params: Any = ()) -> list[sqlite3.Row]:
    with _lock:
        return get_conn().execute(sql, params).fetchall()


# --- platform mapping ------------------------------------------------------

_DOMAIN_SUFFIXES = (("kick.com", "kick"), ("youtube.com", "youtube"), ("twitch.tv", "twitch"))


def platform_for_domain(domain: str) -> Optional[str]:
    """Map a cookie domain to a platform, or None for unrelated domains.

    Accepts the bare host, a leading-dot domain (`.youtube.com`) and
    subdomains (`www.youtube.com`, `m.twitch.tv`). Exact/`.`-boundary
    suffix match so `notyoutube.com` is never misread as youtube.com.
    """
    d = (domain or "").strip().lower().lstrip(".")
    if not d:
        return None
    for suffix, platform in _DOMAIN_SUFFIXES:
        if d == suffix or d.endswith("." + suffix):
            return platform
    return None


def is_kept(platform: str, name: str) -> bool:
    return (name or "") in KEEP_LISTS.get(platform, frozenset())


# --- storage ---------------------------------------------------------------

def upsert_cookies(cookies: list[dict]) -> tuple[int, int]:
    """Store keep-listed, platform-normalized cookies.

    Drops (and returns as ``dropped``) cookies for unrelated domains or
    names outside the platform keep-list — the extension pre-filters, this
    is the authoritative second gate. Returns (accepted, dropped).
    """
    accepted = 0
    dropped = 0
    with _lock:
        conn = get_conn()
        with conn:  # one transaction per POST
            for c in cookies or []:
                if not isinstance(c, dict):
                    dropped += 1
                    continue
                name = (c.get("name") or "").strip()
                domain = (c.get("domain") or "").strip().lower()
                value = c.get("value")
                if not name or not domain or value is None:
                    dropped += 1
                    continue
                platform = platform_for_domain(domain)
                if platform is None or not is_kept(platform, name):
                    dropped += 1
                    continue
                expires = c.get("expirationDate")
                try:
                    expires_f = float(expires) if expires is not None else None
                except (TypeError, ValueError):
                    expires_f = None
                now = _now_iso()
                conn.execute(
                    """INSERT INTO session_cookies
                       (platform, name, domain, path, secure, http_only,
                        value_enc, expires, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(platform, name, domain) DO UPDATE SET
                         path=excluded.path, secure=excluded.secure,
                         http_only=excluded.http_only,
                         value_enc=excluded.value_enc,
                         expires=excluded.expires, updated_at=excluded.updated_at""",
                    (
                        platform,
                        name,
                        domain,
                        str(c.get("path") or "/"),
                        1 if c.get("secure") else 0,
                        1 if c.get("httpOnly") else 0,
                        encrypt_token(str(value)),
                        expires_f,
                        now,
                    ),
                )
                accepted += 1
    return accepted, dropped


def clear(platform: Optional[str] = None) -> None:
    """Delete stored cookies (all platforms, or one). Used by self-check and
    a future 'disconnect bridge' affordance."""
    if platform:
        _execute("DELETE FROM session_cookies WHERE platform = ?", (platform,))
    else:
        _execute("DELETE FROM session_cookies")


# --- expiry ----------------------------------------------------------------

_last_purge_mono = 0.0
# Lazy purge throttle: cookie expiry is a slow process (session cookies never
# expire; SID lives for ~2 years), so one DELETE per minute per process is far
# more than enough. Reads additionally filter expired rows in SQL, so the
# throttle window never serves a stale cookie.
_PURGE_INTERVAL_S = 60.0


def purge_expired() -> int:
    """Delete rows whose expirationDate has passed; returns rows removed."""
    with _lock:
        cur = get_conn().execute(
            "DELETE FROM session_cookies WHERE expires IS NOT NULL AND expires <= ?",
            (time.time(),),
        )
        get_conn().commit()
        return cur.rowcount


def _purge_expired_lazy() -> None:
    """Throttled purge — runs at most once per _PURGE_INTERVAL_S per process."""
    global _last_purge_mono
    with _lock:
        if time.monotonic() - _last_purge_mono < _PURGE_INTERVAL_S:
            return
        _last_purge_mono = time.monotonic()
    purge_expired()


def counts() -> dict[str, int]:
    """Live (non-expired) cookie counts per platform."""
    _purge_expired_lazy()
    rows = _query(
        "SELECT platform, COUNT(*) AS n FROM session_cookies "
        "WHERE expires IS NULL OR expires > ? GROUP BY platform",
        (time.time(),),
    )
    return {r["platform"]: r["n"] for r in rows}


def status() -> dict[str, dict[str, object]]:
    """Per-platform {count, lastGrabAt, expiredCount} for the /status endpoint.

    ``count`` counts live rows (expired rows are never served), ``expiredCount``
    reports how many rows are past their expirationDate (extension push may
    still be on the way), ``lastGrabAt`` is the newest updated_at (UTC ISO).
    """
    _purge_expired_lazy()
    now = time.time()
    rows = _query(
        """SELECT platform,
                   SUM(CASE WHEN expires IS NOT NULL AND expires <= ? THEN 0 ELSE 1 END) AS live,
                   SUM(CASE WHEN expires IS NOT NULL AND expires <= ? THEN 1 ELSE 0 END) AS expired,
                   MAX(updated_at) AS last_grab
            FROM session_cookies GROUP BY platform""",
        (now, now),
    )
    out: dict[str, dict[str, object]] = {}
    for r in rows:
        out[r["platform"]] = {
            "count": int(r["live"] or 0),
            "lastGrabAt": r["last_grab"],
            "expiredCount": int(r["expired"] or 0),
        }
    return out


def list_cookies(platform: str) -> list[dict]:
    """Keep-listed rows for one platform with values decrypted.

    Expired rows are skipped (and lazily purged) — a stale SID must never
    reach a consumer.
    """
    _purge_expired_lazy()
    rows = _query(
        "SELECT * FROM session_cookies WHERE platform = ? "
        "AND (expires IS NULL OR expires > ?) ORDER BY name, domain",
        (platform, time.time()),
    )
    out = []
    for r in rows:
        out.append({
            "name": r["name"],
            "domain": r["domain"],
            "path": r["path"],
            "secure": bool(r["secure"]),
            "httpOnly": bool(r["http_only"]),
            "value": decrypt_token(r["value_enc"]),
            "expirationDate": r["expires"],
        })
    return out


# --- Netscape cookies.txt serialization ------------------------------------

NETSCAPE_HEADER = (
    "# Netscape HTTP Cookie File",
    "# https://curl.haxx.se/rfc/cookie_spec.html",
    "# This is a generated file! Do not edit.",
    "",
)


def pull_netscape(platform: str) -> str:
    """Serialize keep-listed cookies for one platform as a cookies.txt.

    ``#HttpOnly_`` prefix on the domain column for httpOnly cookies (curl
    requirement), TRUE/FALSE flags, expirationDate as integer seconds (0 for
    session cookies), leading-dot domains flagged as subdomain-wide.
    """
    lines = list(NETSCAPE_HEADER)
    for c in list_cookies(platform):
        domain = c["domain"]
        if c["httpOnly"]:
            domain = f"#HttpOnly_{domain}"
        include_sub = "TRUE" if c["domain"].startswith(".") else "FALSE"
        secure = "TRUE" if c["secure"] else "FALSE"
        expires = str(int(c["expirationDate"])) if c["expirationDate"] else "0"
        lines.append("\t".join(
            (domain, include_sub, c["path"] or "/", secure, expires, c["name"], c["value"])
        ))
    lines.append("")
    return "\n".join(lines)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Module self-check (no network): contract invariants must hold on import.
# Runs against a throwaway DB so the real archive.db is never touched. Gated
# behind VODRIP_COOKIE_SELFCHECK=1 (pytest sets it in backend/conftest.py):
# the suite costs ~1.2s (crypto + sqlite schema) and used to be paid on every
# app boot via deps -> download_manager -> youtube_auth -> cookie_store.
if os.environ.get("VODRIP_COOKIE_SELFCHECK", "0") == "1":
    # --- module self-check (no network) ---------------------------------------
    # Contract invariants must hold on import. Runs against a throwaway DB so the
    # real archive.db is never touched.
    import tempfile  # noqa: E402
    
    _selfcheck_env = os.environ.get("VODRIP_COOKIE_DB")
    # Disk hygiene: mktemp leaked a .db per import when a process was killed
    # mid-selfcheck (~120 in %TEMP%). A TemporaryDirectory is cleaned on normal
    # exits (finally + GC); kill leftovers share the prefix and are swept by the
    # startup hygiene pass (services/disk_hygiene.py).
    _selfcheck_dir = tempfile.TemporaryDirectory(prefix="vodrip_cookie_selfcheck_")
    _tmp_db = str(Path(_selfcheck_dir.name) / "selfcheck.db")
    os.environ["VODRIP_COOKIE_DB"] = _tmp_db
    try:
        # crypto roundtrip
        _secret = "sup3r-s3cret-kick-token"
        assert decrypt_token(encrypt_token(_secret)) == _secret, "crypto roundtrip must hold"
    
        # keep-list filtering: random youtube cookies are dropped, SID is kept
        _a, _d = upsert_cookies([
            {"name": "SID", "domain": ".youtube.com", "path": "/", "secure": True,
             "httpOnly": True, "value": "youtube-sid-value", "expirationDate": 1900000000},
            {"name": "random_tracker", "domain": ".youtube.com", "value": "nope"},
            {"name": "auth_token", "domain": ".kick.com", "value": "kick-token",
             "httpOnly": True, "secure": True},
            {"name": "auth-token", "domain": ".twitch.tv", "value": "twitch-token"},
            {"name": "SID", "domain": "evil-youtube.com.evil.example", "value": "phish"},
        ])
        assert _a == 3 and _d == 2, f"keep-list filter must drop 2 of 5, got accepted={_a} dropped={_d}"
        _y = list_cookies("youtube")
        assert [c["name"] for c in _y] == ["SID"], f"youtube keep-list must hold only SID, got {_y}"
        assert _y[0]["value"] == "youtube-sid-value", "decrypted value must round-trip"
    
        # netscape serialization: httpOnly prefix + flags
        _txt = pull_netscape("kick")
        assert _txt.startswith("# Netscape HTTP Cookie File"), "netscape header required"
        assert "#HttpOnly_.kick.com\tTRUE\t/\tTRUE\t0\tauth_token\tkick-token" in _txt, (
            "httpOnly cookie must serialize with #HttpOnly_ prefix"
        )
        _t = pull_netscape("twitch")
        assert "auth-token\ttwitch-token" in _t.replace("#HttpOnly_", ""), "twitch pull must contain auth-token"
        assert "random_tracker" not in pull_netscape("youtube"), "dropped cookies must never appear"
    
        # platform mapping boundaries
        assert platform_for_domain("kick.com") == "kick"
        assert platform_for_domain(".youtube.com") == "youtube"
        assert platform_for_domain("www.twitch.tv") == "twitch"
        assert platform_for_domain("notyoutube.com") is None
        assert platform_for_domain("evil.example") is None
    finally:
        clear()
        # Close the selfcheck connection BEFORE deleting its DB — on Windows an
        # open sqlite handle blocks the unlink. (This ordering bug is exactly
        # what leaked the old mktemp .db files into %TEMP%.)
        if _conn is not None:
            try:
                _conn.close()
            except sqlite3.Error:
                pass
        _conn = None
        _schema_ready = False
        try:
            os.unlink(_tmp_db)
        except OSError:
            pass
        if _selfcheck_env is None:
            os.environ.pop("VODRIP_COOKIE_DB", None)
        else:
            os.environ["VODRIP_COOKIE_DB"] = _selfcheck_env
        # Drop the connection that points at the deleted temp DB — the next
        # get_conn() (the real app path) must open a fresh one on the real file.
        try:
            _selfcheck_dir.cleanup()
        except OSError:
            pass

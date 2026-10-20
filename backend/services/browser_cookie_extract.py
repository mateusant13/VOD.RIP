"""Read and decrypt cookies from Chromium-based browsers (Chrome, Edge, Brave).

Decryption layouts handled:

* **Legacy (pre-Chrome 80)**: ``encrypted_value`` is DPAPI-wrapped directly and
  is unwrapped with ``CryptUnprotectData``.
* **Chrome 80+**: ``encrypted_value`` starts with ``v10``/``v11``. The 32-byte
  AES-256-GCM key lives in ``Local State`` under ``os_crypt.encrypted_key``
  (base64, 5-byte ``DPAPI`` prefix, ``CryptUnprotectData`` unwrapped). Nonce =
  the 12 bytes after the prefix; GCM tag = the trailing 16 bytes.
* **App-bound encryption** (``v20`` prefix, or ``v11`` values that do not
  decrypt with the DPAPI key) is not supported; the affected platform is
  reported in the return reason.

The ``Cookies`` database and its ``-wal``/``-shm`` siblings are copied to a
temp dir before reading, so a running browser's lock never blocks extraction.

This module never raises and never logs cookie values.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import time
from base64 import b64decode
from pathlib import Path

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except Exception:  # pragma: no cover - cryptography is pinned in requirements
    AESGCM = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

__all__ = ["extract_platform_cookies"]

_PLATFORMS = ("youtube", "twitch", "kick", "instagram")

_PLATFORM_DOMAINS: dict[str, tuple[str, ...]] = {
    "youtube": (".youtube.com",),
    "twitch": (".twitch.tv",),
    "kick": (".kick.com",),
    "instagram": (".instagram.com",),
}

_CHROMIUM_EPOCH_OFFSET = 11644473600  # seconds from 1601-01-01 to 1970-01-01
_DPAPI_PREFIX = b"DPAPI"
_V10_PREFIXES = (b"v10", b"v11")
_V20_PREFIX = b"v20"
_GCM_NONCE_LEN = 12
_GCM_TAG_LEN = 16


class _DATA_BLOB(ctypes.Structure):
    _fields_ = (("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char)))


def _crypt_unprotect_data(data: bytes) -> bytes:
    """Unwrap a DPAPI blob via crypt32. Raises RuntimeError on failure."""
    buf = ctypes.create_string_buffer(data)
    blob_in = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise RuntimeError("CryptUnprotectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _read_aes_key(local_state: Path) -> bytes | None:
    """Derive the 32-byte AES key from a Local State file; None on any problem."""
    try:
        state = json.loads(local_state.read_text(encoding="utf-8"))
        encrypted_key = state["os_crypt"]["encrypted_key"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    try:
        blob = b64decode(encrypted_key, validate=True)
    except (ValueError, TypeError):
        return None
    if blob[:5] != _DPAPI_PREFIX:
        return None
    try:
        key = _crypt_unprotect_data(blob[5:])
    except Exception:
        return None
    if len(key) != 32:
        return None
    return key


def _extract_aes_key(candidates: list[Path]) -> bytes | None:
    for path in candidates:
        key = _read_aes_key(path)
        if key is not None:
            return key
    return None


def _decode_plain(plain: bytes) -> str:
    # Chromium padded short values with \x20 on some versions; strip it.
    return plain.rstrip(b"\x20").decode("utf-8", errors="replace")


def _decrypt_value(encrypted: bytes | None, aes_key: bytes | None) -> tuple[str | None, bool]:
    """Return (decrypted value or None, app-bound flag). Never raises."""
    if not encrypted:
        return None, False
    prefix = encrypted[:3]
    if prefix in _V10_PREFIXES:
        if AESGCM is None or aes_key is None:
            return None, False
        if len(encrypted) < 3 + _GCM_NONCE_LEN + _GCM_TAG_LEN:
            return None, False
        nonce = encrypted[3 : 3 + _GCM_NONCE_LEN]
        ct_and_tag = encrypted[3 + _GCM_NONCE_LEN :]
        try:
            plain = AESGCM(aes_key).decrypt(nonce, ct_and_tag, None)
        except Exception:
            if prefix == b"v11":
                # v11 that does not open with the DPAPI key is app-bound.
                return None, True
            logger.debug("v10 value failed AES-GCM decryption")
            return None, False
        return _decode_plain(plain), False
    if prefix == _V20_PREFIX:
        return None, True
    try:
        plain = _crypt_unprotect_data(encrypted)
    except Exception:
        logger.debug("legacy DPAPI value failed to unwrap")
        return None, False
    return _decode_plain(plain), False


def _is_expired(expires_utc: int, now: float) -> bool:
    if not expires_utc:
        return False  # session cookie
    return (expires_utc / 1_000_000 - _CHROMIUM_EPOCH_OFFSET) < now


def _chromium_to_unix(expires_utc: int) -> int | None:
    if not expires_utc:
        return None
    return int(expires_utc / 1_000_000 - _CHROMIUM_EPOCH_OFFSET)


def _platform_for_host(host_key: str) -> str | None:
    host = host_key.lstrip(".")
    for platform, domains in _PLATFORM_DOMAINS.items():
        for domain in domains:
            bare = domain.lstrip(".")
            if host == bare or host.endswith("." + bare):
                return platform
    return None


def _read_cookie_rows(cookies_path: Path) -> list[tuple]:
    """Copy the Cookies DB (+wal/shm) to a temp dir and read all rows."""
    tmp = Path(tempfile.mkdtemp(prefix="vodrip-cookies-"))
    try:
        for suffix in ("", "-wal", "-shm"):
            src = cookies_path.with_name(cookies_path.name + suffix)
            if src.is_file():
                shutil.copy2(src, tmp / src.name)
        db_copy = tmp / cookies_path.name
        try:
            return _query_cookies(db_copy)
        except sqlite3.DatabaseError:
            # A -wal copied mid-write can be inconsistent; retry with the
            # main file only (last checkpoint).
            for suffix in ("-wal", "-shm"):
                (tmp / (cookies_path.name + suffix)).unlink(missing_ok=True)
            shutil.copy2(cookies_path, db_copy)
            return _query_cookies(db_copy)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _query_cookies(db: Path) -> list[tuple]:
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        return conn.execute(
            "SELECT host_key, name, value, encrypted_value, path, expires_utc, "
            "is_secure, is_httponly FROM cookies"
        ).fetchall()
    finally:
        conn.close()


def _cookies_for_rows(
    rows: list[tuple], aes_key: bytes | None, now: float
) -> tuple[list[dict], bool]:
    cookies: list[dict] = []
    app_bound = False
    for row in rows:
        if _is_expired(row[5] or 0, now):
            continue
        plaintext = row[2]
        if plaintext:
            value, was_app_bound = str(plaintext), False
        else:
            value, was_app_bound = _decrypt_value(row[3], aes_key)
        if was_app_bound:
            app_bound = True
            continue
        if value is None:
            continue
        expires_utc = row[5] or 0
        cookies.append(
            {
                "name": str(row[1] or ""),
                "value": value,
                "domain": str(row[0] or ""),
                "path": str(row[4] or "/"),
                "is_session": not expires_utc,
                "expires": _chromium_to_unix(expires_utc),
                "secure": bool(row[6]),
                "httponly": bool(row[7]),
            }
        )
    return cookies, app_bound


def _default_user_data_dirs() -> list[Path]:
    local_appdata = os.environ.get("LOCALAPPDATA")
    if not local_appdata:
        return []
    root = Path(local_appdata)
    return [
        root / "Google" / "Chrome" / "User Data",
        root / "Microsoft" / "Edge" / "User Data",
        root / "BraveSoftware" / "Brave-Browser" / "User Data",
        root / "Vivaldi" / "User Data",
    ]


def _profiles_to_scan(profile_dir: Path | None) -> list[tuple[Path, Path]]:
    """Return (profile_dir, user_data_dir) pairs to scan."""
    if profile_dir is not None:
        return [(profile_dir, profile_dir.parent)]
    pairs: list[tuple[Path, Path]] = []
    for user_data in _default_user_data_dirs():
        if not user_data.is_dir():
            continue
        candidates = [user_data / "Default", *sorted(user_data.glob("Profile *"))]
        pairs.extend((c, user_data) for c in candidates if c.is_dir())
    return pairs


def _cookies_db_path(profile_dir: Path) -> Path | None:
    """Locate a profile's cookie SQLite file.

    Modern Chromium keeps it at ``<profile>/Network/Cookies``; older builds
    used ``<profile>/Cookies``. Both are checked, newest layout first, so a
    profile that has not migrated is still readable.
    """
    for candidate in (
        profile_dir / "Network" / "Cookies",
        profile_dir / "Cookies",
    ):
        if candidate.is_file():
            return candidate
    return None


def _extract(profile_dir: Path | str | None) -> tuple[dict[str, list[dict]], str]:
    now = time.time()
    collected: dict[str, list[dict]] = {}
    reasons: list[str] = []
    app_bound: set[str] = set()
    scanned_db = False

    for prof, user_data in _profiles_to_scan(
        Path(profile_dir) if profile_dir is not None else None
    ):
        # Chromium moved the cookie store to <profile>/Network/Cookies in
        # Chrome 96+ (Edge/Brave/Vivaldi follow). Reading only the legacy
        # <profile>/Cookies found NOTHING on a modern install and reported
        # "no browser Cookies database found" while three browsers sat there
        # fully signed in - the fixture in the tests encoded the old layout,
        # so the suite passed while the real path was dead.
        cookies_path = _cookies_db_path(prof)
        if cookies_path is None:
            continue
        scanned_db = True
        # Real layout: <User Data>/Local State beside <User Data>/Default.
        # A caller-supplied profile dir may carry its own copy instead.
        aes_key = _extract_aes_key([user_data / "Local State", prof / "Local State"])
        try:
            rows = _read_cookie_rows(cookies_path)
        except (sqlite3.Error, OSError) as exc:
            # OSError matters as much as sqlite3.Error: a RUNNING Chrome holds
            # <profile>/Network/Cookies with a share mode that denies even a
            # read, and one locked browser must not abort the scan for the
            # others (Edge/Brave were readable on the same box at the same
            # moment). The reason is surfaced so the UI can say so.
            if isinstance(exc, PermissionError):
                reasons.append(
                    f"locked by a running {user_data.name or 'browser'} "
                    "(close it or use another browser)"
                )
            else:
                reasons.append(f"unreadable Cookies database: {exc}")
            continue
        if not rows:
            continue
        by_platform: dict[str, list[tuple]] = {}
        for row in rows:
            platform = _platform_for_host(str(row[0] or ""))
            if platform is not None:
                by_platform.setdefault(platform, []).append(row)
        for platform, platform_rows in by_platform.items():
            if platform in app_bound:
                continue
            cookies, was_app_bound = _cookies_for_rows(platform_rows, aes_key, now)
            if was_app_bound and not cookies:
                app_bound.add(platform)
                logger.debug("platform %s uses app-bound encryption", platform)
                continue
            if not cookies:
                # A platform whose every cookie is expired must not appear at
                # all: a key with an empty list reads as "the backend has this
                # platform's session", and callers branch on exactly that.
                continue
            collected.setdefault(platform, []).extend(cookies)

    if collected:
        reason = ""
        if app_bound:
            reason = "app-bound encryption not decrypted: " + ", ".join(sorted(app_bound))
        return collected, reason
    if app_bound:
        return {}, "app-bound encryption detected: " + ", ".join(sorted(app_bound))
    if reasons:
        return {}, "; ".join(dict.fromkeys(reasons))[:200]
    if not scanned_db:
        return {}, "no browser Cookies database found"
    return {}, "no matching cookies"


def extract_platform_cookies(
    profile_dir: Path | str | None = None,
) -> tuple[dict[str, list[dict]], str]:
    """Extract per-platform cookies from a Chromium browser profile.

    ``profile_dir`` is a browser profile directory (the one holding ``Cookies``,
    e.g. ``.../User Data/Default``). When ``None``, the default Chrome, Edge,
    Brave and Vivaldi user-data trees are scanned.

    Returns ``(cookies, reason)`` where ``cookies`` maps each platform found
    (``youtube``/``twitch``/``kick``/``instagram``) to a list of cookie dicts
    with keys ``name``, ``value``, ``domain``, ``path``, ``is_session``,
    ``expires`` (unix seconds or None), ``secure`` and ``httponly``. Expired
    cookies and off-domain hosts are filtered out. ``reason`` is empty on
    success and a short explanatory string on failure. Never raises; cookie
    values are never logged.
    """
    try:
        return _extract(profile_dir)
    except Exception as exc:  # never raise
        logger.warning("browser cookie extraction failed: %s", type(exc).__name__)
        return {}, f"extraction failed: {type(exc).__name__}"

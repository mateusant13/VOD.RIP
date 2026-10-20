"""Tests for services.browser_cookie_extract — synthetic Chromium profiles only.

No real browser is touched. Every test builds a throwaway Chromium-style
``Cookies`` SQLite DB plus a ``Local State`` file under ``tmp_path`` and passes
the profile directory explicitly to ``extract_platform_cookies``.

DPAPI is monkeypatched to the identity function (after the 5-byte ``DPAPI``
prefix strip the "unprotected key" IS the raw AES key), so the AES-256-GCM
layer is exercised with a known key. Cookie values are never printed.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path

import pytest

from services import browser_cookie_extract

# ---------------------------------------------------------------------------
# Constants shared by the fixtures
# ---------------------------------------------------------------------------

AES_KEY = bytes(range(32))  # 32-byte key the monkeypatched DPAPI "returns"
NONCE = b"\x01" * 12  # 12-byte GCM nonce
CHROMIUM_EPOCH_OFFSET = 11644473600  # seconds between 1601-01-01 and 1970-01-01

# Full modern Chromium cookies table schema (Chrome 120+)
_COOKIES_DDL = """
CREATE TABLE cookies (
    creation_utc INTEGER NOT NULL,
    host_key TEXT NOT NULL,
    top_frame_site_key TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '',
    encrypted_value BLOB NOT NULL DEFAULT '',
    path TEXT NOT NULL,
    expires_utc INTEGER NOT NULL,
    is_secure INTEGER NOT NULL,
    is_httponly INTEGER NOT NULL,
    last_access_utc INTEGER NOT NULL,
    has_expires INTEGER NOT NULL DEFAULT 0,
    is_persistent INTEGER NOT NULL DEFAULT 0,
    priority INTEGER NOT NULL DEFAULT 1,
    samesite INTEGER NOT NULL DEFAULT -1,
    source_scheme INTEGER NOT NULL DEFAULT 0,
    source_port INTEGER NOT NULL DEFAULT -1,
    last_update_utc INTEGER NOT NULL DEFAULT 0,
    source_type INTEGER NOT NULL DEFAULT 0,
    has_cross_site_ancestor INTEGER NOT NULL DEFAULT 0
)
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _encrypt(plaintext: bytes) -> bytes:
    """Encrypt plaintext the way Chromium v10+ does: v10 + nonce + ciphertext+tag."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    aesgcm = AESGCM(AES_KEY)
    return b"v10" + NONCE + aesgcm.encrypt(NONCE, plaintext, None)


def _unix_to_chromium(unix_seconds: int) -> int:
    """Convert Unix seconds to Chromium's microseconds-since-1601."""
    return (unix_seconds + CHROMIUM_EPOCH_OFFSET) * 1_000_000


def _write_local_state(user_data_dir: Path) -> None:
    """Write a Local State file whose os_crypt.encrypted_key decodes to AES_KEY."""
    user_data_dir.mkdir(parents=True, exist_ok=True)
    encrypted_key = base64.b64encode(b"DPAPI" + AES_KEY).decode("ascii")
    local_state = {"os_crypt": {"encrypted_key": encrypted_key}}
    (user_data_dir / "Local State").write_text(
        json.dumps(local_state), encoding="utf-8"
    )


def _make_cookies_db(
    db_path: Path,
    rows: list[dict],
) -> None:
    """Create a synthetic Chromium Cookies DB and insert rows.

    Each row dict may contain any column; missing columns get Chromium defaults.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(_COOKIES_DDL)
        for row in rows:
            cols = ", ".join(row.keys())
            placeholders = ", ".join("?" for _ in row)
            conn.execute(
                f"INSERT INTO cookies ({cols}) VALUES ({placeholders})",
                list(row.values()),
            )
        conn.commit()
    finally:
        conn.close()


def _base_row(
    host_key: str,
    name: str,
    encrypted_value: bytes,
    *,
    path: str = "/",
    expires_utc: int = 0,
    is_secure: int = 0,
    is_httponly: int = 0,
    has_expires: int = 0,
    is_persistent: int = 0,
    value: str = "",
) -> dict:
    """Build a row dict with Chromium defaults for omitted columns."""
    return {
        "creation_utc": 0,
        "host_key": host_key,
        "top_frame_site_key": "",
        "name": name,
        "value": value,
        "encrypted_value": encrypted_value,
        "path": path,
        "expires_utc": expires_utc,
        "is_secure": is_secure,
        "is_httponly": is_httponly,
        "last_access_utc": 0,
        "has_expires": has_expires,
        "is_persistent": is_persistent,
        "priority": 1,
        "samesite": -1,
        "source_scheme": 0,
        "source_port": -1,
        "last_update_utc": 0,
        "source_type": 0,
        "has_cross_site_ancestor": 0,
    }


@pytest.fixture(autouse=True)
def _identity_dpapi(monkeypatch):
    """Monkeypatch CryptUnprotectData to identity (key = raw bytes after prefix strip)."""
    monkeypatch.setattr(
        browser_cookie_extract, "_crypt_unprotect_data", lambda data: data
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRoundTrip:
    """v10-encrypted cookie decrypts to the original plaintext."""

    def test_basic_round_trip(self, tmp_path: Path):
        user_data = tmp_path / "user_data"
        profile = user_data / "Default"
        _write_local_state(user_data)

        plaintext = b"session_token_abc123"
        _make_cookies_db(
            profile / "Cookies",
            [
                _base_row(
                    host_key=".youtube.com",
                    name="SID",
                    encrypted_value=_encrypt(plaintext),
                )
            ],
        )

        result, reason = browser_cookie_extract.extract_platform_cookies(
            profile_dir=profile
        )

        assert reason == ""
        assert "youtube" in result
        cookies = result["youtube"]
        assert len(cookies) == 1
        assert cookies[0]["name"] == "SID"
        assert cookies[0]["value"] == plaintext.decode("utf-8")
        assert cookies[0]["domain"] == ".youtube.com"

    def test_multiple_platforms(self, tmp_path: Path):
        user_data = tmp_path / "user_data"
        profile = user_data / "Default"
        _write_local_state(user_data)

        _make_cookies_db(
            profile / "Cookies",
            [
                _base_row(
                    host_key=".youtube.com",
                    name="A",
                    encrypted_value=_encrypt(b"val_a"),
                ),
                _base_row(
                    host_key=".twitch.tv",
                    name="B",
                    encrypted_value=_encrypt(b"val_b"),
                ),
                _base_row(
                    host_key=".kick.com",
                    name="C",
                    encrypted_value=_encrypt(b"val_c"),
                ),
            ],
        )

        result, reason = browser_cookie_extract.extract_platform_cookies(
            profile_dir=profile
        )

        assert reason == ""
        assert set(result.keys()) == {"youtube", "twitch", "kick"}
        assert result["youtube"][0]["value"] == "val_a"
        assert result["twitch"][0]["value"] == "val_b"
        assert result["kick"][0]["value"] == "val_c"

    def test_cookie_fields_populated(self, tmp_path: Path):
        user_data = tmp_path / "user_data"
        profile = user_data / "Default"
        _write_local_state(user_data)

        future = _unix_to_chromium(2_000_000_000)  # year 2033
        _make_cookies_db(
            profile / "Cookies",
            [
                _base_row(
                    host_key=".youtube.com",
                    name="PREF",
                    encrypted_value=_encrypt(b"prefs"),
                    path="/watch",
                    expires_utc=future,
                    is_secure=1,
                    is_httponly=1,
                    has_expires=1,
                    is_persistent=1,
                )
            ],
        )

        result, reason = browser_cookie_extract.extract_platform_cookies(
            profile_dir=profile
        )

        assert reason == ""
        cookie = result["youtube"][0]
        assert cookie["path"] == "/watch"
        assert cookie["secure"] is True
        assert cookie["httponly"] is True
        assert cookie["is_session"] is False
        assert cookie["expires"] == 2_000_000_000

    def test_session_cookie_has_no_expiry(self, tmp_path: Path):
        user_data = tmp_path / "user_data"
        profile = user_data / "Default"
        _write_local_state(user_data)

        _make_cookies_db(
            profile / "Cookies",
            [
                _base_row(
                    host_key=".youtube.com",
                    name="SESSION",
                    encrypted_value=_encrypt(b"sess"),
                    expires_utc=0,
                    has_expires=0,
                    is_persistent=0,
                )
            ],
        )

        result, reason = browser_cookie_extract.extract_platform_cookies(
            profile_dir=profile
        )

        assert reason == ""
        cookie = result["youtube"][0]
        assert cookie["is_session"] is True
        assert cookie["expires"] is None


class TestSpacePaddingStrip:
    """Trailing \\x20 padding is stripped from decrypted values."""

    def test_trailing_space_stripped(self, tmp_path: Path):
        user_data = tmp_path / "user_data"
        profile = user_data / "Default"
        _write_local_state(user_data)

        # Chromium pads short cookies with \x20 to a 16-byte boundary
        plaintext = b"short\x20\x20\x20\x20\x20\x20\x20\x20\x20"
        _make_cookies_db(
            profile / "Cookies",
            [
                _base_row(
                    host_key=".youtube.com",
                    name="PAD",
                    encrypted_value=_encrypt(plaintext),
                )
            ],
        )

        result, reason = browser_cookie_extract.extract_platform_cookies(
            profile_dir=profile
        )

        assert reason == ""
        assert result["youtube"][0]["value"] == "short"

    def test_no_padding_unchanged(self, tmp_path: Path):
        user_data = tmp_path / "user_data"
        profile = user_data / "Default"
        _write_local_state(user_data)

        plaintext = b"exactly16bytes!!"
        _make_cookies_db(
            profile / "Cookies",
            [
                _base_row(
                    host_key=".youtube.com",
                    name="EXACT",
                    encrypted_value=_encrypt(plaintext),
                )
            ],
        )

        result, reason = browser_cookie_extract.extract_platform_cookies(
            profile_dir=profile
        )

        assert reason == ""
        assert result["youtube"][0]["value"] == "exactly16bytes!!"


class TestLegacyDpapiLayout:
    """Values without a v10 prefix are decrypted via DPAPI directly."""

    def test_legacy_dpapi_value(self, tmp_path: Path):
        user_data = tmp_path / "user_data"
        profile = user_data / "Default"
        _write_local_state(user_data)

        # Legacy layout: encrypted_value is raw DPAPI output (no v10 prefix).
        # The monkeypatched _crypt_unprotect_data is identity, so the "DPAPI"
        # output IS the plaintext.
        plaintext = b"legacy_cookie_value"
        _make_cookies_db(
            profile / "Cookies",
            [
                _base_row(
                    host_key=".youtube.com",
                    name="LEGACY",
                    encrypted_value=plaintext,
                )
            ],
        )

        result, reason = browser_cookie_extract.extract_platform_cookies(
            profile_dir=profile
        )

        assert reason == ""
        assert result["youtube"][0]["value"] == "legacy_cookie_value"

    def test_legacy_dpapi_with_space_padding(self, tmp_path: Path):
        user_data = tmp_path / "user_data"
        profile = user_data / "Default"
        _write_local_state(user_data)

        # Legacy values also get \x20 padding
        plaintext = b"legacy\x20\x20\x20\x20\x20\x20\x20\x20\x20\x20"
        _make_cookies_db(
            profile / "Cookies",
            [
                _base_row(
                    host_key=".twitch.tv",
                    name="LPAD",
                    encrypted_value=plaintext,
                )
            ],
        )

        result, reason = browser_cookie_extract.extract_platform_cookies(
            profile_dir=profile
        )

        assert reason == ""
        assert result["twitch"][0]["value"] == "legacy"


class TestAppBoundRejection:
    """v20-prefixed values are detected as app-bound and reported in the reason."""

    def test_v20_rejected_with_reason(self, tmp_path: Path):
        user_data = tmp_path / "user_data"
        profile = user_data / "Default"
        _write_local_state(user_data)

        _make_cookies_db(
            profile / "Cookies",
            [
                _base_row(
                    host_key=".youtube.com",
                    name="AB",
                    encrypted_value=b"v20" + b"\x00" * 32,
                )
            ],
        )

        result, reason = browser_cookie_extract.extract_platform_cookies(
            profile_dir=profile
        )

        assert "youtube" not in result
        assert "app-bound" in reason

    def test_v20_mixed_with_good_cookies(self, tmp_path: Path):
        user_data = tmp_path / "user_data"
        profile = user_data / "Default"
        _write_local_state(user_data)

        _make_cookies_db(
            profile / "Cookies",
            [
                _base_row(
                    host_key=".youtube.com",
                    name="GOOD",
                    encrypted_value=_encrypt(b"good_value"),
                ),
                _base_row(
                    host_key=".youtube.com",
                    name="APPBOUND",
                    encrypted_value=b"v20" + b"\x00" * 32,
                ),
            ],
        )

        result, reason = browser_cookie_extract.extract_platform_cookies(
            profile_dir=profile
        )

        # The good cookie is still returned; the app-bound one is dropped
        assert "youtube" in result
        assert len(result["youtube"]) == 1
        assert result["youtube"][0]["name"] == "GOOD"
        # reason stays "" when at least one cookie decrypts
        assert reason == ""

    def test_v11_uses_standard_aes_path(self, tmp_path: Path):
        """v11 goes through the same AES-GCM path as v10."""
        user_data = tmp_path / "user_data"
        profile = user_data / "Default"
        _write_local_state(user_data)

        plaintext = b"v11_value"
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        aesgcm = AESGCM(AES_KEY)
        encrypted = b"v11" + NONCE + aesgcm.encrypt(NONCE, plaintext, None)

        _make_cookies_db(
            profile / "Cookies",
            [
                _base_row(
                    host_key=".kick.com",
                    name="V11",
                    encrypted_value=encrypted,
                )
            ],
        )

        result, reason = browser_cookie_extract.extract_platform_cookies(
            profile_dir=profile
        )

        assert reason == ""
        assert result["kick"][0]["value"] == "v11_value"


class TestMissingAndCorruptDb:
    """Missing or unreadable DBs return ({}, reason)."""

    def test_missing_db(self, tmp_path: Path):
        user_data = tmp_path / "user_data"
        profile = user_data / "Default"
        _write_local_state(user_data)
        # Do NOT create the Cookies DB

        result, reason = browser_cookie_extract.extract_platform_cookies(
            profile_dir=profile
        )

        assert result == {}
        assert reason != ""

    def test_corrupt_db(self, tmp_path: Path):
        user_data = tmp_path / "user_data"
        profile = user_data / "Default"
        _write_local_state(user_data)

        # Write garbage to the Cookies file
        profile.mkdir(parents=True, exist_ok=True)
        (profile / "Cookies").write_bytes(b"this is not a sqlite database" * 10)

        result, reason = browser_cookie_extract.extract_platform_cookies(
            profile_dir=profile
        )

        assert result == {}
        assert reason != ""

    def test_no_matching_cookies(self, tmp_path: Path):
        user_data = tmp_path / "user_data"
        profile = user_data / "Default"
        _write_local_state(user_data)

        # Create a valid DB with no rows
        _make_cookies_db(profile / "Cookies", [])

        result, reason = browser_cookie_extract.extract_platform_cookies(
            profile_dir=profile
        )

        assert result == {}
        assert reason != ""

    def test_wrong_domain_filtered(self, tmp_path: Path):
        user_data = tmp_path / "user_data"
        profile = user_data / "Default"
        _write_local_state(user_data)

        _make_cookies_db(
            profile / "Cookies",
            [
                _base_row(
                    host_key=".example.com",
                    name="OTHER",
                    encrypted_value=_encrypt(b"other"),
                )
            ],
        )

        result, reason = browser_cookie_extract.extract_platform_cookies(
            profile_dir=profile
        )

        assert result == {}
        assert reason != ""


class TestExpiredCookies:
    """Expired cookies are filtered out; session cookies are kept."""

    def test_expired_cookie_dropped(self, tmp_path: Path):
        user_data = tmp_path / "user_data"
        profile = user_data / "Default"
        _write_local_state(user_data)

        past = _unix_to_chromium(1_000_000)  # 1970 — long expired
        _make_cookies_db(
            profile / "Cookies",
            [
                _base_row(
                    host_key=".youtube.com",
                    name="OLD",
                    encrypted_value=_encrypt(b"old"),
                    expires_utc=past,
                    has_expires=1,
                    is_persistent=1,
                )
            ],
        )

        result, reason = browser_cookie_extract.extract_platform_cookies(
            profile_dir=profile
        )

        assert result == {}
        assert reason != ""

    def test_session_cookie_kept(self, tmp_path: Path):
        user_data = tmp_path / "user_data"
        profile = user_data / "Default"
        _write_local_state(user_data)

        _make_cookies_db(
            profile / "Cookies",
            [
                _base_row(
                    host_key=".youtube.com",
                    name="SESS",
                    encrypted_value=_encrypt(b"sess"),
                    expires_utc=0,
                    has_expires=0,
                    is_persistent=0,
                )
            ],
        )

        result, reason = browser_cookie_extract.extract_platform_cookies(
            profile_dir=profile
        )

        assert reason == ""
        assert "youtube" in result


class TestNeverRaises:
    """The outer guard catches everything and returns ({}, reason)."""

    def test_no_local_state(self, tmp_path: Path):
        profile = tmp_path / "Default"
        profile.mkdir(parents=True)
        # No Local State file at all

        result, reason = browser_cookie_extract.extract_platform_cookies(
            profile_dir=profile
        )

        assert result == {}
        assert reason != ""

    def test_invalid_local_state_json(self, tmp_path: Path):
        user_data = tmp_path / "user_data"
        profile = user_data / "Default"
        user_data.mkdir(parents=True)
        (user_data / "Local State").write_text("not json", encoding="utf-8")

        result, reason = browser_cookie_extract.extract_platform_cookies(
            profile_dir=profile
        )

        assert result == {}
        assert reason != ""

    def test_missing_encrypted_key(self, tmp_path: Path):
        user_data = tmp_path / "user_data"
        profile = user_data / "Default"
        user_data.mkdir(parents=True)
        (user_data / "Local State").write_text(
            json.dumps({"os_crypt": {}}), encoding="utf-8"
        )

        result, reason = browser_cookie_extract.extract_platform_cookies(
            profile_dir=profile
        )

        assert result == {}
        assert reason != ""

# ---------------------------------------------------------------------------
# Real-world layout: <profile>/Network/Cookies (Chrome 96+, Edge, Brave)
# ---------------------------------------------------------------------------
#
# The original fixtures wrote <profile>/Cookies - the pre-96 layout. Every test
# passed while the real path returned "no browser Cookies database found" on a
# box with three signed-in browsers. Measured 2026-10-20: Chrome, Edge and
# Brave all store the DB under Network/, so reading only the legacy path found
# nothing at all. These pin the layout the product actually meets.


class TestModernNetworkLayout:
    def test_reads_network_cookies_db(self, tmp_path: Path):
        user_data = tmp_path / "user_data"
        profile = user_data / "Default"
        _write_local_state(user_data)
        plaintext = b"sid_from_modern_layout"
        _make_cookies_db(
            profile / "Network" / "Cookies",
            [
                _base_row(
                    host_key=".youtube.com",
                    name="SID",
                    encrypted_value=_encrypt(plaintext),
                )
            ],
        )

        result, reason = browser_cookie_extract.extract_platform_cookies(
            profile_dir=profile
        )

        assert reason == ""
        assert result["youtube"][0]["value"] == plaintext.decode("utf-8")

    def test_network_layout_wins_over_a_stale_legacy_copy(self, tmp_path: Path):
        """A migrated profile can hold both; the live one is under Network/."""
        user_data = tmp_path / "user_data"
        profile = user_data / "Default"
        _write_local_state(user_data)
        _make_cookies_db(
            profile / "Cookies",
            [
                _base_row(
                    host_key=".youtube.com",
                    name="OLD",
                    encrypted_value=_encrypt(b"stale"),
                )
            ],
        )
        _make_cookies_db(
            profile / "Network" / "Cookies",
            [
                _base_row(
                    host_key=".youtube.com",
                    name="NEW",
                    encrypted_value=_encrypt(b"live"),
                )
            ],
        )

        result, _reason = browser_cookie_extract.extract_platform_cookies(
            profile_dir=profile
        )

        names = {c["name"] for c in result["youtube"]}
        assert names == {"NEW"}

    def test_legacy_layout_still_reads(self, tmp_path: Path):
        user_data = tmp_path / "user_data"
        profile = user_data / "Default"
        _write_local_state(user_data)
        _make_cookies_db(
            profile / "Cookies",
            [
                _base_row(
                    host_key=".youtube.com",
                    name="LEGACY",
                    encrypted_value=_encrypt(b"x"),
                )
            ],
        )

        result, _reason = browser_cookie_extract.extract_platform_cookies(
            profile_dir=profile
        )

        assert {c["name"] for c in result["youtube"]} == {"LEGACY"}


class TestLockedProfileDoesNotAbortTheScan:
    """A running Chrome denies even a read of Network/Cookies (measured
    2026-10-20: CreateFileW with FILE_SHARE_READ|WRITE|DELETE returns
    INVALID_HANDLE_VALUE). One locked browser must not hide the others."""

    def test_locked_cookies_db_is_reported_not_raised(self, tmp_path: Path, monkeypatch):
        user_data = tmp_path / "user_data"
        profile = user_data / "Default"
        _write_local_state(user_data)
        _make_cookies_db(
            profile / "Network" / "Cookies",
            [
                _base_row(
                    host_key=".youtube.com",
                    name="SID",
                    encrypted_value=_encrypt(b"x"),
                )
            ],
        )

        def boom(_path):
            raise PermissionError(13, "denied", str(_path))

        monkeypatch.setattr(browser_cookie_extract, "_read_cookie_rows", boom)

        result, reason = browser_cookie_extract.extract_platform_cookies(
            profile_dir=profile
        )

        assert result == {}
        assert "locked" in reason

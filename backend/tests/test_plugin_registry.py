"""Plugin registry — the nucleus tests.

Covers the contract the whole plugin system stands on:
  * core plugins (link-download, channels) can NEVER be switched off;
  * defaultOn is respected — a settings.json with no "plugins" key at all
    (every existing install) resolves to the registry defaults, EXCEPT the four
    aliased plugins, which resolve to the FEATURE flag the user already chose
    (the owner's live-captions:false must survive the upgrade);
  * POST /api/plugins persists one boolean per plugin into settings.json
    under the plugin's settingsKey;
  * unknown ids are rejected, junk keys are ignored on read and KEPT on write;
  * the backend registry and the src/lib/plugins.tsx mirror do not drift
    (ids/settingsKeys/core/defaultOn/cost — the fields integration breaks on).

Isolation mirrors tests/test_settings_save_interleave.py: the settings_mgr
singleton is pointed at a scratch file and the ffmpeg autofill probe is
neutralised, so the file's writer count is exactly the test's own.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app import app
from deps import settings_mgr
from models.schemas import AppSettings
from services.plugin_registry import (
    PLUGINS,
    defaults,
    enabled_map,
    is_core,
    set_plugin,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TS_MIRROR = _REPO_ROOT / "src" / "lib" / "plugins.tsx"


@pytest.fixture(autouse=True)
def _isolated_settings_file():
    """Point the singleton at a scratch file; no ffmpeg probe, no disk writes."""
    original_file = settings_mgr._settings_file
    original_settings = settings_mgr._settings
    original_probe = settings_mgr._ffmpeg_probe_failed
    temp_file = original_file.parent / f"settings_plugins_{os.getpid()}.json"
    temp_file.unlink(missing_ok=True)
    settings_mgr._settings_file = temp_file
    settings_mgr._settings = AppSettings()
    settings_mgr._ffmpeg_probe_failed = True
    # Seed the file and stamp a committed baseline: without it every get()
    # returns a hand-built object with _vodrip_base=None, which save() treats
    # as "no provenance" and writes WHOLESALE (dropping concurrent commits).
    settings_mgr.save(settings_mgr.get())
    yield temp_file
    settings_mgr._settings_file = original_file
    settings_mgr._settings = original_settings
    settings_mgr._ffmpeg_probe_failed = original_probe
    temp_file.unlink(missing_ok=True)


def _on_disk() -> dict:
    return json.loads(settings_mgr._settings_file.read_text(encoding="utf-8"))


# ── core guard ───────────────────────────────────────────────────────────────

def test_core_plugins_cannot_be_disabled() -> None:
    """The essential experience is not optional — refusing is the contract."""
    for pid in ("link-download", "channels"):
        assert is_core(pid), f"{pid} must be catalogued as core"
        with pytest.raises(ValueError):
            set_plugin(pid, False)
    # RED ARM: delete the core check in set_plugin() and this fails.
    state = enabled_map()
    assert state["link-download"] is True
    assert state["channels"] is True


def test_core_plugin_true_is_a_noop_that_stays_on() -> None:
    """Setting a core plugin to True is a no-op that must not corrupt state."""
    set_plugin("channels", True)
    assert enabled_map()["channels"] is True


def test_unknown_plugin_id_rejected() -> None:
    with pytest.raises(ValueError):
        set_plugin("plugin-that-does-not-exist", True)


# ── defaults / migration ─────────────────────────────────────────────────────

def test_fresh_install_resolves_defaulton() -> None:
    """No "plugins" key anywhere -> every plugin answers its defaultOn."""
    state = enabled_map()
    expected = {p.id: p.defaultOn for p in PLUGINS}
    assert state == expected
    # Pinned map (decision, not description): core on; every HEAVY bonus off
    # (transcribe / live-subtitles / live-preview / ai-ask — the owner's own
    # settings run exactly that profile); light bonus on.
    assert state == {
        "link-download": True,
        "channels": True,
        "transcribe": False,
        "live-subtitles": False,
        "live-preview": False,
        "live-chat": True,
        "clip-editor": True,
        "twitch-clip": True,
        "archive-search": True,
        "channel-explore": True,
        "local-file": True,
        "ai-ask": False,
    }


def test_legacy_settings_without_plugins_key_use_defaults() -> None:
    """An old settings.json (no "plugins" at all) must migrate, not break."""
    settings_mgr._settings_file.write_text(
        json.dumps({"download_threads": 4, "quality": "480p"}), encoding="utf-8"
    )
    settings_mgr._settings = settings_mgr._load()
    state = enabled_map()
    assert state == {p.id: p.defaultOn for p in PLUGINS}
    assert state["link-download"] is True and state["channels"] is True


def test_existing_feature_flag_wins_over_defaulton() -> None:
    """Tolerant migration for installs that predate the plugin system.

    The owner's live settings.json (measured 2026-10-14) carries
    live-captions:false — he switched live subtitles OFF in the old feature
    UI. With no "plugins" key stored yet, the aliased plugin must resolve
    from that flag, NOT from defaultOn, or the upgrade silently turns his
    choice back on.
    """
    settings_mgr._settings_file.write_text(
        json.dumps({
            "features": {
                "core-download": True,
                "transcribe-vod": False,
                "live-captions": False,
                "live-preview": False,
                "chat-live": True,
            }
        }),
        encoding="utf-8",
    )
    settings_mgr._settings = settings_mgr._load()
    state = enabled_map()
    # RED ARM for this direction: test_feature_flag_true_overrides_a_false_
    # defaulton below — drop the PLUGIN_FEATURE_ALIAS fallback in enabled_map()
    # and THAT one fails (these assertions hold either way).
    assert state["live-subtitles"] is False, "his live-captions:false was overridden"
    assert state["transcribe"] is False
    assert state["live-preview"] is False
    assert state["live-chat"] is True, "his chat-live:true was lost"
    # Once the user toggles the plugin itself, the plugin key wins forever.
    set_plugin("live-subtitles", True)
    assert enabled_map()["live-subtitles"] is True


def test_feature_flag_true_overrides_a_false_defaulton() -> None:
    """The other direction: a user who turned a heavy feature ON keeps it on.

    live-subtitles ships defaultOn=False (pesado), but an install whose
    stored feature flag says live-captions:true must NOT lose it to the
    default. This is the case that makes the migration fallback load-bearing.
    """
    settings_mgr._settings_file.write_text(
        json.dumps({"features": {"live-captions": True, "chat-live": True}}),
        encoding="utf-8",
    )
    settings_mgr._settings = settings_mgr._load()
    # RED ARM: drop the PLUGIN_FEATURE_ALIAS fallback in enabled_map() and
    # this assertion fails (defaultOn=False would win).
    assert enabled_map()["live-subtitles"] is True
    # and the plugin's own key still outranks the feature flag once written
    set_plugin("live-subtitles", False)
    assert enabled_map()["live-subtitles"] is False
    assert _on_disk()["plugins"]["plugin_live_subtitles"] is False


def test_legacy_junk_keys_are_ignored_on_read_and_kept_on_write() -> None:
    """A plugin id removed/renamed in a later version must keep its value."""
    settings_mgr._settings_file.write_text(
        json.dumps({"plugins": {"plugin_removed_in_v2": True}}), encoding="utf-8"
    )
    settings_mgr._settings = settings_mgr._load()
    # ignored on read: still the registry defaults
    assert enabled_map() == {p.id: p.defaultOn for p in PLUGINS}
    # kept on write: a toggle never destroys what it did not understand
    set_plugin("transcribe", True)
    stored = _on_disk()["plugins"]
    assert stored["plugin_removed_in_v2"] is True
    assert stored["plugin_transcribe"] is True


# ── persistence ──────────────────────────────────────────────────────────────

def test_set_plugin_persists_one_boolean_key_per_plugin() -> None:
    set_plugin("transcribe", True)
    raw = _on_disk()
    assert raw["plugins"]["plugin_transcribe"] is True
    assert enabled_map()["transcribe"] is True
    # and back off again — the toggle is real in both directions
    set_plugin("transcribe", False)
    assert _on_disk()["plugins"]["plugin_transcribe"] is False
    assert enabled_map()["transcribe"] is False


def test_no_op_toggle_does_not_rewrite_the_file() -> None:
    """defaultOn already equals the requested value -> no settings write."""
    set_plugin("transcribe", True)
    before = settings_mgr._settings_file.read_bytes()
    set_plugin("transcribe", True)
    assert settings_mgr._settings_file.read_bytes() == before


def test_cas_merge_protects_plugins_from_a_concurrent_writer() -> None:
    """Why the flags are a model FIELD and not loose settings.json keys.

    Writer B read the settings BEFORE the plugin toggle landed (its payload
    has no "plugins" edit of its own); B then saves an unrelated field. A
    wholesale write would revert the toggle; the CAS merge must keep it.
    """
    writer_b = settings_mgr.get()          # base snapshot, BEFORE the toggle
    set_plugin("transcribe", True)         # writer A commits the toggle
    writer_b.download_threads = 3          # B touches an unrelated key
    settings_mgr.save(writer_b)
    raw = _on_disk()
    assert raw["plugins"]["plugin_transcribe"] is True, (
        "a concurrent whole-object settings save reverted the plugin toggle"
    )
    assert raw["download_threads"] == 3


# ── registry <-> frontend mirror drift ───────────────────────────────────────

def _parse_ts_mirror() -> list[dict]:
    text = _TS_MIRROR.read_text(encoding="utf-8")
    pat = re.compile(
        r"\{ id: '([^']+)', name: '(?:[^'\\]|\\.)*', blurb: '(?:[^'\\]|\\.)*', "
        r"cost: '([^']+)',( core: true,)? defaultOn: (true|false), settingsKey: '([^']+)' \}",
    )
    rows = []
    for m in pat.finditer(text):
        rows.append(
            {
                "id": m.group(1),
                "cost": m.group(2),
                "core": bool(m.group(3)),
                "defaultOn": m.group(4) == "true",
                "settingsKey": m.group(5),
            }
        )
    return rows


def test_registry_matches_frontend_mirror() -> None:
    """The ids/settingsKeys/defaults the UI renders must BE the backend's.

    RED ARM: change any id, settingsKey, cost, core flag or defaultOn in
    either file alone and this fails — that divergence is exactly a toggle
    the UI cannot find (or a first-run preset that lies about what is on).
    """
    ts_rows = _parse_ts_mirror()
    assert ts_rows, f"failed to parse {_TS_MIRROR}"
    assert [r["id"] for r in ts_rows] == [p.id for p in PLUGINS], (
        "plugin id list/order diverges from src/lib/plugins.tsx"
    )
    py_by_id = {p.id: p for p in PLUGINS}
    for row in ts_rows:
        p = py_by_id[row["id"]]
        assert row["cost"] == p.cost, f"cost diverges for {row['id']}"
        assert row["core"] == p.core, f"core diverges for {row['id']}"
        assert row["defaultOn"] == p.defaultOn, f"defaultOn diverges for {row['id']}"
        assert row["settingsKey"] == p.settingsKey, f"settingsKey diverges for {row['id']}"


def test_every_settingskey_is_unique_and_prefixed() -> None:
    keys = [p.settingsKey for p in PLUGINS]
    assert len(keys) == len(set(keys)), "settingsKey collision in the registry"
    for k in keys:
        assert k.startswith("plugin_"), k


# ── HTTP contract ────────────────────────────────────────────────────────────

@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_get_plugins_payload_contract(client: AsyncClient) -> None:
    r = await client.get("/api/plugins")
    assert r.status_code == 200
    j = r.json()
    ids = [p["id"] for p in j["plugins"]]
    assert ids == [p.id for p in PLUGINS]
    core = sorted(p["id"] for p in j["plugins"] if p["core"])
    assert core == ["channels", "link-download"]
    # enabled[] mirrors the per-plugin flags
    enabled_map_payload = {p["id"]: p["enabled"] for p in j["plugins"]}
    assert set(j["enabled"]) == {k for k, v in enabled_map_payload.items() if v}
    # a fresh install answers the registry defaults
    for p in j["plugins"]:
        py_def = next(d for d in PLUGINS if d.id == p["id"])
        assert p["enabled"] is py_def.defaultOn


async def test_post_plugin_persists_via_route(client: AsyncClient) -> None:
    # A real state CHANGE in each direction: transcribe ships OFF, live-chat
    # ships ON, so each toggle writes (set_plugin skips a no-op on purpose).
    r = await client.post("/api/plugins", json={"id": "transcribe", "enabled": True})
    assert r.status_code == 200
    assert _on_disk()["plugins"]["plugin_transcribe"] is True
    payload = r.json()
    by_id = {p["id"]: p["enabled"] for p in payload["plugins"]}
    assert by_id["transcribe"] is True
    assert "transcribe" in payload["enabled"]
    # and off again through the same route
    r2 = await client.post("/api/plugins", json={"id": "live-chat", "enabled": False})
    assert r2.status_code == 200
    assert _on_disk()["plugins"]["plugin_live_chat"] is False
    by_id = {p["id"]: p["enabled"] for p in r2.json()["plugins"]}
    assert by_id["live-chat"] is False
    assert "live-chat" not in r2.json()["enabled"]


async def test_post_plugin_refuses_core_disable_and_unknown_id(client: AsyncClient) -> None:
    r = await client.post("/api/plugins", json={"id": "channels", "enabled": False})
    assert r.status_code == 400
    assert enabled_map()["channels"] is True
    r2 = await client.post("/api/plugins", json={"id": "no-such-plugin", "enabled": True})
    assert r2.status_code == 400


async def test_post_plugin_rejects_malformed_body(client: AsyncClient) -> None:
    assert (await client.post("/api/plugins", json={"id": "transcribe"})).status_code == 400
    assert (await client.post("/api/plugins", json={"enabled": True})).status_code == 400
    assert (await client.post("/api/plugins", json={"id": 7, "enabled": True})).status_code == 400
    assert (await client.post("/api/plugins", json={"id": "transcribe", "enabled": "yes"})).status_code == 400

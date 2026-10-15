"""Plugin registry — the nucleus tests for the 2026-10-15 model.

The model changed: only FOUR entries are plugins now (transcribe, live-preview,
live-subtitles, ai-ask), because a plugin is something HEAVY. The eight
features that cost no real disk/VRAM/RAM moved to ALWAYS_ON and are NOT
plugins — and is_enabled() answers TRUE for anything that is not a plugin, so
the existing runtime gates keep rendering after that move.

Contract covered here:
  * the exact 4 plugin ids and the exact 8 always-on ids;
  * is_enabled() is TRUE for always-on features and invented ids, and resolves
    the STORE for a real plugin;
  * get_manifest() order: enabled -> recommended -> cheapest -> id;
  * cost_bundle() honours shares_with (transcribe + live-subtitles share the
    397 MB parakeet weights, so disk/VRAM are charged once);
  * tolerant migration: a settings.json without "plugins" resolves the three
    aliased plugins from the stored FEATURE flag, which beats defaultOn;
  * unknown/legacy keys in the "plugins" dict are ignored on read and KEPT on
    write (including the keys of the eight that stopped being plugins);
  * set_plugin() persists; a no-op does not rewrite the file;
  * the CAS merge protects the plugins dict from a concurrent writer;
  * GET/POST /api/plugins carries plugins/alwaysOn/totals in the right shape;
  * the src/lib/plugins.tsx mirror matches the backend 1:1.

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
    ALWAYS_ON,
    PLUGINS,
    PLUGIN_FEATURE_ALIAS,
    cost_bundle,
    defaults,
    enabled_map,
    get_always_on,
    get_manifest,
    is_enabled,
    is_plugin,
    set_plugin,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TS_MIRROR = _REPO_ROOT / "src" / "lib" / "plugins.tsx"

# Decision, not description — the four heavy, toggleable plugins and their
# PLUGINS order (the catalog order the backend defines).
PLUGIN_IDS = ["transcribe", "live-preview", "live-subtitles", "ai-ask"]
# The eight features that are NOT plugins: nothing about them is heavy enough
# to earn a toggle, so they are the app and are always on.
ALWAYS_ON_IDS = [
    "link-download",
    "channels",
    "channel-explore",
    "clip-editor",
    "twitch-clip",
    "archive-search",
    "local-file",
    "live-chat",
]
# The exact public shape of one get_manifest() row / one GET /api/plugins row.
PLUGIN_ROW_KEYS = {
    "id", "name", "blurb", "settingsKey",
    "disk_mb", "vram_mb", "ram_mb",
    "shares_with", "recommended", "defaultOn", "enabled",
}


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


def _write_settings(payload: dict) -> None:
    """Write a raw settings.json and reload the manager from it (tolerant
    migration reads the FILE, so a test must not go through save())."""
    settings_mgr._settings_file.write_text(json.dumps(payload), encoding="utf-8")
    settings_mgr._settings = settings_mgr._load()


# ── 1. the catalog: 4 plugins + 8 always-on ─────────────────────────────────

def test_catalog_four_plugins_and_eight_always_on() -> None:
    assert [p.id for p in PLUGINS] == PLUGIN_IDS
    assert [a["id"] for a in ALWAYS_ON] == ALWAYS_ON_IDS
    assert [a["id"] for a in get_always_on()] == ALWAYS_ON_IDS
    assert len(ALWAYS_ON) == 8
    # Disjoint by construction: a heavy plugin must not also be "always on".
    assert not (set(PLUGIN_IDS) & set(ALWAYS_ON_IDS))
    for a in get_always_on():
        assert set(a) == {"id", "name", "blurb"}
    # is_plugin() partitions exactly the same way.
    for pid in PLUGIN_IDS:
        assert is_plugin(pid) is True
    for aid in ALWAYS_ON_IDS:
        assert is_plugin(aid) is False
    assert is_plugin("invented-id") is False
    # settingsKey convention, defaults() keys, and the one share edge.
    for p in PLUGINS:
        assert p.settingsKey == "plugin_" + p.id.replace("-", "_")
    assert defaults() == {p.settingsKey: p.defaultOn for p in PLUGINS}
    assert {p.id: p.defaultOn for p in PLUGINS} == {
        "transcribe": False, "live-preview": False,
        "live-subtitles": False, "ai-ask": False,
    }
    assert next(p for p in PLUGINS if p.id == "live-subtitles").shares_with == "transcribe"
    assert next(p for p in PLUGINS if p.id == "transcribe").shares_with == ""


# ── 2. is_enabled: always-on / invented -> TRUE, plugin -> the store ─────────

def test_is_enabled_true_for_always_on_and_invented_ids() -> None:
    for aid in ALWAYS_ON_IDS:
        assert is_enabled(aid) is True, f"{aid} must stay usable without a toggle"
    # The rule: anything that is not a plugin is enabled. An invented id must
    # not vanish a surface (the old gates are still wrapped in these ids).
    assert is_enabled("totally-invented-id") is True
    assert is_enabled("") is True


def test_is_enabled_resolves_the_stored_plugin_value() -> None:
    # Fresh install: every heavy plugin ships OFF.
    for pid in PLUGIN_IDS:
        assert is_enabled(pid) is False
    _write_settings({"plugins": {"plugin_transcribe": True, "plugin_ai_ask": True}})
    assert is_enabled("transcribe") is True
    assert is_enabled("ai-ask") is True
    assert is_enabled("live-preview") is False


# ── 3. get_manifest order + shape ───────────────────────────────────────────

def test_get_manifest_order_enabled_recommended_cheapest_id() -> None:
    # Fresh (all off, no aliased features stored): recommended first by cost
    # (live-preview 0+400 < transcribe 397+2048), then non-recommended by cost
    # (ai-ask 0+0 < live-subtitles 397+2048).
    assert [d["id"] for d in get_manifest()] == [
        "live-preview", "transcribe", "ai-ask", "live-subtitles",
    ]
    # An ENABLED plugin jumps ahead of everything, even a cheaper recommended.
    _write_settings({"plugins": {"plugin_transcribe": True}})
    assert [d["id"] for d in get_manifest()] == [
        "transcribe", "live-preview", "ai-ask", "live-subtitles",
    ]
    # Structural invariants: within one (enabled, recommended) bucket the
    # order is cost ascending, ties broken by id.
    _write_settings({})
    rows = get_manifest()
    for a, b in zip(rows, rows[1:]):
        if (a["enabled"], a["recommended"]) == (b["enabled"], b["recommended"]):
            assert (a["disk_mb"] + a["vram_mb"], a["id"]) <= (
                b["disk_mb"] + b["vram_mb"], b["id"]
            ), f"{a['id']} must not precede {b['id']}"


def test_get_manifest_rows_carry_the_public_shape() -> None:
    rows = get_manifest()
    assert {d["id"] for d in rows} == set(PLUGIN_IDS)
    for row in rows:
        assert set(row) == PLUGIN_ROW_KEYS, f"unexpected row shape for {row.get('id')}"
        assert isinstance(row["enabled"], bool)
        assert isinstance(row["recommended"], bool)
        for field in ("disk_mb", "vram_mb", "ram_mb"):
            assert isinstance(row[field], int)
    assert {d["id"]: d["recommended"] for d in rows} == {
        "transcribe": True, "live-preview": True,
        "live-subtitles": False, "ai-ask": False,
    }


# ── 4. cost_bundle + shares_with ────────────────────────────────────────────

def test_cost_bundle_charges_shared_model_once() -> None:
    # THE case: both ASR plugins run the same 397 MB parakeet weights. Disk and
    # VRAM are charged once; only the per-plugin runtime RAM adds up.
    assert cost_bundle(["transcribe", "live-subtitles"]) == {
        "disk_mb": 397, "vram_mb": 2048, "ram_mb": 4096,
    }
    # live-subtitles alone still pays for the shared weights it downloads.
    assert cost_bundle(["live-subtitles"]) == {
        "disk_mb": 397, "vram_mb": 2048, "ram_mb": 1536,
    }
    assert cost_bundle(["transcribe"]) == {
        "disk_mb": 397, "vram_mb": 2048, "ram_mb": 2560,
    }


def test_cost_bundle_without_shares_sums_each_plugin() -> None:
    assert cost_bundle([]) == {"disk_mb": 0, "vram_mb": 0, "ram_mb": 0}
    # Non-plugin ids contribute nothing (and are not an error).
    assert cost_bundle(["link-download", "channels", "invented"]) == {
        "disk_mb": 0, "vram_mb": 0, "ram_mb": 0,
    }
    assert cost_bundle(["transcribe", "live-preview"]) == {
        "disk_mb": 397, "vram_mb": 2448, "ram_mb": 3160,
    }
    assert cost_bundle(["live-preview", "ai-ask"]) == {
        "disk_mb": 0, "vram_mb": 400, "ram_mb": 728,
    }


def test_cost_bundle_shared_model_is_charged_once_regardless_of_order() -> None:
    """The docstring promises the shared model is charged to whichever of the
    pair is enabled and appears FIRST IN PLUGINS order — so the caller's list
    order must not change the total.

    KNOWN BUG (found 2026-10-15, reported to the owner, backend NOT touched by
    this test file): with ["live-subtitles", "transcribe"] the sharer is
    charged before its owner and transcribe charges the 397 MB / 2048 MB again
    -> 794 / 4096. The GET /api/plugins route escapes it today only because it
    feeds get_manifest() order, which puts transcribe first; the function is
    still order-dependent for any other caller.
    """
    forward = cost_bundle(["transcribe", "live-subtitles"])
    backward = cost_bundle(["live-subtitles", "transcribe"])
    assert backward == forward, (
        "cost_bundle double-counts the shared parakeet weights when the "
        "sharer (live-subtitles) is listed before its owner (transcribe)"
    )


# ── 5. tolerant migration: feature flag beats defaultOn ─────────────────────

def test_feature_flag_wins_over_defaulton() -> None:
    """An install predating the plugin system has no "plugins" key: the three
    aliased plugins resolve from the stored FEATURE flag, NOT defaultOn."""
    _write_settings({
        "features": {
            "transcribe-vod": True,
            "live-captions": True,
            "live-preview": True,
            "chat-live": True,
        }
    })
    assert enabled_map() == {
        "transcribe": True,
        "live-preview": True,
        "live-subtitles": True,
        "ai-ask": False,  # no alias, ships off
    }


def test_plugin_key_wins_over_the_feature_flag() -> None:
    """Once a plugin toggle writes its own key, that key wins forever."""
    _write_settings({
        "features": {
            "transcribe-vod": True,
            "live-captions": True,
            "live-preview": True,
        },
        "plugins": {
            "plugin_transcribe": False,
            "plugin_live_subtitles": False,
            "plugin_live_preview": False,
        },
    })
    state = enabled_map()
    assert state["transcribe"] is False
    assert state["live-subtitles"] is False
    assert state["live-preview"] is False


def test_plugin_feature_alias_no_longer_lists_live_chat() -> None:
    assert PLUGIN_FEATURE_ALIAS == {
        "transcribe": "transcribe-vod",
        "live-subtitles": "live-captions",
        "live-preview": "live-preview",
    }
    assert "live-chat" not in PLUGIN_FEATURE_ALIAS
    # chat-live is now just a feature behind an always-on surface: disabling
    # the feature cannot remove the plugin (there is none), and the surface
    # stays usable.
    _write_settings({"features": {"chat-live": False}})
    assert is_enabled("live-chat") is True
    assert "live-chat" not in enabled_map()


# ── 6. legacy/unknown keys: ignored on read, kept on write ──────────────────

def test_legacy_unknown_keys_ignored_on_read() -> None:
    _write_settings({
        "plugins": {
            "plugin_removed_in_v2": True,
            "plugin_live_chat": False,   # was a plugin, is always-on now
            "plugin_clip_editor": True,  # same
            "plugin_link_download": True,
            "not_a_plugin_key": True,
        }
    })
    state = enabled_map()
    # Reads only the 4 real plugin settingsKeys; junk changes nothing.
    assert state == {p.id: p.defaultOn for p in PLUGINS}
    assert set(state) == set(PLUGIN_IDS)


def test_legacy_unknown_keys_kept_on_write() -> None:
    """Renaming/removing a plugin id must never destroy the user's choice."""
    legacy = {
        "plugin_removed_in_v2": True,
        "plugin_live_chat": False,
        "plugin_clip_editor": True,
        "plugin_link_download": True,
    }
    _write_settings({"plugins": dict(legacy)})
    set_plugin("ai-ask", True)
    stored = _on_disk()["plugins"]
    for key, value in legacy.items():
        assert stored.get(key) == value, f"legacy key {key} was destroyed"
    assert stored["plugin_ai_ask"] is True


# ── 7. persistence + no-op ──────────────────────────────────────────────────

def test_set_plugin_persists_one_boolean_key() -> None:
    set_plugin("transcribe", True)
    assert _on_disk()["plugins"]["plugin_transcribe"] is True
    assert enabled_map()["transcribe"] is True
    # and a real change back off again
    set_plugin("transcribe", False)
    assert _on_disk()["plugins"]["plugin_transcribe"] is False
    assert enabled_map()["transcribe"] is False


def test_noop_toggle_does_not_rewrite_the_file(monkeypatch) -> None:
    """A no-op must not WRITE — no save() call, not just identical bytes.

    Byte equality alone cannot prove this: model_dump_json is deterministic,
    so a full rewrite of the same state produces the same bytes. The spy on
    settings_mgr.save() is what makes "did it write?" observable at all.
    """
    set_plugin("transcribe", True)
    calls = {"n": 0}
    real_save = settings_mgr.save

    def spy(s):
        calls["n"] += 1
        return real_save(s)

    monkeypatch.setattr(settings_mgr, "save", spy)
    before = settings_mgr._settings_file.read_bytes()
    set_plugin("transcribe", True)  # effective value already True -> no-op
    assert calls["n"] == 0, "a no-op toggle called settings_mgr.save()"
    assert settings_mgr._settings_file.read_bytes() == before
    assert enabled_map()["transcribe"] is True


# ── 8. CAS merge protects the plugins dict ──────────────────────────────────

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


# ── 9. HTTP route: plugins / alwaysOn / totals ──────────────────────────────

@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_get_plugins_payload_shape(client: AsyncClient) -> None:
    r = await client.get("/api/plugins")
    assert r.status_code == 200
    j = r.json()
    assert set(j) == {"plugins", "alwaysOn", "totals", "enabled"}
    # plugins is the manifest, in manifest order.
    assert [p["id"] for p in j["plugins"]] == [d["id"] for d in get_manifest()]
    for row in j["plugins"]:
        assert set(row) == PLUGIN_ROW_KEYS
    # alwaysOn is the 8, no toggle, exact shape.
    assert [a["id"] for a in j["alwaysOn"]] == ALWAYS_ON_IDS
    for a in j["alwaysOn"]:
        assert set(a) == {"id", "name", "blurb"}
    # fresh install: nothing heavy on, so the totals are zero.
    assert j["totals"] == {"disk_mb": 0, "vram_mb": 0, "ram_mb": 0}
    assert j["enabled"] == []


async def test_get_plugins_totals_respect_shares(client: AsyncClient) -> None:
    _write_settings({
        "plugins": {"plugin_transcribe": True, "plugin_live_subtitles": True}
    })
    r = await client.get("/api/plugins")
    assert r.status_code == 200
    j = r.json()
    assert [p["id"] for p in j["plugins"]] == [
        "transcribe", "live-subtitles", "live-preview", "ai-ask",
    ]
    # 397 MB once, 2048 VRAM once, and both RAM peaks.
    assert j["totals"] == {"disk_mb": 397, "vram_mb": 2048, "ram_mb": 4096}
    assert j["enabled"] == ["transcribe", "live-subtitles"]
    by_id = {p["id"]: p["enabled"] for p in j["plugins"]}
    assert by_id == {
        "transcribe": True, "live-subtitles": True,
        "live-preview": False, "ai-ask": False,
    }
    # enabled[] carries plugin ids only — an always-on feature never appears.
    assert not (set(j["enabled"]) & set(ALWAYS_ON_IDS))


async def test_post_plugin_persists_via_route(client: AsyncClient) -> None:
    r = await client.post("/api/plugins", json={"id": "transcribe", "enabled": True})
    assert r.status_code == 200
    assert _on_disk()["plugins"]["plugin_transcribe"] is True
    j = r.json()
    assert {p["id"]: p["enabled"] for p in j["plugins"]}["transcribe"] is True
    assert "transcribe" in j["enabled"]
    assert j["totals"] == {"disk_mb": 397, "vram_mb": 2048, "ram_mb": 2560}
    # and off again through the same route
    r2 = await client.post("/api/plugins", json={"id": "transcribe", "enabled": False})
    assert r2.status_code == 200
    assert _on_disk()["plugins"]["plugin_transcribe"] is False
    assert "transcribe" not in r2.json()["enabled"]


async def test_post_plugin_refuses_unknown_and_malformed(client: AsyncClient) -> None:
    r = await client.post("/api/plugins", json={"id": "no-such-plugin", "enabled": True})
    assert r.status_code == 400
    assert enabled_map() == {p.id: p.defaultOn for p in PLUGINS}
    assert (await client.post("/api/plugins", json={"id": "transcribe"})).status_code == 400
    assert (await client.post("/api/plugins", json={"enabled": True})).status_code == 400
    assert (await client.post("/api/plugins", json={"id": 7, "enabled": True})).status_code == 400
    assert (await client.post("/api/plugins", json={"id": "transcribe", "enabled": "yes"})).status_code == 400


# ── 10. registry <-> frontend mirror drift ──────────────────────────────────
# The mirror is being rewritten by another subagent as this file lands; this
# test is expected to fail until that edit is complete. It is written anyway —
# the divergence it catches (a toggle the UI cannot find, a cost the dialog
# lies about) is exactly the contract risk.

_TS_FIELD = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*"
    r"('(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|true|false|-?\d+(?:\.\d+)?)"
)
_TS_OBJECT = re.compile(r"\{([^{}]*)\}")


def _matching_bracket(text: str, open_idx: int) -> str:
    """Return the inside of the [...] that starts at open_idx, quote-aware."""
    depth = 0
    quote = ""
    i = open_idx
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1:i]
        i += 1
    raise AssertionError(f"unterminated array in {_TS_MIRROR}")


def _ts_value(raw: str):
    if raw[0] in "'\"":
        return raw[1:-1].replace("\\'", "'").replace('\\"', '"').replace("\\\\", "\\")
    if raw == "true":
        return True
    if raw == "false":
        return False
    return float(raw) if "." in raw else int(raw)


def _parse_ts_plugin_rows() -> list[dict]:
    """Rows of the PLUGINS array in src/lib/plugins.tsx, key-order agnostic.

    Only objects that carry BOTH id and settingsKey are kept, so an ALWAYS_ON
    list in the same file (no settingsKey) is ignored.
    """
    text = _TS_MIRROR.read_text(encoding="utf-8")
    m = re.search(r"(?:export\s+)?const\s+PLUGINS\b[^=]*=\s*\[", text)
    body = _matching_bracket(text, m.end() - 1) if m else text
    rows: list[dict] = []
    for obj in _TS_OBJECT.findall(body):
        fields: dict = {}
        for fm in _TS_FIELD.finditer(obj):
            fields.setdefault(fm.group(1), _ts_value(fm.group(2)))
        if "id" in fields and "settingsKey" in fields:
            rows.append(fields)
    return rows


def test_registry_matches_frontend_mirror() -> None:
    rows = _parse_ts_plugin_rows()
    assert rows, f"failed to parse the PLUGINS catalog out of {_TS_MIRROR}"
    by_id = {r["id"]: r for r in rows}
    assert set(by_id) == set(PLUGIN_IDS), (
        f"plugin ids diverge from {_TS_MIRROR.name}: "
        f"mirror={sorted(by_id)} backend={PLUGIN_IDS}"
    )
    for p in PLUGINS:
        row = by_id[p.id]
        assert row.get("settingsKey") == p.settingsKey, f"settingsKey diverges for {p.id}"
        assert "recommended" in row, f"recommended missing in the mirror for {p.id}"
        assert bool(row["recommended"]) == p.recommended, f"recommended diverges for {p.id}"
        assert (row.get("shares_with") or "") == p.shares_with, f"shares_with diverges for {p.id}"
        assert "defaultOn" in row, f"defaultOn missing in the mirror for {p.id}"
        assert bool(row["defaultOn"]) == p.defaultOn, f"defaultOn diverges for {p.id}"
        for field in ("disk_mb", "vram_mb", "ram_mb"):
            assert field in row, f"{field} missing in the mirror row for {p.id}"
            assert int(row[field]) == getattr(p, field), f"{field} diverges for {p.id}"

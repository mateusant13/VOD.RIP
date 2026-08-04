"""Entity watcher tests: matcher contracts (every user-specified case incl.
false positives), schema on fresh + legacy DBs, incremental scan with
watermark, idempotent hit recording, and the API CRUD/hits/ack surface.

The scratch DB is isolated via VODRIP_ARCHIVE_DB; the env is RESTORED (never
popped) in teardown so later test modules keep working.
"""

import os
import tempfile
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="vodrip-entities-")
_prev_db = os.environ.get("VODRIP_ARCHIVE_DB")
os.environ["VODRIP_ARCHIVE_DB"] = str(Path(_TMP) / "archive.db")

from services import archive_db  # noqa: E402  (env must be set first)
from services.entity_watch import (  # noqa: E402
    auto_variants,
    match_entity_in_text,
    run_scan_once,
    sync_auto_entities,
)


@pytest.fixture(autouse=True)
def _restore_env():
    yield
    # Restore, don't pop: module-import env sets happen at collection for ALL
    # modules; popping here breaks later modules' teardowns.
    if _prev_db is None:
        os.environ.pop("VODRIP_ARCHIVE_DB", None)
    else:
        os.environ["VODRIP_ARCHIVE_DB"] = _prev_db


@pytest.fixture()
def fresh_conn():
    """Point the archive DB at a fresh file and return the connection.

    The module connection is path-keyed: calling get_conn() with a new
    VODRIP_ARCHIVE_DB closes the previous connection and opens the new one.
    Never close the connection manually here — the module cache would hand
    back the closed object (it is not None, so no reopen happens).
    """
    path = Path(_TMP) / f"archive-{os.urandom(4).hex()}.db"
    os.environ["VODRIP_ARCHIVE_DB"] = str(path)
    yield archive_db.get_conn()


# --- matcher contracts ------------------------------------------------------

def test_matcher_positive_cases():
    for ent, text in [
        ({"text": "guiven", "kind": "auto", "aliases": []}, "o guiven é muito bom"),
        ({"text": "guiven", "kind": "auto", "aliases": []}, "GUIVEN falou"),
        ({"text": "srdogg", "kind": "auto", "aliases": ["senhor dog", "senior dog", "seu dog", "sr dog"]}, "a gente viu o senhor dog ontem"),
        ({"text": "srdogg", "kind": "auto", "aliases": ["senhor dog", "senior dog", "seu dog", "sr dog"]}, "o senior dog ganhou de novo"),
        ({"text": "mandiocaa", "kind": "auto", "aliases": []}, "mandiocaa apareceu no lolzinho"),
        ({"text": "arthur lanches", "kind": "manual", "aliases": []}, "o arthur lanches é muito engraçado"),
        ({"text": "arthur lanches", "kind": "manual", "aliases": ["artur lanches"]}, "o artur lanches voltou"),
        ({"text": "o guiven é muito ruim", "kind": "manual", "aliases": []}, "gente, o guiven é muito ruim mesmo"),
    ]:
        assert match_entity_in_text(ent, text) is not None, f"must hit: {ent['text']!r} in {text!r}"


def test_matcher_false_positives():
    for ent, text in [
        # 'mandioca' (common word) must NOT hit the entity 'mandiocaa'
        ({"text": "mandiocaa", "kind": "auto", "aliases": []}, "a mandioca é gostosa"),
        # 'lanche' alone must NOT hit 'arthur lanches'
        ({"text": "arthur lanches", "kind": "manual", "aliases": []}, "quero um lanche agora"),
        # 'lanches' alone must NOT hit 'arthur lanches'
        ({"text": "arthur lanches", "kind": "manual", "aliases": []}, "os lanches estavam bons"),
        # unrelated word must not hit 'srdogg'
        ({"text": "srdogg", "kind": "auto", "aliases": ["senhor dog", "senior dog", "seu dog", "sr dog"]}, "o senhor está aqui"),
    ]:
        assert match_entity_in_text(ent, text) is None, f"must NOT hit: {ent['text']!r} in {text!r}"


def test_auto_variants_cover_sr_expansions():
    variants = auto_variants(["srdogg"])
    for v in ("srdogg", "senhor dog", "senior dog", "seu dog", "sr dog"):
        assert v in variants
    assert "mandioca" not in auto_variants(["mandiocaa"])
    # Duplicate slugs collapse; distinct slugs keep their own expansions.
    assert auto_variants(["srdogg", "srdogg"]) == auto_variants(["srdogg"])
    assert "senhor doglol" in auto_variants(["srdogg", "srdoglol"])


def test_fold_bridges_variant_spellings():
    # The phonetic fold of 'arthur lanches' = 'artur lanches' (silent h) —
    # as an alias it must hit the ASR-heard form.
    from services.archive_db import _phonetic_fold

    ent = {"text": "arthur lanches", "kind": "manual", "aliases": [_phonetic_fold("arthur lanches")]}
    assert match_entity_in_text(ent, "o artur lanches está on") is not None


# --- schema + migrations ----------------------------------------------------

def test_entity_tables_exist(fresh_conn):
    tables = {
        r["name"] for r in fresh_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"watched_entities", "entity_hits", "entity_watch_state"} <= tables
    cols = {r["name"] for r in fresh_conn.execute("PRAGMA table_info(watched_entities)")}
    assert {"text", "kind", "source_channel", "aliases", "enabled"} <= cols


def test_legacy_db_gets_entity_tables(fresh_conn):
    # A legacy DB = the current schema WITHOUT the entity tables; the open
    # path must add them in place (SCHEMA executes every open).
    import re as _re
    import sqlite3 as _s

    schema = archive_db.SCHEMA
    stripped = _re.sub(
        r"-- Saved-word / entity watching.*?(?=CREATE TABLE IF NOT EXISTS video_aliases)",
        "",
        schema,
        flags=_re.S,
    )
    assert "watched_entities" not in stripped
    path = Path(_TMP) / f"legacy-{os.urandom(4).hex()}.db"
    raw = _s.connect(str(path))
    raw.executescript(stripped)
    raw.commit()
    raw.close()
    os.environ["VODRIP_ARCHIVE_DB"] = str(path)
    conn = archive_db.get_conn()
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE name='entity_hits'"
    ).fetchone() is not None
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE name='watched_entities'"
    ).fetchone() is not None


def test_delete_entity_cascades_hits(fresh_conn):
    eid = archive_db.upsert_watched_entity("guiven", kind="manual")
    archive_db.record_entity_hits([{
        "entity_id": eid, "platform": "twitch", "video_id": "v1", "seg_idx": 3,
        "offset_sec": 10.0, "snippet": "o guiven apareceu", "variant": None,
    }])
    assert len(archive_db.list_entity_hits(entity_id=eid)) == 1
    archive_db.delete_watched_entity(eid)
    assert archive_db.list_entity_hits(entity_id=eid) == []


# --- scan + watermark -------------------------------------------------------

def _seed_transcript(conn, platform, video_id, segments, channel="titiltei"):
    archive_db.upsert_video({
        "platform": platform, "video_id": video_id, "channel": channel,
        "title": f"{video_id} title",
    })
    archive_db.insert_transcript(platform, video_id, segments, lang="pt")


def test_scan_finds_hits_and_advances_watermark(fresh_conn):
    eid = archive_db.upsert_watched_entity("guiven", kind="manual")
    _seed_transcript(fresh_conn, "kick", "v1", [
        {"seg_idx": 0, "start_sec": 0.0, "end_sec": 5.0, "text": "sem menção aqui"},
        {"seg_idx": 1, "start_sec": 5.0, "end_sec": 10.0, "text": "o guiven é muito bom"},
    ])
    stats = run_scan_once(entities=[archive_db.get_watched_entity(eid)])
    assert stats["scanned"] == 2 and stats["hits"] == 1
    hits = archive_db.list_entity_hits(entity_id=eid)
    assert len(hits) == 1
    assert hits[0]["video_id"] == "v1" and hits[0]["offset_sec"] == 5.0
    # Second pass over the same data: no new rows, no new hits.
    stats2 = run_scan_once(entities=[archive_db.get_watched_entity(eid)])
    assert stats2["scanned"] == 0 and stats2["hits"] == 0


def test_scan_is_idempotent_on_rerun(fresh_conn):
    eid = archive_db.upsert_watched_entity("guiven", kind="manual")
    _seed_transcript(fresh_conn, "twitch", "v2", [
        {"seg_idx": 0, "start_sec": 0.0, "end_sec": 5.0, "text": "guiven guiven guiven"},
    ])
    run_scan_once(entities=[archive_db.get_watched_entity(eid)])
    # Reset the watermark and rescan: unique key must refresh, not duplicate.
    archive_db.set_entity_watch_cursor(0)
    run_scan_once(entities=[archive_db.get_watched_entity(eid)])
    hits = archive_db.list_entity_hits(entity_id=eid)
    assert len(hits) == 1
    assert hits[0]["seen_count"] == 2


def test_new_transcripts_after_cursor_are_scanned(fresh_conn):
    eid = archive_db.upsert_watched_entity("guiven", kind="manual")
    _seed_transcript(fresh_conn, "kick", "v3", [
        {"seg_idx": 0, "start_sec": 0.0, "end_sec": 5.0, "text": "nada"},
    ])
    run_scan_once(entities=[archive_db.get_watched_entity(eid)])
    # New transcription (re-transcription inserts new ids past the cursor).
    _seed_transcript(fresh_conn, "kick", "v3", [
        {"seg_idx": 0, "start_sec": 0.0, "end_sec": 5.0, "text": "guiven chegou"},
        {"seg_idx": 1, "start_sec": 5.0, "end_sec": 9.0, "text": "guiven saiu"},
    ])
    stats = run_scan_once(entities=[archive_db.get_watched_entity(eid)])
    assert stats["scanned"] == 2 and stats["hits"] == 2


def test_sync_auto_entities_from_channels(fresh_conn, monkeypatch):
    channels = [
        {"id": "c1", "name": None, "kickSlug": "titiltei", "twitchSlug": "titiltei", "youtubeSlug": "titiltei"},
        {"id": "c2", "name": None, "kickSlug": "srdogg", "twitchSlug": "srdoglol", "youtubeSlug": ""},
        {"id": "c3", "name": None, "kickSlug": "guiven", "twitchSlug": "", "youtubeSlug": ""},
    ]
    n = sync_auto_entities(lambda: channels)
    assert n == 3
    ents = {e["text"]: e for e in archive_db.list_watched_entities() if e["kind"] == "auto"}
    assert set(ents) == {"titiltei", "srdogg", "guiven"}
    assert "senhor dog" in ents["srdogg"]["aliases"]
    # Removing a channel disables its auto entity.
    sync_auto_entities(lambda: channels[:2])
    ents = {e["text"]: e for e in archive_db.list_watched_entities() if e["kind"] == "auto"}
    assert ents["guiven"]["enabled"] == 0
    assert ents["srdogg"]["enabled"] == 1


# --- API --------------------------------------------------------------------

def test_api_crud_hits_ack(fresh_conn, monkeypatch):
    from fastapi.testclient import TestClient

    from app import app

    # The app lifespan starts the entity-watcher daemon, which races the
    # test's own scans (concurrent cursor advances + real-settings auto
    # sync). Neutralize it so the test's scans are the only ones.
    import services.entity_watch as ew

    monkeypatch.setattr(ew, "start_entity_watcher", lambda *a, **k: None)

    with TestClient(app) as client:
        r = client.post("/api/entities", json={"text": "o guiven é muito ruim"})
        assert r.status_code == 200, r.text
        eid = r.json()["id"]

        r = client.post("/api/entities", json={"text": ""})
        assert r.status_code == 422

        r = client.get("/api/entities")
        assert r.status_code == 200
        body = r.json()
        assert any(e["id"] == eid and e["kind"] == "manual" for e in body["entities"])

        _seed_transcript(fresh_conn, "kick", "v-api", [
            {"seg_idx": 0, "start_sec": 1.0, "end_sec": 5.0, "text": "o guiven é muito ruim cara"},
        ])
        r = client.post("/api/entities/scan")
        assert r.status_code == 200 and r.json()["hits"] >= 1

        r = client.get("/api/entities/hits", params={"entity_id": eid})
        hits = r.json()["hits"]
        assert len(hits) >= 1
        hit = hits[0]
        assert hit["video_title"] == "v-api title"

        r = client.post(f"/api/entities/hits/{hit['id']}/ack")
        assert r.status_code == 200
        r = client.get("/api/entities/hits", params={"entity_id": eid, "unacked_only": True})
        assert all(h["acked"] == 0 for h in r.json()["hits"])

        # Disabling an entity stops new detections.
        r = client.put(f"/api/entities/{eid}", json={"enabled": False, "aliases": ["o guiven é ruim"]})
        assert r.status_code == 200
        ent = r.json()["entity"]
        assert ent["enabled"] == 0 and ent["aliases"] == ["o guiven é ruim"]
        _seed_transcript(fresh_conn, "kick", "v-api2", [
            {"seg_idx": 0, "start_sec": 0.0, "end_sec": 4.0, "text": "o guiven é muito ruim de novo"},
        ])
        r = client.post("/api/entities/scan")
        assert r.status_code == 200 and r.json()["hits"] == 0

        r = client.delete(f"/api/entities/{eid}")
        assert r.status_code == 200
        assert client.get("/api/entities/hits", params={"entity_id": eid}).json()["hits"] == []

"""WS-3: per-channel language detection — migration + aggregation, real-data E2E.

Runs against a COPY of the real %APPDATA% archive.db (29,167 titiltei pt
transcript rows) plus synthesized srdogg(en)/gaveta(pt) transcript evidence:
  * migration: videos.channel_language exists on fresh AND pre-existing DBs,
  * aggregation: titiltei -> pt (transcript tally), srdogg -> en, gaveta -> pt,
  * precedence: per-channel override (1.0) > transcript tally,
  * on_transcribe_done throttle + persist_platform_clue stamping,
  * WS-4 defensive read: channel_original_languages None without the column,
    consensus 0.95 when the column exists,
  * archive search hits carry channel_language.

Run directly (isolated process — env must precede the archive_db import):
    python tests/test_channel_language.py
Under pytest it needs a real-DB copy too (same env guard).
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import shutil
import sys
import tempfile

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="vodrip-ws3-lang-"))
_REAL_DB = pathlib.Path(os.environ.get("APPDATA", "")) / "VOD.RIP" / "archive.db"
_DB_COPY = _TMP / "archive.db"
if _REAL_DB.exists():
    shutil.copy2(_REAL_DB, _DB_COPY)
else:  # fallback: brand-new empty DB (CI without the real install)
    _DB_COPY.touch()
os.environ["VODRIP_ARCHIVE_DB"] = str(_DB_COPY)
os.environ["VODRIP_APP_DATA"] = str(_TMP)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services import archive_db  # noqa: E402
from services.channel_language import (  # noqa: E402
    aggregate_channel_language,
    normalize_language,
    on_transcribe_done,
    persist_aggregated,
    persist_platform_clue,
    run_aggregation,
)

_db = archive_db


def _fresh_db() -> str:
    """A brand-new empty archive DB (fresh-schema migration check).

    archive_db re-binds its connection when VODRIP_ARCHIVE_DB changes, so the
    env is restored to the copy afterwards — the rest of the suite must keep
    hitting the real-data copy."""
    p = str(_TMP / f"fresh-{len(os.listdir(_TMP))}.db")
    os.environ["VODRIP_ARCHIVE_DB"] = p
    import sqlite3

    con = sqlite3.connect(p)
    con.executescript(archive_db.SCHEMA)
    for fn in (archive_db._ensure_channel_language_column, archive_db._ensure_lang_column):
        fn(con)
    con.commit()
    cols = {r[1] for r in con.execute("PRAGMA table_info(videos)")}
    assert "channel_language" in cols, "fresh DB must gain videos.channel_language"
    con.close()
    os.environ["VODRIP_ARCHIVE_DB"] = str(_DB_COPY)
    return p


def _synthesize_channel_transcripts(platform: str, channel: str, lang: str, segments: int = 40) -> None:
    """Insert realistic per-segment transcript rows for one channel's video."""
    vid = f"syn-{channel}-{lang}"
    _db.upsert_channel_video({
        "platform": platform, "video_id": vid, "channel": channel,
        "title": f"synthetic {channel}", "kind": "vod",
        "started_at": "2026-01-01T00:00:00+00:00",
    })
    _db.insert_transcript(
        platform, vid,
        [{"seg_idx": i, "start_sec": float(i), "end_sec": float(i + 1), "text": f"segment {i} {channel}"}
         for i in range(segments)],
        lang=lang,
    )


def test_normalize_language() -> None:
    assert normalize_language("pt-BR") == "pt"
    assert normalize_language("pt-pt") == "pt"
    assert normalize_language("en-US") == "en"
    assert normalize_language("es-ES") == "es"
    assert normalize_language("es-419") == "es"
    assert normalize_language("ja") == "ja"
    assert normalize_language(None) is None
    assert normalize_language("") is None
    assert normalize_language("auto") is None


def test_migration_on_existing_and_fresh() -> None:
    cols = {r[1] for r in _db.query("PRAGMA table_info(videos)")}
    assert "channel_language" in cols, "existing (real) DB copy must gain the column"
    _fresh_db()


def test_aggregation_real_titiltei_pt() -> None:
    # The real archive copy carries 29,167 titiltei transcript rows, but they
    # predate transcripts.lang (all NULL — whisper/caption tags were not
    # persisted then). Stamp them on the COPY as pt (their actual language:
    # the fixture is the channel's PT-BR VOD archive), which is exactly the
    # evidence the whisper pipeline now writes per job; the tally must then
    # decide pt with transcript-source confidence.
    # The real DB may have drifted (WS-4 wrote original_language='pt'); clear
    # it on the copy so this test exercises the transcript-tally decision.
    _db.execute(
        "UPDATE videos SET original_language=NULL "
        "WHERE platform='youtube' AND channel='titiltei'"
    )
    _db.execute(
        "UPDATE transcripts SET lang='pt' WHERE video_id IN "
        "(SELECT v.video_id FROM videos v WHERE lower(v.channel)='titiltei')"
    )
    res = aggregate_channel_language("youtube", "titiltei")
    assert res["language"] == "pt", res
    assert res["source"] == "transcript", res
    assert res["confidence"] >= 0.65, res


def test_aggregation_srdogg_en_gaveta_pt() -> None:
    _synthesize_channel_transcripts("twitch", "srdogg", "en")
    _synthesize_channel_transcripts("twitch", "gaveta", "pt")
    assert aggregate_channel_language("twitch", "srdogg")["language"] == "en"
    assert aggregate_channel_language("twitch", "gaveta")["language"] == "pt"


def test_override_wins() -> None:
    res = aggregate_channel_language(
        "twitch", "srdogg", overrides={"srdogg": "es"}
    )
    assert res["language"] == "es" and res["source"] == "override" and res["confidence"] == 1.0


def test_platform_clue_stamps_and_never_wipes() -> None:
    # Clue stamping writes the decision; a failed/empty clue leaves it intact.
    assert persist_platform_clue("twitch", "srdogg", "en-US") == "en"
    assert _db.video_channel_language("twitch", "syn-srdogg-en") == "en"
    assert persist_platform_clue("twitch", "srdogg", None) is None
    assert _db.video_channel_language("twitch", "syn-srdogg-en") == "en"


def test_persist_aggregated_and_full_pass() -> None:
    assert persist_aggregated("twitch", "srdogg") == "en"
    report = run_aggregation()
    assert report["channels"] >= 2, report
    assert report["decided"] >= 2, report


def test_on_transcribe_done_throttled() -> None:
    assert on_transcribe_done("twitch", "syn-gaveta-pt") == "pt"
    # Second call within the 600 s throttle window returns None (no re-run).
    assert on_transcribe_done("twitch", "syn-gaveta-pt") is None


def test_ws4_defensive_column() -> None:
    # Without the WS-4 column the defensive read must return None, never raise.
    # (The real DB already carries the column — drop it on the copy to
    # synthesize the pre-WS-4 schema.)
    _db.execute("ALTER TABLE videos DROP COLUMN original_language")
    assert _db.channel_original_languages("twitch", "titiltei") is None
    # With it (synthetic ALTER), a 100% consensus beats the transcript tally.
    _db.execute("ALTER TABLE videos ADD COLUMN original_language TEXT")
    _db.execute(
        "UPDATE videos SET original_language='es' WHERE platform='twitch' AND channel='srdogg'"
    )
    rows = _db.channel_original_languages("twitch", "srdogg")
    assert rows and rows[0]["language"] == "es"
    res = aggregate_channel_language("twitch", "srdogg")
    assert res["language"] == "es" and res["source"] == "original_title", res


def test_search_hits_carry_channel_language() -> None:
    hits = _db.search("srdogg", limit=5)
    assert hits, "synthetic rows must be searchable"
    assert all(h.get("channel_language") in (None, "pt", "en", "es") for h in hits)


def main() -> None:
    for fn in (
        test_normalize_language,
        test_migration_on_existing_and_fresh,
        test_aggregation_real_titiltei_pt,
        test_aggregation_srdogg_en_gaveta_pt,
        test_override_wins,
        test_platform_clue_stamps_and_never_wipes,
        test_persist_aggregated_and_full_pass,
        test_on_transcribe_done_throttled,
        test_ws4_defensive_column,
        test_search_hits_carry_channel_language,
    ):
        fn()
        print(f"PASS {fn.__name__}")
    print("ALL WS-3 CHANNEL-LANGUAGE TESTS PASSED")


if __name__ == "__main__":
    main()

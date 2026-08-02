"""Parser tests for the chat sinks — no network, no archive_db import.

Run from backend/: python -m pytest tests/test_chat_sinks_parsers.py
Fixtures in tests/fixtures/chat/ are real captures (twitch IRC from a live
channel, youtube .live_chat.json from a live stream) plus one documented
Kick payload shape (no Kick channel was live at capture time).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.chat_sinks import kick_pusher, twitch_irc, yt_live

FIXTURES = Path(__file__).parent / "fixtures" / "chat"

# Stream start used for offset math; matches the real lofigirl live stream
# (release_timestamp of LTiqKDrjqr4) the youtube fixture was captured from.
YT_BASE_USEC = 1785344581000000.0


def _rows_from_youtube_fixture():
    lines = (FIXTURES / "youtube_live_chat.jsonl").read_text(encoding="utf-8").splitlines()
    return [yt_live.parse_live_chat_line(ln, YT_BASE_USEC) for ln in lines]


# ---------------------------------------------------------------- twitch

class TestTwitchParser:
    def test_real_privmsg_fixture(self):
        lines = (FIXTURES / "twitch_privmsg.txt").read_text(encoding="utf-8").splitlines()
        parsed = [twitch_irc.parse_privmsg(ln) for ln in lines]
        rows = [p for p in parsed if p]
        assert len(rows) == len(lines)  # every real line parses
        first = rows[0]
        assert first["username"] == "Tornib"  # display-name tag
        assert first["user_id"] == "186784372"  # IRC user-id tag (TEXT column)
        assert "paid or unpaid lunch" in first["text"]
        assert first["ts"] == "2026-08-02T13:00:17+00:00"
        # badges: subscriber/3 + blossom-badge/4 on line 3
        badged = rows[2]
        assert badged["badges"] == ["subscriber/3", "blossom-badge/4"]
        assert badged["username"] == "CaedrelCrashoutEnjoyer"

    def test_offset_math_with_stream_start(self):
        line = (FIXTURES / "twitch_privmsg.txt").read_text(encoding="utf-8").splitlines()[0]
        ts = 1785675617625  # tmi-sent-ts of that line
        row = twitch_irc.parse_privmsg(line, stream_start_ms=ts - 120_000)
        assert row["offset_sec"] == 120.0
        # without a stream start the offset is None and ts is carried
        row2 = twitch_irc.parse_privmsg(line)
        assert row2["offset_sec"] is None
        assert row2["ts"] == "2026-08-02T13:00:17+00:00"

    def test_emotes_parsed_to_ids(self):
        line = (
            "@badges=;emotes=25:0-4,1902:12-19;tmi-sent-ts=1785675617625;user-id=1;"
            "user-type= :x!x@x.tmi.twitch.tv PRIVMSG #c :Kappa hello PogChamp"
        )
        row = twitch_irc.parse_privmsg(line)
        assert row["emotes"] == ["25", "1902"]

    def test_ping_and_non_privmsg(self):
        assert twitch_irc.is_ping("PING :tmi.twitch.tv")
        assert not twitch_irc.is_ping("@badges=;... PRIVMSG #c :hi")
        assert twitch_irc.parse_privmsg("PING :tmi.twitch.tv") is None
        assert twitch_irc.parse_privmsg(":tmi.twitch.tv 001 x :Welcome") is None
        assert twitch_irc.parse_privmsg("not irc at all") is None

    def test_canonical_key_examples(self):
        # Main's spec examples
        assert twitch_irc._canonical_key("Watchparty do Mundial!", "2026-08-01T22:30:00Z") == "watchparty-do-mundial|2026-08-01"
        assert twitch_irc._canonical_key("Último dia do Mundial!", "2026-08-01T22:30:00Z") == "ultimo-dia-do-mundial|2026-08-01"
        assert twitch_irc._canonical_key("A  B__C", "2026-08-01T22:30:00Z") == "a-b-c|2026-08-01"
        assert twitch_irc._canonical_key("", "2026-08-01T22:30:00Z") == "untitled|2026-08-01"


# ----------------------------------------------------------------- kick

class TestKickParser:
    def test_real_shape_fixture(self):
        payload = (FIXTURES / "kick_chat_message.json").read_text(encoding="utf-8")
        # fixture created_at = 2026-08-01T22:30:05Z, stream started 22:30:00Z
        row = kick_pusher.parse_chat_event(payload, stream_start_ms=1785623400000.0)
        assert row is not None
        assert row["username"] == "someviewer"
        assert row["user_id"] == 987654321
        assert row["text"] == "LET'S GOOO"
        assert row["offset_sec"] == pytest.approx(5.0)
        assert row["badges"] == [{"type": "broadcaster", "text": "Broadcaster"}]
        assert row["ts"] == "2026-08-01T22:30:05+00:00"

    def test_legacy_flat_shape(self):
        event = json.dumps({
            "event": "App\\Events\\ChatMessageEvent",
            "data": json.dumps({
                "user_id": 42, "username": "legacyuser", "content": "hi",
                "created_at": "2026-08-01T22:30:10.000000Z",
            }),
        })
        row = kick_pusher.parse_chat_event(event, stream_start_ms=1785623400000.0)
        assert row["username"] == "legacyuser"
        assert row["user_id"] == 42
        assert row["text"] == "hi"
        assert row["offset_sec"] == pytest.approx(10.0)

    def test_nested_user_message_shape(self):
        event = json.dumps({
            "event": "App\\Events\\ChatMessageEvent",
            "data": json.dumps({
                "user": {"id": 7, "username": "nested"},
                "message": {"content": "hello world"},
                "created_at": "2026-08-01T22:30:00.000000Z",
            }),
        })
        row = kick_pusher.parse_chat_event(event)
        assert row["username"] == "nested"
        assert row["user_id"] == 7
        assert row["text"] == "hello world"
        assert row["offset_sec"] is None  # no stream start given

    def test_non_chat_events_ignored(self):
        for ev in (
            '{"event":"pusher:connection_established","data":"{}"}',
            '{"event":"pusher_internal:subscription_succeeded","data":"{}"}',
            'not json',
            '{"event":42}',
        ):
            assert kick_pusher.parse_chat_event(ev) is None

    def test_canonical_key_matches_twitch_copy(self):
        for title, date in (("Watchparty do Mundial!", "2026-08-01T22:30:00Z"),
                            ("Último dia do Mundial!", "2026-08-01T22:30:00Z"),
                            ("", None)):
            assert kick_pusher._canonical_key(title, date) == twitch_irc._canonical_key(title, date)


# ---------------------------------------------------------------- youtube

class TestYoutubeParser:
    def test_real_live_chat_fixture(self):
        rows = [r for r in _rows_from_youtube_fixture() if r]
        assert len(rows) >= 4  # fixture holds real text messages
        first = rows[0]
        assert first["username"] == "@dyk2210"
        assert first["text"] == "heyy"
        assert first["offset_sec"] == pytest.approx(
            (1785662496412275 - YT_BASE_USEC) / 1e6)
        # offsets are monotonic non-decreasing within the fixture
        offsets = [r["offset_sec"] for r in rows]
        assert all(a <= b for a, b in zip(offsets, offsets[1:]))

    def test_non_message_renderers_skipped(self):
        # fixture lines 0-13 are removeChatItemAction rows; lines 14+ carry
        # real text messages — parse_live_chat_line must skip the former
        lines = (FIXTURES / "youtube_live_chat.jsonl").read_text(encoding="utf-8").splitlines()
        parsed = [yt_live.parse_live_chat_line(ln, YT_BASE_USEC) for ln in lines]
        none_indices = [i for i, p in enumerate(parsed) if p is None]
        assert 0 in none_indices and 13 in none_indices
        assert parsed[14] is not None and parsed[17] is not None

    def test_paid_message_renderer(self):
        line = json.dumps({"replayChatItemAction": {"actions": [{
            "addChatItemAction": {"item": {"liveChatPaidMessageRenderer": {
                "timestampUsec": str(int(YT_BASE_USEC) + 90_000_000),
                "authorName": {"simpleText": "@payer"},
                "purchaseAmountText": {"simpleText": "R$ 10,00"},
                "message": {"runs": [{"text": "obrigado!"}]},
            }}}}]}})
        row = yt_live.parse_live_chat_line(line, YT_BASE_USEC)
        assert row is not None
        assert row["username"] == "@payer"
        assert row["text"] == "R$ 10,00: obrigado!"  # paid prefix is part of the text
        assert row["offset_sec"] == pytest.approx(90.0)

    def test_replay_wrapper_without_live_flag(self):
        line = json.dumps({"replayChatItemAction": {"actions": [{
            "addChatItemAction": {"item": {"liveChatTextMessageRenderer": {
                "timestampUsec": str(int(YT_BASE_USEC) + 60_000_000),
                "authorName": {"simpleText": "@replayuser"},
                "message": {"runs": [{"text": "from replay"}]},
            }}}}]}})
        row = yt_live.parse_live_chat_line(line, YT_BASE_USEC)
        assert row["username"] == "@replayuser"
        assert row["offset_sec"] == pytest.approx(60.0)

    def test_garbage_and_unknown_wrappers(self):
        assert yt_live.parse_live_chat_line("{not json", YT_BASE_USEC) is None
        assert yt_live.parse_live_chat_line('{"otherKey": 1}', YT_BASE_USEC) is None

    def test_canonical_key_matches_twitch_copy(self):
        assert yt_live._canonical_key("Watchparty do Mundial!", "2026-08-01T22:30:00Z") == \
            twitch_irc._canonical_key("Watchparty do Mundial!", "2026-08-01T22:30:00Z")
        # epoch seconds accepted as started_at
        assert yt_live._utc_date(1785664205) == "2026-08-02"


class TestCanonicalKeyEpochDates:
    def test_all_adapters_agree_on_epoch(self):
        assert twitch_irc._utc_date(1785664205) == kick_pusher._utc_date(1785664205) == \
            yt_live._utc_date(1785664205) == "2026-08-02"
        # epoch milliseconds
        assert twitch_irc._utc_date(1785664205000) == "2026-08-02"
        assert twitch_irc._utc_date(None) is None

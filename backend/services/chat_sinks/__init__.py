"""Live chat capture sinks — one class per platform, all buffering rows into
the local archive via ChatSink (flush every 5s or 100 rows)."""
from __future__ import annotations

from services.chat_sinks.base import ChatSink
from services.chat_sinks.kick_pusher import KickPusherSink
from services.chat_sinks.twitch_irc import TwitchIRCSink
from services.chat_sinks.yt_live import YTLiveSink

SINKS = {
    "twitch": TwitchIRCSink,
    "kick": KickPusherSink,
    "youtube": YTLiveSink,
}

# Module self-check: sink registry matches the archive contract's platforms.
assert set(SINKS) == {"twitch", "kick", "youtube"}
for _plat, _cls in SINKS.items():
    assert _cls.platform == _plat, (_plat, _cls)

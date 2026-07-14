"""Session PO token resolution and web_safari-first yt-dlp args."""

from services.youtube_session import (
    resolve_video_po_token,
    ytdlp_youtube_extractor_args,
    YouTubeSession,
)


def test_resolve_video_po_token_manual_wins():
    assert resolve_video_po_token("abc12345678", "manual-token") == "manual-token"


def test_resolve_video_po_token_empty_video():
    assert resolve_video_po_token("", "manual") == "manual"
    assert resolve_video_po_token("", None) is None


def test_ytdlp_args_web_safari_first():
    sess = YouTubeSession(visitor_data="vd")
    args = ytdlp_youtube_extractor_args(sess)
    assert args["player_client"][0] == "web_safari"
    assert args["fetch_pot"] in (["auto"], ["never"])

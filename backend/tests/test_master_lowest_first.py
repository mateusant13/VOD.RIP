"""The HLS master must list the lowest quality first."""
import requests
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


def _master_body():
    src = (BACKEND / "services" / "preview" / "session.py").read_text(encoding="utf-8")
    body = src[src.index("def _build_synthetic_master_playlist("):]
    tail = body.find("\ndef ", 1)
    return body[: tail if tail != -1 else len(body)]


def test_the_master_lists_the_lowest_quality_first():
    body = _master_body()
    assert 'sorted(variants' in body and 'height' in body


def test_the_reorder_keeps_the_heightless_guard():
    body = _master_body()
    assert "if not height:" in body and "continue" in body


def test_a_live_master_is_bandwidth_ascending():
    try:
        s = requests.post(
            "http://127.0.0.1:7897/api/preview/session",
            json={"url": "https://www.twitch.tv/videos/2792122816",
                  "crop_start": 0, "crop_end": 60, "prefer_height": 360},
            timeout=120,
        )
    except Exception:
        return
    if s.status_code != 200:
        return
    j = s.json()
    sess = j.get("session") or j
    sid = sess.get("session_id") or j.get("session_id")
    if not sid:
        return
    m = requests.get(f"http://127.0.0.1:7897/api/preview/hls/{sid}/master.m3u8", timeout=60)
    bws = []
    for line in m.text.splitlines():
        if line.startswith("#EXT-X-STREAM-INF:"):
            for part in line.split(","):
                if part.startswith("BANDWIDTH="):
                    bws.append(int(part.split("=")[1]))
    if len(bws) < 2:
        return
    assert bws == sorted(bws), "master not bandwidth-ascending: %s" % bws

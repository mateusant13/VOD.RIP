"""Find titiltei VODs in a duration window."""
import json
import sys
import urllib.request

API = "http://127.0.0.1:7897"
MIN_S = 5 * 3600
MAX_S = 7 * 3600


def fetch(platform: str) -> list:
    url = (
        f"{API}/api/channel/videos?url=titiltei&platforms={platform}"
        f"&limit=30&days=90"
    )
    with urllib.request.urlopen(url, timeout=120) as resp:
        data = json.loads(resp.read().decode())
    out = []
    for v in data.get("videos") or []:
        dur = float(v.get("duration") or 0)
        if MIN_S <= dur <= MAX_S:
            out.append(v)
    return out


if __name__ == "__main__":
    plat = sys.argv[1] if len(sys.argv) > 1 else "Kick"
    for v in fetch(plat):
        dur = float(v.get("duration") or 0)
        print(
            f"{v.get('id')}\t{v.get('duration_string')}\t{dur:.0f}s\t"
            f"{(v.get('title') or '')[:60]}"
        )

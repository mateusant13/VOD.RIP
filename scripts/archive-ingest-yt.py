#!/usr/bin/env python
"""Ingest YouTube channel videos into the local archive (YouTube adapter).

Channel walk uses the flat-playlist extractor; default tab is 'streams'
(past live streams are the videos most likely to have chat replay), with a
fallback to 'videos'. Each selected video is ingested: metadata upsert,
auto captions -> transcript segments, live-chat replay -> messages.

Usage:
  python scripts/archive-ingest-yt.py [--channel URL] [--tab streams|videos]
                                      [--limit N] [--video ID] [--json]

Examples:
  python scripts/archive-ingest-yt.py
  python scripts/archive-ingest-yt.py --video 7lc1GxEEvhM
  python scripts/archive-ingest-yt.py --limit 5 --tab videos
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

from services import archive_ytdlp  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--channel", default="https://www.youtube.com/@TiTiltei",
                    help="channel URL (default @TiTiltei)")
    ap.add_argument("--tab", default="streams", choices=("streams", "videos"),
                    help="channel tab to walk (default streams)")
    ap.add_argument("--limit", type=int, default=3,
                    help="number of most recent videos to ingest (default 3)")
    ap.add_argument("--video", default=None,
                    help="ingest one specific video id/URL instead of walking the channel")
    ap.add_argument("--json", action="store_true",
                    help="print the full per-video report as JSON")
    args = ap.parse_args(argv)

    if args.video:
        ids = [args.video]
    else:
        entries = archive_ytdlp.list_channel_videos(
            args.channel, tab=args.tab, limit=args.limit
        )
        if not entries:
            print(f"no videos found on {args.channel} (tab={args.tab})", file=sys.stderr)
            return 1
        ids = [e["id"] for e in entries]
        print(f"channel walk: {len(entries)} video(s) selected")
        for e in entries:
            print(f"  {e['id']}  {(e['title'] or '')[:60]}")

    reports = []
    for vid in ids:
        try:
            r = archive_ytdlp.ingest_video(vid)
        except Exception as exc:
            print(f"FAILED {vid}: {exc}", file=sys.stderr)
            reports.append({"video_id": vid, "error": str(exc)})
            continue
        reports.append(r)
        print(
            f"ok {r['video_id']}  segs={r['transcript_segments']} "
            f"chat={r['chat_messages']} ({r['chat']})  key={r['canonical_key']}"
        )

    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
    failed = [r for r in reports if r.get("error")]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

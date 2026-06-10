"""Run one Kick Playwright download in an isolated Python process.

Stdout protocol (one JSON object per line):
  {"type": "progress", "data": {...}}  — forwarded to the parent's progress hook
  {"type": "done", "path": "..."}      — success
  {"type": "error", "error": "..."}    — failure
"""

from __future__ import annotations

import json
import sys
import traceback
from typing import Any, Dict


def _emit(obj: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def main() -> int:
    payload = json.loads(sys.stdin.read())

    def progress_hook(data: Dict[str, Any]) -> None:
        _emit({"type": "progress", "data": data})

    try:
        from services.kick_playwright_service import download_vod_sync

        result = download_vod_sync(
            url=payload["url"],
            output_path=payload["output_path"],
            quality=payload.get("quality"),
            crop_start=payload.get("crop_start"),
            crop_end=payload.get("crop_end"),
            progress_hook=progress_hook,
        )
        _emit({"type": "done", "path": result})
        return 0
    except Exception as exc:
        _emit({"type": "error", "error": str(exc), "trace": traceback.format_exc()})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

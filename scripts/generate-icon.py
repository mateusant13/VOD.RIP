"""Refresh assets/icon.png from assets/icon.ico for macOS packaging.

Desktop / tray / exe only — not used in the web UI.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
ICO = ASSETS / "icon.ico"
PNG = ASSETS / "icon.png"
BUILD_ICO = ROOT / "build" / "icon.ico"


def _resolve_ico() -> Path:
    if ICO.is_file() and ICO.stat().st_size > 500:
        return ICO
    if BUILD_ICO.is_file():
        ASSETS.mkdir(parents=True, exist_ok=True)
        ICO.write_bytes(BUILD_ICO.read_bytes())
        return ICO
    print(f"Missing app icon. Add {ICO} or {BUILD_ICO}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    ico_path = _resolve_ico()
    from PIL import Image

    with Image.open(ico_path) as img:
        best = img
        try:
            frames = []
            for i in range(getattr(img, "n_frames", 1)):
                img.seek(i)
                frames.append(img.copy())
            if frames:
                best = max(frames, key=lambda f: f.size[0] * f.size[1])
        except Exception:
            pass
        ASSETS.mkdir(parents=True, exist_ok=True)
        best.save(PNG, format="PNG")

    print(f"App icon ready: {ico_path} ({ico_path.stat().st_size} bytes), {PNG}")


if __name__ == "__main__":
    main()

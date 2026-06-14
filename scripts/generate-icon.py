"""Refresh app icons: multi-size icon.ico (app + installer) and icon.png."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
ICO = ASSETS / "icon.ico"
PNG = ASSETS / "icon.png"
BUILD_ICO = ROOT / "build" / "icon.ico"

ICON_SIZES = (256, 128, 64, 48, 32, 16)


def _resolve_ico() -> Path:
    if ICO.is_file() and ICO.stat().st_size > 500:
        return ICO
    if BUILD_ICO.is_file():
        ASSETS.mkdir(parents=True, exist_ok=True)
        ICO.write_bytes(BUILD_ICO.read_bytes())
        return ICO
    print(f"Missing app icon. Add {ICO} or {BUILD_ICO}", file=sys.stderr)
    sys.exit(1)


def _load_ico_frames(ico_path: Path) -> list:
    from PIL import Image

    with Image.open(ico_path) as img:
        frames = []
        count = getattr(img, "n_frames", 1)
        for i in range(count):
            img.seek(i)
            frames.append(img.copy().convert("RGBA"))
        return frames


def _best_source_frame(frames: list):
    return max(frames, key=lambda f: f.size[0] * f.size[1])


def _frames_multisize(ico_path: Path) -> list:
    from PIL import Image

    source = _best_source_frame(_load_ico_frames(ico_path))
    return [
        source.resize((size, size), Image.Resampling.LANCZOS)
        for size in ICON_SIZES
    ]


def _save_ico(path: Path, frames: list) -> None:
    frames[0].save(
        path,
        format="ICO",
        sizes=[(f.size[0], f.size[1]) for f in frames],
        append_images=frames[1:],
    )


def main() -> None:
    ico_path = _resolve_ico()
    frames = _load_ico_frames(ico_path)
    best = _best_source_frame(frames)

    ASSETS.mkdir(parents=True, exist_ok=True)
    best.save(PNG, format="PNG")

    app_frames = _frames_multisize(ico_path)
    _save_ico(ICO, app_frames)

    print(f"App icon ready: {ICO} ({ICO.stat().st_size} bytes), {PNG}")


if __name__ == "__main__":
    main()

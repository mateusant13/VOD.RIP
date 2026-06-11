"""Refresh app icons: icon.png (macOS) and setup-icon.ico (installer badge)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
ICO = ASSETS / "icon.ico"
PNG = ASSETS / "icon.png"
SETUP_ICO = ASSETS / "setup-icon.ico"
BUILD_ICO = ROOT / "build" / "icon.ico"

SETUP_SIZES = (256, 128, 64, 48, 32, 16)
KICK_GREEN = (83, 252, 24, 255)
ARROW_FILL = (9, 9, 11, 255)


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


def _apply_setup_badge(img) -> object:
    """App icon with a green download-arrow badge on the top-right."""
    from PIL import Image, ImageDraw

    base = img.convert("RGBA")
    w, h = base.size
    side = min(w, h)
    badge_d = max(6, round(side * 0.36))
    margin = max(1, round(side * 0.06))
    x1 = w - margin - badge_d
    y1 = margin
    x2 = x1 + badge_d
    y2 = y1 + badge_d

    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.ellipse((x1, y1, x2, y2), fill=KICK_GREEN)

    bcx = (x1 + x2) / 2
    bcy = (y1 + y2) / 2
    aw = badge_d * 0.22
    ah = badge_d * 0.28
    stem_half = max(1, round(aw * 0.35))
    draw.rectangle(
        (
            bcx - stem_half,
            bcy - ah * 0.55,
            bcx + stem_half,
            bcy + ah * 0.12,
        ),
        fill=ARROW_FILL,
    )
    draw.polygon(
        (
            (bcx, bcy + ah * 0.58),
            (bcx - aw, bcy - ah * 0.08),
            (bcx + aw, bcy - ah * 0.08),
        ),
        fill=ARROW_FILL,
    )
    return Image.alpha_composite(base, overlay)


def _frames_for_setup(ico_path: Path) -> list:
    from PIL import Image

    source = _best_source_frame(_load_ico_frames(ico_path))
    out = []
    for size in SETUP_SIZES:
        resized = source.resize((size, size), Image.Resampling.LANCZOS)
        out.append(_apply_setup_badge(resized))
    return out


def main() -> None:
    from PIL import Image

    ico_path = _resolve_ico()
    frames = _load_ico_frames(ico_path)
    best = _best_source_frame(frames)

    ASSETS.mkdir(parents=True, exist_ok=True)
    best.save(PNG, format="PNG")

    setup_frames = _frames_for_setup(ico_path)
    setup_frames[0].save(
        SETUP_ICO,
        format="ICO",
        sizes=[(f.size[0], f.size[1]) for f in setup_frames],
        append_images=setup_frames[1:],
    )

    print(f"App icon ready: {ico_path} ({ico_path.stat().st_size} bytes), {PNG}")
    print(f"Setup icon ready: {SETUP_ICO} ({SETUP_ICO.stat().st_size} bytes)")


if __name__ == "__main__":
    main()

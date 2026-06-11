"""Generate VOD.RIP application icons (assets/icon.ico + assets/icon.png)."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
BG = (9, 9, 11, 255)
PURPLE = (145, 70, 255, 255)
WHITE = (250, 250, 250, 255)


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in ("arialbd.ttf", "Arial Bold.ttf", "segoeuib.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_icon(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = max(2, size // 16)
    draw.rounded_rectangle((pad, pad, size - pad, size - pad), radius=max(4, size // 6), fill=BG)

    font = _font(max(10, int(size * 0.34)))
    text = "V"
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    x = (size - tw) // 2 - max(1, size // 24)
    y = (size - th) // 2 - bbox[1]
    draw.text((x, y), text, fill=WHITE, font=font)

    dot = max(3, size // 10)
    cx = x + tw + max(1, size // 32)
    cy = y + th // 3
    draw.ellipse((cx, cy, cx + dot, cy + dot), fill=PURPLE)
    return img


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = [render_icon(s) for s in sizes]
    images[0].save(
        ASSETS / "icon.ico",
        format="ICO",
        sizes=[(s, s) for s in sizes],
        append_images=images[1:],
    )
    render_icon(256).save(ASSETS / "icon.png", format="PNG")
    print(f"Wrote {ASSETS / 'icon.ico'} and {ASSETS / 'icon.png'}")


if __name__ == "__main__":
    main()

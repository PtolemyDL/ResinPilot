"""Draw the ResinPilot icon (a resin drop on a rounded tile) into resinpilot/assets/branding/."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "resinpilot" / "assets" / "branding"
SIZE = 1024
TILE = (10, 132, 255)
DROP = (255, 255, 255)


def draw(size: int = SIZE) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 1024
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=int(228 * s), fill=TILE)
    # Drop: a circle with a triangle on top.
    cx, cy, r = 512 * s, 600 * s, 230 * s
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=DROP)
    tip = 170 * s
    dist = cy - tip
    ty = cy - r * r / dist
    tx = r * (1 - (r / dist) ** 2) ** 0.5
    d.polygon([(cx, tip), (cx - tx, ty), (cx + tx, ty)], fill=DROP)
    # Layer lines across the drop.
    for i, y in enumerate((560, 640, 720)):
        y *= s
        half = (r**2 - (y - cy) ** 2) ** 0.5 * 0.72
        d.rounded_rectangle((cx - half, y - 14 * s, cx + half, y + 14 * s), radius=int(14 * s), fill=TILE + (90 + 40 * i,))
    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    big = draw()
    big.resize((512, 512), Image.LANCZOS).save(OUT / "logo.png")
    big.resize((64, 64), Image.LANCZOS).save(OUT / "favicon.png")
    big.save(OUT / "favicon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote", OUT)


if __name__ == "__main__":
    main()

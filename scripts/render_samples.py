"""Render samples/**/*.txt into JPEGs that look like a phone photo of a printed letter.

Usage: python3 scripts/render_samples.py [samples_dir]
Needs Pillow and a TrueType font with Polish diacritics (PAPERTRAIL_FONT overrides).
"""
import os
import random
import sys
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT_CANDIDATES = [
    os.environ.get("PAPERTRAIL_FONT", ""),
    "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
]
PAGE_W, PAGE_H, MARGIN, SIZE = 1240, 1754, 110, 26


def load_font(size, bold=False):
    for path in FONT_CANDIDATES:
        if not path:
            continue
        if bold:
            bold_path = path.replace(".ttf", " Bold.ttf").replace("Serif.ttf", "Serif-Bold.ttf")
            if os.path.exists(bold_path):
                return ImageFont.truetype(bold_path, size)
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    sys.exit("no TrueType font with Polish glyphs found; set PAPERTRAIL_FONT")


def render_page(text):
    page = Image.new("RGB", (PAGE_W, PAGE_H), (250, 249, 244))
    draw = ImageDraw.Draw(page)
    regular, bold = load_font(SIZE), load_font(SIZE, bold=True)
    width_chars = 78
    y = MARGIN
    for i, raw in enumerate(text.splitlines()):
        heading = i < 3 or (raw.isupper() and len(raw) > 10)
        font = bold if heading else regular
        lines = textwrap.wrap(raw, width_chars) or [""]
        for line in lines:
            draw.text((MARGIN, y), line, font=font, fill=(25, 25, 30))
            y += int(SIZE * 1.45)
    return page


def photograph(page, seed):
    rnd = random.Random(seed)
    canvas = Image.new("RGB", (PAGE_W + 160, PAGE_H + 160), (92, 84, 74))
    canvas.paste(page, (80, 80))
    canvas = canvas.rotate(rnd.uniform(-2.0, 2.0), resample=Image.BICUBIC, fillcolor=(92, 84, 74))
    shade = Image.linear_gradient("L").resize(canvas.size).point(lambda v: 255 - v // 6)
    canvas = Image.composite(canvas, Image.new("RGB", canvas.size, (0, 0, 0)), shade)
    return canvas.filter(ImageFilter.GaussianBlur(0.6))


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "samples")
    for txt in sorted(root.rglob("*.txt")):
        out = txt.with_suffix(".jpg")
        photograph(render_page(txt.read_text(encoding="utf-8")), txt.name).save(out, quality=82)
        print(out)


if __name__ == "__main__":
    main()

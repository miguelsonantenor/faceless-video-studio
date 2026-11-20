"""Image helpers for what Claude sees: grids, safe-zone outlines, size limits (MCP-4)."""

from __future__ import annotations

from PIL import Image, ImageDraw

from fvs.analyze.sheet import label_image
from fvs.settings import SafeZoneSettings

MAX_IMAGE = 512  # one frame (MCP-4)
MAX_SHEET = 1024  # a sheet of frames
GAP = 6
BG = (24, 24, 24)
SAFE_COLOUR = (0, 255, 160)


def fit(img: Image.Image, box: int) -> Image.Image:
    scale = min(1.0, box / max(img.width, img.height))
    if scale >= 1.0:
        return img
    return img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                      Image.Resampling.LANCZOS)


def draw_safe_zone(img: Image.Image, safe: SafeZoneSettings) -> Image.Image:
    """Outline the area captions must stay inside, scaled from 1080×1920 (SEE-3)."""
    img = img.copy()
    sx, sy = img.width / 1080, img.height / 1920
    box = (safe.left * sx, safe.top * sy, img.width - safe.right * sx,
           img.height - safe.bottom * sy)
    ImageDraw.Draw(img).rectangle(box, outline=SAFE_COLOUR, width=max(1, round(3 * sx)))
    return img


def grid(images: list[Image.Image], labels: list[str], *, cols: int) -> Image.Image:
    """Tiles in a grid, each labelled, the whole sheet no larger than ``MAX_SHEET``."""
    tw = max(i.width for i in images)
    th = max(i.height for i in images)
    rows = (len(images) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tw + (cols + 1) * GAP, rows * th + (rows + 1) * GAP), BG)
    for k, (img, text) in enumerate(zip(images, labels, strict=True)):
        tile = img.convert("RGB")
        if text:
            label_image(tile, text)
        r, c = divmod(k, cols)
        sheet.paste(tile, (GAP + c * (tw + GAP), GAP + r * (th + GAP)))
    return fit(sheet, MAX_SHEET)

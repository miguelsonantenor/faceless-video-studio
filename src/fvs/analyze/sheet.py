"""Contact sheets: a 3×2 grid of frames with their times burned in (DS 8.3).

The sheet stays within 1024 px on its long side and is saved as JPEG (MCP-4).
"""

from __future__ import annotations

import io
from collections.abc import Callable
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from fvs.atomic import atomic_path
from fvs.media.tools import run_tool

COLS, ROWS = 3, 2
TILE_BOX = 320  # each frame fits in a 320×320 box, keeping its shape
GAP = 8
MAX_SIDE = 1024
BG = (23, 24, 28)


def even_times(duration_s: float, n: int = COLS * ROWS) -> list[float]:
    return [round(duration_s * (i + 0.5) / n, 2) for i in range(n)]


def fmt_time(t: float) -> str:
    m, s = divmod(t, 60)
    return f"{int(m)}:{s:04.1f}"


def tile_size(width: int, height: int) -> tuple[int, int]:
    if width >= height:
        return TILE_BOX, max(2, round(TILE_BOX * height / max(width, 1)))
    return max(2, round(TILE_BOX * width / max(height, 1))), TILE_BOX


def grab_frame(path: Path, t: float, size: tuple[int, int],
               cancelled: Callable[[], bool] | None = None) -> Image.Image | None:
    """One frame at ``t`` seconds, scaled to ``size``; None if there is no frame there."""
    w, h = size
    res = run_tool("ffmpeg", ["-hide_banner", "-loglevel", "error", "-ss", f"{t:.3f}",
                              "-i", str(path), "-frames:v", "1", "-vf", f"scale={w}:{h}",
                              "-f", "image2pipe", "-c:v", "png", "-"],
                   timeout=60, cancelled=cancelled)
    if res.returncode != 0 or not res.stdout:
        return None
    try:
        return Image.open(io.BytesIO(res.stdout)).convert("RGB")
    except OSError:
        return None


def _label(img: Image.Image, text: str) -> None:
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=16)
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    pad = 4
    box = (6, img.height - (bottom - top) - 2 * pad - 6, 6 + (right - left) + 2 * pad,
           img.height - 6)
    draw.rectangle(box, fill=(0, 0, 0))
    draw.text((box[0] + pad - left, box[1] + pad - top), text, fill=(255, 255, 255), font=font)


def make_sheet(path: Path, out: Path, *, width: int, height: int, duration_s: float,
               times: list[float] | None = None,
               cancelled: Callable[[], bool] | None = None) -> list[float]:
    """Write the sheet to ``out`` (JPEG). Returns the frame times used."""
    times = times or even_times(duration_s)
    tw, th = tile_size(width, height)
    sheet_w = COLS * tw + (COLS + 1) * GAP
    rows = (len(times) + COLS - 1) // COLS
    sheet_h = rows * th + (rows + 1) * GAP
    sheet = Image.new("RGB", (sheet_w, sheet_h), BG)
    for i, t in enumerate(times):
        frame = grab_frame(path, t, (tw, th), cancelled) or Image.new("RGB", (tw, th), (60, 0, 0))
        _label(frame, fmt_time(t))
        x = GAP + (i % COLS) * (tw + GAP)
        y = GAP + (i // COLS) * (th + GAP)
        sheet.paste(frame, (x, y))
    if max(sheet.size) > MAX_SIDE:
        sheet.thumbnail((MAX_SIDE, MAX_SIDE))
    with atomic_path(out) as tmp:
        sheet.save(tmp, format="JPEG", quality=82)
    return times

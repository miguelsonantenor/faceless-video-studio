"""The eight checks run after every render (QA-1, QA-2).

Each check gives pass, warn or fail with the measured value, the limit, and a fix that
names the Edit Spec field to change. The checks only read: they never change the render.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np

from fvs.analyze.probe import Probe
from fvs.compile.compiler import FPS, H, W
from fvs.media.tools import run_tool
from fvs.see.stills import segment_at
from fvs.settings import SafeZoneSettings

Status = Literal["pass", "warn", "fail"]
TAIL_MIN, TAIL_MAX = 0.3, 1.0
LOUDNESS_TOL = 1.0
VOICE_MIN_MATCH = 0.95
STILL_MAX_S = 0.5
MAX_CAPTION_SAMPLES = 40
ALPHA_MIN = 16  # caption pixels are those with more alpha than this (0–255)
SCALE = 2  # the caption layer is measured at half size

_TAGS = re.compile(r"\{[^}]*\}")
_DIALOGUE = re.compile(r"^Dialogue: \d+,([0-9:.]+),([0-9:.]+),(\w+),[^,]*,\d+,\d+,\d+,[^,]*,"
                       r"(.*)$", re.M)
WINDOWS_FONTS = {"arial black": "ariblk.ttf", "arial": "arial.ttf"}


@dataclass
class Check:
    id: str
    name: str
    status: Status
    measured: str
    limit: str
    fix: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _secs(ts: str) -> float:
    h, m, s = ts.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


# ---- 1. format --------------------------------------------------------------------------


def check_format(info: Probe) -> Check:
    got = (f"{info.video_codec} {info.width}×{info.height} {info.fps:g} fps {info.pix_fmt}, "
           f"{info.audio_codec} {info.sample_rate} Hz")
    ok = (info.video_codec == "h264" and (info.width, info.height) == (W, H)
          and abs(info.fps - FPS) < 0.01 and info.pix_fmt == "yuv420p"
          and info.audio_codec == "aac" and info.sample_rate == 48_000)
    return Check("format", "Format", "pass" if ok else "fail", got,
                 "h264 1080×1920 30 fps yuv420p, aac 48000 Hz",
                 None if ok else "Render again; if it repeats, run `fvs doctor` to check "
                                 "the FFmpeg build.")


# ---- 2. duration and tail ---------------------------------------------------------------


def check_tail(duration_s: float, last_word_end: float) -> Check:
    tail = duration_s - last_word_end
    ok = TAIL_MIN - 1 / FPS <= tail <= TAIL_MAX + 1 / FPS
    return Check("tail", "Duration and tail", "pass" if ok else "fail",
                 f"{duration_s:.2f} s, ends {tail:.2f} s after the last word",
                 f"ends {TAIL_MIN}–{TAIL_MAX} s after the last word",
                 None if ok else "Set end.tail_seconds between 0.3 and 1.0.")


# ---- 3–4. loudness and true peak --------------------------------------------------------


def check_loudness(lufs: float | None, target: float) -> Check:
    if lufs is None:
        return Check("loudness", "Loudness", "fail", "silent", f"{target:g} ± 1 LUFS",
                     "The render has no sound; check the voiceover and audio.original.mode.")
    off = abs(lufs - target)
    status: Status = "pass" if off <= LOUDNESS_TOL else "warn" if off <= 2 else "fail"
    return Check("loudness", "Loudness", status, f"{lufs:.1f} LUFS", f"{target:g} ± 1 LUFS",
                 None if status == "pass" else
                 "Render again; if it stays off, lower audio.music.gain_db or set "
                 "audio.original.mode to \"duck\".")


def check_peak(peak: float | None, limit: float) -> Check:
    if peak is None:
        return Check("true_peak", "True peak", "pass", "silent", f"≤ {limit:g} dBTP")
    status: Status = "pass" if peak <= limit else "warn" if peak <= limit + 0.5 else "fail"
    return Check("true_peak", "True peak", status, f"{peak:.1f} dBTP", f"≤ {limit:g} dBTP",
                 None if status == "pass" else
                 "Lower audio.music.gain_db or audio.loudness.target_lufs by 1 and render "
                 "again.")


# ---- 5. voice complete ------------------------------------------------------------------


def check_voice(diff: dict[str, Any] | None, reason: str | None = None) -> Check:
    limit = f"≥ {VOICE_MIN_MATCH:.0%} of script words heard"
    if diff is None:
        return Check("voice", "Voice complete", "warn", "not checked", limit, reason)
    ok = diff["match"] >= VOICE_MIN_MATCH
    gone = diff["missing_sentences"]
    measured = f"{diff['match']:.0%} of {diff['script_words']} words"
    if gone:
        measured += f"; sentence(s) {', '.join(map(str, gone))} not heard"
    return Check("voice", "Voice complete", "pass" if ok else "fail", measured, limit,
                 None if ok else
                 "Listen with fvs_listen. If the voiceover itself is missing words, make it "
                 "again (fvs_voiceover_generate); if the voice is buried, set "
                 "audio.original.mode to \"duck\" or lower audio.music.gain_db.")


# ---- 6. captions inside the safe zone ---------------------------------------------------


def caption_events(ass_text: str) -> list[dict[str, Any]]:
    return [{"start": _secs(a), "end": _secs(b), "style": style,
             "text": _TAGS.sub("", text).replace("\\N", " ")}
            for a, b, style, text in _DIALOGUE.findall(ass_text)]


def sample_times(events: list[dict[str, Any]]) -> list[float]:
    mids = sorted({round((e["start"] + e["end"]) / 2 * FPS) / FPS for e in events})
    if len(mids) <= MAX_CAPTION_SAMPLES:
        return mids
    step = len(mids) / MAX_CAPTION_SAMPLES
    return [mids[int(k * step)] for k in range(MAX_CAPTION_SAMPLES)]


def caption_boxes(folder: Path, ass_name: str, fonts_dir: str | None, times: list[float],
                  duration_s: float, cancelled: Callable[[], bool] | None = None
                  ) -> list[tuple[float, tuple[int, int, int, int] | None]]:
    """The box around the caption layer's visible pixels at each time, in 1080×1920."""
    if not times:
        return []
    frames = sorted({min(int(round(t * FPS)), int(duration_s * FPS) - 1) for t in times})
    select = "+".join(f"eq(n,{n})" for n in frames)
    sub = f"subtitles=filename={ass_name}:alpha=1"
    if fonts_dir:
        sub += f":fontsdir={fonts_dir}"
    w, h = W // SCALE, H // SCALE
    res = run_tool("ffmpeg", [
        "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
        f"color=c=black@0:s={W}x{H}:r={FPS}:d={duration_s:.3f},format=rgba",
        "-vf", f"select='{select}',{sub},alphaextract,scale={w}:{h}",
        "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        cwd=folder, cancelled=cancelled, timeout=120)
    data = np.frombuffer(res.stdout, np.uint8)
    n = data.size // (w * h)
    out: list[tuple[float, tuple[int, int, int, int] | None]] = []
    for k, frame in enumerate(data[: n * w * h].reshape(n, h, w)):
        on = np.argwhere(frame > ALPHA_MIN)
        t = frames[k] / FPS
        if on.size == 0:
            out.append((t, None))
            continue
        (y0, x0), (y1, x1) = on.min(0), on.max(0)
        out.append((t, (int(x0) * SCALE, int(y0) * SCALE, (int(x1) + 1) * SCALE,
                        (int(y1) + 1) * SCALE)))
    return out


def check_safe_zone(boxes: list[tuple[float, tuple[int, int, int, int] | None]],
                    safe: SafeZoneSettings, *, captions_on: bool) -> Check:
    limit = (f"inside x {safe.left}–{W - safe.right}, y {safe.top}–{H - safe.bottom} "
             "(1080×1920)")
    if not captions_on:
        return Check("safe_zone", "Captions in the safe zone", "pass", "no captions", limit)
    tol = SCALE
    outside = [(t, b) for t, b in boxes if b is not None and (
        b[0] < safe.left - tol or b[1] < safe.top - tol or b[2] > W - safe.right + tol
        or b[3] > H - safe.bottom + tol)]
    if not boxes or all(b is None for _, b in boxes):
        return Check("safe_zone", "Captions in the safe zone", "warn", "no caption pixels "
                     "found", limit, "Check the captions with fvs_frames (safe_zone true).")
    if not outside:
        return Check("safe_zone", "Captions in the safe zone", "pass",
                     f"{len(boxes)} caption moments inside", limit)
    t, b = outside[0]
    return Check("safe_zone", "Captions in the safe zone", "fail",
                 f"{len(outside)} of {len(boxes)} moments outside; at {t:.2f} s the text "
                 f"spans x {b[0]}–{b[2]}, y {b[1]}–{b[3]}", limit,
                 "Use captions.max_words 1–2, captions.position \"center\", or a shorter "
                 "captions.overrides text; a long hook needs a shorter hook.text.")


# ---- 7. black or frozen -----------------------------------------------------------------


def check_still(black: list[dict[str, float]], frozen: list[dict[str, float]],
                timeline: dict[str, Any]) -> Check:
    long_black = [b for b in black if b["end"] - b["start"] > STILL_MAX_S]
    long_frozen = [f for f in frozen if f["end"] - f["start"] > STILL_MAX_S]
    limit = f"no black or frozen stretch over {STILL_MAX_S} s"
    if not long_black and not long_frozen:
        return Check("black_frozen", "Black or frozen", "pass", "none", limit)
    kind, span = ("black", long_black[0]) if long_black else ("frozen", long_frozen[0])
    seg = segment_at(timeline, span["start"])
    i = seg["index"]
    if kind == "black":
        fix = (f"timeline.segments[{i}] (clip #{seg['clip']}) is dark here: move its "
               "\"in\" or use another clip.")
    elif seg["hold_s"]:
        fix = (f"timeline.segments[{i}] holds its last frame for {seg['hold_s']:.1f} s: "
               "set fit \"loop\" or \"speed\", or use a longer clip.")
    else:
        fix = (f"timeline.segments[{i}] (clip #{seg['clip']}) does not move here: set its "
               "motion to \"zoom_in\" or pick another part of the clip.")
    measured = "; ".join([f"black {b['start']:.1f}–{b['end']:.1f} s" for b in long_black]
                         + [f"frozen {f['start']:.1f}–{f['end']:.1f} s" for f in long_frozen])
    return Check("black_frozen", "Black or frozen", "fail", measured, limit, fix)


# ---- 8. missing glyphs ------------------------------------------------------------------


def font_file(fonts_dir: Path, family: str) -> Path | None:
    key = family.lower().replace(" ", "")
    if fonts_dir.is_dir():
        for p in sorted(fonts_dir.iterdir()):
            if p.suffix.lower() in (".ttf", ".otf") and \
                    p.name.lower().replace(" ", "").startswith(key):
                return p
    name = WINDOWS_FONTS.get(family.lower())
    if name and sys.platform == "win32":
        path = Path(r"C:\Windows\Fonts") / name
        return path if path.exists() else None
    return None


def check_glyphs(text: str, font: Path | None, family: str) -> Check:
    limit = f"every caption character exists in {family}"
    if font is None:
        return Check("glyphs", "Caption characters", "warn", f"font file for {family} not "
                     "found", limit, "Add the font file to the assets fonts folder.")
    from fontTools.ttLib import TTFont

    cmap = TTFont(str(font), lazy=True).getBestCmap()
    missing = sorted({c for c in text if not c.isspace() and ord(c) not in cmap})
    if not missing:
        return Check("glyphs", "Caption characters", "pass", "all found", limit)
    return Check("glyphs", "Caption characters", "fail", "missing: " + " ".join(missing),
                 limit, "Replace those characters with captions.overrides (or in the "
                        "script with {caption|spoken}), or pick another captions.style.")


def overall(checks: list[Check]) -> Status:
    statuses = {c.status for c in checks}
    return "fail" if "fail" in statuses else "warn" if "warn" in statuses else "pass"

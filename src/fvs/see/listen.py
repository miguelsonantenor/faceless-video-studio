"""Claude's ears (SEE-5): what was said, how it differs from the script, how loud it is.

- Word differences: script and transcript are lower-cased and stripped of punctuation,
  then aligned with difflib into missing, extra and changed words. A sentence counts as
  missing when most of its words are.
- Loudness over time: momentary loudness (400 ms) per track from ``ebur128`` frame
  metadata, one FFmpeg run for all tracks, drawn as one 1024×300 chart.
- Silences over 0.7 s (``silencedetect``), clipped samples, integrated loudness and
  true peak of the main track.
"""

from __future__ import annotations

import difflib
import math
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from fvs.compile.graph import Graph, f
from fvs.errors import ErrorCode, FvsError
from fvs.media.audio import decode_pcm
from fvs.media.tools import run_tool

SILENCE_DB = -45
SILENCE_MIN_S = 0.7
CLIP_LEVEL = 0.999
CHART_W, CHART_H = 1024, 300
FLOOR_LU = -60.0
STEM_COLOURS = {"mix": (235, 235, 235), "voice": (255, 205, 60), "original": (90, 170, 255),
                "music": (255, 110, 160), "clip": (90, 170, 255)}
MISSING_SHARE = 0.6  # a sentence is "missing" when this share of its words is

_WORD = re.compile(r"[\w']+")
_SILENCE_START = re.compile(r"silence_start:\s*(-?[0-9.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?[0-9.]+)")


# ---- words ------------------------------------------------------------------------------


def words_of(text: str) -> list[str]:
    text = text.lower().replace("’", "'")
    return [w.strip("'") for w in _WORD.findall(text) if w.strip("'")]


def compare(sentences: list[str], heard: list[dict[str, Any]]) -> dict[str, Any]:
    """Align the script's sentences with the words Whisper heard (each with start/end)."""
    script = [(w, i) for i, s in enumerate(sentences) for w in words_of(s)]
    said = [(w, h) for h in heard for w in words_of(h["word"])]
    sm = difflib.SequenceMatcher(None, [w for w, _ in script], [w for w, _ in said],
                                 autojunk=False)
    missing, extra, changed = [], [], []
    missing_per_sentence = [0] * len(sentences)

    def at(j: int) -> float | None:
        return said[min(j, len(said) - 1)][1]["start"] if said else None

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "delete":
            for _, s in script[i1:i2]:
                missing_per_sentence[s] += 1
            missing.append({"sentence": script[i1][1] + 1,
                            "words": " ".join(w for w, _ in script[i1:i2]), "near_s": at(j1)})
        elif tag == "insert":
            extra.append({"words": " ".join(w for w, _ in said[j1:j2]),
                          "start": said[j1][1]["start"], "end": said[j2 - 1][1]["end"]})
        elif tag == "replace":
            changed.append({"sentence": script[i1][1] + 1,
                            "expected": " ".join(w for w, _ in script[i1:i2]),
                            "heard": " ".join(w for w, _ in said[j1:j2]),
                            "start": said[j1][1]["start"], "end": said[j2 - 1][1]["end"]})
    matched = sum(b.size for b in sm.get_matching_blocks())
    counts = [sum(1 for _, s in script if s == i) for i in range(len(sentences))]
    gone = [i + 1 for i, (m, n) in enumerate(zip(missing_per_sentence, counts, strict=True))
            if n and m / n >= MISSING_SHARE]
    return {"match": round(matched / len(script), 3) if script else 1.0,
            "script_words": len(script), "heard_words": len(said),
            "missing_sentences": gone, "missing": missing, "extra": extra, "changed": changed}


# ---- loudness ---------------------------------------------------------------------------


def parse_r128(text: str) -> dict[str, Any]:
    """Momentary loudness per 0.1 s frame, the final integrated value and true peak."""
    series: list[tuple[float, float]] = []
    t = 0.0
    integrated, peak = None, None
    for line in text.splitlines():
        if line.startswith("frame:"):
            m = re.search(r"pts_time:\s*([0-9.]+)", line)
            t = float(m.group(1)) if m else t
        elif line.startswith("lavfi.r128.M="):
            series.append((round(t, 2), max(FLOOR_LU, float(line.split("=", 1)[1]))))
        elif line.startswith("lavfi.r128.I="):
            integrated = float(line.split("=", 1)[1])
        elif line.startswith("lavfi.r128.true_peak="):
            peak = float(line.split("=", 1)[1])
    return {
        "series": series,
        "integrated_lufs": round(integrated, 1) if integrated is not None
        and integrated > -70 else None,
        "true_peak_dbtp": round(20 * math.log10(peak), 1) if peak else None,
    }


def silences(stderr: str, duration_s: float) -> list[dict[str, float]]:
    starts = [max(0.0, float(x)) for x in _SILENCE_START.findall(stderr)]
    ends = [float(x) for x in _SILENCE_END.findall(stderr)]
    return [{"start": round(s, 2), "end": round(ends[i] if i < len(ends) else duration_s, 2)}
            for i, s in enumerate(starts)]


def measure(inputs: list[str], g: Graph, stems: dict[str, str], main: str, workdir: Path,
            duration_s: float, cancelled: Callable[[], bool] | None = None
            ) -> dict[str, Any]:
    """One FFmpeg run: loudness per stem, plus silences and true peak of ``main``."""
    workdir.mkdir(parents=True, exist_ok=True)
    outs: list[str] = []
    for name, label in stems.items():
        chain = [f("ebur128", metadata=1, peak="true" if name == main else None),
                 f("ametadata", mode="print", file=f"{name}.txt")]
        if name == main:
            chain.append(f("silencedetect", n=f"{SILENCE_DB}dB", d=SILENCE_MIN_S))
        out = g.one([label], chain, "m")
        outs += ["-map", f"[{out}]", "-f", "null", "-"]
    res = run_tool("ffmpeg", ["-hide_banner", "-nostats", *inputs, "-filter_complex",
                              g.render(), *outs], cwd=workdir, cancelled=cancelled,
                   timeout=max(120.0, duration_s * 2))
    if res.returncode != 0:
        raise FvsError(ErrorCode.CLIP_UNREADABLE, f"FFmpeg could not measure the audio: "
                       f"{res.stderr_tail(3)}", "Check that the file plays, then try again.")
    tracks = {name: parse_r128((workdir / f"{name}.txt").read_text(encoding="utf-8"))
              for name in stems}
    return {"tracks": tracks, "silences": silences(res.stderr, duration_s)}


def clipped_samples(media: Path, cancelled: Callable[[], bool] | None = None) -> int:
    samples = decode_pcm(media, 48_000, cancelled)
    return int(np.count_nonzero(np.abs(samples) >= CLIP_LEVEL))


def chart(tracks: dict[str, dict[str, Any]], duration_s: float,
          silent: list[dict[str, float]]) -> Image.Image:
    """Momentary loudness of each track over time, with silences shaded."""
    img = Image.new("RGB", (CHART_W, CHART_H), (20, 20, 24))
    d = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=13)
    left, right, top, bottom = 44, CHART_W - 10, 24, CHART_H - 22
    span = max(duration_s, 0.1)

    def x(t: float) -> float:
        return left + (right - left) * min(1.0, t / span)

    def y(lu: float) -> float:
        return top + (bottom - top) * (min(0.0, lu) / FLOOR_LU)

    for s in silent:
        d.rectangle((x(s["start"]), top, x(s["end"]), bottom), fill=(48, 36, 36))
    for lu in (0, -14, -30, -45, -60):
        d.line((left, y(lu), right, y(lu)), fill=(60, 60, 66) if lu != -14 else (90, 120, 90))
        d.text((4, y(lu) - 7), f"{lu}", fill=(150, 150, 150), font=font)
    step = 1 if span <= 15 else 5 if span <= 75 else 15
    for t in range(0, int(span) + 1, step):
        d.text((x(t) - 4, bottom + 4), f"{t}s", fill=(150, 150, 150), font=font)
    lx = left
    for name, data in tracks.items():
        colour = STEM_COLOURS.get(name, (200, 200, 200))
        pts = [(x(t), y(lu)) for t, lu in data["series"]]
        if len(pts) > 1:
            d.line(pts, fill=colour, width=2)
        label = name + (f" {data['integrated_lufs']} LUFS" if data["integrated_lufs"] else "")
        d.text((lx, 4), label, fill=colour, font=font)
        lx += int(d.textlength(label, font=font)) + 18
    return img

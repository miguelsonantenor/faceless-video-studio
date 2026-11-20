"""ANA-2, ANA-3, ANA-5 in one decode pass.

One FFmpeg run decodes the video once (downscaled after decoding) and the audio once:
- scene changes: ``select='gt(scene,T)'`` + ``metadata=print`` (ANA-2)
- black stretches: ``blackdetect`` (ANA-3)
- frozen stretches: ``freezedetect`` (ANA-3)
- loudness: ``ebur128=peak=true`` integrated loudness, range and true peak (ANA-5)

The detectors run before ``select`` in the chain, so they see every frame.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fvs.errors import ErrorCode, FvsError
from fvs.media.tools import run_tool

SCENE_THRESHOLD = 0.3
MIN_SCENE_S = 0.4
BLACK_MIN_S = 0.3
FREEZE_MIN_S = 0.5
ANALYSIS_HEIGHT = 240

_SCENE_RE = re.compile(r"Parsed_metadata.*pts_time:\s*([0-9.]+)")
_BLACK_RE = re.compile(r"black_start:\s*([0-9.]+)\s+black_end:\s*([0-9.]+)")
_FREEZE_START_RE = re.compile(r"freeze_start:\s*([0-9.]+)")
_FREEZE_END_RE = re.compile(r"freeze_end:\s*([0-9.]+)")
_LUFS_RE = re.compile(r"^\s*I:\s*(-?[0-9.]+|-inf)\s*LUFS", re.M)
_LRA_RE = re.compile(r"^\s*LRA:\s*(-?[0-9.]+)\s*LU\s*$", re.M)
_PEAK_RE = re.compile(r"True peak:\s*\n\s*Peak:\s*(-?[0-9.]+|-inf)\s*dBFS", re.M)


@dataclass
class Detection:
    scenes: list[dict[str, float]]
    black: list[dict[str, float]]
    frozen: list[dict[str, float]]
    loudness: dict[str, float | None] | None

    def to_dict(self) -> dict[str, Any]:
        return {"scenes": self.scenes, "black": self.black, "frozen": self.frozen,
                "loudness": self.loudness}


def _f(text: str) -> float | None:
    return None if text == "-inf" else float(text)


def _r(x: float) -> float:
    return round(x, 3)


def build_args(path: Path, *, has_audio: bool) -> list[str]:
    vf = (f"scale=-2:{ANALYSIS_HEIGHT},"
          f"blackdetect=d={BLACK_MIN_S}:pix_th=0.10,"
          f"freezedetect=n=-60dB:d={FREEZE_MIN_S},"
          f"select='gt(scene,{SCENE_THRESHOLD})',metadata=print")
    args = ["-hide_banner", "-nostats", "-loglevel", "info", "-i", str(path),
            "-map", "0:v:0", "-vf", vf]
    if has_audio:
        args += ["-map", "0:a:0", "-af", "ebur128=peak=true:framelog=quiet"]
    return [*args, "-f", "null", "-"]


def parse_output(stderr: str, *, duration_s: float, start_s: float,
                 has_audio: bool) -> Detection:
    def rel(t: float) -> float:
        return max(0.0, min(duration_s, t - start_s))

    cuts = sorted({rel(float(m)) for m in _SCENE_RE.findall(stderr)})
    bounds = [0.0]
    for c in cuts:
        if c - bounds[-1] >= MIN_SCENE_S and duration_s - c >= MIN_SCENE_S:
            bounds.append(c)
    bounds.append(duration_s)
    scenes = [{"start": _r(a), "end": _r(b)} for a, b in zip(bounds, bounds[1:], strict=False)
              if b > a]

    black = [{"start": _r(rel(float(a))), "end": _r(rel(float(b)))}
             for a, b in _BLACK_RE.findall(stderr)]

    starts = [rel(float(x)) for x in _FREEZE_START_RE.findall(stderr)]
    ends = [rel(float(x)) for x in _FREEZE_END_RE.findall(stderr)]
    frozen = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else duration_s  # frozen until the end
        frozen.append({"start": _r(s), "end": _r(e)})

    loudness = None
    if has_audio:
        i_m, lra_m, peak_m = _LUFS_RE.findall(stderr), _LRA_RE.findall(stderr), \
            _PEAK_RE.findall(stderr)
        integrated = _f(i_m[-1]) if i_m else None
        if integrated is not None and integrated <= -70.0:
            integrated = None  # silent: below the gating threshold
        loudness = {
            "integrated_lufs": integrated,
            "range_lu": float(lra_m[-1]) if lra_m else None,
            "true_peak_dbtp": _f(peak_m[-1]) if peak_m else None,
        }
        if loudness["true_peak_dbtp"] is not None and math.isinf(loudness["true_peak_dbtp"]):
            loudness["true_peak_dbtp"] = None
    return Detection(scenes, black, frozen, loudness)


def detect(path: Path, *, duration_s: float, start_s: float, has_audio: bool,
           cancelled: Callable[[], bool] | None = None) -> Detection:
    res = run_tool("ffmpeg", build_args(path, has_audio=has_audio), cancelled=cancelled,
                   timeout=max(120.0, duration_s * 4))
    if res.returncode != 0:
        raise FvsError(
            ErrorCode.CLIP_UNREADABLE,
            f"FFmpeg could not analyse {path.name}: {res.stderr_tail(3)}",
            "Check that the clip plays; re-export it as MP4 (H.264) if it does not.",
        )
    return parse_output(res.stderr, duration_s=duration_s, start_s=start_s,
                        has_audio=has_audio)

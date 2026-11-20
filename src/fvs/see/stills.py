"""Still frames of an Edit Spec without a full render (SEE-2, NFR-P2; DS 5.5).

A still at output time ``t`` renders only the segment under ``t``: the clip is read from
the matching source time, goes through the same reframe and motion filters as the render
(with the motion clock set to the segment's own time), then the caption layer at ``t``.
During a fade the still shows the incoming shot, not the blend.
"""

from __future__ import annotations

from typing import Any

from fvs.compile.compiler import FPS, Inputs, motion_filters, reframe
from fvs.compile.graph import Graph, f
from fvs.spec.models import EditSpec

STILL_W, STILL_H = 288, 512


def segment_at(timeline: dict[str, Any], t: float) -> dict[str, Any]:
    """The segment on screen at ``t`` (the later one where two overlap in a fade)."""
    current = timeline["segments"][0]
    for seg in timeline["segments"]:
        if seg["video_start"] <= t + 1e-6:
            current = seg
    return current


def motion_time(seg: dict[str, Any], local: float) -> float:
    """Where the segment's own clock is at ``local`` seconds in, after loop and hold."""
    span = (seg["src_out"] - seg["src_in"]) / seg["speed"]  # output seconds of footage
    if seg["loop"]:
        loop_len = max(1, int(span * FPS)) / FPS
        return local % loop_len
    return max(0.0, min(local, span - 1 / FPS))


def still_args(spec: EditSpec, timeline: dict[str, Any], inputs: Inputs, t: float, *,
               ass_name: str | None) -> list[str]:
    """FFmpeg arguments that write one PNG of the edit at ``t`` to stdout."""
    seg = segment_at(timeline, t)
    m = motion_time(seg, max(0.0, t - seg["video_start"]))
    length = round(seg["out_end"] - seg["video_start"], 4)
    clip = inputs.clips[seg["clip"]]
    g = Graph()
    v = g.one(["0:v"], [f"setpts=PTS-STARTPTS+{m:.4f}/TB", f("fps", FPS)], "v")
    post = [*motion_filters(seg["motion"], length), f"setpts=PTS-STARTPTS+{t:.4f}/TB"]
    if ass_name is not None:
        sub = f"subtitles=filename={ass_name}"
        if inputs.fonts_dir:
            sub += f":fontsdir={inputs.fonts_dir}"
        post.append(sub)
    post.append(f("scale", STILL_W, STILL_H))
    g.chain([reframe(g, v, spec.reframe)], post, ["out"])
    return ["-hide_banner", "-loglevel", "error", "-ss",
            f"{seg['src_in'] + m * seg['speed']:.4f}", "-i", clip["path"],
            "-filter_complex", g.render(), "-map", "[out]", "-frames:v", "1",
            "-f", "image2pipe", "-c:v", "png", "-"]

"""Edit Spec → FFmpeg render plan (RND-1, EDT-6–8, CAP-1–6, AUD-1–4; DS 5.5).

Stages, each a function so a new edit feature means one schema field and one stage
change (NFR-M2):

1. Resolve: done by ``fvs.spec.resolve`` before this module is called.
2. Video per segment: seek and cut at the input, speed, 30 fps, reframe to 1080×1920,
   slow zoom or pan (scale and crop expressions), loop or hold, exact trim.
3. Join: ``concat`` for cuts, ``xfade`` for fades.
4. (P2 overlays.)
5. Captions and hook: one ASS file, burned in last with ``subtitles``.
6. Audio: voice; the clips' own sound per segment (mute, duck or keep); music from
   ``start_at``, looped, faded; ducking under the voice; mix; loudness in two passes
   (an audio-only pass measures, the render applies the measured values).
7. Output: 1080×1920, 30 fps, H.264 High, yuv420p, AAC 48 kHz, fast start, progress.

Ducking uses a volume envelope built from the voiceover's sentence times (0.15 s ramps)
rather than a compressor keyed by the voice: the times are known exactly, so the drop
is exactly ``duck_db`` and AC-5 does not depend on how loud the voice happens to be.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fvs.compile.ass import build_ass
from fvs.compile.graph import Graph, f
from fvs.settings import SafeZoneSettings
from fvs.spec.models import EditSpec, Reframe

W, H, FPS = 1080, 1920, 30
SAMPLE_RATE = 48_000
MOTION_AMOUNT = 0.08  # slow zoom: 8 % over the segment
PAN_SCALE = 1.1
DUCK_RAMP_S = 0.15
INPUT_MARGIN_S = 0.25  # read a little past the cut so the trim, not the input, ends it
AUDIO_FMT = f("aformat", sample_rates=SAMPLE_RATE, channel_layouts="stereo")


@dataclass
class Inputs:
    clips: dict[int, dict[str, Any]]  # number → clip row (path, has_audio, duration_s)
    voice_path: Path
    timings: dict[str, Any]
    style: dict[str, Any]
    font: str
    safe: SafeZoneSettings
    hook_text: str | None
    music: dict[str, Any] | None = None  # asset row
    fonts_dir: str | None = None  # relative to the working folder


@dataclass
class RenderPlan:
    args: list[str]  # the final render, after "ffmpeg"
    measure_args: list[str]  # the audio-only loudness pass
    ass: str | None  # caption file text, to write at ``ass_name``
    duration_s: float


# ---- stage 2: video per segment ----------------------------------------------------------


def reframe(g: Graph, label: str, r: Reframe) -> str:
    if r.mode == "blur_fill":
        a, b = g.chain([label], ["split"], 2, "s")
        bg = g.one([a], [f("scale", W, H, force_original_aspect_ratio="increase"),
                         f("crop", W, H), f("boxblur", luma_radius=24, luma_power=2)], "bg")
        fg = g.one([b], [f("scale", W, H, force_original_aspect_ratio="decrease")], "fg")
        return g.one([bg, fg], [f("overlay", x="(W-w)/2", y="(H-h)/2")], "rf")
    if r.mode == "fit_bars":
        return g.one([label], [f("scale", W, H, force_original_aspect_ratio="decrease"),
                               f("pad", W, H, "(ow-iw)/2", "(oh-ih)/2", "black")], "rf")
    zoom = r.zoom if r.mode == "manual" else 1.0
    return g.one([label], [
        f("scale", w=int(W * zoom), h=int(H * zoom), force_original_aspect_ratio="increase"),
        f("crop", W, H, x=f"(iw-ow)*{r.focus_x:g}", y=f"(ih-oh)*{r.focus_y:g}")], "rf")


def motion_filters(motion: str, length: float) -> list[str]:
    a, n = MOTION_AMOUNT, f"{length:.4f}"
    if motion in ("zoom_in", "zoom_out"):
        grow = f"t/{n}" if motion == "zoom_in" else f"(1-t/{n})"
        return [f("scale", w=f"trunc({W}*(1+{a}*{grow})/2)*2",
                  h=f"trunc({H}*(1+{a}*{grow})/2)*2", eval="frame"), f("crop", W, H)]
    if motion in ("pan_left", "pan_right"):
        move = f"t/{n}" if motion == "pan_right" else f"(1-t/{n})"
        return [f("scale", int(W * PAN_SCALE) // 2 * 2, int(H * PAN_SCALE) // 2 * 2),
                f("crop", W, H, x=f"(iw-ow)*{move}", y="(ih-oh)/2")]
    return []


def _segment_video(g: Graph, idx: int, seg: dict[str, Any], spec: EditSpec) -> str:
    length = round(seg["out_end"] - seg["video_start"], 4)
    pre = ["setpts=PTS-STARTPTS"]
    if seg["speed"] != 1.0:
        pre.append(f"setpts=PTS/{seg['speed']:g}")
    pre.append(f("fps", FPS))
    label = reframe(g, g.one([f"{idx}:v"], pre, "v"), spec.reframe)
    post = motion_filters(seg["motion"], length)
    if seg["loop"]:
        frames = max(1, int((seg["src_out"] - seg["src_in"]) / seg["speed"] * FPS))
        post.append(f("loop", loop=-1, size=frames, start=0))
    if seg["hold_s"]:
        post.append(f("tpad", stop_mode="clone", stop_duration=seg["hold_s"]))
    post += [f("trim", duration=length), "setpts=PTS-STARTPTS", f("format", "yuv420p"),
             f("setsar", 1)]
    return g.one([label], post, "seg")


# ---- stage 3: join -----------------------------------------------------------------------


def _join(g: Graph, labels: list[str], segments: list[dict[str, Any]]) -> str:
    cur = labels[0]
    for label, seg in zip(labels[1:], segments[1:], strict=True):
        t = seg["transition_in"]
        if t["type"] == "fade":
            cur = g.one([cur, label], [f("xfade", transition="fade", duration=t["seconds"],
                                         offset=seg["video_start"])], "j")
        else:
            cur = g.one([cur, label], [f("concat", n=2, v=1, a=0)], "j")
    return cur


# ---- stage 6: audio ----------------------------------------------------------------------


def speech_windows(timings: dict[str, Any]) -> list[tuple[float, float]]:
    out = []
    for s in timings["sentences"]:
        if s["words"]:
            out.append((s["words"][0]["start"], s["words"][-1]["end"]))
    return out


def duck_volume(windows: list[tuple[float, float]], duck_db: float) -> str:
    """A ``volume`` expression: ``duck_db`` while the voice speaks, 0 dB otherwise."""
    if not windows:
        return "1"
    r = DUCK_RAMP_S
    parts = [f"clip(min((t-{a - r:.3f})/{r},({b + r:.3f}-t)/{r}),0,1)" for a, b in windows]
    env = parts[0]
    for p in parts[1:]:
        env = f"max({env},{p})"
    gain = 10 ** (duck_db / 20)
    return f"1-{1 - gain:.5f}*{env}"


def audio_stems(g: Graph, spec: EditSpec, timeline: dict[str, Any], inputs: Inputs,
                voice_idx: int, music_idx: int | None) -> dict[str, str]:
    """Each track as it goes into the mix (after ducking): voice, original, music."""
    total = timeline["duration_s"]
    windows = speech_windows(inputs.timings)
    stems = {"voice": g.one([f"{voice_idx}:a"], [AUDIO_FMT, "apad", f("atrim", duration=total),
                                                 "asetpts=PTS-STARTPTS"], "voice")}

    orig = spec.audio.original
    if orig.mode != "mute" and any(inputs.clips[s["clip"]]["has_audio"]
                                   for s in timeline["segments"]):
        parts = []
        for idx, seg in enumerate(timeline["segments"]):
            length = round(seg["out_end"] - seg["out_start"], 4)
            if inputs.clips[seg["clip"]]["has_audio"] and not seg["loop"]:
                chain = ["asetpts=PTS-STARTPTS"]
                if seg["speed"] != 1.0:
                    chain.append(f("atempo", seg["speed"]))
                chain += [AUDIO_FMT, "apad", f("atrim", duration=length),
                          "asetpts=PTS-STARTPTS"]
                parts.append(g.one([f"{idx}:a"], chain, "oa"))
            else:
                parts.append(g.one([], [f("aevalsrc", "0", d=length, s=SAMPLE_RATE),
                                        AUDIO_FMT], "oa"))
        original = g.one(parts, [f("concat", n=len(parts), v=0, a=1)], "orig")
        if orig.mode == "duck":
            original = g.one([original], [f("volume", volume=duck_volume(windows,
                                                                         orig.duck_db),
                                            eval="frame")], "orig")
        stems["original"] = original

    music = spec.audio.music
    if music is not None and music_idx is not None:
        chain = [AUDIO_FMT, f("atrim", duration=total), "asetpts=PTS-STARTPTS",
                 f("volume", f"{music.gain_db:g}dB")]
        if music.fade_in:
            chain.append(f("afade", t="in", d=music.fade_in))
        if music.fade_out:
            chain.append(f("afade", t="out", st=max(0.0, total - music.fade_out),
                           d=music.fade_out))
        if music.duck:
            chain.append(f("volume", volume=duck_volume(windows, -14.0), eval="frame"))
        stems["music"] = g.one([f"{music_idx}:a"], chain, "music")
    return stems


def _audio(g: Graph, spec: EditSpec, timeline: dict[str, Any], inputs: Inputs,
           voice_idx: int, music_idx: int | None) -> str:
    mix = list(audio_stems(g, spec, timeline, inputs, voice_idx, music_idx).values())
    if len(mix) == 1:
        return mix[0]
    return g.one(mix, [f("amix", inputs=len(mix), normalize=0, duration="first")], "mix")


def loudnorm(spec: EditSpec, measured: dict[str, Any] | None) -> str:
    """Pass 1 (``measured`` None) prints the measurement; pass 2 applies it linearly."""
    target = spec.audio.loudness
    # Aim 0.5 dB under the true-peak limit: AAC encoding can add a little.
    opts: dict[str, object] = {"I": target.target_lufs, "TP": target.true_peak_db - 0.5,
                               "LRA": 11}
    if measured is None:
        return f("loudnorm", **opts, print_format="json")
    return f("loudnorm", **opts, measured_I=measured["input_i"],
             measured_TP=measured["input_tp"], measured_LRA=measured["input_lra"],
             measured_thresh=measured["input_thresh"], offset=measured["target_offset"],
             linear="true")


def parse_loudnorm(stderr: str) -> dict[str, Any]:
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr, re.DOTALL)
    if match is None:
        raise ValueError("loudnorm printed no measurement")
    return json.loads(match.group(0))


# ---- the whole plan ----------------------------------------------------------------------


def input_args(timeline: dict[str, Any], spec: EditSpec, inputs: Inputs, *,
                audio_only: bool) -> tuple[list[str], int, int | None]:
    args: list[str] = []
    for seg in timeline["segments"]:
        clip = inputs.clips[seg["clip"]]
        src_len = seg["src_out"] - seg["src_in"] + INPUT_MARGIN_S
        if audio_only:
            args.append("-vn")
        args += ["-ss", f"{seg['src_in']:.4f}", "-t", f"{src_len:.4f}", "-i", clip["path"]]
    voice_idx = len(timeline["segments"])
    args += ["-i", str(inputs.voice_path)]
    music_idx = None
    if spec.audio.music is not None and inputs.music is not None:
        music_idx = voice_idx + 1
        args += ["-stream_loop", "-1", "-ss", f"{spec.audio.music.start_at:.3f}",
                 "-i", inputs.music["path"]]
    return args, voice_idx, music_idx


def caption_ass(spec: EditSpec, inputs: Inputs, total: float) -> str | None:
    """The caption and hook layer as ASS text, or None when there is nothing to show."""
    hook = spec.hook.text if spec.hook.text is not None else inputs.hook_text
    if spec.captions.mode == "off" and not hook:
        return None
    return build_ass(timings=inputs.timings, captions=spec.captions, style=inputs.style,
                     font=inputs.font, hook_text=hook, hook_seconds=spec.hook.seconds,
                     end_s=total, safe=inputs.safe)


def compile_render(spec: EditSpec, timeline: dict[str, Any], inputs: Inputs, *,
                   ass_name: str, out_name: str,
                   measured: dict[str, Any] | None = None) -> RenderPlan:
    total = timeline["duration_s"]
    ass_text = caption_ass(spec, inputs, total)

    # Pass 1: audio only, measure loudness.
    ga = Graph()
    in_args, v_idx, m_idx = input_args(timeline, spec, inputs, audio_only=True)
    measured_out = ga.one([_audio(ga, spec, timeline, inputs, v_idx, m_idx)],
                          [loudnorm(spec, None)], "aout")
    measure = ["-hide_banner", "-nostats", *in_args, "-filter_complex", ga.render(),
               "-map", f"[{measured_out}]", "-f", "null", "-"]

    # Pass 2: the render.
    g = Graph()
    in_args, v_idx, m_idx = input_args(timeline, spec, inputs, audio_only=False)
    segs = [_segment_video(g, i, s, spec) for i, s in enumerate(timeline["segments"])]
    video = _join(g, segs, timeline["segments"])
    if ass_text is not None:
        sub = f"subtitles=filename={ass_name}"
        if inputs.fonts_dir:
            sub += f":fontsdir={inputs.fonts_dir}"
        video = g.one([video], [sub, f("format", "yuv420p")], "cap")
    g.chain([video], ["null"], ["vout"])
    mixed = _audio(g, spec, timeline, inputs, v_idx, m_idx)
    g.chain([mixed], [loudnorm(spec, measured), AUDIO_FMT], ["aout"])
    args = ["-y", "-hide_banner", "-nostats", "-progress", "pipe:1", *in_args,
            "-filter_complex", g.render(), "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-profile:v", "high", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-r", str(FPS), "-c:a", "aac", "-b:a", "192k",
            "-ar", str(SAMPLE_RATE), "-ac", "2", "-movflags", "+faststart",
            "-t", f"{total:.4f}", out_name]
    return RenderPlan(args=args, measure_args=measure, ass=ass_text, duration_s=total)

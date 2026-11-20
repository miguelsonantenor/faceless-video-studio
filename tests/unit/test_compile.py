"""Compiler structure, graph escaping and the caption file (RND-1, CAP-1–6, DS 5.5)."""

from __future__ import annotations

from pathlib import Path

from fvs.compile.ass import build_ass, colour, ts
from fvs.compile.compiler import Inputs, compile_render, duck_volume, parse_loudnorm
from fvs.compile.graph import Graph, f, value
from fvs.settings import SafeZoneSettings
from fvs.spec.models import Captions, EditSpec
from fvs.spec.resolve import Context, resolve
from fvs.spec.templates import CAPTION_STYLES
from tests.conftest import TIMINGS

CLIPS = {1: {"id": "c1", "duration_s": 20.0, "content_hash": "h1", "path": "/in/a.mp4",
             "has_audio": 1},
         2: {"id": "c2", "duration_s": 3.0, "content_hash": "h2", "path": "/in/b.mp4",
             "has_audio": 0}}


def _plan(measured: dict | None = None, **over: object):
    spec = EditSpec.model_validate({"project": 1, "timeline": {"segments": [
        {"clip": 1, "in": 2, "cue": {"sentences": [1]}, "motion": "zoom_in"},
        {"clip": 2, "in": 1.3, "cue": {"sentences": [2]}, "transition_in": "fade",
         "fit": "slow"},  # 1.7 s left for 2.0 s (slot + fade): slowed to 0.85×
        {"clip": 1, "in": 9, "cue": {"until": "end"}, "motion": "pan_left"}]}, **over})
    timeline = resolve(spec, Context(timings=TIMINGS, clips=CLIPS,
                                     caption_styles=set(CAPTION_STYLES)))
    inputs = Inputs(clips=CLIPS, voice_path=Path("voice/v.wav"), timings=TIMINGS,
                    style=CAPTION_STYLES["bold_yellow"], font="Montserrat",
                    safe=SafeZoneSettings(), hook_text="Volcanoes make lightning",
                    fonts_dir="../../assets/fonts")
    return compile_render(spec, timeline, inputs, ass_name="captions/x.ass",
                          out_name="renders/x.mp4", measured=measured)


def test_values_are_quoted_only_when_needed() -> None:
    assert value(1080) == "1080" and value(0.5) == "0.5" and value(2.0) == "2"
    assert value("if(gt(t,1),2,3)") == "'if(gt(t,1),2,3)'"
    assert value("it's") == "'it'\\''s'"
    assert f("scale", 1080, 1920, force_original_aspect_ratio="increase") == \
        "scale=1080:1920:force_original_aspect_ratio=increase"


def test_graph_labels_and_chains() -> None:
    g = Graph()
    a, b = g.chain(["0:v"], ["split"], 2, "s")
    out = g.one([a, b], ["hstack"])
    assert g.render() == f"[0:v]split[{a}][{b}];[{a}][{b}]hstack[{out}]"


def test_inputs_are_cut_at_the_source_with_a_margin() -> None:
    args = _plan().args
    i = [k for k, a in enumerate(args) if a == "-i"]
    assert [args[k + 1] for k in i] == ["/in/a.mp4", "/in/b.mp4", "/in/a.mp4",
                                        str(Path("voice/v.wav"))]
    assert args[i[0] - 4:i[0]] == ["-ss", "2.0000", "-t", "1.7500"]


def test_stages_appear_in_the_graph() -> None:
    plan = _plan()
    graph = plan.args[plan.args.index("-filter_complex") + 1]
    assert "boxblur" in graph  # the blur-fill reframe
    assert "*(1+0.08*t/1.5000)" in graph and "eval=frame" in graph  # slow zoom in
    assert "x=(iw-ow)*(1-t/1.7000)" in graph  # pan left
    assert "setpts=PTS/0.85" in graph and "atempo" not in graph  # clip 2 is silent
    assert "xfade=transition=fade:duration=0.3:offset=1.2" in graph
    assert "concat=n=2:v=1:a=0" in graph
    assert "subtitles=filename=captions/x.ass:fontsdir=../../assets/fonts" in graph
    assert graph.count("aevalsrc") == 1  # clip 2 has no sound: silence for its segment
    assert plan.args[-1] == "renders/x.mp4" and "+faststart" in plan.args
    measure = plan.measure_args[plan.measure_args.index("-filter_complex") + 1]
    assert "print_format=json" in measure and "boxblur" not in measure
    assert plan.measure_args.count("-vn") == 3


def test_second_pass_applies_the_measurement() -> None:
    m = parse_loudnorm('noise {\n "input_i" : "-23.5", "input_tp" : "-4.0", '
                       '"input_lra" : "3.1", "input_thresh" : "-34.0", '
                       '"target_offset" : "0.4" }\n')
    plan = _plan(measured=m)
    graph = plan.args[plan.args.index("-filter_complex") + 1]
    assert ("loudnorm=I=-14:TP=-1.5:LRA=11:measured_I=-23.5:measured_TP=-4.0:"
            "measured_LRA=3.1:measured_thresh=-34.0:offset=0.4:linear=true") in graph


def test_mute_drops_the_original_sound() -> None:
    graph = " ".join(_plan(audio={"original": {"mode": "mute"}}).args)
    assert "[orig" not in graph and "[voice" in graph


def test_duck_volume_expression() -> None:
    expr = duck_volume([(1.0, 2.0)], -14)
    assert expr.startswith("1-0.80047*clip(") and duck_volume([], -14) == "1"


def _ass(**captions: object) -> str:
    return build_ass(timings=TIMINGS, captions=Captions(**captions),
                     style=CAPTION_STYLES["bold_yellow"], font="Montserrat",
                     hook_text="Volcanoes make lightning", hook_seconds=2.5, end_s=4.9,
                     safe=SafeZoneSettings())


def test_ass_header_style_and_hook() -> None:
    text = _ass()
    assert "PlayResX: 1080" in text and "PlayResY: 1920" in text
    assert "Style: cap,Montserrat,86,&H00FFFFFF,&H004DE1FF" in text
    assert ",2,60,120,420,1" in text  # bottom centre, safe-zone margins
    assert "Dialogue: 0,0:00:00.00,0:00:02.50,hook,,0,0,0,,VOLCANOES MAKE LIGHTNING" in text


def test_ass_word_highlight_moves_through_each_chunk() -> None:
    lines = [ln for ln in _ass().splitlines() if ",cap,," in ln]
    first = lines[:3]
    assert first[0].startswith("Dialogue: 0,0:00:00.05,0:00:00.30,cap")
    assert "{\\c&H004DE1FF}THE{\\c&H00FFFFFF} VOLCANO ERUPTS." in first[0]
    assert "THE {\\c&H004DE1FF}VOLCANO{\\c&H00FFFFFF} ERUPTS." in first[1]
    # Sentence 2 starts a new chunk; the last chunk stays 0.4 s after its last word.
    assert any("0:00:01.55" in ln and "IT" in ln for ln in lines)
    assert lines[-1].split(",")[2] == "0:00:04.70"


def test_ass_chunks_overrides_and_emphasis() -> None:
    timings_emph = {"sentences": [{**TIMINGS["sentences"][0], "words": [
        {**w, "emph": w["word"] == "volcano"} for w in TIMINGS["sentences"][0]["words"]]}]}
    text = build_ass(timings=timings_emph,
                     captions=Captions(mode="chunks", max_words=2,
                                       overrides=[{"word": 3, "text": "BLOWS {UP}"}]),
                     style=CAPTION_STYLES["bold_yellow"], font="F", hook_text=None,
                     hook_seconds=2, end_s=2.0, safe=SafeZoneSettings())
    events = [ln.split(",,", 1)[1].split(",,", 1)[1] for ln in text.splitlines()
              if ln.startswith("Dialogue")]
    # #FF5A4D (emphasis red) in ASS byte order is &H004D5AFF.
    assert events == ["THE {\\c&H004D5AFF}VOLCANO{\\c&H00FFFFFF}", "BLOWS (UP)"]


def test_ass_line_mode_and_off() -> None:
    lines = [ln for ln in _ass(mode="line").splitlines() if ",cap,," in ln]
    assert len(lines) == 3 and lines[0].endswith("THE VOLCANO ERUPTS.")
    assert ",cap,," not in _ass(mode="off")


def test_colour_and_time_formats() -> None:
    assert colour("#FFE14D") == "&H004DE1FF" and ts(3725.456) == "1:02:05.46"

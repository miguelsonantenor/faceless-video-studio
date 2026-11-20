"""Perception helpers: times, spec stills, word differences, loudness, images (SEE-2–5)."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from fvs.compile.compiler import Inputs
from fvs.errors import FvsError
from fvs.see import listen as ears
from fvs.see.images import SAFE_COLOUR, draw_safe_zone, grid
from fvs.see.service import pick_times
from fvs.see.stills import motion_time, segment_at, still_args
from fvs.settings import SafeZoneSettings
from fvs.spec.models import EditSpec
from fvs.spec.resolve import Context, resolve
from fvs.spec.templates import CAPTION_STYLES
from tests.conftest import TIMINGS
from tests.unit.test_compile import CLIPS


def _timeline() -> tuple[EditSpec, dict, Inputs]:
    spec = EditSpec.model_validate({"project": 1, "timeline": {"segments": [
        {"clip": 1, "in": 2, "cue": {"sentences": [1]}, "motion": "zoom_in"},
        {"clip": 2, "in": 1.3, "cue": {"sentences": [2]}, "transition_in": "fade",
         "fit": "slow"},
        {"clip": 1, "in": 9, "cue": {"until": "end"}, "motion": "pan_left"}]}})
    timeline = resolve(spec, Context(timings=TIMINGS, clips=CLIPS,
                                     caption_styles=set(CAPTION_STYLES)))
    inputs = Inputs(clips=CLIPS, voice_path=Path("voice/v.wav"), timings=TIMINGS,
                    style=CAPTION_STYLES["bold_yellow"], font="Montserrat",
                    safe=SafeZoneSettings(), hook_text=None, fonts_dir="../../assets/fonts")
    return spec, timeline, inputs


# ---- times ------------------------------------------------------------------------------


def test_exact_times_and_spread_ranges() -> None:
    assert pick_times(10, [0.5, 2], None, None, None) == [0.5, 2]
    assert pick_times(10, [10], None, None, None) == [round(10 - 1 / 30, 3)]  # last frame
    assert pick_times(6, None, None, None, None) == [0.5, 1.5, 2.5, 3.5, 4.5, 5.5]
    assert pick_times(10, None, 2, 4, 2) == [2.5, 3.5]


@pytest.mark.parametrize("args", [([11], None, None, None), ([-1], None, None, None),
                                  (None, 5, 4, 2), (None, 0, 2, 0), (list(range(25)), None,
                                                                     None, None)])
def test_bad_times_explain_the_limit(args: tuple) -> None:
    with pytest.raises(FvsError) as e:
        pick_times(10, *args)
    assert e.value.fix


# ---- spec stills ------------------------------------------------------------------------


def test_the_segment_under_a_time_prefers_the_incoming_shot_in_a_fade() -> None:
    _, timeline, _ = _timeline()
    second = timeline["segments"][1]
    assert second["video_start"] < second["out_start"]  # it fades in before its cue
    assert segment_at(timeline, 0.2)["index"] == 0
    assert segment_at(timeline, second["video_start"] + 0.01)["index"] == 1
    assert segment_at(timeline, 4.8)["index"] == 2


def test_motion_time_follows_loop_and_hold() -> None:
    seg = {"src_in": 0.0, "src_out": 1.0, "speed": 1.0, "loop": False}
    assert motion_time(seg, 0.4) == 0.4
    assert motion_time(seg, 2.0) == pytest.approx(1 - 1 / 30)  # held on the last frame
    assert motion_time({**seg, "loop": True}, 2.5) == pytest.approx(0.5)


def test_a_still_reads_the_matching_source_time() -> None:
    spec, timeline, inputs = _timeline()
    t = 2.5
    seg = segment_at(timeline, t)
    args = still_args(spec, timeline, inputs, t, ass_name="captions/x.ass")
    local = t - seg["video_start"]
    assert args[args.index("-ss") + 1] == f"{seg['src_in'] + local * seg['speed']:.4f}"
    assert args[args.index("-i") + 1] == CLIPS[seg["clip"]]["path"]
    graph = args[args.index("-filter_complex") + 1]
    assert f"setpts=PTS-STARTPTS+{local:.4f}/TB" in graph  # motion sees the segment's clock
    assert f"setpts=PTS-STARTPTS+{t:.4f}/TB" in graph  # captions see the output clock
    assert "subtitles=filename=captions/x.ass:fontsdir=../../assets/fonts" in graph
    assert "scale=288:512" in graph and args[-4:] == ["image2pipe", "-c:v", "png", "-"]


def test_a_still_without_captions_has_no_subtitle_layer() -> None:
    spec, timeline, inputs = _timeline()
    args = still_args(spec, timeline, inputs, 0.5, ass_name=None)
    assert "subtitles" not in args[args.index("-filter_complex") + 1]


# ---- listening --------------------------------------------------------------------------


def _heard(text: str, start: float = 0.0) -> list[dict]:
    return [{"word": w, "start": start + k * 0.3, "end": start + k * 0.3 + 0.25}
            for k, w in enumerate(text.split())]


def test_a_missing_sentence_is_flagged() -> None:
    sentences = ["The volcano makes its own lightning.", "Ash builds a charge.",
                 "Then the whole sky lights up at once."]
    d = ears.compare(sentences, _heard("The volcano makes its own lightning. Then the whole "
                                       "sky lights up at once."))
    assert d["missing_sentences"] == [2]
    assert d["missing"][0]["words"] == "ash builds a charge"
    assert d["match"] == pytest.approx(14 / 18, abs=0.001)


def test_extra_and_changed_words() -> None:
    d = ears.compare(["Lightning is hot."], _heard("Um lightning was hot"))
    assert d["extra"][0]["words"] == "um"
    assert d["changed"][0] == {"sentence": 1, "expected": "is", "heard": "was",
                               "start": pytest.approx(0.6), "end": pytest.approx(0.85)}
    assert ears.compare(["It's here."], _heard("it’s HERE"))["match"] == 1.0


R128 = """frame:0    pts:0       pts_time:0
lavfi.r128.M=-120.691
lavfi.r128.I=-70.000
frame:1    pts:4800    pts_time:0.1
lavfi.r128.M=-20.5
lavfi.r128.I=-21.3
lavfi.r128.true_peak=0.5
"""


def test_r128_frames_become_a_series() -> None:
    r = ears.parse_r128(R128)
    assert r["series"] == [(0.0, ears.FLOOR_LU), (0.1, -20.5)]
    assert r["integrated_lufs"] == -21.3 and r["true_peak_dbtp"] == -6.0


def test_silences_close_at_the_end_when_still_silent() -> None:
    err = "silence_start: 1.2\nsilence_end: 2.4 | silence_duration: 1.2\nsilence_start: 5\n"
    assert ears.silences(err, 6.0) == [{"start": 1.2, "end": 2.4}, {"start": 5.0, "end": 6.0}]


def test_chart_size() -> None:
    img = ears.chart({"voice": {"series": [(0, -30), (1, -18)], "integrated_lufs": -20},
                      "mix": {"series": [(0, -20), (1, -14)], "integrated_lufs": -14}},
                     2.0, [{"start": 1.2, "end": 2.0}])
    assert img.size == (ears.CHART_W, ears.CHART_H)


# ---- images -----------------------------------------------------------------------------


def test_sheets_stay_within_1024_and_the_safe_zone_is_drawn() -> None:
    tiles = [Image.new("RGB", (288, 512), (k * 10, 0, 0)) for k in range(12)]
    sheet = grid(tiles, [str(k) for k in range(12)], cols=6)
    assert max(sheet.size) <= 1024
    marked = draw_safe_zone(Image.new("RGB", (288, 512)), SafeZoneSettings())
    top = 108 * 512 / 1920
    assert SAFE_COLOUR in {marked.getpixel((144, y)) for y in range(int(top) - 1, int(top) + 2)}
    assert marked.getpixel((144, 256)) == (0, 0, 0)  # only the outline

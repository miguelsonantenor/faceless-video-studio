"""Cue resolution, fit rules and error messages (DS 5.3, EDT-2, EDT-3, EDT-5)."""

from __future__ import annotations

import pytest

from fvs.errors import ErrorCode, FvsError
from fvs.spec.models import EditSpec
from fvs.spec.resolve import Context, resolve
from tests.conftest import TIMINGS

CLIPS = {1: {"id": "c1", "duration_s": 20.0, "content_hash": "h1"},
         2: {"id": "c2", "duration_s": 3.0, "content_hash": "h2"}}


def ctx(**kw: object) -> Context:
    base = {"timings": TIMINGS, "clips": CLIPS, "caption_styles": {"bold_yellow"},
            "script_hook": "Volcanoes make lightning"}
    return Context(**{**base, **kw})  # type: ignore[arg-type]


def spec(*segments: dict, **over: object) -> EditSpec:
    return EditSpec.model_validate({"project": 1, "timeline": {"segments": list(segments)},
                                    **over})


def seg(cue: dict, clip: int = 1, at: float = 0.0, **kw: object) -> dict:
    return {"clip": clip, "in": at, "cue": cue, **kw}


def errors_of(s: EditSpec, c: Context | None = None) -> FvsError:
    with pytest.raises(FvsError) as info:
        resolve(s, c or ctx())
    return info.value


def test_sentence_cues_follow_the_slots_and_carry_their_text() -> None:
    t = resolve(spec(seg({"sentences": [1]}), seg({"sentences": [2, 3]}, at=5)), ctx())
    assert t["duration_s"] == 4.9 and t["fps"] == 30
    a, b = t["segments"]
    assert (a["out_start"], a["out_end"], a["src_in"], a["src_out"]) == (0.0, 1.5, 0.0, 1.5)
    assert (b["out_start"], b["out_end"], b["src_in"], b["src_out"]) == (1.5, 4.9, 5.0, 8.4)
    assert b["sentences"] == [2, 3] and b["text"] == "It happens often. Nobody knows why."


def test_word_cues_line_up_with_sentence_starts() -> None:
    t = resolve(spec(seg({"words": [1, 2]}), seg({"words": [3, 3]}),
                     seg({"words": [4, 6]}), seg({"until": "end"})), ctx())
    bounds = [(s["out_start"], s["out_end"]) for s in t["segments"]]
    # Word 1 starts sentence 1 (slot 0.0); word 4 starts sentence 2 (slot 1.5);
    # words 4–6 end where word 7, the first of sentence 3, starts its slot (3.2).
    assert bounds == [(0.0, 0.8), (0.8, 1.5), (1.5, 3.2), (3.2, 4.9)]


def test_seconds_and_until_end() -> None:
    t = resolve(spec(seg({"seconds": 2}), seg({"seconds": 1.5}), seg({"until": "end"})), ctx())
    assert [(s["out_start"], s["out_end"]) for s in t["segments"]] == [
        (0.0, 2.0), (2.0, 3.5), (3.5, 4.9)]


def test_boundaries_snap_to_the_frame_grid() -> None:
    t = resolve(spec(seg({"seconds": 1.01}), seg({"until": "end"})), ctx())
    edge = t["segments"][0]["out_end"]
    assert edge == 1.0 and abs(edge * 30 - round(edge * 30)) < 1e-6


def test_a_fade_starts_before_the_segment_and_needs_more_source() -> None:
    t = resolve(spec(seg({"sentences": [1]}),
                     seg({"until": "end"}, at=10, transition_in={"type": "fade",
                                                                  "seconds": 0.3})), ctx())
    b = t["segments"][1]
    assert (b["video_start"], b["out_start"], b["src_in"], b["src_out"]) == (1.2, 1.5, 10, 13.7)
    assert b["transition_in"] == {"type": "fade", "seconds": 0.3}


@pytest.mark.parametrize(("fit", "expect"), [
    ("slow", {"speed": 0.8824, "src_out": 3.0, "hold_s": 0.0, "loop": False}),
    ("hold_last", {"speed": 1.0, "src_out": 3.0, "hold_s": 0.4, "loop": False}),
    ("loop", {"speed": 1.0, "src_out": 3.0, "hold_s": 0.0, "loop": True}),
])
def test_fit_rules_when_the_clip_is_short(fit: str, expect: dict) -> None:
    t = resolve(spec(seg({"sentences": [1]}), seg({"sentences": [2, 3]}, clip=2, fit=fit)),
                ctx())
    got = t["segments"][1]
    assert {k: got[k] for k in expect} == expect


def test_shortfall_names_the_numbers_and_the_options() -> None:
    err = errors_of(spec(seg({"sentences": [1]}), seg({"sentences": [2, 3]}, clip=1, at=17.5)))
    assert err.code == ErrorCode.CUE_UNRESOLVED
    (d,) = err.details
    assert d["path"] == "timeline.segments[1]"
    assert d["problem"] == "clip #1 has 2.5 s left after 17.5 s but sentences 2–3 need 3.4 s."
    assert d["fix"].startswith("Use an earlier in (≤ 16.6)")
    assert 'set fit "hold_last" or "loop"' in d["fix"]


def test_too_slow_is_refused_even_with_fit_slow() -> None:
    err = errors_of(spec(seg({"sentences": [1]}), seg({"sentences": [2, 3]}, clip=2, at=1,
                                                       fit="slow")))
    assert "below the 0.8× limit" in err.details[0]["problem"]


@pytest.mark.parametrize(("segments", "path", "needle"), [
    ([seg({"sentences": [1]}), seg({"sentences": [3]})], "timeline.segments[1].cue", "a gap"),
    ([seg({"sentences": [1, 2]}), seg({"sentences": [2, 3]})], "timeline.segments[1].cue",
     "an overlap"),
    ([seg({"sentences": [1]}), seg({"sentences": [2]})], "timeline.segments", "stop at 3.20"),
    ([seg({"sentences": [1, 4]})], "timeline.segments[0].cue", "sentence 4 does not exist"),
    ([seg({"words": [1, 12]})], "timeline.segments[0].cue", "word 12 does not exist"),
    ([seg({"seconds": 9})], "timeline.segments[0].cue", "past the end of the video"),
    ([seg({"until": "end"}, clip=7)], "timeline.segments[0].clip", "#1, #2"),
    ([seg({"until": "end"}, at=25)], "timeline.segments[0].in", "past the end of clip #1"),
    ([seg({"until": "end"}, transition_in="fade")], "timeline.segments[0].transition_in",
     "cannot fade in"),
])
def test_timeline_errors(segments: list[dict], path: str, needle: str) -> None:
    err = errors_of(spec(*segments))
    assert err.code == ErrorCode.SPEC_INVALID
    found = [d for d in err.details if d["path"] == path]
    assert found and needle in found[0]["problem"] and found[0]["fix"]


def test_rules_are_all_reported_together() -> None:
    s = spec(seg({"until": "end"}), output={"max_seconds": 5},
             captions={"style": "neon", "overrides": [{"word": 99, "text": "x"}]},
             hook={"text": "one two three four five six seven eight nine"},
             cover={"at_seconds": 6})
    err = errors_of(s, ctx(timings={**TIMINGS, "sentences": TIMINGS["sentences"]}))
    paths = {d["path"] for d in err.details}
    assert paths == {"captions.style", "captions.overrides[0].word", "hook.text",
                     "cover.at_seconds"}
    long = errors_of(spec(seg({"until": "end"}), output={"max_seconds": 5},
                          end={"tail_seconds": 1.0}),
                     ctx(timings={"sentences": [{**TIMINGS["sentences"][0],
                                                 "words": [{"word": "x", "start": 0,
                                                            "end": 4.5, "emph": False}]}]}))
    assert long.details[0]["path"] == "output.max_seconds"


def test_unknown_music_asset_and_start_past_its_end() -> None:
    music = {"m1": {"id": "m1", "name": "ambient", "duration_s": 30.0, "path": "/a.mp3"}}
    err = errors_of(spec(seg({"until": "end"}), audio={"music": {"asset": "nope"}}),
                    ctx(assets=music))
    assert err.details[0]["path"] == "audio.music.asset"
    err = errors_of(spec(seg({"until": "end"}), audio={"music": {"asset": "m1",
                                                                  "start_at": 40}}),
                    ctx(assets=music))
    assert err.details[0]["path"] == "audio.music.start_at"


def test_schema_refuses_unknown_fields_and_bad_cues() -> None:
    from pydantic import ValidationError

    from fvs.spec.models import schema_errors

    with pytest.raises(ValidationError) as info:
        EditSpec.model_validate({"project": 1, "ffmpeg_args": "-y", "timeline": {"segments": [
            {"clip": 1, "in": -1, "cue": {"sentences": [2], "seconds": 3}}]}})
    paths = {e["path"]: e for e in schema_errors(info.value)}
    assert "Remove this field" in paths["ffmpeg_args"]["fix"]
    assert paths["timeline.segments[0].in"]["fix"] == "Use a value at least 0."
    assert "exactly one" in paths["timeline.segments[0].cue"]["problem"]
